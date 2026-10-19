"""Runtime supervisor for a live mirroring session.

Wires together: scrcpy server -> video socket -> demuxer -> decoder -> newest
frame, plus a serialised control channel for injection.

Threading model
---------------
* one daemon reader thread performs recv -> demux -> decode -> publish,
* one daemon watcher thread polls keyguard/keyboard/foreground over adb,
* one daemon audio thread demuxes and decodes opus,
* control messages are written from the GUI thread under a lock.

Because decoding happens on the reader thread and the newest decoded frame is
handed straight to the renderer, there is no queue anywhere on the hot path:
latency is (encode + USB + decode), with no buffering added by us.

"no queue on the hot path" is about *our* code. FFmpeg's own frame-threaded
decoding (``DEFAULT_THREAD_TYPE = "FRAME"``, two threads) holds a small
bounded pipeline internally, and frames it returns that we do not need are
dropped rather than rendered -- see ``_handle_event``.
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass
from typing import Callable

import av

from .adb import Adb, AdbError, Device
from .audio import AudioStream
from .decoder import HW_OFF, DecodedFrame, VideoDecoder
from .discord_rpc import detect_foreground
from .lockscreen import detect_keyboard_shown, detect_locked, detect_lock_type
from .protocol import (
    AndroidKey,
    CodecId,
    CopyKey,
    KeyAction,
    MediaPacket,
    MotionAction,
    ProtocolError,
    SessionInfo,
    VideoDemuxer,
    serialize_get_clipboard,
    serialize_inject_keycode,
    serialize_inject_text,
    serialize_inject_touch,
    serialize_set_clipboard,
)
from .server import ScrcpyServer, ServerOptions
from .volume import VolumeState, get_volume, set_volume

RECV_CHUNK = 1 << 16

#: Lag beyond this (measured against the device's own pts clock) means the
#: reader can no longer keep pace with the encoder: the socket is banking old
#: video and the mirror is drifting seconds behind. The stream is then drained
#: (without decoding) until the next keyframe, which is the only place a
#: H.264 stream can be legally re-entered.
LAG_CATCHUP_S = 2.5

#: How long a single control-socket write may block. Every touch, key and
#: camera-drag delta goes through here from the GUI thread, so an unbounded
#: write is a UI freeze; see send_control().
CONTROL_SEND_TIMEOUT_S = 1.0

#: How often the lockscreen watcher asks the device (background thread,
#: dumpsys is too heavy for the GUI thread).
LOCK_POLL_S = 2.0


@dataclass
class SessionStatus:
    """Human-readable progress state, surfaced in the UI."""

    message: str
    level: str = "info"  # info | error


FrameCallback = Callable[[DecodedFrame], None]
StatusCallback = Callable[[SessionStatus], None]
SimpleCallback = Callable[[], None]


class FpsCounter:
    """Frame rate over a sliding window.

    A naive ``total_frames / total_seconds`` badly under-reports here: the
    scrcpy encoder sends *nothing* while the screen is static, so idle periods
    get averaged in and the label reads ~30fps while the pipeline is actually
    running well above that under load. This samples the cumulative count at a
    fixed interval instead.
    """

    def __init__(self, window_s: float = 1.0) -> None:
        self.window_s = window_s
        self._anchor: tuple[float, int] | None = None
        self.rate = 0.0

    def reset(self) -> None:
        self._anchor = None
        self.rate = 0.0

    def sample(self, cumulative_frames: int, now: float | None = None) -> float:
        """Feed the running frame total; returns fps measured this window."""
        if now is None:
            now = time.monotonic()
        if self._anchor is None:
            self._anchor = (now, cumulative_frames)
            return self.rate

        t0, n0 = self._anchor
        if cumulative_frames < n0:
            # Stream restarted (reconnect): re-anchor rather than go negative.
            self._anchor = (now, cumulative_frames)
            return self.rate

        elapsed = now - t0
        if elapsed < self.window_s:
            return self.rate

        self.rate = (cumulative_frames - n0) / elapsed
        self._anchor = (now, cumulative_frames)
        return self.rate


class LagMeter:
    """How stale is the newest packet, in seconds?

    The frame header carries the device's own MediaCodec presentation time
    (its monotonic clock, microseconds). The first usable packet of a *fresh*
    connection is assumed to be ~live, so staleness from then on is simply:

        wall time elapsed  -  pts time advanced

    No device clock read is needed after that anchor -- and it stays correct
    across device suspend, because the anchor absorbs the offset once. A
    negative or absurd result means the pts clock jumped (encoder restart,
    reconnect); the meter re-anchors instead of reporting nonsense.
    """

    #: Anything beyond this cannot be real staleness -- it's a clock jump.
    MAX_LAG_S = 300.0

    def __init__(self) -> None:
        self.lag = 0.0
        self._wall0: float | None = None
        self._pts0: int | None = None

    def reset(self) -> None:
        self.lag = 0.0
        self._wall0 = None
        self._pts0 = None

    def feed(self, pts_us: int, now: float | None = None) -> float:
        """Fold one packet into the measurement; returns staleness in seconds."""
        if now is None:
            now = time.monotonic()
        if pts_us <= 0:
            # Config packets carry no usable pts: keep the last reading.
            return self.lag
        if self._wall0 is None:
            self._wall0, self._pts0 = now, pts_us
            self.lag = 0.0
            return 0.0
        pts_adv = pts_us - self._pts0
        if pts_adv < 0:
            # pts never moves backwards on a live stream; an encoder restart
            # would otherwise *inflate* the lag (wall - negative = big number)
            # and fake a catch-up. Re-anchor instead.
            self._wall0, self._pts0 = now, pts_us
            self.lag = 0.0
            return 0.0
        lag = (now - self._wall0) - pts_adv / 1e6
        if lag < 0.0 or lag > self.MAX_LAG_S:
            self._wall0, self._pts0 = now, pts_us
            self.lag = 0.0
            return 0.0
        self.lag = lag
        return lag


class MirrorSession:
    """A single live connection to an Android device."""

    def __init__(
        self,
        adb: Adb,
        options: ServerOptions | None = None,
        on_frame: FrameCallback | None = None,
        on_status: StatusCallback | None = None,
        on_device_name: Callable[[str], None] | None = None,
        on_closed: SimpleCallback | None = None,
        hw_decode: str = HW_OFF,
    ) -> None:
        self.adb = adb
        self.options = options or ServerOptions()
        #: ``off`` / ``auto`` / an FFmpeg device type -- handed to VideoDecoder.
        self.hw_decode = hw_decode or HW_OFF
        self._on_frame = on_frame
        self._on_status = on_status
        self._on_device_name = on_device_name
        self._on_closed = on_closed

        self._server: ScrcpyServer | None = None
        self._video_sock: socket.socket | None = None
        self._control_sock: socket.socket | None = None
        self._control_lock = threading.Lock()
        self._reader: threading.Thread | None = None
        #: Live audio thread (device sound on this computer); None when off.
        self.audio: AudioStream | None = None
        #: Playback muted? Applies to the running stream and to any later one.
        self.muted = False
        self._stop = threading.Event()

        #: Lockscreen state: True = keyguard up, False = unlocked,
        #: None = not yet known (or no longer connected). Written by the
        #: watcher thread, read by the GUI -- a bool/None assignment is
        #: atomic under the GIL, no lock needed.
        self.device_locked: bool | None = None
        self._lock_thread: threading.Thread | None = None
        #: Which screen lock the device has: ``pin``/``pattern``/``password``/
        #: ``swipe``/None (unknown). Fetched once per session by the watcher.
        self.lock_type: str | None = None
        #: Is the device's on-screen keyboard up? True/False/None (unknown).
        #: The x-key camera lock stays out of the way while it is True, so
        #: typing in a text field is never hijacked into mouse-look.
        self.keyboard_shown: bool | None = None
        #: Package id of whatever is in the foreground on the device, polled
        #: by the watcher. None before the first reading (or when unknown).
        #: Feeds the Discord Rich Presence ("what am I actually using?").
        self.foreground_app: str | None = None
        #: Whether anything is actually listening to foreground_app. The poll
        #: costs a `dumpsys window` every ~8s, so it is off unless the UI
        #: turns it on -- which it does only when Discord presence is live.
        self.foreground_polling: bool = False

        self.device_name: str = ""
        self.frame_width: int = 0
        self.frame_height: int = 0
        self.frames_decoded: int = 0
        #: Packets intentionally skipped while catching up (drain mode).
        self.frames_dropped: int = 0
        #: Staleness of the newest packet vs the device clock, seconds.
        #: -1 until the first packet calibrates the meter.
        self.video_lag_s: float = -1.0
        self.last_frame: DecodedFrame | None = None

        self._decoder: VideoDecoder | None = None
        self._codec: CodecId | None = None
        self._start_time = 0.0
        #: Wall-clock connect time, for anything that stamps a real timestamp.
        self._started_epoch = 0.0
        self._lag = LagMeter()
        self._draining = False
        self._drain_dropped = 0

    # -- state ------------------------------------------------------------ #

    @property
    def is_running(self) -> bool:
        return self._reader is not None and self._reader.is_alive()

    @property
    def frame_size(self) -> tuple[int, int]:
        return self.frame_width, self.frame_height

    @property
    def decoder_backend(self) -> str:
        """``amf``/``qsv``/... or ``software``; ``-`` before the codec arrives."""
        if self._decoder is None:
            return "-"
        return self._decoder.backend

    @property
    def fps(self) -> float:
        elapsed = time.monotonic() - self._start_time
        return self.frames_decoded / elapsed if elapsed > 0 else 0.0

    @property
    def started_at(self) -> float:
        """Unix time of the connect, or 0.0 when not connected.

        Wall-clock seconds since the epoch -- *not* ``time.monotonic()``.
        The Discord presence stamps "playing since" from this, and it reads
        the number as a Unix timestamp: handing it the monotonic clock made
        it report a thirteen-year session.
        """
        return self._started_epoch

    def _emit_status(self, message: str, level: str = "info") -> None:
        if self._on_status:
            self._on_status(SessionStatus(message, level))

    # -- lifecycle -------------------------------------------------------- #

    def start(self, device: Device | None = None) -> None:
        self._stop.clear()
        self._emit_status("Preparing scrcpy server...")
        self._server = ScrcpyServer(self.adb, device=device, options=self.options)

        try:
            self._server.prepare()
            self._emit_status("Starting device server...")
            connection = self._server.start()
        except (AdbError, OSError, RuntimeError) as exc:
            self._emit_status(str(exc), level="error")
            self.stop()
            raise

        self._video_sock = connection.video
        self._control_sock = connection.control
        self.device_name = connection.device_name
        if self._on_device_name:
            self._on_device_name(self.device_name)

        self._start_time = time.monotonic()
        self._started_epoch = time.time()
        # Fresh connection: buffers are empty, so the first packet is live and
        # the lag meter starts from that assumption.
        self._lag.reset()
        self._draining = False
        self._drain_dropped = 0
        self.frames_dropped = 0
        self.video_lag_s = -1.0
        # The codec and the decoder must also be cleared. Both are discovered
        # from the wire on the first session-meta packet, and _ensure_decoder
        # is a no-op whenever _decoder is not None -- so leaving them set from
        # a previous connection made a *restarted* session skip decoder setup
        # entirely. If the old decoder had already been closed (stop() closes
        # it now) its context is None, feed() accepts every packet and returns
        # nothing, and the mirror comes back as a permanent black screen with
        # no error. The GUI happened to build a fresh MirrorSession each time,
        # which is why this never showed up.
        if self._decoder is not None:
            self._decoder.close()
        self._decoder = None
        self._codec = None
        self.frame_width = 0
        self.frame_height = 0
        self._reader = threading.Thread(
            target=self._read_loop, name="droidmirror-video", daemon=True
        )
        self._reader.start()

        if connection.audio is not None:
            self.audio = AudioStream(
                connection.audio,
                on_status=lambda msg, level="info": self._emit_status(msg, level),
                mute=self.muted,
            )
            self.audio.start()

        self.device_locked = None  # fresh connection: state is unknown again
        self.lock_type = None
        self.keyboard_shown = None
        self.foreground_app = None
        self._lock_thread = threading.Thread(
            target=self._lock_watch, name="droidmirror-lock", daemon=True
        )
        self._lock_thread.start()

        self._emit_status(f"Connected to {self.device_name}")

    def set_muted(self, muted: bool) -> None:
        """Mute/unmute this computer's playback, now and for future sessions."""
        self.muted = muted
        if self.audio is not None:
            self.audio.set_muted(muted)

    # -- tablet volume ---------------------------------------------------- #
    # The tablet's own media volume, which is a different thing entirely from
    # set_muted() above: muting silences the sound this app plays on this
    # computer, while these change how loud the *device* is. Both are reachable
    # from the toolbar, next to each other, and they are not interchangeable.
    #
    # Neither goes over the scrcpy control socket -- the protocol has no
    # set-volume message -- so both shell out to adb and both block. Callers
    # run them off the GUI thread (see MainWindow._run_worker).

    def device_volume(self) -> VolumeState | None:
        """Read the tablet's media volume; None when it cannot be read."""
        return get_volume(self.adb)

    def set_device_volume(self, index: int) -> bool:
        """Set the tablet's media volume to an absolute index."""
        return set_volume(self.adb, index)

    def stop(self) -> None:
        self._stop.set()

        # Close the sockets *before* joining: the reader is usually parked in a
        # blocking recv(), and only closing the socket will wake it. Joining
        # first would stall for the full timeout on every disconnect.
        if self.audio is not None:
            self.audio.stop()
        if self._server is not None:
            self._server.stop()
            self._server = None
        self._video_sock = None
        self._control_sock = None

        if self._reader is not None and self._reader.is_alive():
            self._reader.join(timeout=2.0)
        self._reader = None
        if self.audio is not None and self.audio.is_alive():
            self.audio.join(timeout=2.0)
        self.audio = None

        # The watcher exits on the next loop tick (identity check below);
        # nobody may read a lock state from a connection that is gone.
        self._lock_thread = None
        self.device_locked = None
        self.lock_type = None
        self.keyboard_shown = None
        self.foreground_app = None

        # Tear the decoder down here, on the thread that owns it. This used to
        # sit at the bottom of _lock_watch(), which meant: the decoder was only
        # ever closed by the watcher, which is the one thread stop() does not
        # join and which sits in an 8s adb shell for most of its life; the two
        # early `return`s in that loop skipped it entirely; and the close
        # happened while the reader thread might still be feeding packets into
        # it. The reader is joined above, so closing here is safe.
        if self._decoder is not None:
            self._decoder.close()
            self._decoder = None
        self.frame_width = 0
        self.frame_height = 0

    def _lock_watch(self) -> None:
        """Poll the keyguard state in the background, publish to device_locked.

        Waits first (no dumpsys during the connection handshake), keeps the
        last known state when a poll fails, and dies as soon as ``stop()``
        clears the thread reference -- a stale watcher must never outlive a
        restarted session on the same event object.
        """
        me = threading.current_thread()
        type_done = False
        tick = 0
        while not self._stop.wait(LOCK_POLL_S):
            if self._lock_thread is not me:
                return  # stop() or a newer start() retired us
            if not type_done:
                try:
                    kind = detect_lock_type(self.adb)
                except Exception:
                    kind = None
                if self._lock_thread is not me:
                    return
                if kind is not None:
                    self.lock_type = kind
                    type_done = True  # the lock kind does not change; ask once
            try:
                state = detect_locked(self.adb)
            except Exception:
                state = None
            # Re-check identity: stop() may have landed while dumpsys ran, and
            # a state from a dead connection must not resurface.
            if state is not None and self._lock_thread is me:
                self.device_locked = state

            # The soft keyboard matters for the x hotkey: while it is up the
            # user is typing in a field, and stealing the mouse into a camera
            # lock would be wrong. Polled alongside the lock state, but a
            # quarter as often: this is the heaviest probe in the app (the
            # plain dump measured ~1.3 MB, filtered ~24 bytes), and "the
            # keyboard is up" only has to be true by the time a right-drag
            # begins -- it is not a race against a second or two.
            tick += 1
            # Deliberately ``== 2`` and not ``== 0``: the foreground probe below
            # fires on ``% 4 == 0``, and sharing a tick would put two dumps
            # back-to-back into the same instant, which is exactly the burst
            # that used to land while a frame was being encoded.
            if tick % 4 == 2:  # every ~8s: the keyboard rarely flickers
                try:
                    keyboard = detect_keyboard_shown(self.adb)
                except Exception:
                    keyboard = None
                if keyboard is not None and self._lock_thread is me:
                    self.keyboard_shown = keyboard

            # What is in the foreground, for the Discord presence. Same poll,
            # same care: a failed dumpsys keeps the last known app rather than
            # blanking what the user is doing.
            #
            # ...and it only runs when someone is listening. This probe exists
            # solely to feed Rich Presence, and gating it means a session with
            # Discord switched off does not pay for it at all. Measured on a
            # Galaxy Tab S9 FE while mirroring, the whole polling loop was
            # spending ~500 ms of every second on the tablet and pulling
            # ~475 KB/s of dumpsys text onto the link carrying the video --
            # which is what made the picture stutter. Every probe here now goes
            # through adb.device_grep first, so what crosses the link is the few
            # hundred bytes that matter instead of the whole dump.
            if tick % 4 == 0 and self.foreground_polling:
                try:
                    found = detect_foreground(self.adb)
                except Exception:
                    found = None
                if found is not None and self._lock_thread is me:
                    self.foreground_app = found[0]

    # -- video path ------------------------------------------------------- #

    def _read_loop(self) -> None:
        sock = self._video_sock
        if sock is None:  # stopped before the thread got scheduled
            return
        demuxer = VideoDemuxer()

        try:
            while not self._stop.is_set():
                try:
                    chunk = sock.recv(RECV_CHUNK)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not chunk:
                    break

                try:
                    events = demuxer.feed(chunk)
                except ProtocolError as exc:
                    self._emit_status(f"Protocol error: {exc}", level="error")
                    break

                # The codec id arrives on the very first bytes, so the decoder
                # can be built before any media packet is dispatched.
                if (
                    self._codec is None
                    and demuxer.codec is not None
                    and demuxer.codec not in (CodecId.DISABLED, CodecId.ERROR)
                ):
                    self._codec = demuxer.codec
                    if not self._ensure_decoder():
                        break

                for event in events:
                    self._handle_event(event)

                if demuxer.codec is CodecId.ERROR:
                    # ERROR is a fatal device-side configuration failure;
                    # DISABLED just means that stream is turned off.
                    self._emit_status(
                        "Device reported a video error (check the server log "
                        "below for the reason).",
                        level="error",
                    )
                    break
                if demuxer.codec is CodecId.DISABLED:
                    self._emit_status(
                        "Device reported the video stream was disabled.",
                        level="error",
                    )
                    break
        finally:
            # Only report a closed stream if it ended on its own; an
            # intentional stop() must not bounce back as a "stream ended"
            # notification (it would re-enter the UI's disconnect path).
            if not self._stop.is_set():
                self._emit_status("Stream ended.")
                if self._on_closed:
                    self._on_closed()

    def _handle_event(self, event: SessionInfo | MediaPacket) -> None:
        if isinstance(event, SessionInfo):
            size_changed = (event.width, event.height) != (
                self.frame_width,
                self.frame_height,
            )
            self.frame_width, self.frame_height = event.width, event.height
            # A session restart means a fresh encoder: reset decoder state even
            # when the dimensions match the previous session.
            if self._decoder is not None:
                self._decoder.reset()
            if size_changed:
                self._emit_status(f"Video session {event.width}x{event.height}")
            return

        # Bind the decoder once. stop() runs on the GUI thread and sets _decoder to
        # None; checking the attribute and then using it again on the next line
        # left a window where the reader was feeding a decoder that had just
        # been closed. A local reference keeps the object alive for the whole
        # call even if stop() lands mid-way.
        decoder = self._decoder
        if decoder is None:
            return

        lag = self._lag.feed(event.pts)
        self.video_lag_s = lag

        if self._draining:
            if event.config or event.key_frame:
                # SPS/PPS or an IDR: the one place a H.264 stream can be
                # legally re-entered. Resume decoding from here.
                self._draining = False
                self._emit_status(
                    f"Caught up ({self._drain_dropped} stale frames skipped)."
                )
                self._drain_dropped = 0
            else:
                # Mid-GOP frames reference the stale ones we just discarded;
                # decoding them would smear the skipped span across the screen.
                self._drain_dropped += 1
                self.frames_dropped += 1
                return
        elif lag > LAG_CATCHUP_S:
            # The reader is slower than the encoder: seconds of old video are
            # banking up in the transport buffers (this is the "mirror is 10
            # seconds behind" failure). Drain the backlog at socket speed --
            # no decoding -- until the next keyframe.
            self._draining = True
            self._drain_dropped = 1
            self.frames_dropped += 1
            self._emit_status(
                f"Video is {lag:.1f}s behind - skipping to the next keyframe..."
            )
            return

        frames = decoder.feed(event)
        if not frames:
            return

        # Only the newest frame matters for latency; earlier ones are stale.
        frame = frames[-1]
        self.last_frame = frame
        # Counts frames *published*, not frames FFmpeg decoded: with FRAME
        # threading the decoder hands back more than one at a time and the
        # extras are dropped. So this is the render rate, and it is what the
        # FPS readout shows -- not raw decode throughput.
        self.frames_decoded += 1
        if self._on_frame:
            self._on_frame(frame)

    def _ensure_decoder(self) -> bool:
        """Build the decoder for the negotiated codec. False means give up."""
        if self._decoder is not None:
            return True
        codec = self._codec or CodecId.H264
        try:
            self._decoder = VideoDecoder(codec, hw_decode=self.hw_decode)
        except (ValueError, RuntimeError, av.FFmpegError, OSError) as exc:
            # Unsupported codec or an FFmpeg build without it. This is terminal:
            # there is no video without a decoder.
            #
            # av.FFmpegError matters here and used to be missed: it subclasses
            # Exception directly, *not* RuntimeError, so a missing decoder
            # (av.DecoderNotFoundError) escaped this handler, killed the reader
            # thread with an unhandled traceback on stderr, and reached the user
            # as a bare "Stream ended." instead of the message below.
            self._emit_status(f"Could not start the video decoder: {exc}", level="error")
            return False
        return True

    # -- control path ----------------------------------------------------- #

    def send_control(self, message: bytes) -> bool:
        """Write a serialized control message. Thread-safe.

        The socket is given a send timeout for the duration of the write. It is
        otherwise blocking, and this is called from the GUI thread on every key
        press and every mouse move -- if the device stops draining the control
        socket (an ANR, a stalled adb forward), ``sendall`` would block the
        whole UI indefinitely while holding ``_control_lock``, so nothing else
        could inject either. Dropping one touch is always better than freezing
        the window; the next one will carry the state anyway.
        """
        sock = self._control_sock
        if sock is None:
            return False
        _unset = object()
        with self._control_lock:
            try:
                previous: Any = sock.gettimeout()
            except OSError:
                previous = _unset
            try:
                # _unset == "previous could not be read", in which case we leave
                # the socket's own timeout alone rather than guess. Note that a
                # blocking socket returns None from gettimeout(), which is a
                # valid value we must replace with CONTROL_SEND_TIMEOUT_S.
                if previous is not _unset:
                    sock.settimeout(CONTROL_SEND_TIMEOUT_S)
            except OSError:
                previous = _unset
            try:
                sock.sendall(message)
            except OSError:
                return False
            finally:
                # Always restore, including back to None (blocking). Skipping
                # the restore when the old value was None left the control
                # socket permanently on a timeout after the first touch.
                if previous is not _unset:
                    try:
                        sock.settimeout(previous)
                    except OSError:
                        pass
        return True

    def inject_touch(
        self,
        action: MotionAction,
        pointer_id: int,
        x: int,
        y: int,
        width: int,
        height: int,
        action_button: int = 0,
        buttons: int = 0,
    ) -> bool:
        """Fast path used by the keymapper for every bound key.

        Finger touches carry no button flags (matching scrcpy's virtual finger),
        so ``action_button``/``buttons`` default to 0 and are only set when a
        real mouse is driving the mirror.
        """
        pressure = 0.0 if action == MotionAction.UP else 1.0
        return self.send_control(
            serialize_inject_touch(
                action=action,
                pointer_id=pointer_id,
                x=x,
                y=y,
                screen_width=width,
                screen_height=height,
                pressure=pressure,
                action_button=action_button,
                buttons=buttons,
            )
        )

    def inject_keycode(self, action, keycode: int) -> bool:
        return self.send_control(serialize_inject_keycode(action, keycode))

    def inject_text(self, text: str) -> bool:
        """INJECT_TEXT -- commit a string into the device's focused field.

        scrcpy's text message; the device turns it into key events on its
        side, so the IME/on-screen keyboard can be visible or not -- the
        text lands either way.
        """
        if not text:
            return False
        return self.send_control(serialize_inject_text(text))

    # -- clipboard -------------------------------------------------------- #

    def set_clipboard(self, text: str, paste: bool = False) -> bool:
        """Put ``text`` on the device's clipboard.

        With ``paste=True`` the device also presses its PASTE key, so the text
        lands in the focused field -- this is the computer -> device Ctrl+V
        path. The device sends nothing back (sequence 0 = no ack requested),
        so the control socket stays write-only.
        """
        if not text:
            return False
        return self.send_control(serialize_set_clipboard(text, paste=paste))

    def request_clipboard(self, copy_key: CopyKey = CopyKey.NONE) -> bool:
        """Ask the device for its clipboard.

        Nothing calls this, and it cannot work as written: the reply is a
        *device* message arriving on the control socket, and this client has no
        parser for device messages at all -- :mod:`droidmirror.protocol` only
        serialises. The control socket is therefore write-only in practice.
        Use :meth:`paste_device_clipboard` for the device -> computer direction,
        which needs no round trip.

        Kept because the wire message is implemented and tested, and because
        finishing the read side is a legitimate future change.
        """
        return self.send_control(serialize_get_clipboard(copy_key))

    def paste_device_clipboard(self) -> bool:
        """Press the device's own PASTE key -- pastes what the *device* copied.

        No clipboard round trip: the device pastes its current clipboard into
        the focused field itself.
        """
        down = self.inject_keycode(KeyAction.DOWN, int(AndroidKey.PASTE))
        up = self.inject_keycode(KeyAction.UP, int(AndroidKey.PASTE))
        return down and up
