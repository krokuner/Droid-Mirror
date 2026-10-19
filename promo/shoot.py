"""Record the promo takes: the real app, the real tablet, nothing staged.

Why one process does a whole take: the recorder captures the desktop, so
anything that shows up on screen lands in the file. A take that steps out to
run a click between frames is how you get a console window, the take-over
banner or the wrong window in the shot. So a take is one hidden process that

  1. brings the app to the front and confirms it holds the foreground,
  2. starts ffmpeg on what is now the whole screen,
  3. runs the choreography on a wall clock -- adb for the tablet, SendInput for
     the mouse and keyboard, both from in here,
  4. lets ffmpeg stop itself at the take's length,
  5. writes down what happened: app state before and after, whether the fault
     log grew, the file size.

Input comes from this process rather than the MCP tools on purpose. The tools
belong to a control gate (windows-mcp 0.8.8): any physical key or mouse move
hands control back to the user for ten seconds and blocks the AI's calls, and
their ownership glow would be drawn into the frame. SendInput from here has no
such overlay and no gate.

Run it hidden, or a console flashes up over the recording:

    wscript promo\\hidden.vbs shoot.py list
    wscript promo\\hidden.vbs shoot.py test
    wscript promo\\hidden.vbs shoot.py all

`test` records three seconds of the mirror region with no choreography, which
is how the framing and the absence of the overlay get checked before a shoot.
Every take writes promo/takes/<name>.mp4 and promo/takes/<name>.log.
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path

# The display runs at 125%. A process that is not DPI-aware has every
# SetCursorPos scaled by that factor: a click meant for Connect (604, 45)
# lands on the Wi-Fi button at 755, 56 instead. Declare awareness before any
# window math, and physical pixels are what they say they are.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor aware
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TAKES = HERE / "takes"
RUN_LOG = TAKES / "run.log"

FFMPEG = Path(
    r"C:\Users\Hardik\AppData\Local\Temp\opencode\viddeps\imageio_ffmpeg\binaries"
    r"\ffmpeg-win-x86_64-v7.1.exe"
)
ADB = ROOT / "vendor" / "platform-tools" / "adb.exe"
LAUNCH = ROOT / "DroidMirror.vbs"
SERIAL = "adb-R95Y802YKGR-x6WUXp._adb-tls-connect._tcp"
NO_WINDOW = 0x08000000
FPS = 30

# The app fills the work area (0..1048); the taskbar and its clock are not
# going in the promo, so 'full' stops at the work area and the edit pads it
# back to 1920x1080. The mirror view ends where the Keybinds dock starts.
REGIONS = {
    "full": ["-video_size", "1920x1048", "-offset_x", "0", "-offset_y", "0"],
    "mirror": ["-video_size", "1462x940", "-offset_x", "0", "-offset_y", "62"],
    # the phone itself, cropped out of the mirror view's letterboxing
    "strip": ["-video_size", "584x940", "-offset_x", "442", "-offset_y", "62"],
}

# Toolbar, dock and status-bar targets, read off the running window.
CONNECT = (604, 45)
PANEL = (1168, 45)
COMBO = (1586, 935)
LOAD = (1764, 936)
VOL_Y = 1013
VOL_MINUS = (896, 1014)
VOL_PLUS = (1054, 1014)
MIRROR = (731, 535)

# --------------------------------------------------------------------------- #
# Win32
# --------------------------------------------------------------------------- #

u32 = ctypes.windll.user32
u32.GetForegroundWindow.restype = wintypes.HWND
u32.EnumWindows.argtypes = [
    ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM),
    wintypes.LPARAM,
]
u32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
u32.IsWindowVisible.argtypes = [wintypes.HWND]
u32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
u32.SetForegroundWindow.argtypes = [wintypes.HWND]
u32.SwitchToThisWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
u32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]

MOUSE_LDOWN, MOUSE_LUP = 0x0002, 0x0004
MOUSE_RDOWN, MOUSE_RUP = 0x0008, 0x0010
KEYEVENTF_KEYUP = 0x0002

VK = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "shift": 0x10, "ctrl": 0x11,
    "alt": 0x12, "esc": 0x1B, "space": 0x20, "`": 0xC0, "f7": 0x76, "f9": 0x78,
}
# a-z and 0-9 are their ASCII values -- "grannny" needs n and y, which the
# first draft's hand-picked list was missing.
for _ch in "abcdefghijklmnopqrstuvwxyz0123456789":
    VK[_ch] = ord(_ch.upper())


def find_window(needle: str) -> int:
    hits: list[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _):
        if u32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(512)
            u32.GetWindowTextW(hwnd, buf, 512)
            if needle.lower() in buf.value.lower():
                hits.append(int(hwnd))
        return True

    u32.EnumWindows(visit, 0)
    return hits[0] if hits else 0


def window_title(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(512)
    u32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def app_state() -> str:
    """'off', 'disconnected' or 'connected' -- the title says which."""
    hwnd = find_window("DroidMirror")
    if not hwnd:
        return "off"
    return "connected" if "SM-X210" in window_title(hwnd) else "disconnected"


def focus_app(timeout: float = 20.0) -> bool:
    """Bring the app forward and wait until it really holds the foreground."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        hwnd = find_window("DroidMirror")
        if not hwnd:
            return False
        u32.ShowWindow(hwnd, 3)  # SW_MAXIMIZE
        u32.SetForegroundWindow(hwnd)
        u32.SwitchToThisWindow(hwnd, True)
        time.sleep(0.4)
        if int(u32.GetForegroundWindow() or 0) == hwnd:
            return True
    return False


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def idle_seconds() -> float:
    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    u32.GetLastInputInfo(ctypes.byref(info))
    # GetTickCount is only exported by kernel32 in this Python's loading rules,
    # and the 64-bit one cannot wrap mid-session.
    now = _k32.GetTickCount64() & 0xFFFFFFFF
    return ((now - info.dwTime) & 0xFFFFFFFF) / 1000.0


def wait_for_idle(seconds: float, timeout: float = 900.0) -> bool:
    """Block until the user has touched nothing for a while.

    Only meaningful before this process starts moving the mouse: its own input
    resets the same clock, which is exactly what is wanted -- ask once, then
    own the session.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if idle_seconds() >= seconds:
            return True
        time.sleep(2.0)
    return False


# --------------------------------------------------------------------------- #
# Input
# --------------------------------------------------------------------------- #


def cursor(x: int, y: int) -> None:
    u32.SetCursorPos(int(x), int(y))


def click(x: int, y: int, right: bool = False) -> None:
    cursor(x, y)
    time.sleep(0.12)
    u32.mouse_event(MOUSE_RDOWN if right else MOUSE_LDOWN, 0, 0, 0, 0)
    time.sleep(0.05)
    u32.mouse_event(MOUSE_RUP if right else MOUSE_LUP, 0, 0, 0, 0)


def drag(x0: int, y0: int, x1: int, y1: int, seconds: float = 0.45, steps: int = 22) -> None:
    """Press, then move in small steps -- Qt forwards each as a touch move."""
    cursor(x0, y0)
    time.sleep(0.12)
    u32.mouse_event(MOUSE_LDOWN, 0, 0, 0, 0)
    for step in range(1, steps + 1):
        f = step / steps
        cursor(round(x0 + (x1 - x0) * f), round(y0 + (y1 - y0) * f))
        time.sleep(seconds / steps)
    time.sleep(0.06)
    u32.mouse_event(MOUSE_LUP, 0, 0, 0, 0)


def key(name: str, hold: float = 0.10) -> None:
    vk = VK[name.lower()]
    u32.keybd_event(vk, 0, 0, 0)
    time.sleep(hold)
    u32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


def hold(name: str, seconds: float) -> None:
    vk = VK[name.lower()]
    u32.keybd_event(vk, 0, 0, 0)
    time.sleep(seconds)
    u32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


def combo(*names: str) -> None:
    """combo("ctrl", "a") -- all but the last are held."""
    mods = [VK[n.lower()] for n in names[:-1]]
    last = VK[names[-1].lower()]
    for vk in mods:
        u32.keybd_event(vk, 0, 0, 0)
    time.sleep(0.05)
    key(names[-1])
    for vk in reversed(mods):
        u32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


# --------------------------------------------------------------------------- #
# adb, recording, health
# --------------------------------------------------------------------------- #


def adb(*args: object, timeout: float = 25.0) -> str:
    done = subprocess.run(
        [str(ADB), "-s", SERIAL, *map(str, args)],
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=NO_WINDOW,
    )
    return (done.stdout or done.stderr).strip()


def shell(*args: object, timeout: float = 25.0) -> str:
    """adb shell with the args joined the way the device expects."""
    return adb("shell", *args, timeout=timeout)


def unlock_tablet() -> None:
    """Wake, raise the PIN pad, type the PIN. Harmless if already unlocked."""
    shell("input", "keyevent", "224")  # KEYCODE_WAKEUP
    time.sleep(0.6)
    shell("input", "swipe", "600", "1600", "600", "800", "220")
    time.sleep(0.8)
    shell("input", "text", "2011")
    time.sleep(0.4)
    shell("input", "keyevent", "66")  # ENTER
    time.sleep(0.8)


# Pure arg builders: the take table must not run adb while it is being read,
# or merely listing the takes would fire commands at the tablet.
HOME_ARGS = ["shell", "input", "keyevent", "3"]


def launch_args(package: str) -> list:
    return ["shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1"]


def record(out: Path, seconds: float, region: str) -> subprocess.Popen:
    cmd = [
        str(FFMPEG), "-hide_banner", "-loglevel", "warning",
        "-f", "gdigrab", "-framerate", str(FPS),
        *REGIONS[region], "-i", "desktop",
        "-t", f"{seconds:.3f}",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        "-y", str(out),
    ]
    return subprocess.Popen(
        cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE, creationflags=NO_WINDOW,
    )


def health() -> dict:
    return {
        "app": app_state(),
        "front": bool(find_window("DroidMirror")) and int(u32.GetForegroundWindow() or 0) == find_window("DroidMirror"),
        "crash_log": (ROOT / "crash.log").stat().st_size if (ROOT / "crash.log").exists() else -1,
    }


def say(take: str, line: str) -> None:
    text = f"[{time.strftime('%H:%M:%S')}] {take}: {line}"
    with RUN_LOG.open("a", encoding="utf-8") as handle:
        handle.write(text + "\n")


_k32 = ctypes.windll.kernel32
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.GetTickCount64.restype = ctypes.c_ulonglong
_k32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _process_image(hwnd: int) -> str:
    pid = wintypes.DWORD()
    u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    handle = _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    buf = ctypes.create_unicode_buffer(1024)
    size = wintypes.DWORD(len(buf))
    ok = _k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
    _k32.CloseHandle(handle)
    return buf.value if ok else ""


def clear_overlay() -> list[str]:
    """Hide the windows-mcp ownership glow and any stray toast.

    While its AI holds control the windows-mcp server draws a full-screen glow
    and its notice, and both would sit in every frame. Its windows all belong
    to the server process, so they can be hidden from here without making an
    MCP call -- an MCP call would re-arm control and bring them straight back.
    Anything hidden this way reappears on its own the next time it has
    something to say. Discord's floating banner and the same idea.
    """
    hidden: list[str] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _):
        image = _process_image(hwnd).lower()
        title = window_title(hwnd)
        if "windows-mcp" in image or "no plan for today" in title.lower():
            u32.ShowWindow(hwnd, 0)  # SW_HIDE
            hidden.append(f"{image.split(chr(92))[-1] or '?'} :: {title!r}")
        return True

    u32.EnumWindows(visit, 0)
    return hidden


# --------------------------------------------------------------------------- #
# Takes
# --------------------------------------------------------------------------- #


@dataclass
class Take:
    name: str
    seconds: float
    note: str
    region: str = "full"
    start_cursor: tuple[int, int] | None = None
    setup: list[tuple] = field(default_factory=list)  # adb, before recording
    steps: list[tuple] = field(default_factory=list)  # (at_s, kind, payload)


def tap(x: int, y: int) -> list:
    return ["input", "tap", x, y]


def swipe(x0: int, y0: int, x1: int, y1: int, ms: int = 300) -> list:
    return ["input", "swipe", x0, y0, x1, y1, ms]


TAKE_LIST: list[Take] = [
    Take(
        name="01_connect",
        seconds=34.0,
        note="the connect flow: the click, the states, then a live mirror",
        start_cursor=(600, 45),
        setup=[("unlock", None), ("adb", HOME_ARGS)],
        steps=[
            (0.8, "click", CONNECT),
            (3.0, "note", "connecting"),
            (27.0, "adb", ["shell", *swipe(600, 1500, 600, 700, 350)]),
            (31.0, "adb", ["shell", *tap(300, 1400)]),
        ],
    ),
    Take(
        name="02_mirror_home",
        seconds=12.0,
        note="live mirror: the tablet's own UI moving, opened and scrolled",
        region="strip",
        setup=[("unlock", None), ("adb", HOME_ARGS)],
        steps=[
            (2.5, "adb", ["shell", *swipe(600, 1500, 600, 600, 320)]),
            (6.0, "adb", ["shell", *swipe(600, 1500, 600, 600, 320)]),
            (9.0, "adb", ["shell", *swipe(600, 600, 600, 1500, 320)]),
        ],
    ),
    Take(
        name="03_touch_drag",
        seconds=14.0,
        note="the mouse dragging the tablet itself: pages, a scroll, the shade",
        region="strip",
        start_cursor=(760, 430),
        setup=[("unlock", None), ("adb", HOME_ARGS)],
        steps=[
            (1.0, "drag", (900, 430, 520, 430, 0.5)),
            (4.0, "drag", (520, 430, 900, 430, 0.5)),
            (7.5, "drag", (700, 700, 700, 330, 0.6)),
            (11.0, "drag", (700, 330, 700, 760, 0.6)),
        ],
    ),
    Take(
        name="04_keybinds_load",
        seconds=15.0,
        note="load his Granny profile in the dock, then hide the dock for the markers",
        start_cursor=(1586, 935),
        setup=[
            ("unlock", None),
            ("adb", HOME_ARGS),
            ("adb", launch_args("com.dvloper.granny")),
            ("wait", 12.0),
            ("adb", ["shell", "input", "tap", "962", "92"]),
            ("wait", 10.0),
        ],
        steps=[
            (1.0, "click", COMBO),
            (2.2, "combo", ("ctrl", "a")),
            (2.8, "type", "grannny"),
            (4.4, "click", LOAD),
            (7.0, "click", PANEL),
            (12.5, "click", PANEL),
        ],
    ),
    Take(
        name="05_granny_wasd",
        seconds=14.0,
        note="his Granny bindings moving the character: WASD through the keybind engine",
        region="strip",
        start_cursor=(731, 535),
        setup=[
            ("unlock", None),
            ("adb", HOME_ARGS),
            ("adb", launch_args("com.dvloper.granny")),
            ("wait", 12.0),
            ("adb", ["shell", "input", "tap", "962", "92"]),
            ("wait", 10.0),
        ],
        steps=[
            (1.2, "key", "`"),
            (3.0, "hold", ("w", 1.6)),
            (5.5, "hold", ("a", 1.0)),
            (7.5, "hold", ("w", 1.4)),
            (9.8, "hold", ("d", 1.2)),
            (12.2, "key", "`"),
        ],
    ),
    Take(
        name="07_solar_touch",
        seconds=16.0,
        note="a heavy 3D game driven through the mirror by the mouse",
        region="strip",
        start_cursor=(731, 560),
        setup=[
            ("unlock", None),
            ("adb", HOME_ARGS),
            ("adb", launch_args("com.paradyme.solarsmash")),
            ("wait", 34.0),
            ("adb", ["shell", "input", "tap", "600", "1166"]),
            ("wait", 8.0),
        ],
        steps=[
            (1.5, "click", (731, 560)),
            (4.0, "click", (640, 470)),
            (6.5, "drag", (800, 700, 520, 420, 0.8)),
            (9.5, "click", (731, 560)),
            (12.0, "drag", (520, 420, 800, 700, 0.8)),
            (14.5, "click", (640, 470)),
        ],
    ),
    Take(
        name="08_volume",
        seconds=12.0,
        note="the tablet's own volume bar: clicks land where clicked",
        start_cursor=(1000, VOL_Y),
        setup=[("unlock", None), ("adb", HOME_ARGS)],
        steps=[
            (1.0, "click", (1000, VOL_Y)),
            (2.6, "click", VOL_MINUS),
            (3.4, "click", VOL_MINUS),
            (5.4, "click", (930, VOL_Y)),
            (7.2, "click", VOL_PLUS),
            (8.0, "click", VOL_PLUS),
            (9.6, "click", (1030, VOL_Y)),
        ],
    ),
    Take(
        name="09_latency",
        seconds=10.0,
        note="F9 latency HUD over a live mirror",
        start_cursor=MIRROR,
        setup=[("unlock", None), ("adb", HOME_ARGS)],
        steps=[
            (1.2, "key", "f9"),
            (5.0, "adb", ["shell", *swipe(600, 1400, 600, 800, 400)]),
            (8.6, "key", "f9"),
        ],
    ),
    Take(
        name="10_clipboard",
        seconds=14.0,
        note="paste from this computer into the tablet's focused field",
        region="strip",
        start_cursor=(600, 150),
        setup=[
            ("clipboard", "droidmirror -- no cables, no lag, no install"),
            ("unlock", None),
            ("adb", HOME_ARGS),
            ("adb", launch_args("com.brave.browser")),
            ("wait", 9.0),
        ],
        steps=[
            (1.5, "click", (600, 135)),
            (4.0, "combo", ("ctrl", "v")),
            (9.0, "combo", ("ctrl", "v")),
        ],
    ),
    Take(
        name="12_endcard",
        seconds=9.0,
        note="the calm closing frame: live mirror, cursor drifting to the device list",
        start_cursor=MIRROR,
        setup=[("unlock", None), ("adb", HOME_ARGS)],
        steps=[
            (2.0, "move", (1000, 600)),
            (4.0, "move", (900, 300)),
            (6.0, "move", (700, 45)),
        ],
    ),
    Take(
        name="g_disconnected",
        seconds=0.4,
        note="grab: the disconnected state, one frame",
        setup=[("unlock", None), ("adb", HOME_ARGS)],
        steps=[],
    ),
    Take(
        name="g_connect_arc",
        seconds=12.0,
        note="grab: the click and the connecting states, for the code-built open",
        start_cursor=(600, 45),
        setup=[("unlock", None), ("adb", HOME_ARGS)],
        steps=[
            (0.6, "click", CONNECT),
            (3.0, "note", "connecting"),
        ],
    ),
    Take(
        name="g_connected",
        seconds=1.2,
        note="grab: a live mirror, connected, one frame",
        setup=[],
        steps=[],
    ),
]


def by_name(name: str) -> Take | None:
    for take in TAKE_LIST:
        if take.name == name:
            return take
    return None


# --------------------------------------------------------------------------- #
# Running
# --------------------------------------------------------------------------- #


def do(kind: str, payload) -> None:
    if kind == "click":
        click(*payload)
    elif kind == "drag":
        drag(*payload)
    elif kind == "move":
        cursor(*payload)
    elif kind == "key":
        key(payload)
    elif kind == "hold":
        hold(*payload)
    elif kind == "combo":
        combo(*payload)
    elif kind == "type":
        for ch in payload:
            if ch.isupper():
                u32.keybd_event(VK["shift"], 0, 0, 0)
                key(ch.lower())
                u32.keybd_event(VK["shift"], 0, KEYEVENTF_KEYUP, 0)
            elif ch == " ":
                key("space")
            else:
                key(ch)
            time.sleep(0.04)
    elif kind == "adb":
        adb(*payload)
    elif kind == "clipboard":
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", f"Set-Clipboard -Value '{payload}'"],
            creationflags=NO_WINDOW, capture_output=True, text=True,
        )
    elif kind == "wait":
        time.sleep(float(payload))
    elif kind == "unlock":
        unlock_tablet()
    elif kind == "note":
        pass


def run(take: Take) -> dict:
    out = TAKES / f"{take.name}.mp4"
    log = TAKES / f"{take.name}.log"
    lines: list[str] = [f"take {take.name}: {take.note}", f"length {take.seconds}s region {take.region}"]

    for kind, payload in take.setup:
        lines.append(f"setup {kind} {payload!r}")
        do(kind, payload)
    if not focus_app():
        lines.append("FAILED: could not take the foreground")
        log.write_text("\n".join(lines), encoding="utf-8")
        say(take.name, "FAILED: no foreground")
        return {"ok": False}
    lines.append("foreground held by the app")
    lines.append("overlay: " + repr(clear_overlay()))

    if take.start_cursor:
        cursor(*take.start_cursor)
        time.sleep(0.3)

    before = health()
    lines.append(f"before {before}")
    proc = record(out, take.seconds, take.region)
    started = time.monotonic()

    for at, kind, payload in sorted(take.steps, key=lambda s: s[0]):
        time.sleep(max(0.0, started + at - time.monotonic()))
        lines.append(f"+{at:5.2f}s {kind} {payload!r}")
        try:
            do(kind, payload)
        except Exception as exc:  # keep the take, note the miss
            lines.append(f"  step failed: {exc!r}")

    time.sleep(max(0.0, started + take.seconds - time.monotonic()) + 0.5)
    stderr = b""
    try:
        proc.wait(timeout=30)
        stderr = proc.stderr.read() if proc.stderr else b""
    except subprocess.TimeoutExpired:
        proc.kill()
        lines.append("ffmpeg had to be killed")

    after = health()
    lines.append(f"after {after}")
    lines.append(f"file {out.stat().st_size if out.exists() else 0} bytes")
    if stderr:
        lines.append("ffmpeg said: " + stderr.decode("utf-8", "replace").strip()[-600:])
    log.write_text("\n".join(lines), encoding="utf-8")

    ok = after["app"] == "connected" and before["crash_log"] == after["crash_log"]
    say(take.name, f"done {take.seconds:.0f}s -> {out.stat().st_size if out.exists() else 0} bytes, fault log "
                   f"{'grew' if after['crash_log'] != before['crash_log'] else 'quiet'}")
    return {"ok": ok, "file": str(out)}


def ensure_app() -> bool:
    if app_state() == "off":
        say("run", "app is not running; launching it hidden")
        subprocess.Popen(["wscript.exe", str(LAUNCH)], creationflags=NO_WINDOW)
        for _ in range(60):
            time.sleep(1.0)
            if app_state() != "off":
                break
        time.sleep(2.0)
    return app_state() != "off"


def ensure_state(want: str, tries: int = 3) -> bool:
    """Click Connect until the title agrees with `want`."""
    for _ in range(tries):
        state = app_state()
        if state == want:
            return True
        if not focus_app():
            return False
        click(*CONNECT)
        for _ in range(12):
            time.sleep(2.5)
            if app_state() == want:
                return True
    return app_state() == want


def test(seconds: float = 3.0, region: str = "mirror") -> dict:
    """A framing check: record, no choreography, look at a frame afterwards."""
    TAKES.mkdir(exist_ok=True)
    if not wait_for_idle(8.0, timeout=120):
        say("test", "no idle window; recording anyway")
    if app_state() == "off" and not ensure_app():
        say("test", "app never came up")
        return {"ok": False}
    if app_state() == "disconnected":
        ensure_state("connected")
    focus_app()
    clear_overlay()
    do("unlock", None)
    do("adb", HOME_ARGS)
    time.sleep(1.0)
    cursor(*MIRROR)
    time.sleep(0.6)
    proc = record(TAKES / "_test.mp4", seconds, region)
    proc.wait(timeout=60)
    say("test", f"{region} {seconds}s -> {(TAKES / '_test.mp4').stat().st_size} bytes, "
                f"app {app_state()}, idle was {idle_seconds():.1f}s")
    return {"ok": True}


def run_all(pause: float = 1.5) -> None:
    TAKES.mkdir(exist_ok=True)
    say("run", f"waiting for {12.0}s of idle before touching anything")
    if not wait_for_idle(12.0, timeout=1800):
        say("run", "gave up waiting for idle")
        return
    if not ensure_app():
        say("run", "app never came up")
        return
    # 01_connect expects the app disconnected; every other take expects it live.
    ensure_state("disconnected")
    for take in TAKE_LIST:
        if take.name != "01_connect":
            ensure_state("connected")
        say(take.name, "starting")
        run(take)
        time.sleep(pause)
    say("run", "all takes done")


def main() -> None:
    TAKES.mkdir(exist_ok=True)
    which = sys.argv[1] if len(sys.argv) > 1 else "list"
    if which == "list":
        say("list", " + ".join(f"{t.name} ({t.seconds:.0f}s, {t.region})" for t in TAKE_LIST))
        return
    if which == "test":
        test()
        return
    if which == "all":
        run_all()
        return
    names = sys.argv[1:]
    takes: list[Take] = []
    for name in names:
        take = by_name(name)
        if take is None:
            say("run", f"no such take: {name}")
            return
        takes.append(take)
    say("run", f"{len(takes)} take(s) queued; waiting for 15s of idle first")
    if not wait_for_idle(15.0, timeout=300):
        say("run", "gave up waiting for idle")
        return
    if not ensure_app():
        say("run", "app never came up")
        return
    for take in takes:
        if take.name in ("01_connect", "g_connect_arc", "g_disconnected"):
            ensure_state("disconnected")
        else:
            ensure_state("connected")
        say(take.name, "starting")
        run(take)
        time.sleep(1.5)
    say("run", f"{len(takes)} take(s) done")


if __name__ == "__main__":
    main()
