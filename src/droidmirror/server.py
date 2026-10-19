"""Lifecycle of the scrcpy device server and the USB socket handshake.

The device side is the stock ``scrcpy-server`` jar. We push it, run it as the
``shell`` user over adb, and speak the client half of the protocol ourselves --
which is what lets us inject keybind-driven touches at arbitrary coordinates.

Socket/handshake order (verified against scrcpy v4.1 ``Server.java`` and
``DesktopConnection.java``), with a forward tunnel:

    video socket:   dummy byte -> 64-byte device name -> codec id -> stream
    audio socket:   codec id -> stream            (dialed between video/control)
    control socket: (no handshake, bidirectional messages)
"""

from __future__ import annotations

import collections
import random
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .adb import Adb, AdbError, Device
from .config import (
    DEFAULT_BIT_RATE,
    DEFAULT_MAX_FPS,
    DEFAULT_MAX_SIZE,
    DEFAULT_TCP_PORT,
    DEVICE_JAR_PATH,
    DEVICE_TMP_DIR,
    SCRCPY_VERSION,
    SERVER_JAR_NAME,
    SERVER_JAR_URL,
    WIRELESS_BIT_RATE,
    WIRELESS_MAX_SIZE,
    bundle_root,
    server_jar_cache_path,
)
from .protocol import DEVICE_NAME_FIELD_LENGTH, decode_device_name


def wireless_options(options: "ServerOptions") -> "ServerOptions":
    """``options`` retuned for a Wi-Fi link (a copy; the input is untouched).

    Only fields still at their USB defaults are changed, so an explicit
    ``--max-size`` / ``--bit-rate`` always wins.
    """
    from dataclasses import replace

    changes = {}
    if options.max_size == DEFAULT_MAX_SIZE:
        changes["max_size"] = WIRELESS_MAX_SIZE
    if options.bit_rate == DEFAULT_BIT_RATE:
        changes["bit_rate"] = WIRELESS_BIT_RATE
    return replace(options, **changes) if changes else options

#: How long to wait for the device to start listening. The device has to boot
#: a JVM via app_process before it creates its socket, which can take a couple
#: of seconds on a cold start.
CONNECT_TIMEOUT_S = 12.0


class ServerError(RuntimeError):
    pass


@dataclass
class ServerOptions:
    """Server-side options; all tuned around latency."""

    max_size: int = DEFAULT_MAX_SIZE
    bit_rate: int = DEFAULT_BIT_RATE
    max_fps: int = DEFAULT_MAX_FPS
    video_codec: str = "h264"
    control: bool = True
    audio: bool = True
    stay_awake: bool = True
    log_level: str = "info"
    port: int = DEFAULT_TCP_PORT

    def to_args(self, scid: int) -> list[str]:
        """Render the ``key=value`` tail passed to the server main class."""
        args = [
            # scid is parsed with Integer.parseInt(value, 16) on the device, so
            # it must be hexadecimal -- sending decimal makes the server abort
            # with NumberFormatException before it ever opens a socket.
            f"scid={scid:08x}",
            f"log_level={self.log_level}",
            f"video_codec={self.video_codec}",
            f"max_size={self.max_size}",
            f"video_bit_rate={self.bit_rate}",
            f"audio={'true' if self.audio else 'false'}",
            f"control={'true' if self.control else 'false'}",
            # Forward tunnel: we accept nothing, the device listens and we dial in.
            "tunnel_forward=true",
            "cleanup=true",
            # Meta flags: everything on. Our demuxer relies on stream + frame meta.
            "send_device_meta=true",
            "send_frame_meta=true",
            "send_stream_meta=true",
            "send_dummy_byte=true",
            f"stay_awake={'true' if self.stay_awake else 'false'}",
        ]
        if self.max_fps > 0:
            args.append(f"max_fps={self.max_fps}")
        return args


@dataclass
class Connection:
    """Live sockets to the device plus its reported name."""

    video: socket.socket
    control: socket.socket | None
    device_name: str
    audio: socket.socket | None = None
    process: subprocess.Popen | None = None
    log_tail: collections.deque = field(
        default_factory=lambda: collections.deque(maxlen=200)
    )

    def logs(self) -> str:
        return "\n".join(self.log_tail)

    def close(self) -> None:
        for sock in (self.video, self.audio, self.control):
            if sock is None:
                continue
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass


def ensure_server_jar(*, redownload: bool = False) -> Path:
    """Return a local copy of the pinned scrcpy server, downloading if needed."""
    path = server_jar_cache_path()
    if path.is_file() and not redownload:
        return path

    # A frozen build ships the jar, so a first run with no network still works.
    # `bundle_root()` is the per-run extraction directory, which is deleted on
    # exit -- copy it into the writable cache beside the executable, where the
    # next launch (and the download fallback) expects it.
    bundled = bundle_root() / "vendor" / SERVER_JAR_NAME
    if bundled.is_file() and not redownload:
        try:
            shutil.copyfile(bundled, path)
            return path
        except OSError:
            pass

    tmp = path.with_suffix(".part")
    try:
        with urllib.request.urlopen(SERVER_JAR_URL, timeout=60) as resp:
            data = resp.read()
    except OSError as exc:
        raise ServerError(
            f"Could not download {SERVER_JAR_NAME} from {SERVER_JAR_URL}: {exc}"
        ) from exc

    if len(data) < 1024:
        raise ServerError("Downloaded server jar looks truncated.")
    tmp.write_bytes(data)
    tmp.replace(path)
    return path


def _tune_socket(sock: socket.socket) -> None:
    """Disable Nagle: we want frames on the wire immediately, not coalesced."""
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass
    sock.settimeout(None)


class ScrcpyServer:
    """Owns the device-side process and the two forwarded sockets."""

    def __init__(
        self,
        adb: Adb,
        device: Device | None = None,
        options: ServerOptions | None = None,
    ) -> None:
        self.adb = adb
        self.device = device
        self.options = options or ServerOptions()
        self.scid = random.getrandbits(31)
        self._process: subprocess.Popen | None = None
        self._logs_sink: collections.deque | None = None
        self._connection: Connection | None = None
        self._prepared = False
        self._local_spec = f"tcp:{self.options.port}"
        self._remote_spec = f"localabstract:{self.socket_name}"

    # -- naming ----------------------------------------------------------- #

    @property
    def socket_name(self) -> str:
        """Abstract socket name; must match DesktopConnection.getSocketName()."""
        return f"scrcpy_{self.scid:08x}"

    # -- lifecycle -------------------------------------------------------- #

    def prepare(self) -> None:
        """Download + push the server jar and install the adb forward."""
        jar = ensure_server_jar()
        self.adb.start_server()
        if self.device is None:
            self.device = self.adb.pick_device()

        # Bind the transport *before* any device-scoped command. When the
        # caller already chose a device we skip pick_device(), so adb.serial
        # would otherwise stay None -- and with both a cable and a wireless
        # device attached, a bare adb invocation (no -s) is ambiguous and adb
        # aborts with "more than one device/emulator".
        if self.device is not None:
            self.adb.serial = self.device.serial

        self.adb.ensure_dir(DEVICE_TMP_DIR)
        self.adb.push(jar, DEVICE_JAR_PATH)
        self.adb.forward(self._local_spec, self._remote_spec)
        self._prepared = True

    def _server_command(self) -> str:
        args = self.options.to_args(self.scid)
        return " ".join(
            [
                f"CLASSPATH={DEVICE_JAR_PATH}",
                "app_process",
                "/",
                "com.genymobile.scrcpy.Server",
                SCRCPY_VERSION,
                *args,
            ]
        )

    def start(self) -> Connection:
        """Launch the server and complete the handshake. Returns live sockets."""
        if not self._prepared and self.adb is not None:
            self.prepare()
        if self.adb.serial is None and self.device is not None:
            self.adb.serial = self.device.serial

        self._process = self.adb.popen(["shell", self._server_command()])
        log_tail: collections.deque = collections.deque(maxlen=200)
        self._logs_sink = log_tail
        threading.Thread(
            target=self._drain_logs, args=(self._process, log_tail), daemon=True
        ).start()

        try:
            video, audio, control, device_name = self._handshake()
        except Exception:
            self.stop()
            raise

        self._connection = Connection(
            video=video,
            control=control,
            device_name=device_name,
            audio=audio,
            process=self._process,
            log_tail=log_tail,
        )
        return self._connection

    def _drain_logs(self, proc: subprocess.Popen, sink: collections.deque) -> None:
        if proc.stdout is None:
            return
        for line in proc.stdout:
            sink.append(line.rstrip("\n"))

    def _connect_socket(self, deadline: float) -> socket.socket:
        last: Exception | None = None
        while time.monotonic() < deadline:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(0.5)
            try:
                sock.connect(("127.0.0.1", self.options.port))
            except OSError as exc:
                last = exc
                sock.close()
                time.sleep(0.1)
                continue
            _tune_socket(sock)
            return sock
        raise ServerError(
            f"Could not connect to the forwarded port {self.options.port}: {last}"
        )

    def _handshake(self) -> tuple[socket.socket, socket.socket | None, socket.socket | None, str]:
        """Complete the video/audio/control handshake, retrying until the deadline.

        A successful TCP connect does *not* mean the device is ready: adb
        accepts the forwarded connection locally and only then dials the
        device's abstract socket. If the server has not finished starting, that
        dial fails and we see an immediate EOF. Since the server needs a moment
        to boot a JVM, we retry the whole attempt rather than failing.

        Socket roles are assigned by *accept order* on the device
        (DesktopConnection.open): video, then audio, then control -- the
        connects must happen in exactly that sequence or the device would
        treat our control dial as the audio stream.
        """
        deadline = time.monotonic() + CONNECT_TIMEOUT_S
        video: socket.socket | None = None
        audio: socket.socket | None = None
        control: socket.socket | None = None
        device_name = ""

        while time.monotonic() < deadline:
            candidate = self._connect_socket(deadline)
            candidate_audio: socket.socket | None = None
            candidate_control: socket.socket | None = None
            try:
                # 1) Dummy byte (forward tunnel only) -- written as soon as the
                #    server accepts the video socket, so receiving it proves the
                #    server is up.
                if not self._read_exactly(candidate, 1, deadline):
                    candidate.close()
                    time.sleep(0.15)
                    continue

                # 2) Audio socket next: the device accepts in video -> audio ->
                #    control order. Nothing is read here; the codec id is the
                #    audio thread's business.
                if self.options.audio:
                    candidate_audio = self._connect_socket(deadline)

                # 3) Open the control socket *before* reading any further.
                #    With a forward tunnel the server accepts video, then
                #    audio, then blocks accepting control, and only *then*
                #    calls sendDeviceMeta(). Reading the device name first
                #    would block forever (see DesktopConnection.open()).
                if self.options.control:
                    candidate_control = self._connect_socket(deadline)

                # 4) 64-byte, NUL-padded UTF-8 device name -- only ever on the
                #    first (video) socket.
                raw = self._read_exactly(
                    candidate, DEVICE_NAME_FIELD_LENGTH, deadline
                )
                device_name = decode_device_name(raw)
            except ServerError:
                candidate.close()
                if candidate_audio is not None:
                    candidate_audio.close()
                if candidate_control is not None:
                    candidate_control.close()
                if not self._logs():
                    raise
                raise ServerError(
                    f"Handshake failed. Server output:\n{self._logs()}"
                ) from None

            video, audio, control = candidate, candidate_audio, candidate_control
            break

        if video is None:
            raise ServerError(
                "The device never started streaming. Server output:\n"
                f"{self._logs() or '(no output)'}"
            )

        # Handshake reads set per-read timeouts; the reader loop wants blocking
        # sockets again.
        _tune_socket(video)
        if audio is not None:
            _tune_socket(audio)
        if control is not None:
            _tune_socket(control)
        return video, audio, control, device_name

    def _logs(self) -> str:
        if self._process is None or self._logs_sink is None:
            return ""
        return "\n".join(self._logs_sink)

    def _read_exactly(
        self, sock: socket.socket, count: int, deadline: float
    ) -> bytes:
        """Read exactly ``count`` bytes, returning empty bytes on immediate EOF.

        Uses a socket timeout so the deadline is actually enforceable -- a
        blocking recv() would otherwise hang forever if the peer goes quiet.
        """
        buf = bytearray()
        while len(buf) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ServerError("Timed out reading the server handshake.")
            try:
                sock.settimeout(min(0.5, remaining))
                chunk = sock.recv(count - len(buf))
            except socket.timeout:
                continue
            except OSError as exc:
                raise ServerError(f"Handshake read failed: {exc}") from exc
            if not chunk:
                if not buf:
                    return b""
                raise ServerError("Device closed the socket mid-handshake.")
            buf.extend(chunk)
        return bytes(buf)

    # -- teardown --------------------------------------------------------- #

    def stop(self) -> None:
        """Close sockets, terminate the process and undo the port forward."""
        if self._connection is not None:
            self._connection.close()
            self._connection = None

        if self._process is not None:
            try:
                self._process.terminate()
            except OSError:
                pass
            try:
                self._process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    self._process.kill()
                except OSError:
                    pass
            self._process = None

        try:
            self.adb.forward_remove(self._local_spec)
        except AdbError:
            pass
