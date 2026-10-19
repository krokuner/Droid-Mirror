"""Command line entry point.

    python -m droidmirror                 # launch the GUI
    python -m droidmirror --check         # verify adb / device / server jar
    python -m droidmirror --list-devices
    python -m droidmirror --tcpip         # go wireless from an attached USB device
    python -m droidmirror --tcpip 192.168.1.42   # connect to an IP directly
"""

from __future__ import annotations

import argparse
import sys

from .adb import Adb, AdbError, find_adb, normalize_address
from .config import (
    DEFAULT_ADB_TCP_PORT,
    DEFAULT_BIT_RATE,
    DEFAULT_MAX_FPS,
    DEFAULT_MAX_SIZE,
    SCRCPY_VERSION,
)
from .decoder import HW_ALL, HW_MODES, HW_OFF, available_hw_devices
from .keymap import Profile
from .server import ServerOptions, ensure_server_jar


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="droidmirror",
        description=(
            "Ultra-low-latency Android screen mirroring over USB with on-screen "
            "keybind mapping."
        ),
    )
    parser.add_argument("--serial", help="adb device serial (needed if several are attached)")
    parser.add_argument("--adb", help="path to the adb executable")
    parser.add_argument(
        "--max-size",
        type=int,
        default=DEFAULT_MAX_SIZE,
        help="max video dimension in pixels; 0 = native resolution",
    )
    parser.add_argument(
        "--bit-rate",
        type=int,
        default=DEFAULT_BIT_RATE,
        help="video bit rate in bits/s",
    )
    parser.add_argument(
        "--fps",
        type=int,
        default=DEFAULT_MAX_FPS,
        help="max capture fps; 0 lets the device decide",
    )
    parser.add_argument("--port", type=int, default=None, help="local forwarding port")
    parser.add_argument(
        "--tcpip",
        nargs="?",
        const="",
        default=None,
        metavar="HOST[:PORT]",
        help=(
            "connect over Wi-Fi instead of USB: with no value, switch the "
            "attached USB device to TCP and connect to its Wi-Fi address; "
            "with an address, just run 'adb connect' on it"
        ),
    )
    parser.add_argument(
        "--tcp-port",
        type=int,
        default=DEFAULT_ADB_TCP_PORT,
        help=f"adbd TCP port used by --tcpip (default {DEFAULT_ADB_TCP_PORT})",
    )
    parser.add_argument(
        "--disconnect",
        nargs="?",
        const="",
        default=None,
        metavar="HOST[:PORT]",
        help="disconnect wireless device(s) and exit",
    )
    parser.add_argument(
        "--hw-decode",
        choices=list(HW_MODES + HW_ALL),
        default=HW_OFF,
        help=(
            "hardware decoding backend: 'auto' picks the fastest measured one; "
            "default 'off' (measured within noise of software on this host)"
        ),
    )
    parser.add_argument("--profile", help="keybind profile name to load on start")
    parser.add_argument(
        "--check",
        action="store_true",
        help="check adb, the device and the server jar, then exit",
    )
    parser.add_argument(
        "--list-devices", action="store_true", help="list attached devices and exit"
    )
    parser.add_argument("--version", action="store_true", help="print versions and exit")
    return parser


def _options(args: argparse.Namespace) -> ServerOptions:
    options = ServerOptions(
        max_size=args.max_size,
        bit_rate=args.bit_rate,
        max_fps=args.fps,
    )
    if args.port is not None:
        options.port = args.port
    return options


def _check(args: argparse.Namespace) -> int:
    print(f"droidmirror, targeting scrcpy server {SCRCPY_VERSION}")

    adb_path = args.adb or find_adb()
    if not adb_path:
        print("FAIL  adb not found. Install Android platform-tools or set ANDROID_HOME.")
        return 1
    print(f"ok    adb: {adb_path}")

    try:
        adb = Adb(adb_path, serial=args.serial)
        print(f"ok    adb version: {adb.version()}")
        devices = adb.devices()
    except AdbError as exc:
        print(f"FAIL  {exc}")
        return 1

    if not devices:
        print(
            "FAIL  no device attached. Connect over USB and enable USB "
            "debugging (or use --tcpip to go wireless)."
        )
        return 1
    for device in devices:
        mark = "ok   " if device.is_ready else "warn "
        print(f"{mark} device: {device.display_name} [{device.state}]")
    if not any(d.is_ready for d in devices):
        print("FAIL  no usable device (unauthorized?).")
        return 1

    try:
        jar = ensure_server_jar()
        print(f"ok    server jar: {jar}")
    except Exception as exc:
        print(f"FAIL  {exc}")
        return 1

    hw = available_hw_devices()
    print(f"{'ok   ' if hw else 'note '} hw decode devices: {', '.join(hw) or 'none'}")

    print("All good.")
    return 0


def _do_disconnect(args: argparse.Namespace) -> int:
    try:
        adb = Adb(args.adb or find_adb(), serial=args.serial)
    except AdbError as exc:
        print(f"FAIL  {exc}")
        return 1
    address = normalize_address(args.disconnect, args.tcp_port) if args.disconnect else None
    if args.disconnect and address is None:
        print(f"FAIL  '{args.disconnect}' is not a valid address.")
        return 2
    print((adb.disconnect(address) or "disconnected").strip())
    return 0


def _connect_wireless(args: argparse.Namespace) -> int:
    """Handle ``--tcpip``; returns an exit code, or sets args.serial and returns 0."""
    try:
        adb = Adb(args.adb or find_adb(), serial=args.serial)
    except AdbError as exc:
        print(f"FAIL  {exc}")
        return 1

    if not args.tcpip.strip():
        # No address given: switch the attached USB device over automatically.
        try:
            address = adb.enable_wireless(args.tcp_port)
        except AdbError as exc:
            print(f"FAIL  {exc}")
            return 1
        print(f"ok    wireless device ready: {address}")
    else:
        address = normalize_address(args.tcpip, args.tcp_port)
        if address is None:
            print(f"FAIL  '{args.tcpip}' is not a valid address.")
            return 2
        print((adb.connect(address) or f"connecting to {address}").strip())
        if not adb.is_connected(address):
            print(f"FAIL  could not reach {address}.")
            return 1
        print(f"ok    wireless device ready: {address}")

    args.serial = args.serial or address
    print("ok    you can now unplug the USB cable.")
    return 0


def _load_profile(name: str | None) -> Profile:
    from .keymap import DEFAULT_PROFILE_NAME, profile_path

    target = name or DEFAULT_PROFILE_NAME
    path = profile_path(target)
    if path.is_file():
        try:
            return Profile.load(path)
        except (OSError, ValueError):
            pass
    return Profile(name=target)


def main(argv: list[str] | None = None) -> int:
    # First: anything that dies from here on (including native faults) must
    # leave a readable trace behind -- the console vanishes with the process.
    from . import crashlog

    crashlog.install()

    args = build_parser().parse_args(argv)

    if args.version:
        from . import __version__

        print(f"droidmirror {__version__} (scrcpy server {SCRCPY_VERSION})")
        return 0

    if args.disconnect is not None:
        return _do_disconnect(args)

    if args.tcpip is not None:
        code = _connect_wireless(args)
        if code:
            return code

    if args.check:
        return _check(args)

    if args.list_devices:
        try:
            adb = Adb(args.adb or find_adb(), serial=args.serial)
            devices = adb.devices()
        except AdbError as exc:
            print(f"FAIL  {exc}")
            return 1
        for device in devices:
            print(f"{device.serial}\t{device.state}\t{device.display_name}")
        return 0

    # GUI
    from PySide6.QtWidgets import QApplication

    from .ui.main_window import MainWindow

    try:
        adb: Adb | None = Adb(args.adb or find_adb(), serial=args.serial)
    except AdbError:
        adb = None  # the window explains this to the user

    app = QApplication(sys.argv)
    app.setApplicationName("DroidMirror")
    window = MainWindow(
        adb=adb,
        options=_options(args),
        profile=_load_profile(args.profile),
        hw_decode=args.hw_decode,
    )
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
