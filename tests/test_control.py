"""Control-message serialization tests.

The expected byte arrays below are copied verbatim from scrcpy's own
``app/tests/test_control_msg_serialize.c`` (v4.1). If our encoder ever drifts
from the native client, these fail.
"""

from __future__ import annotations

import pytest

from droidmirror.protocol import (
    AndroidKey,
    CopyKey,
    KeyAction,
    MetaState,
    MotionAction,
    _clipboard_payload,
    serialize_back_or_screen_on,
    serialize_get_clipboard,
    serialize_inject_keycode,
    serialize_inject_text,
    serialize_inject_touch,
    serialize_resize_display,
    serialize_rotate_device,
    serialize_set_clipboard,
    serialize_set_display_power,
)


def test_inject_keycode_matches_native_client():
    # test_serialize_inject_keycode()
    got = serialize_inject_keycode(
        action=KeyAction.UP,
        keycode=AndroidKey.ENTER,
        repeat=5,
        metastate=MetaState.SHIFT_ON | MetaState.SHIFT_LEFT_ON,
    )
    expected = bytes(
        [
            0x00,
            0x01,  # AKEY_EVENT_ACTION_UP
            0x00, 0x00, 0x00, 0x42,  # AKEYCODE_ENTER
            0x00, 0x00, 0x00, 0x05,  # repeat
            0x00, 0x00, 0x00, 0x41,  # SHIFT_ON | SHIFT_LEFT_ON
        ]
    )
    assert got == expected
    assert len(got) == 14


def test_inject_text_matches_native_client():
    # test_serialize_inject_text()
    got = serialize_inject_text("hello, world!")
    expected = (
        bytes([0x01, 0x00, 0x00, 0x00, 0x0D]) + b"hello, world!"
    )
    assert got == expected
    assert len(got) == 18


def test_inject_text_uses_utf8_length():
    got = serialize_inject_text("é")
    assert got == bytes([0x01, 0x00, 0x00, 0x00, 0x02]) + "é".encode()


def test_inject_touch_matches_native_client():
    # test_serialize_inject_touch_event()
    got = serialize_inject_touch(
        action=MotionAction.DOWN,
        pointer_id=0x1234567887654321,
        x=100,
        y=200,
        screen_width=1080,
        screen_height=1920,
        pressure=1.0,
        action_button=1,
        buttons=1,
    )
    expected = bytes(
        [
            0x02,  # INJECT_TOUCH_EVENT
            0x00,  # AMOTION_EVENT_ACTION_DOWN
            0x12, 0x34, 0x56, 0x78, 0x87, 0x65, 0x43, 0x21,  # pointer id
            0x00, 0x00, 0x00, 0x64,  # x = 100
            0x00, 0x00, 0x00, 0xC8,  # y = 200
            0x04, 0x38,  # width  = 1080
            0x07, 0x80,  # height = 1920
            0xFF, 0xFF,  # pressure = 1.0
            0x00, 0x00, 0x00, 0x01,  # action button
            0x00, 0x00, 0x00, 0x01,  # buttons
        ]
    )
    assert got == expected
    assert len(got) == 32


def test_touch_pressure_encodes_as_scrcpy_does():
    """scrcpy uses `up ? 0.0f : 1.0f` -> 0x0000 / 0xffff."""
    up = serialize_inject_touch(
        MotionAction.UP, 0, 5, 6, 100, 200, pressure=0.0
    )
    down = serialize_inject_touch(
        MotionAction.DOWN, 0, 5, 6, 100, 200, pressure=1.0
    )
    assert up[22:24] == b"\x00\x00"
    assert down[22:24] == b"\xff\xff"
    assert up[1] == MotionAction.UP
    assert down[1] == MotionAction.DOWN


def test_touch_pressure_is_clamped():
    over = serialize_inject_touch(MotionAction.MOVE, 0, 0, 0, 1, 1, pressure=9.0)
    under = serialize_inject_touch(MotionAction.MOVE, 0, 0, 0, 1, 1, pressure=-3.0)
    assert over[22:24] == b"\xff\xff"
    assert under[22:24] == b"\x00\x00"


def test_touch_pointer_id_is_unsigned_64():
    got = serialize_inject_touch(MotionAction.DOWN, -1, 0, 0, 1, 1)
    assert got[2:10] == b"\xff\xff\xff\xff\xff\xff\xff\xff"


def test_negative_coordinates_are_signed():
    got = serialize_inject_touch(MotionAction.MOVE, 0, -1, -2, 100, 100)
    assert got[10:14] == b"\xff\xff\xff\xff"
    assert got[14:18] == b"\xff\xff\xff\xfe"


def test_simple_commands():
    assert serialize_back_or_screen_on(KeyAction.UP) == bytes([0x04, 0x01])
    assert serialize_set_display_power(True) == bytes([0x0A, 0x01])
    assert serialize_rotate_device() == bytes([0x0B])
    assert serialize_resize_display(1920, 1080) == bytes(
        [0x15, 0x07, 0x80, 0x04, 0x38]
    )


@pytest.mark.parametrize(
    "action,expected_len",
    [
        (MotionAction.DOWN, 32),
        (MotionAction.MOVE, 32),
        (MotionAction.UP, 32),
    ],
)
def test_touch_message_size_is_stable(action, expected_len):
    assert (
        len(serialize_inject_touch(action, 7, 1, 2, 3, 4, 0.5, 0, 0))
        == expected_len
    )


def test_get_clipboard_matches_native_client():
    # control_msg.c: type(1) + copy_key(1)
    assert serialize_get_clipboard(CopyKey.NONE) == bytes([0x08, 0x00])
    assert serialize_get_clipboard(CopyKey.COPY) == bytes([0x08, 0x01])
    assert serialize_get_clipboard(CopyKey.CUT) == bytes([0x08, 0x02])
    assert serialize_get_clipboard() == bytes([0x08, 0x00])


def test_set_clipboard_matches_native_client():
    # control_msg.c: type(1) sequence(8) paste(1) length(4) + utf-8 text
    got = serialize_set_clipboard("hello")
    expected = (
        bytes([0x09])
        + bytes(8)  # sequence 0 = no ack requested
        + bytes([0x00])  # paste flag
        + bytes([0x00, 0x00, 0x00, 0x05])
        + b"hello"
    )
    assert got == expected
    assert len(got) == 14 + 5


def test_set_clipboard_paste_flag_and_sequence():
    got = serialize_set_clipboard("hi", paste=True, sequence=7)
    assert got[0] == 0x09
    assert got[1:9] == (7).to_bytes(8, "big")
    assert got[9] == 1
    assert got[10:14] == (2).to_bytes(4, "big")
    assert got[14:] == b"hi"


def test_set_clipboard_length_counts_utf8_bytes():
    got = serialize_set_clipboard("é")
    assert got[10:14] == (2).to_bytes(4, "big")
    assert got[14:] == "é".encode()


def test_set_clipboard_never_splits_a_code_point_when_truncating():
    # "é" is two bytes: a 3-byte budget must yield one whole char, not 1.5.
    assert _clipboard_payload("ééé", 3) == "é".encode()
    assert _clipboard_payload("abc", 10) == b"abc"


def test_session_set_clipboard_sends_the_wire_message():
    from droidmirror.session import MirrorSession

    session = MirrorSession(None)
    sent: list[bytes] = []
    session.send_control = lambda msg: sent.append(msg) or True  # type: ignore

    assert session.set_clipboard("hello", paste=True) is True
    assert sent == [serialize_set_clipboard("hello", paste=True)]

    # Empty text never touches the socket.
    assert session.set_clipboard("") is False
    assert len(sent) == 1


def test_session_paste_device_clipboard_presses_the_paste_key():
    from droidmirror.session import MirrorSession

    session = MirrorSession(None)
    sent: list[bytes] = []
    session.send_control = lambda msg: sent.append(msg) or True  # type: ignore

    assert session.paste_device_clipboard() is True
    assert sent == [
        serialize_inject_keycode(KeyAction.DOWN, AndroidKey.PASTE),
        serialize_inject_keycode(KeyAction.UP, AndroidKey.PASTE),
    ]


def test_session_inject_text_sends_the_wire_message():
    from droidmirror.session import MirrorSession

    session = MirrorSession(None)
    sent: list[bytes] = []
    session.send_control = lambda msg: sent.append(msg) or True  # type: ignore

    assert session.inject_text("hi") is True
    assert sent == [serialize_inject_text("hi")]

    # Empty text never touches the socket.
    assert session.inject_text("") is False
    assert len(sent) == 1


def test_send_control_sets_and_restores_timeout_on_blocking_socket():
    from droidmirror.session import CONTROL_SEND_TIMEOUT_S, MirrorSession

    class FakeSocket:
        def __init__(self) -> None:
            self.timeout: float | None = None  # blocking socket
            self.timeouts_seen_during_send: list[float | None] = []
            self.sent: list[bytes] = []

        def gettimeout(self) -> float | None:
            return self.timeout

        def settimeout(self, value: float | None) -> None:
            self.timeout = value

        def sendall(self, data: bytes) -> None:
            self.timeouts_seen_during_send.append(self.timeout)
            self.sent.append(data)

    session = MirrorSession(None)
    sock = FakeSocket()
    session._control_sock = sock  # type: ignore[assignment]

    assert session.send_control(b"\x0b") is True
    assert sock.sent == [b"\x0b"]
    assert sock.timeouts_seen_during_send == [CONTROL_SEND_TIMEOUT_S]
    assert sock.timeout is None, "restored back to blocking mode after send"
