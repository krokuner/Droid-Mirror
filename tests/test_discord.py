"""Discord Rich Presence: foreground detection, naming, and safe degradation."""

from __future__ import annotations

import pytest

from droidmirror.discord_rpc import (
    EMOJI_GAME,
    EMOJI_HOME,
    EMOJI_PHONE,
    HOME_DETAILS,
    IDLE_IMAGE_URL,
    DiscordPresence,
    app_label,
    detect_foreground,
    is_home,
    parse_foreground,
)


# --------------------------------------------------------------------------- #
# Foreground parsing
# --------------------------------------------------------------------------- #

WINDOW_DUMP = """
WINDOW MANAGER DISPLAY CONTENTS (dumpsys window)
  mCurrentFocus=Window{1f2e3a4 u0 com.google.android.youtube/
      com.google.android.apps.youtube.MainActivity}
  mFocusedApp=AppWindowToken{token}
"""

ACTIVITY_DUMP = """
ACTIVITY MANAGER ACTIVITIES (dumpsys activity activities)
  mResumedActivity: ActivityRecord{9a8b7c6 u0 com.roblox.client/
      com.roblox.client.ui.RobloxActivity t42}
"""

SAMSUNG_FOCUS = """
  mFocusedApp=AppWindowToken{xyz u0 com.sec.android.app.launcher/
      com.sec.android.app.launcher.Launcher}
"""


def test_parse_foreground_reads_the_window_dump():
    assert parse_foreground(WINDOW_DUMP) == (
        "com.google.android.youtube",
        "com.google.android.apps.youtube.MainActivity",
    )


def test_parse_foreground_reads_the_activity_dump():
    assert parse_foreground(ACTIVITY_DUMP)[0] == "com.roblox.client"


def test_parse_foreground_is_unknown_when_nothing_is_focused():
    assert parse_foreground("") is None
    assert parse_foreground("some unrelated dump") is None


def test_detect_foreground_asks_the_device():
    class Result:
        stdout = WINDOW_DUMP

    calls: list[str] = []

    class FakeAdb:
        def shell(self, command, **kwargs):
            calls.append(command)
            return Result()

    assert detect_foreground(FakeAdb())[0] == "com.google.android.youtube"
    assert calls[0].startswith("dumpsys window | grep"), (
        "the plain dump is ~90 KB of text per call, on the wire the video uses"
    )


def test_detect_foreground_survives_a_failing_dump():
    class FakeAdb:
        def shell(self, command, **kwargs):
            raise RuntimeError("offline")

    assert detect_foreground(FakeAdb()) is None


# --------------------------------------------------------------------------- #
# Naming + home screen
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "package,label",
    [
        ("com.google.android.youtube", "YouTube"),
        ("com.roblox.client", "Roblox"),
        ("com.instagram.android", "Instagram"),
        ("com.whatsapp", "WhatsApp"),
    ],
)
def test_known_apps_get_a_friendly_name(package, label):
    assert app_label(package) == label


def test_an_unknown_package_falls_back_to_its_last_segment():
    assert app_label("com.vendor.thing") == "Thing"
    assert app_label("") == "Unknown app"


def test_home_screen_is_recognised():
    assert is_home("com.google.android.apps.nexuslauncher")
    assert is_home("com.sec.android.app.launcher")
    assert not is_home("com.google.android.youtube")
    assert not is_home(None)


@pytest.mark.parametrize(
    "package",
    [
        "com.sec.android.app.launcher",
        "com.sec.android.emergencylauncher",
        "com.google.android.apps.nexuslauncher",
        "com.movtery.zalithlauncher",
    ],
)
def test_every_launcher_build_counts_as_home(package):
    assert is_home(package)


@pytest.mark.parametrize(
    "package",
    [
        "com.sec.android.app.fetch",  # Samsung "App Fetch"
        "com.samsung.android.app.fetch",
        "com.sec.android.app.sdrawer",
    ],
)
def test_launcher_surfaces_report_as_home_not_as_an_app(package):
    """Sitting on the home screen must say so, whatever surface is focused.

    Samsung focuses "App Fetch" instead of the launcher package, which put
    "App Fetch" on the profile as if it were the app in use.
    """
    assert is_home(package), package


def test_a_real_app_is_not_mistaken_for_home():
    """The hint matching must not swallow real apps."""
    for package in (
        "com.google.android.youtube",
        "com.instagram.android",
        "com.whatsapp",
        "com.android.vending",  # Play Store
        "com.discord",
    ):
        assert not is_home(package), package


def test_a_null_focus_line_does_not_grab_a_later_window():
    """With DOTALL the pattern ran past `mCurrentFocus=null` and captured the
    package from whichever window came next in the dump."""
    dump = (
        "  mCurrentFocus=null\n"
        "  mFocusedApp=null\n"
        "  mCurrentFocus=Window{45c3914 u0 com.whatsapp/"
        "com.whatsapp.home.ui.HomeActivity}\n"
    )
    assert parse_foreground(dump)[0] == "com.whatsapp"


def test_the_home_screen_has_its_own_icon():
    """Home is not an app -- it gets a house, and the phone emoji for apps."""
    assert EMOJI_HOME
    assert EMOJI_PHONE
    assert EMOJI_GAME
    assert HOME_DETAILS == "Using mobile phone"


# --------------------------------------------------------------------------- #
# The presence client (never allowed to take the mirror down)
# --------------------------------------------------------------------------- #


class FakeRpc:
    """Stands in for pypresence's Presence."""

    def __init__(self, fail_on_update: bool = False) -> None:
        self.updates: list[dict] = []
        self.connected = False
        self.closed = False
        self.cleared = False
        self.fail_on_update = fail_on_update

    def connect(self) -> None:
        self.connected = True

    def update(self, **payload) -> None:
        if self.fail_on_update:
            raise RuntimeError("discord went away")
        self.updates.append(payload)

    def clear(self) -> None:
        self.cleared = True

    def close(self) -> None:
        self.closed = True
        self.connected = False


def _presence(monkeypatch, rpc: FakeRpc | None, app_id="123"):
    """A DiscordPresence wired to a fake pypresence module."""
    import sys
    import types

    module = types.ModuleType("pypresence")

    def factory(_client_id):
        if rpc is None:
            raise RuntimeError("discord is not running")
        return rpc

    module.Presence = factory  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pypresence", module)
    return DiscordPresence()


def test_without_an_id_the_presence_is_off(monkeypatch):
    rpc = FakeRpc()
    presence = _presence(monkeypatch, rpc)
    assert presence.configure("", enabled=True) is False
    assert not presence.connected
    assert presence.update_app("com.google.android.youtube") is None
    assert rpc.updates == [], "nothing sent"


def test_discord_not_running_leaves_the_presence_off(monkeypatch):
    presence = _presence(monkeypatch, None)
    assert presence.configure("123", enabled=True) is False
    assert not presence.connected


def test_a_connected_presence_publishes_the_app(monkeypatch):
    rpc = FakeRpc()
    presence = _presence(monkeypatch, rpc)
    assert presence.configure("123", enabled=True) is True

    presence.update_app("com.roblox.client", state="DroidMirror")

    assert rpc.updates[-1]["details"] == "Roblox"
    assert rpc.updates[-1]["state"] == "DroidMirror"


def test_the_home_screen_reports_a_picture_of_a_phone(monkeypatch):
    rpc = FakeRpc()
    presence = _presence(monkeypatch, rpc)
    presence.configure("123", enabled=True)

    presence.update_app("com.sec.android.app.launcher")

    assert rpc.updates[-1]["details"] == HOME_DETAILS
    # The home screen is not an app, so there is no store page to fetch an icon
    # from -- its picture is the fixed IDLE_IMAGE_URL. A url is the only
    # reference the desktop client resolves, which is why the emoji it used to
    # fall back on could never be drawn (see test_only_a_url_is_sent_as_an_image).
    assert rpc.updates[-1]["large_image"] == IDLE_IMAGE_URL

    # An image is still honoured if a caller has one.
    presence.update_app(
        "com.sec.android.app.launcher", image="https://cdn.discordapp.com/p.png"
    )
    assert rpc.updates[-1]["large_image"].endswith("p.png")


def test_a_real_icon_image_wins_over_the_emoji(monkeypatch):
    rpc = FakeRpc()
    presence = _presence(monkeypatch, rpc)
    presence.configure("123", enabled=True)

    presence.update_app(
        "com.discord", image="https://cdn.discordapp.com/x.png"
    )

    assert rpc.updates[-1]["large_image"] == "https://cdn.discordapp.com/x.png"


def test_only_a_url_is_sent_as_an_image(monkeypatch):
    """A url, and nothing else -- ids and keys are both dropped.

    Measured against the running client rather than read out of the docs,
    because the two disagree. A ``SET_ACTIVITY`` returns the activity the client
    now holds, including the ``large_image`` it accepted:

    * a url comes back rewritten as ``mp:external/<hash>/https/…`` -- the
      client saying it fetched and proxied the image;
    * an asset id comes back as ``None``, even though the ``Activity`` spec says
      an uploaded asset is referenced as ``{application_asset_id}``;
    * an asset key comes back as ``None`` too.

    A dropped value is worse than no value: Discord then paints its grey "?"
    rather than falling back to the application's own icon. So each of these
    must be refused on the way out, not handed over and hoped for.
    """
    rpc = FakeRpc()
    presence = _presence(monkeypatch, rpc)
    presence.configure("123", enabled=True)

    # The id form the spec describes -- the desktop client drops it regardless.
    presence.update_app("com.instagram.android", image="1560714379794579618")
    assert "large_image" not in rpc.updates[-1]

    # The readable key, which the upload returns right beside the id.
    presence.update_app("com.instagram.android", image="com-instagram-android-e9d73a12")
    assert "large_image" not in rpc.updates[-1]

    # An emoji stand-in: a string, but not an image of any kind.
    presence.update_app("com.example.app", image="📱")
    assert "large_image" not in rpc.updates[-1]

    # The one shape that actually renders.
    presence.update_app(
        "com.instagram.android",
        image="https://play-lh.googleusercontent.com/yHi59jmO.png",
    )
    assert rpc.updates[-1]["large_image"].endswith("yHi59jmO.png")


def test_an_unchanged_app_is_not_resent_every_poll(monkeypatch):
    """The poll runs every few seconds; spamming Discord would be rude."""
    rpc = FakeRpc()
    presence = _presence(monkeypatch, rpc)
    presence.configure("123", enabled=True)

    presence.update_app("com.discord")
    before = len(rpc.updates)
    presence.update_app("com.discord")
    presence.update_app("com.discord")

    assert len(rpc.updates) == before, "same app, no new packet"


def test_discord_dying_mid_session_never_raises(monkeypatch):
    rpc = FakeRpc(fail_on_update=True)
    presence = _presence(monkeypatch, rpc)
    presence.configure("123", enabled=True)

    presence.update_app("com.discord")  # must not raise

    assert not presence.connected, "the dead connection is dropped quietly"


def test_closing_clears_the_presence(monkeypatch):
    rpc = FakeRpc()
    presence = _presence(monkeypatch, rpc)
    presence.configure("123", enabled=True)

    presence.close()

    assert rpc.cleared and rpc.closed
    assert not presence.connected


def test_no_placeholder_image_is_ever_sent(monkeypatch):
    """Whatever goes out as large_image has to be something Discord resolves.

    An emoji and an asset id both come back dropped from the running client, so
    neither is ever sent -- Discord paints the grey "?" for a dropped value
    rather than falling back to the application's own icon.
    """
    rpc = FakeRpc()
    presence = _presence(monkeypatch, rpc)
    presence.configure("123", enabled=True)

    # Home screen, which has no store page behind it: the fixed idle url.
    presence.update_app("com.sec.android.app.launcher")
    assert rpc.updates[-1]["large_image"] == IDLE_IMAGE_URL

    # A real url is used when we have one.
    presence.update_app(
        "com.discord", image="https://cdn.discordapp.com/app-assets/1/a.png"
    )
    assert rpc.updates[-1]["large_image"].startswith("https://")


def test_the_start_timestamp_is_a_unix_time(monkeypatch):
    """A monotonic clock here made Discord report a 13-year session."""
    import time

    rpc = FakeRpc()
    presence = _presence(monkeypatch, rpc)
    presence.configure("123", enabled=True)

    now = time.time()
    presence.update_app("com.discord", start=now)

    sent = rpc.updates[-1]["start"]
    assert abs(sent - now) < 5, "start must be Unix epoch seconds"
    assert sent > 1_600_000_000, "not a monotonic clock"


def test_the_session_stamps_wall_clock_time():
    """started_at is what the presence sends, so it must be epoch seconds."""
    import time

    from droidmirror.session import MirrorSession

    class FakeAdb:
        serial = None

        def devices(self, timeout=30.0):
            return []

    session = MirrorSession(adb=FakeAdb())
    session._started_epoch = time.time()
    started = session.started_at

    assert abs(started - time.time()) < 5
    assert started > 1_600_000_000, "must be wall clock, not monotonic"