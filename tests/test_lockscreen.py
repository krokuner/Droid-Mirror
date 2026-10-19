"""Lockscreen detection: dumpsys parsing + command fallback order."""

from __future__ import annotations

import re
import threading
import time

import droidmirror.session as session_mod
from droidmirror.lockscreen import (
    _COMMANDS,
    _LOCK_SOURCES,
    _SIGNALS,
    detect_keyboard_shown,
    detect_lock_type,
    detect_locked,
    parse_credential_type,
    parse_display_size,
    parse_keyboard_shown,
    parse_keyguard,
    parse_node_bounds,
    scale_rect,
)
from droidmirror.session import MirrorSession

ACTIVITY_LOCKED = """
ACTIVITY MANAGER ACTIVITIES (dumpsys activity activities)
Displays (in chronological order):
  displayId=0
KeyguardController:
  mKeyguardShowing=true
  mKeyguardOccluded=false
  mKeyguardShowingAndNotOccluded=true
"""

ACTIVITY_UNLOCKED = """
KeyguardController:
  mKeyguardShowing=false
  mKeyguardOccluded=false
  mKeyguardShowingAndNotOccluded=false
"""

WINDOW_LOCKED = """
WINDOW MANAGER WINDOWS (dumpsys window windows)
  Window #3 Window{a1b2c3 u0 NotificationShade}:
    mHasSurface=true
  mShowingLockscreen=true
"""

WINDOW_UNLOCKED = """
  mShowingLockscreen=false
"""

TRUST_LOCKED = """
Trust state:
  Device locked: true
"""

GARBAGE = """
nothing about keyguards here, just vibes and scheduler dumps
"""


class _Result:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout


class FakeAdb:
    """Serves canned shell answers in order and records the commands."""

    def __init__(self, answers: list) -> None:
        self._answers = list(answers)
        self.commands: list[str] = []

    def shell(self, command: str, *, timeout=None, check=True):
        self.commands.append(command)
        answer = self._answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return _Result(answer)


# -- parser ---------------------------------------------------------------- #


def test_parse_activity_dump_locked():
    assert parse_keyguard(ACTIVITY_LOCKED) is True


def test_parse_activity_dump_unlocked():
    assert parse_keyguard(ACTIVITY_UNLOCKED) is False


def test_parse_window_dump():
    assert parse_keyguard(WINDOW_LOCKED) is True
    assert parse_keyguard(WINDOW_UNLOCKED) is False


def test_parse_trust_dump():
    assert parse_keyguard(TRUST_LOCKED) is True


def test_parse_returns_none_when_not_stated():
    assert parse_keyguard(GARBAGE) is None
    assert parse_keyguard("") is None


def test_parse_ignores_the_longer_field_names():
    """mKeyguardShowingAndNotOccluded must not be read as mKeyguardShowing."""
    assert parse_keyguard("  mKeyguardShowingAndNotOccluded=false\n") is None


# -- detect (command fallback) --------------------------------------------- #


def test_the_filtered_source_that_answers_costs_one_call():
    """The cheap form is tried first, and answering ends the walk."""
    adb = FakeAdb([ACTIVITY_LOCKED])

    assert detect_locked(adb) is True
    assert len(adb.commands) == 1
    assert adb.commands[0].startswith("dumpsys activity activities | grep")


def test_a_filter_that_misses_falls_through_to_the_next_source():
    """A filter that drops the field costs latency, never the answer."""
    adb = FakeAdb([GARBAGE, GARBAGE, WINDOW_LOCKED])

    assert detect_locked(adb) is True
    assert adb.commands[2].startswith("dumpsys window | grep")


def test_shell_failure_falls_through_to_the_next_source():
    adb = FakeAdb([RuntimeError("adb device vanished"), TRUST_LOCKED])

    assert detect_locked(adb) is True
    assert adb.commands[1].startswith("dumpsys trust | grep")


def test_all_sources_unreadable_is_unknown_not_unlocked():
    adb = FakeAdb([GARBAGE] * len(_COMMANDS))

    assert detect_locked(adb) is None
    assert len(adb.commands) == len(_COMMANDS), "unknown stays unknown"


def test_total_failure_is_unknown():
    adb = FakeAdb([RuntimeError("nope")] * len(_COMMANDS))

    assert detect_locked(adb) is None


def test_every_source_has_a_filtered_form_first_and_a_plain_fallback_after():
    """The order is the whole trick: bytes on the wire, not bytes generated.

    Leading with a plain dump (as this did) spends ~90 KB of text per poll on
    the same link the video stream rides; the filtered form spends ~26 bytes.
    The plain dumps have to stay reachable, or a build whose fields differ goes
    blind instead of merely slow.
    """
    for command in _COMMANDS[: len(_LOCK_SOURCES)]:
        assert "| grep -E -m" in command
    assert [c.split(" | ")[0] for c in _COMMANDS[: len(_LOCK_SOURCES)]] == [
        c for c, _m in _LOCK_SOURCES
    ]
    assert _COMMANDS[len(_LOCK_SOURCES):] == tuple(
        c for c, _m in _LOCK_SOURCES
    ), "every filtered source keeps its plain fallback, in the same order"


def test_every_filter_marker_is_a_field_its_own_parser_reads():
    """The filter and the parser must not drift apart.

    A marker that no longer appears in the pattern for the same source would
    silently strip the field the parser looks for, and the probe would quietly
    downgrade to the slow fallback forever.
    """
    for command, markers in _LOCK_SOURCES:
        label, pattern = next(
            (label, pattern) for label, pattern in _SIGNALS if label in command
        )
        for marker in markers:
            assert re.search(re.escape(marker), pattern.pattern, re.I), (
                f"{label} filter keeps {marker!r}, but its parser never reads it"
            )
        assert all(" " not in m for m in markers), (
            "markers go to the device's shell unquoted, so they must be one word"
        )


# -- lock type (dumpsys lock_settings) -------------------------------------- #

LOCK_SETTINGS_PIN = """
Lock Settings Service state:
  Enable:
  NonStrongBiometricIdleTimeout: 0
  Credentials:
    User 0:
      CredentialType: PIN
    User 150:
      CredentialType: PASSWORD
"""

LOCK_SETTINGS_PATTERN = "  User 0:\n    CredentialType: PATTERN\n"
LOCK_SETTINGS_NO_LOCK = "  User 0:\n    CredentialType: NONE\n"
LOCK_SETTINGS_NULL = "  User 0:\n    CredentialType: null\n"


def test_parse_credential_type_reads_the_first_user():
    assert parse_credential_type(LOCK_SETTINGS_PIN) == "pin"


def test_parse_credential_type_maps_every_kind():
    assert parse_credential_type(LOCK_SETTINGS_PATTERN) == "pattern"
    assert parse_credential_type(LOCK_SETTINGS_NO_LOCK) == "swipe"
    assert parse_credential_type(LOCK_SETTINGS_NULL) is None
    assert parse_credential_type(GARBAGE) is None
    assert parse_credential_type("") is None


def test_detect_lock_type_asks_lock_settings():
    adb = FakeAdb([LOCK_SETTINGS_PIN])

    assert detect_lock_type(adb) == "pin"
    assert adb.commands == ["dumpsys lock_settings"]


def test_detect_lock_type_failure_is_unknown():
    adb = FakeAdb([RuntimeError("device gone")])

    assert detect_lock_type(adb) is None


# -- keyguard node bounds (uiautomator) ------------------------------------- #

KEYGUARD_UI_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="" resource-id="com.android.keyguard:id/keyguard_pin_view"
        class="android.widget.LinearLayout" bounds="[285,0][915,1848]">
    <node index="2" text="" resource-id="com.android.keyguard:id/password_entry_box"
          class="android.widget.FrameLayout" bounds="[285,1248][915,1314]" />
    <node index="3" text="OK" resource-id="com.android.keyguard:id/key_enter_text"
          class="android.widget.Button" bounds="[687,1662][777,1752]" />
  </node>
</hierarchy>
"""


def test_parse_node_bounds_reads_the_ok_key():
    assert parse_node_bounds(KEYGUARD_UI_XML, "key_enter_text") == (687, 1662, 777, 1752)


def test_parse_node_bounds_finds_the_pin_pad_view():
    assert parse_node_bounds(KEYGUARD_UI_XML, "keyguard_pin_view") == (285, 0, 915, 1848)


def test_parse_node_bounds_missing_node_is_none():
    assert parse_node_bounds(KEYGUARD_UI_XML, "pattern") is None
    assert parse_node_bounds("", "key_enter_text") is None


# -- display size + frame scaling ------------------------------------------- #


def test_parse_display_size():
    assert parse_display_size("Physical size: 1200x1920\n") == (1200, 1920)
    assert parse_display_size("Override size: 720x1280\n") is None
    assert parse_display_size("") is None


def test_scale_rect_maps_display_pixels_into_frame_space():
    # 1200x1920 display mirrored as a 1000x1600 stream: 0.833.. on both axes.
    scaled = scale_rect((600, 960, 900, 1200), (1200, 1920), (1000, 1600))

    assert scaled == (500, 800, 750, 1000)


def test_scale_rect_survives_a_broken_display_size():
    rect = (10, 20, 30, 40)

    assert scale_rect(rect, (0, 0), (1000, 1600)) == rect


# -- the session's lock watcher --------------------------------------------- #


def test_foreground_poll_only_runs_when_something_is_listening(monkeypatch):
    """The presence probe must not run unless Discord is on.

    Regression test. The foreground probe exists only to feed Rich Presence,
    but the watcher fired it every 4th tick for the whole session regardless,
    for every user who never touches the Discord button.
    """
    monkeypatch.setattr(session_mod, "LOCK_POLL_S", 0.01)
    monkeypatch.setattr(session_mod, "detect_lock_type", lambda adb: "pin")
    monkeypatch.setattr(session_mod, "detect_locked", lambda adb: False)
    monkeypatch.setattr(session_mod, "detect_keyboard_shown", lambda adb: False)

    foreground_calls = {"n": 0}

    def fake_foreground(adb):
        foreground_calls["n"] += 1
        return ("com.foo.bar", ".Main")

    monkeypatch.setattr(session_mod, "detect_foreground", fake_foreground)

    session = MirrorSession(None)
    session.foreground_polling = False  # Discord off -- the default
    watcher = threading.Thread(target=session._lock_watch, daemon=True)
    session._lock_thread = watcher
    watcher.start()
    try:
        time.sleep(0.3)  # ~30 ticks, so the every-4th-tick branch fires plenty
        assert foreground_calls["n"] == 0, (
            "detect_foreground ran while nothing was reading foreground_app"
        )
        assert session.foreground_app is None

        # Now switch it on, as the UI does when Discord connects.
        session.foreground_polling = True
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and foreground_calls["n"] == 0:
            time.sleep(0.01)
        assert foreground_calls["n"] >= 1, "foreground polling never switched on"
        assert session.foreground_app == "com.foo.bar"
    finally:
        session._stop.set()
        session._lock_thread = None
        watcher.join(timeout=2.0)


def test_lock_watch_grabs_the_type_once_and_publishes_both(monkeypatch):
    monkeypatch.setattr(session_mod, "LOCK_POLL_S", 0.01)
    calls = {"type": 0, "locked": 0}

    def fake_type(adb):
        calls["type"] += 1
        return "pin"

    def fake_locked(adb):
        calls["locked"] += 1
        return True

    monkeypatch.setattr(session_mod, "detect_lock_type", fake_type)
    monkeypatch.setattr(session_mod, "detect_locked", fake_locked)

    session = MirrorSession(None)
    watcher = threading.Thread(target=session._lock_watch, daemon=True)
    session._lock_thread = watcher  # the identity the watcher checks against
    watcher.start()
    try:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and session.lock_type is None:
            time.sleep(0.01)
        assert session.lock_type == "pin"
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and session.device_locked is None:
            time.sleep(0.01)
        assert session.device_locked is True

        time.sleep(0.05)  # a few more polls: the type is asked exactly once
        assert calls["type"] == 1, "lock kind never changes mid-session"
        assert calls["locked"] >= 1
    finally:
        session._stop.set()
        session._lock_thread = None
        watcher.join(timeout=2.0)
    assert not watcher.is_alive(), "stop() must retire the watcher"


def test_lock_watch_retires_when_a_newer_session_takes_over(monkeypatch):
    monkeypatch.setattr(session_mod, "LOCK_POLL_S", 0.01)
    monkeypatch.setattr(session_mod, "detect_locked", lambda adb: True)

    session = MirrorSession(None)
    stale = threading.Thread(target=session._lock_watch, daemon=True)
    session._lock_thread = stale
    stale.start()
    session._lock_thread = None  # stop()/restart() replaced us
    stale.join(timeout=2.0)

    assert not stale.is_alive()


# --------------------------------------------------------------------------- #
# On-screen keyboard detection (the x-hotkey must not steal the mouse)
# --------------------------------------------------------------------------- #


IME_SHOWN = """
INPUT MANAGER (dumpsys input_method)
  mInputShown=true
  mImeWindowVis=0x2
  mIsInputViewShown=true
"""

IME_HIDDEN = """
INPUT MANAGER (dumpsys input_method)
  mInputShown=false
  mImeWindowVis=0x0
  mIsInputViewShown=false
"""


def test_parse_keyboard_shown_reads_the_input_manager_flag():
    assert parse_keyboard_shown(IME_SHOWN) is True
    assert parse_keyboard_shown(IME_HIDDEN) is False


def test_parse_keyboard_shown_falls_back_to_the_window_insets():
    """Some OEM builds only expose mImeWindowVis; bit 0x2 means visible."""
    assert parse_keyboard_shown("mImeWindowVis=0x2") is True
    assert parse_keyboard_shown("mImeWindowVis=0x0") is False


def test_parse_keyboard_shown_is_unknown_when_nothing_answers():
    assert parse_keyboard_shown("") is None
    assert parse_keyboard_shown("some unrelated dump") is None


def test_detect_keyboard_shown_asks_the_device(monkeypatch):
    calls: list[str] = []

    class FakeResult:
        stdout = IME_SHOWN

    class FakeAdb:
        def shell(self, command, **kwargs):
            calls.append(command)
            return FakeResult()

    assert detect_keyboard_shown(FakeAdb()) is True
    assert calls[0].startswith("dumpsys input_method | grep"), (
        "the plain dump is 1.29 MB of text on the link the video uses"
    )


def test_detect_keyboard_shown_survives_a_failing_dump():
    class FakeAdb:
        def shell(self, command, **kwargs):
            raise RuntimeError("offline")

    assert detect_keyboard_shown(FakeAdb()) is None
