"""Wireless (adb over Wi-Fi) support tests.

The parts that can be exercised without a phone: address/route parsing, the
command shapes the client builds (``connect`` must never carry ``-s``), and the
``enable_wireless`` retry loop.
"""

from __future__ import annotations

import subprocess

import pytest

from droidmirror.adb import (
    IP_HISTORY_MAX,
    Adb,
    AdbError,
    Device,
    looks_wireless,
    normalize_address,
    parse_device_ip,
    remember_ip,
)
from droidmirror.config import DEFAULT_ADB_TCP_PORT


# --------------------------------------------------------------------------- #
# Address parsing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text,expected",
    [
        ("192.168.1.42", "192.168.1.42:5555"),
        (" 192.168.1.42 ", "192.168.1.42:5555"),
        ("192.168.1.42:4321", "192.168.1.42:4321"),
        ("adb-ABC123-xyz._adb-tls-connect._tcp", "adb-ABC123-xyz._adb-tls-connect._tcp:5555"),
    ],
)
def test_normalize_address_fills_in_the_default_port(text, expected):
    assert normalize_address(text) == expected


@pytest.mark.parametrize("text", ["", "   ", "http://1.2.3.4", "has space:5555", "[::1]:5555"])
def test_normalize_address_rejects_junk(text):
    assert normalize_address(text) is None


def test_normalize_address_honours_a_custom_default_port():
    assert normalize_address("10.0.0.5", 1234) == "10.0.0.5:1234"


# --------------------------------------------------------------------------- #
# IP discovery
# --------------------------------------------------------------------------- #


def test_parse_device_ip_prefers_the_default_route_source():
    route = (
        "192.168.1.0/24 dev wlan0 proto kernel scope link src 192.168.1.42\n"
        "default via 192.168.1.1 dev wlan0 proto dhcp src 192.168.1.42 metric 303\n"
    )
    assert parse_device_ip(route) == "192.168.1.42"


def test_parse_device_ip_falls_back_to_an_interface_address():
    addr = "    inet 10.1.2.3/24 brd 10.1.2.255 scope global wlan0\n"
    assert parse_device_ip(addr) == "10.1.2.3"


@pytest.mark.parametrize(
    "route",
    [
        "",
        "default via 192.168.1.1 dev wlan0\n",  # gateway only, no src
        "127.0.0.1\n",
        "169.254.10.10\n",
    ],
)
def test_parse_device_ip_ignores_unusable_output(route):
    assert parse_device_ip(route) is None


# --------------------------------------------------------------------------- #
# Transport classification
# --------------------------------------------------------------------------- #


def test_looks_wireless_distinguishes_tcp_and_mdns_serials():
    assert looks_wireless("192.168.1.42:5555")
    assert looks_wireless("adb-ABC._adb-tls-connect._tcp")
    assert not looks_wireless("R95Y802YKGR")
    assert not looks_wireless("emulator-5554")


def test_device_is_wireless_matches_its_serial():
    assert Device(serial="10.0.0.2:5555", state="device").is_wireless
    assert not Device(serial="R95Y", state="device").is_wireless


def test_device_link_names_the_transport():
    assert Device(serial="10.0.0.2:5555", state="device").link == "Wi-Fi"
    assert Device(serial="R95Y", state="device").link == "USB"


def test_device_display_name_tags_the_transport():
    """The device list must not leave you guessing USB vs Wi-Fi."""
    usb = Device(serial="R95Y", state="device", model="SM-X210")
    wifi = Device(serial="10.0.0.2:5555", state="device", model="SM-X210")
    assert usb.display_name == "SM-X210 (R95Y) - USB"
    assert wifi.display_name == "SM-X210 (10.0.0.2:5555) - Wi-Fi"


# --------------------------------------------------------------------------- #
# Command construction
# --------------------------------------------------------------------------- #


@pytest.fixture
def fake_subprocess(monkeypatch):
    """Capture argv of subprocess.run and return a canned CompletedProcess."""
    calls: list[list[str]] = []

    def run(cmd, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr("droidmirror.adb.subprocess.run", run)
    return calls


def test_connect_never_sends_a_device_selector(fake_subprocess):
    """adb rejects -s on host-level networking commands."""
    adb = Adb("adb", serial="USB123")
    assert adb.connect("192.168.1.42:5555") == "ok"

    assert fake_subprocess == [["adb", "connect", "192.168.1.42:5555"]]
    assert "-s" not in fake_subprocess[0]


def test_disconnect_omits_the_address_when_none_is_given(fake_subprocess):
    adb = Adb("adb", serial="USB123")
    adb.disconnect()
    adb.disconnect("192.168.1.42:5555")
    assert fake_subprocess == [
        ["adb", "disconnect"],
        ["adb", "disconnect", "192.168.1.42:5555"],
    ]


def test_tcpip_targets_the_attached_device(fake_subprocess):
    adb = Adb("adb", serial="USB123")
    adb.tcpip()
    assert fake_subprocess == [["adb", "-s", "USB123", "tcpip", "5555"]]


def test_devices_is_host_global(fake_subprocess, monkeypatch):
    """Must keep working after tcpip drops the USB transport."""
    monkeypatch.setattr(
        "droidmirror.adb.subprocess.run",
        lambda cmd, **kw: subprocess.CompletedProcess(
            cmd, 0, stdout="List of devices attached\nR95Y802YKGR\tdevice\n", stderr=""
        ),
    )
    adb = Adb("adb", serial="USB123")
    devices = adb.devices()
    assert devices[0].serial == "R95Y802YKGR"


# --------------------------------------------------------------------------- #
# enable_wireless flow
# --------------------------------------------------------------------------- #


def _stub(monkeypatch, adb: Adb, *, ip="192.168.1.42", connected=True):
    calls: dict = {"tcpip": [], "connect": []}
    monkeypatch.setattr(adb, "pick_device", lambda: Device(serial="USB123", state="device"))
    monkeypatch.setattr(adb, "device_ip", lambda: ip)
    monkeypatch.setattr(adb, "tcpip", lambda port=5555: calls["tcpip"].append(port) or "")
    monkeypatch.setattr(
        adb, "connect", lambda address, **kw: calls["connect"].append(address) or "connected"
    )
    monkeypatch.setattr(adb, "is_connected", lambda address: connected)
    monkeypatch.setattr("droidmirror.adb.time.sleep", lambda _s: None)
    return calls


def test_enable_wireless_switches_and_selects_the_address(monkeypatch):
    adb = Adb("adb", serial="USB123")
    calls = _stub(monkeypatch, adb)

    address = adb.enable_wireless()

    assert address == "192.168.1.42:5555"
    assert calls["tcpip"] == [DEFAULT_ADB_TCP_PORT]
    assert calls["connect"] == [address]
    assert adb.serial == address, "the client now targets the wireless transport"


def test_enable_wireless_honours_a_custom_port(monkeypatch):
    adb = Adb("adb", serial="USB123")
    calls = _stub(monkeypatch, adb)
    assert adb.enable_wireless(4321) == "192.168.1.42:4321"
    assert calls["tcpip"] == [4321]


def test_enable_wireless_retries_until_connected(monkeypatch):
    adb = Adb("adb", serial="USB123")
    monkeypatch.setattr(adb, "pick_device", lambda: Device(serial="USB123", state="device"))
    monkeypatch.setattr(adb, "device_ip", lambda: "192.168.1.42")
    monkeypatch.setattr(adb, "tcpip", lambda port=5555: "")
    attempts = {"n": 0}

    def connect(address, **kw):
        attempts["n"] += 1
        return "failed" if attempts["n"] < 3 else "connected"

    monkeypatch.setattr(adb, "connect", connect)
    monkeypatch.setattr(adb, "is_connected", lambda address: attempts["n"] >= 3)
    monkeypatch.setattr("droidmirror.adb.time.sleep", lambda _s: None)

    assert adb.enable_wireless(attempts=5) == "192.168.1.42:5555"
    assert attempts["n"] == 3


def test_enable_wireless_gives_up_with_a_clear_error(monkeypatch):
    adb = Adb("adb", serial="USB123")
    monkeypatch.setattr(adb, "pick_device", lambda: Device(serial="USB123", state="device"))
    monkeypatch.setattr(adb, "device_ip", lambda: "192.168.1.42")
    monkeypatch.setattr(adb, "tcpip", lambda port=5555: "")
    monkeypatch.setattr(adb, "connect", lambda address, **kw: "cannot connect")
    monkeypatch.setattr(adb, "is_connected", lambda address: False)
    monkeypatch.setattr("droidmirror.adb.time.sleep", lambda _s: None)

    with pytest.raises(AdbError, match="192.168.1.42:5555"):
        adb.enable_wireless(attempts=2)


def test_enable_wireless_explains_when_there_is_no_wifi_address(monkeypatch):
    adb = Adb("adb", serial="USB123")
    monkeypatch.setattr(adb, "pick_device", lambda: Device(serial="USB123", state="device"))
    monkeypatch.setattr(adb, "device_ip", lambda: None)
    monkeypatch.setattr(adb, "tcpip", lambda port=5555: pytest.fail("must not tcpip"))

    with pytest.raises(AdbError, match="Wi-Fi address"):
        adb.enable_wireless()


# --------------------------------------------------------------------------- #
# CLI wiring
# --------------------------------------------------------------------------- #


def test_tcpip_defaults_to_none_so_gui_is_unaffected():
    from droidmirror.__main__ import build_parser

    args = build_parser().parse_args([])
    assert args.tcpip is None
    assert args.disconnect is None
    assert args.tcp_port == DEFAULT_ADB_TCP_PORT


def test_tcpip_flag_accepts_a_bare_ip_or_an_address():
    from droidmirror.__main__ import build_parser

    parser = build_parser()
    assert parser.parse_args(["--tcpip"]).tcpip == ""
    assert parser.parse_args(["--tcpip", "192.168.1.5"]).tcpip == "192.168.1.5"
    assert parser.parse_args(["--tcpip", "192.168.1.5:4321"]).tcpip == "192.168.1.5:4321"


def test_disconnect_flag_is_optional():
    from droidmirror.__main__ import build_parser

    parser = build_parser()
    assert parser.parse_args(["--disconnect"]).disconnect == ""
    assert parser.parse_args(["--disconnect", "1.2.3.4:5555"]).disconnect == "1.2.3.4:5555"


# --------------------------------------------------------------------------- #
# IP prompt history (newest first, bounded)
# --------------------------------------------------------------------------- #


def test_remember_ip_puts_the_newest_address_first():
    assert remember_ip([], "10.0.0.1:5555") == ["10.0.0.1:5555"]
    assert remember_ip(["10.0.0.1:5555", "10.0.0.2:5555"], "10.0.0.2:5555") == [
        "10.0.0.2:5555",
        "10.0.0.1:5555",
    ]


def test_remember_ip_dedupes_and_drops_junk():
    assert remember_ip(["a", "b"], "c") == ["c", "a", "b"]
    assert remember_ip(["", "a", None], "a") == ["a"]


def test_remember_ip_keeps_the_history_bounded():
    old = [f"10.0.0.{n}:5555" for n in range(1, 20)]
    history = remember_ip(old, "192.168.1.4:5555")

    assert len(history) == IP_HISTORY_MAX
    assert history[0] == "192.168.1.4:5555"


# --------------------------------------------------------------------------- #
# Robustness: junk output, frozen GUI, crash trails
# --------------------------------------------------------------------------- #


def test_adb_output_decoding_survives_junk_bytes():
    """adbd can emit invalid UTF-8 while it restarts (tcpip); that must not
    raise -- the locale codec would crash the whole wireless flow."""
    import sys

    adb = Adb(sys.executable)
    proc = adb.run(
        ["-c", "import sys; sys.stdout.buffer.write(b'ok: \\xff\\xfe\\x81 junk')"],
        use_serial=False,
    )
    assert "ok:" in proc.stdout and "junk" in proc.stdout


def test_enable_wireless_connects_with_a_short_timeout(monkeypatch):
    """The retry loop runs on the GUI thread: a sleeping host must not
    freeze the window for minutes."""
    adb = Adb("adb", serial="USB123")
    calls = _stub(monkeypatch, adb)
    timeouts: list = []
    real_connect = adb.connect

    def connect(address, **kw):
        timeouts.append(kw.get("timeout"))
        return real_connect(address, **kw)

    monkeypatch.setattr(adb, "connect", connect)
    adb.enable_wireless()
    assert timeouts == [10.0]


def test_crashlog_hook_writes_the_trace(monkeypatch, tmp_path):
    """Any unhandled error must leave a readable file behind -- the console
    tied to run.bat dies with the process."""
    from droidmirror import crashlog

    log = tmp_path / "crash.log"
    monkeypatch.setattr(crashlog, "LOG_PATH", log)
    # The hook shows a box on the GUI thread; never let a test hang on one.
    monkeypatch.setattr(
        "PySide6.QtWidgets.QMessageBox.critical", lambda *a, **k: None
    )
    import sys as _sys
    import threading

    old_thread_hook = threading.excepthook
    crashlog.install()

    try:
        try:
            raise ValueError("boom")
        except ValueError:
            crashlog._sys_hook(ValueError, ValueError("boom"), _sys.exc_info()[2])
        text = log.read_text(encoding="utf-8")
        assert "ValueError: boom" in text
    finally:
        crashlog.faulthandler.disable()
        if crashlog._handle is not None:
            crashlog._handle.close()
        crashlog._handle = None
        _sys.excepthook = _sys.__excepthook__
        threading.excepthook = old_thread_hook


def test_find_adb_uses_platform_exe_in_sdk_env_vars(monkeypatch, tmp_path):
    from droidmirror import adb as adb_mod

    monkeypatch.setattr(adb_mod.shutil, "which", lambda _name: None)
    monkeypatch.setenv("DROIDMIRROR_HOME", str(tmp_path / "no_vendor"))
    sdk = tmp_path / "sdk"
    pt = sdk / "platform-tools"
    pt.mkdir(parents=True)
    exe_file = pt / adb_mod._exe("adb")
    exe_file.write_bytes(b"fake")
    monkeypatch.setenv("ANDROID_HOME", str(sdk))

    assert adb_mod.find_adb() == str(exe_file)


def test_pick_device_raises_when_requested_serial_is_missing(monkeypatch):
    adb = Adb("adb", serial="MISSING123")
    monkeypatch.setattr(
        adb, "devices", lambda: [Device(serial="OTHER456", state="device")]
    )
    with pytest.raises(AdbError, match="MISSING123"):
        adb.pick_device()


def test_cli_adb_flag_overrides_find_adb_and_list_devices_handles_error(monkeypatch):
    from droidmirror import __main__ as cli

    seen_paths: list[str | None] = []

    class FakeAdb:
        def __init__(self, path=None, serial=None):
            seen_paths.append(path)
            raise AdbError("custom adb failed")

    monkeypatch.setattr(cli, "find_adb", lambda: "/system/adb")
    monkeypatch.setattr(cli, "Adb", FakeAdb)
    monkeypatch.setattr("droidmirror.crashlog.install", lambda: None)

    rc = cli.main(["--adb", "/custom/adb", "--list-devices"])
    assert rc == 1
    assert seen_paths == ["/custom/adb"]


def test_load_profile_none_loads_default_json_when_present(monkeypatch, tmp_path):
    from droidmirror import __main__ as cli
    from droidmirror.keymap import Binding, Profile

    monkeypatch.setenv("DROIDMIRROR_HOME", str(tmp_path))
    preset = Profile(name="default", bindings=[Binding("space", 0.5, 0.8)])
    preset.save()

    loaded = cli._load_profile(None)
    assert loaded.name == "default"
    assert len(loaded.bindings) == 1
    assert loaded.bindings[0].key == "space"
