"""Shared configuration and on-disk paths."""

from __future__ import annotations

import os
import sys
from pathlib import Path

# --------------------------------------------------------------------------- #
# scrcpy device server
# --------------------------------------------------------------------------- #

#: scrcpy versions the client and server must match *exactly* -- the server
#: refuses to run otherwise. The wire format implemented in protocol.py is
#: verified against this tag.
SCRCPY_VERSION = "4.1"

SERVER_JAR_NAME = f"scrcpy-server-v{SCRCPY_VERSION}"
SERVER_JAR_URL = (
    f"https://github.com/Genymobile/scrcpy/releases/download/"
    f"v{SCRCPY_VERSION}/{SERVER_JAR_NAME}"
)

#: Where the server jar is pushed on the device.
DEVICE_TMP_DIR = "/data/local/tmp/droidmirror"
DEVICE_JAR_PATH = f"{DEVICE_TMP_DIR}/{SERVER_JAR_NAME}.jar"

#: Local port used to carry the forwarded sockets over USB.
DEFAULT_TCP_PORT = 27183

#: Port adbd listens on after `adb tcpip`. This is the standard adb-over-Wi-Fi
#: port; it is unrelated to DEFAULT_TCP_PORT, which is our own local forward.
DEFAULT_ADB_TCP_PORT = 5555


def bundle_root() -> Path:
    """Read-only files shipped *inside* a frozen build.

    PyInstaller unpacks bundled data into a temporary directory and points
    ``sys._MEIPASS`` at it; that directory is deleted when the process exits, so
    it can only ever hold things that are read and never written. Everything a
    normal checkout finds at the repository root is found here instead when
    running frozen.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parents[2]


def is_frozen() -> bool:
    """True when running from a PyInstaller/Nuitka one-file executable."""
    return bool(getattr(sys, "frozen", False))


def project_root() -> Path:
    """Writable state directory: repository root, or the executable's folder.

    Everything the app *writes* lives here -- profiles, settings.json, the
    cached server jar, downloaded icons and crash.log. A checkout keeps them
    beside the sources; a frozen build keeps them beside the .exe so they
    survive the per-run extraction directory being wiped.
    """
    env = os.environ.get("DROIDMIRROR_HOME")
    if env:
        return Path(env).expanduser()
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def vendor_dir() -> Path:
    """Where the downloaded scrcpy server jar is cached."""
    d = project_root() / "vendor"
    d.mkdir(parents=True, exist_ok=True)
    return d


def server_jar_cache_path() -> Path:
    return vendor_dir() / SERVER_JAR_NAME


def profiles_dir() -> Path:
    d = project_root() / "profiles"
    d.mkdir(parents=True, exist_ok=True)
    return d


def settings_path() -> Path:
    """JSON file holding UI preferences (not keybinds -- those are profiles)."""
    return project_root() / "settings.json"


# --------------------------------------------------------------------------- #
# Streaming defaults tuned for latency
# --------------------------------------------------------------------------- #

#: 0 means "native resolution" -- scrcpy only downscales when > 0.
DEFAULT_MAX_SIZE = 0
#: Generous ceiling; USB has plenty of headroom and this keeps motion crisp.
DEFAULT_BIT_RATE = 16_000_000
#: Cap the encoder. Lower = less latency; 0 lets the device pick.
DEFAULT_MAX_FPS = 0

#: Wi-Fi defaults. The USB ones above (native res @ 16 Mbps) overrun a
#: wireless adb tunnel, so frames queue up and the mirror stutters. A smaller
#: frame at half the bit rate keeps the frame rate up over Wi-Fi.
WIRELESS_MAX_SIZE = 1280
WIRELESS_BIT_RATE = 8_000_000
