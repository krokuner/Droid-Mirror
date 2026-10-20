"""Thin, dependency-free wrapper around the Android Debug Bridge.

We shell out to the ``adb`` binary rather than reimplementing the ADB protocol:
it is the same transport scrcpy itself uses, and it keeps USB setup to a single
well-tested dependency.

The same client carries both transports: over USB the device shows up as a
hardware serial, over Wi-Fi as ``host:port`` after ``adb tcpip`` + ``adb
connect``. Everything downstream (forward tunnel, server push, sockets) is
independent of which one is in use.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .config import DEFAULT_ADB_TCP_PORT


class AdbError(RuntimeError):
    """Raised when adb is missing or a command fails."""


# --------------------------------------------------------------------------- #
# Wireless helpers (pure -- no adb needed, so they are trivially testable)
# --------------------------------------------------------------------------- #

#: ``host`` or ``host:port`` -- also matches adb's mDNS serials.
_HOST_PORT = re.compile(r"^(?P<host>[A-Za-z0-9._-]+)(?::(?P<port>\d+))?$")


def _usable_ip(address: str) -> bool:
    """Whether an IPv4 address can actually be reached from this computer."""
    parts = address.split(".")
    if len(parts) != 4:
        return False
    try:
        octets = [int(p) for p in parts]
    except ValueError:
        return False
    if any(o < 0 or o > 255 for o in octets):
        return False
    # Loopback, unspecified and link-local are never the device's LAN address.
    if octets[0] in (0, 127) or (octets[0] == 169 and octets[1] == 254):
        return False
    return True


def parse_device_ip(output: str) -> str | None:
    """Best-effort LAN IPv4 out of ``ip route`` / ``ip addr`` output.

    Prefers the ``src`` address of the default route -- that is the interface
    the device actually reaches the network through -- and ignores loopback,
    unspecified and link-local addresses. Returns None when nothing usable is
    present (e.g. Wi-Fi is off).
    """
    if not output:
        return None
    # 1) A route's ``src X`` is the address traffic actually leaves from.
    for match in re.finditer(r"\bsrc\s+(\d{1,3}(?:\.\d{1,3}){3})", output):
        if _usable_ip(match.group(1)):
            return match.group(1)
    # 2) ``inet X/prefix`` -- an interface's own address (from ``ip addr``).
    for match in re.finditer(r"\binet\s+(\d{1,3}(?:\.\d{1,3}){3})/", output):
        if _usable_ip(match.group(1)):
            return match.group(1)
    return None


def normalize_address(text: str, default_port: int = DEFAULT_ADB_TCP_PORT) -> str | None:
    """Turn user-typed input into adb's ``host:port`` serial form.

    Accepts ``192.168.1.42``, ``192.168.1.42:5555`` and mDNS names such as
    ``adb-R95Y802YKGR-abc._adb-tls-connect._tcp``. Returns None if the text is
    not a plausible address (so callers can tell the user rather than invent
    something).
    """
    text = (text or "").strip()
    if not text:
        return None
    if "[" in text or "]" in text:  # bracketed IPv6: not supported
        return None
    match = _HOST_PORT.match(text)
    if not match:
        return None
    if match.group("port"):
        return text
    return f"{text}:{default_port}"


#: How many dialled addresses the wireless prompt remembers (newest first).
IP_HISTORY_MAX = 8


def remember_ip(history: list[str], address: str) -> list[str]:
    """``history`` with ``address`` moved to the front, deduplicated.

    The wireless prompt pre-fills from the head of this list, so the most
    recently *successful* address is always the one that comes up. Capped so
    the settings file cannot grow without bound.
    """
    history = [a for a in history if a and a != address]
    return ([address] + history)[:IP_HISTORY_MAX]


def looks_wireless(serial: str) -> bool:
    """Whether an adb serial denotes a network transport rather than USB.

    TCP devices are ``host:port``; Android 11+ wireless debugging pairs over
    mDNS and shows up as ``..._adb-tls-connect._tcp``.
    """
    return ":" in serial or "_adb-tls" in serial


#: mDNS service types adbd advertises on the LAN.
MDNS_CONNECT = "_adb-tls-connect._tcp"  # Android 11+ Wireless debugging
MDNS_PAIRING = "_adb-tls-pairing._tcp"  # the "pair with code" popup
MDNS_LEGACY = "_adb._tcp"  # classic ``adb tcpip``


def parse_mdns_services(output: str) -> list[tuple[str, str, str]]:
    """``(name, service, host:port)`` rows out of ``adb mdns services``."""
    rows: list[tuple[str, str, str]] = []
    for line in (output or "").splitlines():
        parts = line.split()
        if len(parts) < 3 or parts[0].lower().startswith("list"):
            continue
        name, service, address = parts[0], parts[1].rstrip("."), parts[-1]
        if _HOST_PORT.match(address) and ":" in address:
            rows.append((name, service, address))
    return rows


def dedupe_wireless(devices: list[Device]) -> list[Device]:
    """Drop mDNS-named duplicates of a device also connected by IP.

    After pairing, adb auto-connects through mDNS *and* keeps the
    ``ip:port`` transport, so the same tablet shows up twice.
    """
    by_ip = {
        (d.model, d.product)
        for d in devices
        if d.is_ready and ":" in d.serial and "_adb-tls" not in d.serial
    }
    return [
        d for d in devices
        if not ("_adb-tls" in d.serial and (d.model, d.product) in by_ip)
    ]


#: Directories to probe when `adb` is not on PATH.
_SDK_RELATIVE = ("platform-tools", "adb")
_SDK_ENV_VARS = ("ANDROID_HOME", "ANDROID_SDK_ROOT", "ANDROID_AVD_HOME")

_EXTRA_CANDIDATES = (
    "C:/platform-tools/adb.exe",
    "C:/adb/adb.exe",
    "/usr/lib/android-sdk/platform-tools/adb",
    "/usr/local/share/android-sdk/platform-tools/adb",
    "/opt/homebrew/bin/adb",
)


def _exe(name: str) -> str:
    return f"{name}.exe" if sys.platform == "win32" else name


#: Windows-only: never allocate a console for a child process.
#:
#: This matters because the GUI polls adb constantly -- the lock/keyboard/
#: foreground watcher alone issues `dumpsys` every couple of seconds. When the
#: app runs without a console of its own (pythonw.exe, a .pyw shortcut, or a
#: packaged build) every one of those would otherwise pop its own console
#: window on screen and steal focus, so a session looked like it was
#: randomly opening command prompts. CREATE_NO_WINDOW suppresses that.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
_SUBPROCESS_FLAGS: dict = {"creationflags": _NO_WINDOW} if _NO_WINDOW else {}


def find_adb() -> str | None:
    """Locate the adb executable, or return None.

    Search order: PATH first (so a system install always wins), then the
    well-known Android SDK locations, then a copies bundled inside this
    checkout under ``vendor/platform-tools`` -- which is what lets the project
    work without touching the system PATH at all.
    """
    found = shutil.which("adb")
    if found:
        return found

    candidates: list[Path] = []
    try:
        from .config import bundle_root, is_frozen, project_root

        if is_frozen():
            #: A frozen build carries its own copy of platform-tools, which
            #: PyInstaller unpacks under `_MEIPASS`, not beside the .exe.
            candidates.append(
                bundle_root() / "vendor" / "platform-tools" / _exe("adb")
            )
        candidates.append(project_root() / "vendor" / "platform-tools" / _exe("adb"))
    except Exception:  # pragma: no cover - config is import-safe
        pass

    for var in _SDK_ENV_VARS:
        root = os.environ.get(var)
        if root:
            candidates.append(
                Path(root).joinpath(*_SDK_RELATIVE[:-1], _exe(_SDK_RELATIVE[-1]))
            )

    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "Android" / "Sdk" / "platform-tools" / _exe("adb"))

    home = Path.home()
    candidates += [
        home / "Android" / "Sdk" / "platform-tools" / _exe("adb"),
        home / "Library" / "Android" / "sdk" / "platform-tools" / "adb",
    ]
    candidates += [Path(p) for p in _EXTRA_CANDIDATES]

    for c in candidates:
        if c.is_file():
            return str(c)
    return None


@dataclass(frozen=True)
class Device:
    """A single adb device."""

    serial: str
    state: str
    model: str = ""
    product: str = ""
    transport_id: str = ""

    @property
    def is_ready(self) -> bool:
        return self.state == "device"

    @property
    def is_unauthorized(self) -> bool:
        return self.state == "unauthorized"

    @property
    def is_wireless(self) -> bool:
        """Whether this device is reachable over the network, not USB."""
        return looks_wireless(self.serial)

    @property
    def link(self) -> str:
        """Human name for the transport: ``"Wi-Fi"`` or ``"USB"``."""
        return "Wi-Fi" if self.is_wireless else "USB"

    @property
    def display_name(self) -> str:
        name = self.model or self.product or self.serial
        return f"{name} ({self.serial}) - {self.link}"


def device_grep(command: str, markers: tuple[str, ...], limit: int = 4) -> str:
    """``command`` narrowed down by the *device's* own grep.

    This is not tidiness. The dumps these probes read are enormous -- an
    unfiltered ``dumpsys window`` is ~90 KB of text and ``dumpsys input_method``
    ~1.3 MB -- and all of it crosses the same adb link the video stream uses.
    Measured while mirroring, the session's probes were pulling **~475 KB/s**
    of dumpsys output onto that link, which is what stuttered the picture.

    Filtering on the device leaves a few hundred bytes on the wire instead of
    hundreds of kilobytes. ``-m`` matters as much as the pattern: once grep has
    its matches it exits, ``dumpsys`` gets SIGPIPE and stops building a dump
    that is about to be thrown away.

    Markers must be single words -- the whole command goes to the device's
    shell as one argument, so anything needing quotes *there* would need its
    own escaping, and silently losing a quote turns a filter into a second
    pipeline. Every caller therefore tries the filtered command first and falls
    back to the plain dump, so a filter that misses a field costs latency
    rather than correctness.
    """
    return command + f" | grep -E -m{limit} " + " ".join(f"-e {m}" for m in markers)


class Adb:
    """A bound adb client for one executable and (optionally) one device."""

    def __init__(self, adb_path: str | None = None, serial: str | None = None) -> None:
        self.adb_path = adb_path or find_adb()
        if not self.adb_path:
            raise AdbError(
                "Could not find 'adb'. Install Android platform-tools and either "
                "add it to PATH or set ANDROID_HOME."
            )
        self.serial = serial

    # -- low level -------------------------------------------------------- #

    def _base(self) -> list[str]:
        cmd = [self.adb_path]
        if self.serial:
            cmd += ["-s", self.serial]
        return cmd

    def run(
        self,
        args: Sequence[str],
        *,
        use_serial: bool = True,
        timeout: float | None = 30.0,
        check: bool = True,
        binary: bool = False,
    ) -> subprocess.CompletedProcess:
        # Host-level commands (connect/disconnect) are not device-targeted and
        # adb rejects a -s selector on them, hence ``use_serial``.
        base = self._base() if use_serial else [self.adb_path]
        cmd = base + list(args)
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                timeout=timeout,
                # Explicit UTF-8 with replacement: adbd can emit junk bytes
                # while it restarts (tcpip), and the locale codec raises on
                # some of them -- a crash, not an error message.
                # `binary` opts out entirely for `exec-out` reads (APK bytes),
                # where decoding would corrupt the payload.
                **({} if binary else {"encoding": "utf-8", "errors": "replace"}),
                **_SUBPROCESS_FLAGS,
            )
        except FileNotFoundError as exc:
            raise AdbError(f"adb not found: {self.adb_path}") from exc
        except subprocess.TimeoutExpired as exc:
            raise AdbError(f"adb timed out: {' '.join(cmd)}") from exc

        if check and proc.returncode != 0:
            err = (proc.stderr or "").strip()
            raise AdbError(f"adb failed ({proc.returncode}): {err or ' '.join(cmd)}")
        return proc

    def popen(self, args: Sequence[str], **kwargs) -> subprocess.Popen:
        """Start a long-running adb command (used for the device server)."""
        cmd = self._base() + list(args)
        # Caller-supplied creationflags win, so a caller that genuinely wants a
        # console (or no window) can still say so.
        kwargs.setdefault("creationflags", _NO_WINDOW)
        return subprocess.Popen(
            cmd,
            stdout=kwargs.pop("stdout", subprocess.PIPE),
            stderr=kwargs.pop("stderr", subprocess.STDOUT),
            text=True,
            bufsize=1,
            **kwargs,
        )

    # -- connection ------------------------------------------------------- #

    def start_server(self) -> None:
        """Make sure the adb daemon is running."""
        self.run(["start-server"], use_serial=False, check=False)

    def version(self) -> str:
        out = self.run(["version"], use_serial=False).stdout or ""
        return out.splitlines()[0].strip() if out else "unknown"

    def devices(self, timeout: float = 30.0) -> list[Device]:
        # Host-global: lists *all* transports regardless of the selector, and
        # must keep working after ``tcpip`` drops the USB transport.
        #
        # ``timeout`` is tunable because the GUI calls this synchronously from
        # six places (toolbar refresh, filter toggles, serial selection). The
        # default 30s is right for a CLI; on the GUI thread an unreachable adb
        # would freeze the window for half a minute, so the window passes a
        # much shorter one and simply reports "no device".
        out = self.run(["devices", "-l"], use_serial=False, timeout=timeout).stdout or ""
        devices: list[Device] = []
        for line in out.splitlines()[1:]:
            line = line.strip()
            if not line or line.startswith("*"):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            attrs = {}
            for token in parts[2:]:
                if ":" in token:
                    k, v = token.split(":", 1)
                    attrs[k] = v
            devices.append(
                Device(
                    serial=parts[0],
                    state=parts[1],
                    model=attrs.get("model", ""),
                    product=attrs.get("product", ""),
                    transport_id=attrs.get("transport_id", ""),
                )
            )
        return devices

    def pick_device(self) -> Device:
        """Return the sole ready device, or raise with a helpful message."""
        ready = [d for d in self.devices() if d.is_ready]
        if not ready:
            unauth = [d for d in self.devices() if d.is_unauthorized]
            if unauth:
                raise AdbError(
                    "Device is unauthorized. Accept the 'Allow USB debugging' "
                    "prompt on the phone."
                )
            raise AdbError(
                "No Android device connected. Attach one over USB (or enable "
                "Wi-Fi with --tcpip) and enable USB debugging."
            )
        if len(ready) > 1 and not self.serial:
            names = ", ".join(d.serial for d in ready)
            raise AdbError(
                f"Multiple devices connected ({names}). Pick one with --serial."
            )
        if self.serial:
            matched = next((d for d in ready if d.serial == self.serial), None)
            if matched is None:
                raise AdbError(
                    f"Device '{self.serial}' is not connected or not ready."
                )
            device = matched
        else:
            device = ready[0]
        self.serial = device.serial
        return device

    # -- wireless --------------------------------------------------------- #

    def tcpip(self, port: int = DEFAULT_ADB_TCP_PORT) -> str:
        """Make the attached device listen for adb over TCP on ``port``.

        This is the one step that must happen while USB is still plugged in;
        afterwards the device is reachable by address alone.
        """
        out = self.run(["tcpip", str(port)], timeout=60.0).stdout or ""
        return out.strip()

    def connect(self, address: str, *, timeout: float = 30.0) -> str:
        """``adb connect host:port``.

        adb reports failures on stdout with a zero exit status, so the output
        is what callers should inspect (see :meth:`is_connected`).
        """
        out = self.run(
            ["connect", address], use_serial=False, check=False, timeout=timeout
        ).stdout or ""
        return out.strip()

    def pair(self, address: str, code: str, *, timeout: float = 30.0) -> str:
        """``adb pair host:port code`` for Android 11+ Wireless debugging.

        The pairing port/code come from *Wireless debugging > Pair device with
        pairing code* and differ from the connect port on the main screen.
        Raises :class:`AdbError` unless adb reports success.
        """
        proc = self.run(
            ["pair", address, code], use_serial=False, check=False, timeout=timeout
        )
        out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        if "successfully paired" not in out.lower():
            raise AdbError(
                f"Pairing with {address} failed. adb said: {out or 'no output'}\n\n"
                "Make sure the 'Pair device with pairing code' screen is still "
                "open on the tablet, and use the IP:port and 6-digit code shown "
                "there (not the port on the main Wireless debugging screen)."
            )
        return out

    def mdns_services(self) -> list[tuple[str, str, str]]:
        """Devices adbd is advertising on this network (``adb mdns services``)."""
        try:
            out = self.run(
                ["mdns", "services"], use_serial=False, check=False, timeout=10.0
            ).stdout or ""
        except AdbError:
            return []
        return parse_mdns_services(out)

    def find_pairing_address(self) -> str | None:
        """``host:port`` of an open "Pair device with pairing code" popup."""
        for _name, service, address in self.mdns_services():
            if service == MDNS_PAIRING:
                return address
        return None

    def discover_wireless(self) -> tuple[str | None, str | None]:
        """Find the device on the same network and connect to it.

        Returns ``(connected_serial, seen_address)``: the serial that is now
        ready, or None -- plus the first advertised connect address (so a
        caller can pre-fill it when the device still needs pairing).
        """
        seen: str | None = None
        for _name, service, address in self.mdns_services():
            if service not in (MDNS_CONNECT, MDNS_LEGACY):
                continue
            seen = seen or address
            if self.is_connected(address):
                return address, address
            self.connect(address, timeout=10.0)
            if self.is_connected(address):
                return address, address
        return None, seen

    def disconnect(self, address: str | None = None, *, timeout: float = 30.0) -> str:
        """``adb disconnect [host:port]``; with no address, all of them."""
        args = ["disconnect"] + ([address] if address else [])
        out = self.run(
            args, use_serial=False, check=False, timeout=timeout
        ).stdout or ""
        return out.strip()

    def is_connected(self, address: str) -> bool:
        """Whether ``address`` is present and ready in ``adb devices``."""
        try:
            return any(d.serial == address and d.is_ready for d in self.devices())
        except AdbError:
            return False

    def device_ip(self) -> str | None:
        """Best-effort Wi-Fi IPv4 of the selected device, or None.

        Three probes, most reliable first: the routing table (the address the
        default route leaves from), the wlan0 interface, then the DHCP
        property still set by some older devices.
        """
        for command in ("ip route", "ip -f inet addr show wlan0"):
            out = self.shell(command, check=False).stdout or ""
            ip = parse_device_ip(out)
            if ip:
                return ip
        prop = self.getprop("dhcp.wlan0.ipaddress")
        if prop and _usable_ip(prop):
            return prop
        return None

    def enable_wireless(
        self,
        port: int = DEFAULT_ADB_TCP_PORT,
        *,
        attempts: int = 6,
        delay: float = 0.5,
    ) -> str:
        """Switch the attached USB device to Wi-Fi and connect to it.

        Returns the new ``host:port`` serial. adbd restarts after ``tcpip``,
        so the connect is retried a few times before giving up.
        """
        self.pick_device()
        ip = self.device_ip()
        if not ip:
            raise AdbError(
                "Could not find the device's Wi-Fi address. Make sure it is "
                "connected to the same Wi-Fi network as this computer, then "
                "try again -- or connect by IP address instead."
            )
        address = f"{ip}:{port}"
        self.tcpip(port)
        last = ""
        for _ in range(max(1, attempts)):
            if delay:
                time.sleep(delay)  # let adbd finish restarting
            # Short timeout on purpose: this runs on the GUI thread, and an
            # unreachable host would otherwise freeze the window for minutes.
            last = self.connect(address, timeout=10.0)
            if self.is_connected(address):
                self.serial = address
                return address
        raise AdbError(
            f"Could not connect to {address} over Wi-Fi. adb said: {last or 'no output'}"
        )

    # -- device operations ------------------------------------------------ #

    def shell(self, command: str, *, timeout: float | None = 30.0, check: bool = True):
        return self.run(["shell", command], timeout=timeout, check=check)

    def getprop(self, prop: str) -> str:
        out = self.shell(f"getprop {prop}", check=False).stdout or ""
        return out.strip()

    def push(self, local: str | Path, remote: str) -> None:
        self.run(["push", str(local), remote], timeout=180.0)

    def ensure_dir(self, remote: str) -> None:
        self.shell(f"mkdir -p {remote}")

    def forward(self, local: str, remote: str) -> None:
        self.run(["forward", local, remote])

    def forward_remove(self, local: str) -> None:
        self.run(["forward", "--remove", local], check=False)

    def kill_server_process(self) -> None:
        """Best-effort cleanup of a server left running on the device."""
        self.shell("pkill -f com.genymobile.scrcpy.Server", check=False)

    def remove(self, remote: str) -> None:
        self.shell(f"rm -f {remote}", check=False)
