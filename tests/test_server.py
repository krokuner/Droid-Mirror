"""Server launch options and socket handshake tests.

The handshake is the most fragile part of the connection (ordering, the dummy
byte, the fixed-width device name), so it is exercised here against a fake TCP
server that mimics what the real device does -- no phone required.
"""

from __future__ import annotations

import socket
import struct
import threading

import pytest

from droidmirror.protocol import CodecId
from droidmirror.server import ScrcpyServer, ServerError, ServerOptions

REQUIRED_KEYS = {
    "scid",
    "log_level",
    "video_codec",
    "max_size",
    "video_bit_rate",
    "audio",
    "control",
    "tunnel_forward",
    "cleanup",
    "send_device_meta",
    "send_frame_meta",
    "send_stream_meta",
    "send_dummy_byte",
    "stay_awake",
}


def parse_args(args: list[str]) -> dict[str, str]:
    return dict(item.split("=", 1) for item in args)


# --------------------------------------------------------------------------- #
# Options
# --------------------------------------------------------------------------- #


def test_options_cover_every_required_key():
    parsed = parse_args(ServerOptions().to_args(0x1234))
    assert REQUIRED_KEYS <= set(parsed)


def test_options_enable_audio_and_control_by_default():
    parsed = parse_args(ServerOptions().to_args(1))
    assert parsed["audio"] == "true"
    assert parsed["control"] == "true"


def test_options_can_disable_audio():
    parsed = parse_args(ServerOptions(audio=False).to_args(1))
    assert parsed["audio"] == "false"
    assert parsed["control"] == "true"


def test_options_enable_all_stream_metadata():
    """Our demuxer depends on codec id + session packets + frame headers."""
    parsed = parse_args(ServerOptions().to_args(1))
    assert parsed["send_stream_meta"] == "true"
    assert parsed["send_frame_meta"] == "true"
    assert parsed["send_device_meta"] == "true"
    assert parsed["send_dummy_byte"] == "true"


def test_options_use_a_forward_tunnel():
    """Forward tunnel is what we handshake against (dummy byte first)."""
    assert parse_args(ServerOptions().to_args(1))["tunnel_forward"] == "true"


def test_scid_is_sent_as_hexadecimal():
    """The device parses scid with radix 16; decimal makes it abort."""
    parsed = parse_args(ServerOptions().to_args(0x0ABCDEF))
    assert parsed["scid"] == "00abcdef"
    assert int(parsed["scid"], 16) == 0x0ABCDEF


def test_scid_is_zero_padded_and_never_signed():
    # The value is a 31-bit random, so this must not overflow a signed int.
    for value in (0, 1, 0x7FFFFFFF):
        parsed = parse_args(ServerOptions().to_args(value))
        assert len(parsed["scid"]) == 8
        assert int(parsed["scid"], 16) == value


def test_max_fps_is_omitted_when_unset():
    assert "max_fps" not in parse_args(ServerOptions(max_fps=0).to_args(1))
    assert parse_args(ServerOptions(max_fps=60).to_args(1))["max_fps"] == "60"


def test_options_do_not_use_legacy_key_names():
    """v4.x renamed these; sending the old names makes the server refuse."""
    parsed = parse_args(ServerOptions().to_args(1))
    assert "bit_rate" not in parsed
    assert "send_codec_meta" not in parsed


# --------------------------------------------------------------------------- #
# Socket naming
# --------------------------------------------------------------------------- #


def test_socket_name_matches_scrcpy_convention():
    server = ScrcpyServer(adb=None, options=ServerOptions())
    server.scid = 0x00ABCDEF
    # DesktopConnection.getSocketName() -> "scrcpy_%08x"
    assert server.socket_name == "scrcpy_00abcdef"
    assert len(server.socket_name) == len("scrcpy_") + 8


def test_scid_is_a_31_bit_random():
    for _ in range(50):
        server = ScrcpyServer(adb=None, options=ServerOptions())
        assert 0 <= server.scid < (1 << 31)


# --------------------------------------------------------------------------- #
# Handshake
# --------------------------------------------------------------------------- #


class FakeDevice:
    """Mimics the device side of the forward-tunnel handshake.

    Crucially it reproduces the real ordering: the dummy byte is written as
    soon as the video socket is accepted, then the server blocks accepting
    the audio and control sockets in that order, and only after *that* does
    it send the device name. A client that reads the name before opening
    control (or dials sockets out of order) will hang here, exactly as it
    would against a real device.
    """

    def __init__(
        self,
        device_name: str = "Fake Phone",
        control: bool = True,
        audio: bool = True,
    ) -> None:
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(6)
        self.port = self._listener.getsockname()[1]
        self.device_name = device_name
        self.control = control
        self.audio = audio
        self.sockets: list[socket.socket] = []
        #: Role of every accepted connection, in accept order.
        self.accept_order: list[str] = []
        #: True once the meta was sent with the control socket already accepted.
        self.name_sent_after_control = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            # 1) video socket + dummy byte
            video, _ = self._listener.accept()
            self.sockets.append(video)
            self.accept_order.append("video")
            video.sendall(b"\x00")

            # 2) audio socket (no bytes until the stream starts)
            if self.audio:
                audio, _ = self._listener.accept()
                self.sockets.append(audio)
                self.accept_order.append("audio")

            # 3) control socket -- the server blocks here before sending meta
            if self.control:
                control, _ = self._listener.accept()
                self.sockets.append(control)
                self.accept_order.append("control")
            self.name_sent_after_control = True

            # 4) fixed-width NUL-padded device name, then the codec id
            video.sendall(self.device_name.encode("utf-8").ljust(64, b"\x00"))
            video.sendall(struct.pack(">I", int(CodecId.H264)))
        except OSError:
            pass

    def close(self) -> None:
        for sock in self.sockets:
            try:
                sock.close()
            except OSError:
                pass
        self._listener.close()


@pytest.fixture
def fake_device():
    device = FakeDevice()
    yield device
    device.close()


def make_server(port: int, control: bool = True, audio: bool = True) -> ScrcpyServer:
    options = ServerOptions(port=port, control=control, audio=audio)
    server = ScrcpyServer(adb=None, options=options)
    server.device = object()  # truthy: start() must not try to prepare()
    return server


def test_handshake_reads_dummy_byte_name_and_opens_control(fake_device):
    server = make_server(fake_device.port)
    video, audio, control, name = server._handshake()

    assert name == "Fake Phone"
    assert control is not None
    assert audio is not None
    assert video.fileno() != -1
    # The codec id should still be pending on the video socket.
    assert struct.unpack(">I", server._read_exactly(video, 4, 1e9))[0] == int(
        CodecId.H264
    )


def test_handshake_dials_audio_between_video_and_control(fake_device):
    """Device-side roles are positional: video, audio, control, in that
    order. Dialing audio after control would hand the device our control
    socket as its audio stream and deadlock the third accept."""
    server = make_server(fake_device.port)
    video, audio, control, _name = server._handshake()
    assert fake_device.accept_order == ["video", "audio", "control"]
    assert audio.fileno() != -1
    assert control.fileno() != -1
    assert video.fileno() != -1


def test_handshake_without_audio_only_dials_two_sockets():
    device = FakeDevice(audio=False)
    try:
        server = make_server(device.port, audio=False)
        video, audio, control, name = server._handshake()
        assert audio is None
        assert control is not None
        assert device.accept_order == ["video", "control"]
        assert name == "Fake Phone"
    finally:
        device.close()


def test_handshake_opens_control_before_reading_the_device_name(fake_device):
    """Regression: the server only sends meta once control is accepted."""
    server = make_server(fake_device.port)
    _, _, control, name = server._handshake()
    assert control is not None
    assert name == "Fake Phone"
    assert fake_device.name_sent_after_control


def test_handshake_skips_control_when_disabled():
    device = FakeDevice(control=False)
    try:
        server = make_server(device.port, control=False)
        video, _audio, control, name = server._handshake()
        assert control is None
        assert name == "Fake Phone"
        assert device.accept_order == ["video", "audio"]
    finally:
        device.close()


def test_handshake_decodes_unicode_device_names():
    device = FakeDevice("Pixel 7 – José")
    try:
        server = make_server(device.port)
        _, _, _, name = server._handshake()
        assert name == "Pixel 7 – José"
    finally:
        device.close()


def test_handshake_fails_if_the_device_closes_immediately(monkeypatch):
    # A connect that EOFs means the server is not listening yet -- which is
    # indistinguishable, from here, from one that is still booting. So the
    # handshake retries until its deadline, exactly as it does for a real
    # device. Shorten that deadline for the test: without this it burns the
    # production CONNECT_TIMEOUT_S (12s) on every run to prove a failure that
    # is guaranteed.
    monkeypatch.setattr("droidmirror.server.CONNECT_TIMEOUT_S", 0.4)
    """A connect that EOFs means the server is not listening yet."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def close_immediately():
        conn, _ = listener.accept()
        conn.close()

    threading.Thread(target=close_immediately, daemon=True).start()
    server = make_server(port)
    with pytest.raises(ServerError):
        server._handshake()
    listener.close()


def test_handshake_times_out_when_nothing_connects(monkeypatch):
    # A closed port -> every connect attempt is refused.
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()

    # Don't wait the production timeout for a guaranteed failure.
    monkeypatch.setattr("droidmirror.server.CONNECT_TIMEOUT_S", 0.4)
    server = ScrcpyServer(adb=None, options=ServerOptions(port=port))
    with pytest.raises(ServerError):
        server._handshake()


# --------------------------------------------------------------------------- #
# prepare(): device-scoped commands must be pinned to the chosen transport
# --------------------------------------------------------------------------- #


class RecordingAdb:
    """Records the -s selector in force at the moment of each adb call."""

    def __init__(self, serial: str | None = None) -> None:
        self.serial = serial
        self.calls: list[tuple[str, str | None]] = []

    def _note(self, name: str) -> None:
        self.calls.append((name, self.serial))

    def start_server(self) -> None:
        self._note("start-server")

    def pick_device(self):
        raise AssertionError("a device was already chosen; pick_device must not run")

    def ensure_dir(self, remote: str) -> None:
        self._note("ensure_dir")

    def push(self, local, remote) -> None:
        self._note("push")

    def forward(self, local, remote) -> None:
        self._note("forward")


def test_prepare_pins_commands_to_the_chosen_device(monkeypatch, tmp_path):
    """With a cable *and* a wireless device attached, a bare adb call is
    ambiguous ("more than one device/emulator"). A caller-supplied device
    must bind adb.serial before any device-scoped command runs."""
    from droidmirror import server as server_mod
    from droidmirror.adb import Device

    monkeypatch.setattr(server_mod, "ensure_server_jar", lambda: tmp_path / "jar")
    adb = RecordingAdb(serial=None)
    device = Device(serial="10.0.0.9:5555", state="device")
    server = ScrcpyServer(adb=adb, device=device, options=ServerOptions())

    server.prepare()

    assert [name for name, _ in adb.calls] == ["start-server", "ensure_dir", "push", "forward"]
    scoped = [serial for name, serial in adb.calls if name != "start-server"]
    assert scoped == ["10.0.0.9:5555"] * 3, (
        "every device-scoped command must carry -s for the chosen device"
    )


def test_prepare_binds_the_serial_pick_device_returns(monkeypatch, tmp_path):
    from droidmirror import server as server_mod
    from droidmirror.adb import Device

    monkeypatch.setattr(server_mod, "ensure_server_jar", lambda: tmp_path / "jar")
    adb = RecordingAdb(serial=None)
    adb.pick_device = lambda: Device(serial="USB123", state="device")
    server = ScrcpyServer(adb=adb, device=None, options=ServerOptions())

    server.prepare()

    scoped = [serial for name, serial in adb.calls if name != "start-server"]
    assert scoped == ["USB123"] * 3


def test_start_calls_prepare_even_when_device_is_provided(monkeypatch, tmp_path):
    from droidmirror import server as server_mod
    from droidmirror.adb import Device

    monkeypatch.setattr(server_mod, "ensure_server_jar", lambda: tmp_path / "jar")
    adb = RecordingAdb(serial=None)
    device = Device(serial="USB123", state="device")
    server = ScrcpyServer(adb=adb, device=device, options=ServerOptions())

    class DummyProc:
        stdout = None

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    adb.popen = lambda _args: DummyProc()  # type: ignore[attr-defined]
    monkeypatch.setattr(
        server, "_handshake", lambda: ("video", "audio", "control", "Phone")
    )

    server.start()
    assert [name for name, _ in adb.calls] == [
        "start-server",
        "ensure_dir",
        "push",
        "forward",
    ]
