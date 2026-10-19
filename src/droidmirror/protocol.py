"""Wire protocol for the scrcpy device server.

This module is deliberately free of any I/O so the whole thing can be unit
tested offline. It implements the *client* side of the scrcpy v4.1 protocol:

* ``ControlMessage`` builders  -- client -> device, binary control messages.
* ``VideoDemuxer``             -- incremental parser for the video socket.

Byte layouts are taken directly from the scrcpy v4.1 sources rather than
guessed:

* ``app/src/control_msg.h``                  -- message type enum + structs
* ``app/tests/test_control_msg_serialize.c`` -- exact byte vectors (used as the
  expected values in our tests)
* ``server/.../device/Streamer.java``        -- codec id / session / frame meta
* ``server/.../device/DesktopConnection.java`` -- 64-byte device name field

Everything on the wire is big-endian.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import Iterator

# --------------------------------------------------------------------------- #
# Control message types (enum sc_control_msg_type, control_msg.h)
# --------------------------------------------------------------------------- #


class ControlType(IntEnum):
    INJECT_KEYCODE = 0
    INJECT_TEXT = 1
    INJECT_TOUCH_EVENT = 2
    INJECT_SCROLL_EVENT = 3
    BACK_OR_SCREEN_ON = 4
    EXPAND_NOTIFICATION_PANEL = 5
    EXPAND_SETTINGS_PANEL = 6
    COLLAPSE_PANELS = 7
    GET_CLIPBOARD = 8
    SET_CLIPBOARD = 9
    SET_DISPLAY_POWER = 10
    ROTATE_DEVICE = 11
    UHID_CREATE = 12
    UHID_INPUT = 13
    UHID_DESTROY = 14
    OPEN_HARD_KEYBOARD_SETTINGS = 15
    START_APP = 16
    RESET_VIDEO = 17
    CAMERA_SET_TORCH = 18
    CAMERA_ZOOM_IN = 19
    CAMERA_ZOOM_OUT = 20
    RESIZE_DISPLAY = 21
    SCAN_FILE = 22


class MotionAction(IntEnum):
    """android.view.MotionEvent actions."""

    DOWN = 0
    UP = 1
    MOVE = 2


class KeyAction(IntEnum):
    """android.view.KeyEvent actions."""

    DOWN = 0
    UP = 1


class Button(IntEnum):
    """android.view.MotionEvent button flags."""

    NONE = 0
    PRIMARY = 1
    SECONDARY = 2
    TERTIARY = 4
    BACK = 8
    FORWARD = 16


class MetaState(IntEnum):
    """android.view.KeyEvent meta states (android_metastate)."""

    NONE = 0
    ALT_ON = 0x02
    ALT_LEFT_ON = 0x10
    SHIFT_ON = 0x01
    SHIFT_LEFT_ON = 0x40
    CTRL_ON = 0x1000
    CTRL_LEFT_ON = 0x4000
    META_ON = 0x10000
    CAPS_LOCK_ON = 0x100000
    NUM_LOCK_ON = 0x2000000


class CopyKey(IntEnum):
    """``sc_copy_key`` -- what GET_CLIPBOARD should do on the device."""

    NONE = 0
    COPY = 1
    CUT = 2


#: Clipboard payload cap, from scrcpy's control_msg.h
#: (SC_CONTROL_MSG_MAX_SIZE - 14). Longer text is truncated, not rejected.
CLIPBOARD_TEXT_MAX_LENGTH = (1 << 18) - 14

#: Sequence 0 means "no acknowledgement requested" on the device
#: (ControlMessage.SEQUENCE_INVALID) -- we never wait for the ack, so we
#: always send it and the device stays silent on the control socket.
CLIPBOARD_SEQUENCE_INVALID = 0


# Pointer ids. scrcpy reserves the negative space for non-finger pointers.
POINTER_ID_MOUSE = 0xFFFFFFFFFFFFFFFF  # -1
POINTER_ID_GENERIC_FINGER = 0xFFFFFFFFFFFFFFFE  # -2
POINTER_ID_VIRTUAL_FINGER = 0xFFFFFFFFFFFFFFFD  # -3

_U64 = 0xFFFFFFFFFFFFFFFF


# --------------------------------------------------------------------------- #
# Control message serialization
# --------------------------------------------------------------------------- #


def serialize_inject_touch(
    action: MotionAction,
    pointer_id: int,
    x: int,
    y: int,
    screen_width: int,
    screen_height: int,
    pressure: float = 1.0,
    action_button: int = 0,
    buttons: int = 0,
) -> bytes:
    """INJECT_TOUCH_EVENT -- 32 bytes.

    ``pressure`` is transmitted as a u16 fixed-point value; scrcpy uses
    ``up ? 0.0f : 1.0f`` (see ``input_manager.c``), which maps to 0x0000 / 0xffff.
    """
    pressure_u16 = max(0, min(0xFFFF, int(round(pressure * 0xFFFF))))
    return struct.pack(
        ">BBQiiHHHII",
        ControlType.INJECT_TOUCH_EVENT,
        int(action),
        pointer_id & _U64,
        int(x),
        int(y),
        int(screen_width),
        int(screen_height),
        pressure_u16,
        int(action_button),
        int(buttons),
    )


def serialize_inject_keycode(
    action: KeyAction,
    keycode: int,
    repeat: int = 0,
    metastate: int = 0,
) -> bytes:
    """INJECT_KEYCODE -- 14 bytes."""
    return struct.pack(
        ">BBIII",
        ControlType.INJECT_KEYCODE,
        int(action),
        int(keycode),
        int(repeat),
        int(metastate),
    )


def serialize_inject_text(text: str) -> bytes:
    """INJECT_TEXT -- 5 + len(bytes)."""
    raw = text.encode("utf-8")
    return struct.pack(">BI", ControlType.INJECT_TEXT, len(raw)) + raw


def serialize_back_or_screen_on(action: KeyAction = KeyAction.UP) -> bytes:
    """BACK_OR_SCREEN_ON -- 2 bytes. Turns the screen on if it was off."""
    return struct.pack(">BB", ControlType.BACK_OR_SCREEN_ON, int(action))


def serialize_set_display_power(on: bool) -> bytes:
    return struct.pack(">BB", ControlType.SET_DISPLAY_POWER, 1 if on else 0)


def serialize_rotate_device() -> bytes:
    return struct.pack(">B", ControlType.ROTATE_DEVICE)


def serialize_resize_display(width: int, height: int) -> bytes:
    """RESIZE_DISPLAY -- 5 bytes, 16-bit dimensions."""
    return struct.pack(">BHH", ControlType.RESIZE_DISPLAY, width, height)


def serialize_reset_video() -> bytes:
    return struct.pack(">B", ControlType.RESET_VIDEO)


def serialize_get_clipboard(copy_key: int = CopyKey.NONE) -> bytes:
    """GET_CLIPBOARD -- 2 bytes.

    ``COPY``/``CUT`` ask the device to copy/cut its current selection first;
    ``NONE`` just reads the clipboard as it is. The reply arrives as a device
    message on the control socket.
    """
    return struct.pack(">BB", ControlType.GET_CLIPBOARD, int(copy_key))


def _clipboard_payload(text: str, max_bytes: int) -> bytes:
    """UTF-8 bytes truncated at a code-point boundary (never split a char)."""
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return raw
    cut = max_bytes
    # 10xxxxxx is a UTF-8 continuation byte, i.e. we are mid code point.
    while cut > 0 and (raw[cut] & 0xC0) == 0x80:
        cut -= 1
    return raw[:cut]


def serialize_set_clipboard(
    text: str,
    paste: bool = False,
    sequence: int = CLIPBOARD_SEQUENCE_INVALID,
) -> bytes:
    """SET_CLIPBOARD -- 14 + len(bytes).

    Layout (control_msg.c): type(1), sequence(8), paste(1), length(4), then the
    UTF-8 text. With ``paste`` the device also presses its own PASTE key, so
    the text lands in whatever field has focus.
    """
    payload = _clipboard_payload(text, CLIPBOARD_TEXT_MAX_LENGTH)
    return (
        struct.pack(
            ">BQBI",
            ControlType.SET_CLIPBOARD,
            int(sequence),
            1 if paste else 0,
            len(payload),
        )
        + payload
    )


#: Keycodes for the hardware buttons we expose in the UI.
class AndroidKey(IntEnum):
    BACK = 4
    HOME = 3
    APP_SWITCH = 187
    MENU = 82
    VOLUME_UP = 24
    VOLUME_DOWN = 25
    POWER = 26
    ENTER = 66
    DEL = 67  # Backspace
    # Editing / navigation keys forwarded from the laptop keyboard.
    TAB = 61
    FORWARD_DEL = 112  # the Delete key (DEL above is Backspace)
    DPAD_UP = 19
    DPAD_DOWN = 20
    DPAD_LEFT = 21
    DPAD_RIGHT = 22
    MOVE_HOME = 122
    MOVE_END = 123
    PAGE_UP = 92
    PAGE_DOWN = 93
    # Clipboard editing keys (Android >= 7).
    CUT = 277
    COPY = 278
    PASTE = 279


# --------------------------------------------------------------------------- #
# Video socket demuxer
# --------------------------------------------------------------------------- #


class CodecId(IntEnum):
    """Codec ids are the 4 ASCII bytes of the codec name, big-endian u32."""

    H264 = 0x68323634  # "h264"
    H265 = 0x68323635  # "h265"
    AV1 = 0x00617631  # "av1"
    VP8 = 0x00767038  # "vp8"
    VP9 = 0x00767039  # "vp9"

    # Pseudo codecs used by Streamer.writeDisableStream().
    DISABLED = 0  # stream explicitly disabled, keep mirroring without it
    ERROR = 1  # configuration error on the device -> abort


PACKET_FLAG_SESSION = 1 << 63
PACKET_FLAG_CONFIG = 1 << 62
PACKET_FLAG_KEY_FRAME = 1 << 61
PTS_MASK = (1 << 61) - 1

#: Largest video payload we will buffer for (128 MiB). A frame is bounded by
#: the surface being encoded, so a few tens of MiB is already generous; the
#: header carries no smaller field, and a demuxer's buffer grows until a
#: payload of this declared size is complete -- so this doubles as the ceiling
#: on how much a malformed or hostile device can make the client hold at once.
#: Named (rather than inline at the check) so it reads like the audio bound,
#: MAX_AUDIO_PACKET, and can be tightened in one place.
MAX_VIDEO_PACKET = 1 << 27

#: The "client resized" marker is the low bit of the *32-bit* session flags
#: word, i.e. bit 32 of the first big-endian u64 of a session packet
#: (see the ``0000000R`` diagram in doc/develop.md and
#: Streamer.writeSessionMeta()).
CLIENT_RESIZED_FLAG = 1 << 32

DEVICE_NAME_FIELD_LENGTH = 64                    # DesktopConnection.java
VIDEO_HEADER_LENGTH = 12                         # session + frame meta
CODEC_ID_LENGTH = 4


class ProtocolError(RuntimeError):
    pass


@dataclass(frozen=True)
class SessionInfo:
    """Sent whenever the capture/encoder session restarts (e.g. rotation)."""

    width: int
    height: int
    client_resized: bool = False


@dataclass(frozen=True)
class MediaPacket:
    """One encoded frame (or codec configuration) from MediaCodec."""

    data: bytes
    pts: int
    config: bool
    key_frame: bool


VideoEvent = SessionInfo | MediaPacket


class VideoDemuxer:
    """Incremental parser for the scrcpy video socket.

    Feed it arbitrary chunks with :meth:`feed`; it returns whatever complete
    events those bytes completed. It is agnostic to chunk boundaries and will
    buffer partial headers/payloads across calls.

    Stream layout (with stream + frame meta enabled, the defaults):

        u32            codec id
        [12 bytes]     session packet   (repeated whenever the session changes)
        [12 bytes]     frame header  +  payload   (repeated)
    """

    _CODEC = 0
    _HEADER = 1
    _PAYLOAD = 2

    def __init__(self) -> None:
        self.codec: CodecId | None = None
        self.error_code: int | None = None
        self._buf = bytearray()
        self._state = self._CODEC
        self._pending: tuple[int, bool, bool] | None = None  # pts, config, key

    def feed(self, data: bytes) -> list[VideoEvent]:
        self._buf.extend(data)
        events: list[VideoEvent] = []

        while True:
            if self._state == self._CODEC:
                if len(self._buf) < CODEC_ID_LENGTH:
                    break
                raw = struct.unpack_from(">I", self._buf)[0]
                del self._buf[:CODEC_ID_LENGTH]
                try:
                    self.codec = CodecId(raw)
                except ValueError as exc:
                    raise ProtocolError(f"unknown video codec id 0x{raw:08x}") from exc
                if self.codec in (CodecId.DISABLED, CodecId.ERROR):
                    # Server may not send any further metadata in this case.
                    self.error_code = int(self.codec)
                    self._state = self._PAYLOAD
                    break
                self._state = self._HEADER
                continue

            if self._state == self._HEADER:
                if len(self._buf) < VIDEO_HEADER_LENGTH:
                    break
                first = struct.unpack_from(">Q", self._buf)[0]
                if first & PACKET_FLAG_SESSION:
                    width = struct.unpack_from(">I", self._buf, 4)[0]
                    height = struct.unpack_from(">I", self._buf, 8)[0]
                    del self._buf[:VIDEO_HEADER_LENGTH]
                    events.append(
                        SessionInfo(
                            width,
                            height,
                            client_resized=bool(first & CLIENT_RESIZED_FLAG),
                        )
                    )
                else:
                    size = struct.unpack_from(">I", self._buf, 8)[0]
                    self._pending = (
                        first & PTS_MASK,
                        bool(first & PACKET_FLAG_CONFIG),
                        bool(first & PACKET_FLAG_KEY_FRAME),
                    )
                    del self._buf[:VIDEO_HEADER_LENGTH]
                    if size > MAX_VIDEO_PACKET:
                        raise ProtocolError(f"implausible packet size {size}")
                    self._size = size
                    self._state = self._PAYLOAD
                continue

            if self._state == self._PAYLOAD:
                if self._pending is None:
                    # Disabled/errored stream: nothing more to parse.
                    break
                size = self._size
                if len(self._buf) < size:
                    break
                payload = bytes(self._buf[:size])
                del self._buf[:size]
                pts, config, key = self._pending
                self._pending = None
                self._state = self._HEADER
                events.append(MediaPacket(payload, pts, config, key))
                continue

            break

        return events


# --------------------------------------------------------------------------- #
# Audio socket demuxer
# --------------------------------------------------------------------------- #


class AudioCodecId(IntEnum):
    """Audio codec ids, same 4-ASCII-byte big-endian scheme as CodecId.

    0 / 1 are the special first-word codes Streamer.writeDisableStream() uses:
    the capture failed (video keeps running) or the config is fatally wrong.
    """

    OPUS = 0x6F707573  # "opus" -- scrcpy's default
    AAC = 0x00616163  # "aac"
    FLAC = 0x666C6163  # "flac"
    RAW = 0x00726177  # "raw"

    DISABLED = 0  # capture unavailable on this device -- skip audio
    ERROR = 1  # fatal config error -- abort the session


#: Largest audio payload we will buffer for; opus frames are ~20ms (~2KB).
MAX_AUDIO_PACKET = 1 << 20


class AudioDemuxer:
    """Incremental parser for the scrcpy audio socket.

    Byte layout (with stream + frame meta enabled, the defaults):

        u32            codec id
        [12 bytes]     frame header  +  payload   (repeated)

    Identical frame headers to the video socket -- the same 8-byte
    ``pts_and_flags`` + 4-byte size -- but no session packets (those are
    video-only) and no dummy byte / device name (those live on the video
    socket alone). Feed it arbitrary chunks; it buffers partial messages
    across calls.
    """

    _CODEC = 0
    _HEADER = 1
    _PAYLOAD = 2

    def __init__(self) -> None:
        self.codec: AudioCodecId | None = None
        self.error_code: int | None = None
        self._buf = bytearray()
        self._state = self._CODEC
        self._pending: tuple[int, bool, bool] | None = None  # pts, config, key
        self._size = 0

    def feed(self, data: bytes) -> list[MediaPacket]:
        self._buf.extend(data)
        events: list[MediaPacket] = []

        while True:
            if self._state == self._CODEC:
                if len(self._buf) < CODEC_ID_LENGTH:
                    break
                raw = struct.unpack_from(">I", self._buf)[0]
                del self._buf[:CODEC_ID_LENGTH]
                try:
                    self.codec = AudioCodecId(raw)
                except ValueError as exc:
                    raise ProtocolError(
                        f"unknown audio codec id 0x{raw:08x}"
                    ) from exc
                if self.codec in (AudioCodecId.DISABLED, AudioCodecId.ERROR):
                    # The server sends nothing further on this socket.
                    self.error_code = int(self.codec)
                    self._state = self._PAYLOAD
                    break
                self._state = self._HEADER
                continue

            if self._state == self._HEADER:
                if len(self._buf) < VIDEO_HEADER_LENGTH:
                    break
                first = struct.unpack_from(">Q", self._buf)[0]
                if first & PACKET_FLAG_SESSION:
                    raise ProtocolError(
                        "session packet on the audio socket (video-only)"
                    )
                size = struct.unpack_from(">I", self._buf, 8)[0]
                if size > MAX_AUDIO_PACKET:
                    raise ProtocolError(f"implausible audio packet size {size}")
                self._pending = (
                    first & PTS_MASK,
                    bool(first & PACKET_FLAG_CONFIG),
                    bool(first & PACKET_FLAG_KEY_FRAME),
                )
                del self._buf[:VIDEO_HEADER_LENGTH]
                self._size = size
                self._state = self._PAYLOAD
                continue

            if self._state == self._PAYLOAD:
                if self._pending is None:
                    break  # disabled/errored stream: nothing more to parse
                size = self._size
                if len(self._buf) < size:
                    break
                payload = bytes(self._buf[:size])
                del self._buf[:size]
                pts, config, key = self._pending
                self._pending = None
                self._state = self._HEADER
                events.append(MediaPacket(payload, pts, config, key))
                continue

            break

        return events


def decode_device_name(raw: bytes) -> str:
    """Decode the fixed-width, NUL-padded device name field."""
    if len(raw) < DEVICE_NAME_FIELD_LENGTH:
        raise ProtocolError(
            f"device name field too short: {len(raw)} < {DEVICE_NAME_FIELD_LENGTH}"
        )
    return raw[:DEVICE_NAME_FIELD_LENGTH].split(b"\x00", 1)[0].decode(
        "utf-8", errors="replace"
    )


def iter_annexb_nal_types(payload: bytes) -> Iterator[int]:
    """Yield NAL unit types from an Annex-B payload (test/debug helper).

    Handles both the 3-byte (``00 00 01``) and 4-byte (``00 00 00 01``) start
    codes, including one that lands in the final four bytes.
    """
    i = 0
    n = len(payload)
    while i + 3 <= n:
        if payload[i] == 0 and payload[i + 1] == 0:
            if payload[i + 2] == 1:
                if i + 3 >= n:
                    return  # start code with no type byte after it
                yield payload[i + 3] & 0x1F
                i += 3
                continue
            if payload[i + 2] == 0 and i + 4 <= n and payload[i + 3] == 1:
                if i + 4 >= n:
                    return
                yield payload[i + 4] & 0x1F
                i += 4
                continue
        i += 1
