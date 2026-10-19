"""Lockscreen (keyguard) detection over adb.

Android's own services track whether the keyguard is up and print it in
their ``dumpsys`` dumps -- the exact service and field name drift across
Android versions and OEM skins, so the detector asks a couple of sources
and reads whichever one actually answers.

``parse_keyguard`` is a pure function over dump text so it can be unit
tested against captured samples without a device attached; ``detect_locked``
runs the commands through the normal :class:`~droidmirror.adb.Adb`.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .adb import device_grep

if TYPE_CHECKING:
    from .adb import Adb

#: (source label, pattern): first signal present in a dump wins.
#: Order matters: a dump can mention several of these fields.
_SIGNALS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # AOSP KeyguardController (dumpsys activity activities)
    ("activity", re.compile(r"\bmKeyguardShowing\s*=\s*(true|false)", re.I)),
    # Legacy DisplayPolicy / window dumps
    (
        "window",
        re.compile(r"\bm(?:ShowingLockscreen|KeyguardShowing)\s*[=:]\s*(true|false)", re.I),
    ),
    # TrustManagerService
    ("trust", re.compile(r"\bDevice locked\s*:\s*(true|false)", re.I)),
)

#: (command, the words that command prints the keyguard state in).
#:
#: The words serve two masters on purpose: they are the device-side filter's
#: markers *and* the fields :func:`parse_keyguard` reads, which is why they are
#: one list instead of two that can drift apart. A test cross-checks each
#: marker against the :data:`_SIGNALS` pattern for the same source.
#:
#: Order is measured, not guessed. On Android 16 ``dumpsys window`` no longer
#: prints a keyguard field at all, so leading with it -- as this used to --
#: spent ~90 KB and ~370 ms per poll to learn nothing and then paid for the
#: ``activity`` dump anyway. The filtered ``activity`` dump answers, and the
#: filtered ``trust`` dump is by far the smallest (~1.5 KB) for the devices
#: where it answers.
_LOCK_SOURCES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("dumpsys activity activities", ("mKeyguardShowing",)),
    ("dumpsys trust", ("Device", "locked")),
    ("dumpsys window", ("ShowingLockscreen", "KeyguardShowing")),
)

#: Filtered forms first, then the plain dumps as fallbacks. Every source
#: appears twice so a build whose fields differ -- or a filter that misses one
#: -- degrades to slow rather than to blind. Repeating it here rather than
#: hand-listing both halves means a new source cannot be added without its
#: fallback.
_COMMANDS = tuple(
    device_grep(command, markers) for command, markers in _LOCK_SOURCES
) + tuple(command for command, _markers in _LOCK_SOURCES)

#: CredentialType values seen in `dumpsys lock_settings`, mapped to the tab
#: the unlock dialog opens.
_CREDENTIAL_TYPES = {
    "PIN": "pin",
    "PATTERN": "pattern",
    "PASSWORD": "password",
    "NONE": "swipe",
}

_CREDENTIAL_RE = re.compile(r"CredentialType:\s*(\w+)")
_WM_SIZE_RE = re.compile(r"Physical size:\s*(\d+)x(\d+)")

#: (display left, top, right, bottom) in device pixels.
Rect = tuple[int, int, int, int]


def parse_keyguard(dump: str) -> bool | None:
    """True = lockscreen showing, False = unlocked, None = not stated."""
    for _source, pattern in _SIGNALS:
        match = pattern.search(dump)
        if match:
            return match.group(1).lower() == "true"
    return None


def detect_locked(adb: "Adb") -> bool | None:
    """Ask the device whether the lockscreen is up.

    Walks :data:`_COMMANDS` -- filtered forms first, plain dumps after -- and
    returns the first answer. Returns ``None`` when no source could answer
    (device briefly offline, exotic dump format) -- callers should treat that
    as "unknown", never as "unlocked".
    """
    for command in _COMMANDS:
        try:
            result = adb.shell(command, check=False, timeout=8.0)
        except Exception:
            continue  # transient adb hiccup: try the next source
        text = getattr(result, "stdout", "") or ""
        state = parse_keyguard(text)
        if state is not None:
            return state
    return None


def parse_credential_type(dump: str) -> str | None:
    """CredentialType from `dumpsys lock_settings` -> ``pin``/``pattern``/
    ``password``/``swipe`` (first user wins -- user 0 is listed first)."""
    match = _CREDENTIAL_RE.search(dump)
    if not match:
        return None
    return _CREDENTIAL_TYPES.get(match.group(1).upper(), None)


def detect_lock_type(adb: "Adb") -> str | None:
    """Which kind of screen lock the device has (does not need it unlocked)."""
    try:
        result = adb.shell("dumpsys lock_settings", check=False, timeout=8.0)
    except Exception:
        return None
    return parse_credential_type(getattr(result, "stdout", "") or "")


def parse_node_bounds(xml: str, id_fragment: str) -> Rect | None:
    """Bounds of the first uiautomator node whose resource-id contains the
    fragment (e.g. ``key_enter_text`` on the PIN pad), in display pixels."""
    pattern = re.compile(
        r'<node[^>]*resource-id="[^"]*'
        + re.escape(id_fragment)
        + r'[^"]*"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"'
    )
    match = pattern.search(xml)
    if not match:
        return None
    return tuple(int(value) for value in match.groups())  # type: ignore[return-value]


def fetch_node_bounds(adb: "Adb", id_fragment: str) -> Rect | None:
    """Dump the UI hierarchy (works on the locked keyguard) and read a node."""
    try:
        adb.shell("uiautomator dump /sdcard/droidmirror_ui.xml", check=False, timeout=15.0)
        result = adb.shell("cat /sdcard/droidmirror_ui.xml", check=False, timeout=10.0)
        adb.shell("rm -f /sdcard/droidmirror_ui.xml", check=False, timeout=5.0)
    except Exception:
        return None
    return parse_node_bounds(getattr(result, "stdout", "") or "", id_fragment)


def parse_display_size(wm_output: str) -> tuple[int, int] | None:
    match = _WM_SIZE_RE.search(wm_output)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def fetch_display_size(adb: "Adb") -> tuple[int, int] | None:
    try:
        result = adb.shell("wm size", check=False, timeout=8.0)
    except Exception:
        return None
    return parse_display_size(getattr(result, "stdout", "") or "")


# --------------------------------------------------------------------------- #
# On-screen keyboard
# --------------------------------------------------------------------------- #

#: `dumpsys input_method` states, most reliable first. AOSP's InputMethodManager
#: dumps mInputShown; some OEM builds only expose the window insets, so those
#: are checked too.
_KEYBOARD_SIGNALS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bmInputShown\s*=\s*(true|false)", re.I),
    # `mImeWindowVis=0x...` bit 0x2 is VISIBLE on most builds.
    re.compile(r"\bmImeWindowVis\s*=\s*0x([0-9a-f]+)", re.I),
    # `isInputViewShown=true` (Samsung / older AOSP wording).
    re.compile(r"\bisInputViewShown\s*[=:]\s*(true|false)", re.I),
)


def parse_keyboard_shown(dump: str) -> bool | None:
    """True = the soft keyboard is up, False = it is not, None = not stated."""
    for pattern in _KEYBOARD_SIGNALS:
        match = pattern.search(dump)
        if not match:
            continue
        value = match.group(1).lower()
        if value in ("true", "false"):
            return value == "true"
        # Hex insets mask: bit 2 (0x2) == the IME window is visible.
        try:
            return bool(int(value, 16) & 0x2)
        except ValueError:
            continue
    return None


#: Fields :func:`parse_keyboard_shown` reads, and therefore the words the
#: device-side filter keeps. Cross-checked against :data:`_KEYBOARD_SIGNALS`
#: by a test, so the filter cannot quietly drop a field the parser needs.
_KEYBOARD_MARKERS = ("mInputShown", "mImeWindowVis", "isInputViewShown")

#: Filtered first, and this probe needs it most: the plain ``dumpsys
#: input_method`` dump measured **1.29 MB** of text, pulled every few seconds
#: onto the link carrying the video. The filtered form comes back at ~24 bytes.
#: The full dumps stay as fallbacks for a build that words the field
#: differently -- which costs latency, never the answer.
_KEYBOARD_COMMANDS = (
    device_grep("dumpsys input_method", _KEYBOARD_MARKERS),
    "dumpsys input_method",
    "dumpsys window",
)


def detect_keyboard_shown(adb: "Adb") -> bool | None:
    """Whether the device's on-screen keyboard is currently showing.

    ``None`` when no source could answer -- callers must treat that as
    "unknown" and keep the normal behaviour rather than assuming "closed".
    """
    for command in _KEYBOARD_COMMANDS:
        try:
            result = adb.shell(command, check=False, timeout=8.0)
        except Exception:
            continue
        state = parse_keyboard_shown(getattr(result, "stdout", "") or "")
        if state is not None:
            return state
    return None


def scale_rect(rect: Rect, display: tuple[int, int], frame: tuple[int, int]) -> Rect:
    """Display pixels -> stream-frame pixels (scrcpy scales them back on
    inject, so whatever we send must be in frame space)."""
    dw, dh = display
    if dw <= 0 or dh <= 0:
        return rect
    fx, fy = frame[0] / dw, frame[1] / dh
    return (
        int(rect[0] * fx),
        int(rect[1] * fy),
        int(rect[2] * fx),
        int(rect[3] * fy),
    )
