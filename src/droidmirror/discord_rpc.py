"""Discord Rich Presence: show what is actually running on the tablet.

Presence is a *nice-to-have*, so everything here is inert unless it has to be:
no Discord running, no application id configured, or pypresence not installed
all degrade to a no-op instead of an error. Nothing on the mirror or in the
connection path may break because of this.

The device tells us what is in the foreground through ``dumpsys``. The parse
is a pure function over the dump text (testable with no device attached),
mirroring the shape of :mod:`droidmirror.lockscreen`.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from .adb import device_grep

if TYPE_CHECKING:
    from .adb import Adb

# --------------------------------------------------------------------------- #
# Foreground app detection
# --------------------------------------------------------------------------- #

#: Sources in preference order. Each pattern pulls (package, activity) out of
#: a different dumpsys; OEM skins report one or the other, so we try both.
_FOCUS_PATTERNS: tuple[re.Pattern[str], ...] = (
    # `mCurrentFocus=Window{... u0 com.foo.bar/com.foo.bar.MainActivity}`
    # `[^\n]*?` keeps the match on its own line: with DOTALL a window that
    # reports "mCurrentFocus=null" (the status shade, say) would run on and
    # capture the package from some *other* window further down the dump.
    # The trailing \s* still allows an activity name that wrapped onto the
    # next line, which is how a long dumpsys prints it.
    re.compile(r"mCurrentFocus=[^\n]*?\s(u\d+ )?(?P<pkg>[\w.]+)/\s*(?P<act>[\w.$]+)"),
    # `mFocusedApp=AppWindowToken{... u0 com.foo.bar/.Main}`
    re.compile(r"mFocusedApp=[^\n]*?\s(u\d+ )?(?P<pkg>[\w.]+)/\s*(?P<act>[\w.$]+)"),
    # `mResumedActivity: ActivityRecord{... u0 com.foo.bar/.MainActivity t123}`
    re.compile(r"mResumedActivity[^\n]*?\s(u\d+ )?(?P<pkg>[\w.]+)/\s*(?P<act>[\w.$]+)"),
    # `topResumedActivity=ActivityRecord{... com.foo.bar/.MainActivity}`
    re.compile(r"topResumedActivity[^\n]*?\s(u\d+ )?(?P<pkg>[\w.]+)/\s*(?P<act>[\w.$]+)"),
)

#: Launcher packages -- the home screen. Several because Samsung ships its own
#: and keeps the AOSP one installed.
HOME_PACKAGES = frozenset(
    {
        "com.google.android.apps.nexuslauncher",
        "com.google.android.launcher",
        "com.android.launcher",
        "com.android.launcher3",
        "com.sec.android.app.launcher",
        "com.sec.android.emergencylauncher",
        "com.samsung.android.app.launcher",
        "com.miui.home",
        "com.oppo.launcher",
        "com.huawei.android.launcher",
    }
)

#: Substrings that mark a package as part of the home screen on some OEM
#: build. Samsung is the awkward one: tapping through the launcher surfaces
#: "App Fetch" (``*.app.fetch``) rather than the launcher package itself, and
#: reporting that as the app in use put "App Fetch" on someone's profile
#: while they were plainly sitting on the home screen.
_HOME_HINTS = (
    "launcher",
    "app.fetch",
    "appfetch",
    "sdrawer",
    "homescreen",
    "home.screen",
    "tw.launcher",
)

#: Package -> a name worth showing. Only apps people actually mirror; anything
#: missing falls back to the last dotted segment (e.g. "bar" from com.foo.bar).
APP_NAMES: dict[str, str] = {
    "com.google.android.youtube": "YouTube",
    "com.google.android.apps.youtube.tv": "YouTube TV",
    "com.google.android.gm": "Gmail",
    "com.google.android.apps.messaging": "Messages",
    "com.google.android.googlequicksearchbox": "Google",
    "com.android.chrome": "Chrome",
    "com.chrome.beta": "Chrome Beta",
    "com.microsoft.emmx": "Edge",
    "com.whatsapp": "WhatsApp",
    "com.whatsapp.w4b": "WhatsApp Business",
    "org.telegram.messenger": "Telegram",
    "com.discord": "Discord",
    "com.facebook.katana": "Facebook",
    "com.instagram.android": "Instagram",
    "com.twitter.android": "X (Twitter)",
    "com.reddit.frontpage": "Reddit",
    "com.linkedin.android": "LinkedIn",
    "com.spotify.music": "Spotify",
    "com.netflix.mediaclient": "Netflix",
    "com.amazon.avod": "Prime Video",
    "com.jio.jiostv": "JioHotstar",
    "com.google.android.apps.youtube.vr.oculus": "YouTube VR",
    "com.roblox.client": "Roblox",
    "com.mojang.minecraftpe": "Minecraft",
    "com.miHoYo.GenshinImpact": "Genshin Impact",
    "com.miHoYo.Nap": "Honkai: Star Rail",
    "com.HoYoverse.hkrpgoversea": "Honkai Impact 3rd",
    "com.pubg.krmobile": "BGMI",
    "com.tencent.ig": "PUBG Mobile",
    "com.activision.callofduty.shooter": "Call of Duty: Mobile",
    "com.garena.game.codm": "Call of Duty: Mobile",
    "com.dts.freefireth": "Free Fire",
    "com.dts.freefiremax": "Free Fire MAX",
    "com.ea.gp.fifamobile": "EA SPORTS FC",
    "com.dxco.pandavszombies": "Plants vs. Zombies",
    "com.supercell.clashofclans": "Clash of Clans",
    "com.supercell.clashroyale": "Clash Royale",
    "com.supercell.brawlstars": "Brawl Stars",
    "com.nianticlabs.pokemongo": "Pokémon GO",
    "org.telegram.messenger.web": "Telegram Web",
    "com.microsoft.office.outlook": "Outlook",
    "com.microsoft.teams": "Teams",
    "com.adobe.reader": "Adobe Reader",
    "com.obsproject.app": "OBS Studio",
    "com.karapps.kalkulator": "Calculator",
    "com.android.settings": "Settings",
    "com.android.deskclock": "Clock",
    "com.android.contacts": "Contacts",
    "com.android.dialer": "Phone",
    "com.android.camera": "Camera",
    "com.android.gallery3d": "Gallery",
    "com.google.android.apps.photos": "Photos",
    "com.google.android.apps.maps": "Maps",
    "com.samsung.android.email.provider": "Email",
    "com.sec.android.gallery3d": "Gallery",
    "com.sec.android.app.sbrowser": "Samsung Internet",
    "com.sec.android.app.settings": "Settings",
}


def parse_foreground(dump: str) -> tuple[str, str] | None:
    """(package, activity) from a dumpsys dump, or None if not stated."""
    for pattern in _FOCUS_PATTERNS:
        match = pattern.search(dump)
        if match:
            return match.group("pkg"), match.group("act")
    return None


def detect_foreground(adb: "Adb") -> tuple[str, str] | None:
    """Ask the device what is in the foreground.

    The filtered dump comes first: the plain ``dumpsys window`` is ~90 KB of
    text per call and this runs on a timer for the whole session, so its output
    rides the same link as the video (see :func:`~droidmirror.adb.device_grep`).
    The plain dumps remain as fallbacks, so a build that words the focus field
    differently still answers -- just less cheaply.

    ``None`` when nothing could answer (device busy, exotic build) -- callers
    keep the last known state rather than blanking the presence.
    """
    for command in (
        device_grep(
            "dumpsys window", ("mCurrentFocus", "mFocusedApp", "mResumedActivity")
        ),
        device_grep(
            "dumpsys activity activities",
            ("mCurrentFocus", "mFocusedApp", "mResumedActivity"),
        ),
        "dumpsys window",
        "dumpsys activity activities",
    ):
        try:
            result = adb.shell(command, check=False, timeout=8.0)
        except Exception:
            continue
        found = parse_foreground(getattr(result, "stdout", "") or "")
        if found is not None:
            return found
    return None


def app_label(package: str) -> str:
    """A friendly name for a package id."""
    if not package:
        return "Unknown app"
    if package in APP_NAMES:
        return APP_NAMES[package]
    # Fall back to the last dotted segment, capitalised -- better than showing
    # "com.some.vendor.thing" raw, and honest about not knowing the name.
    tail = package.rsplit(".", 1)[-1]
    return tail[:1].upper() + tail[1:] if tail else package


def is_home(package: str | None) -> bool:
    """Whether the device is sitting on its home screen.

    Covers the launcher package *and* the launcher-adjacent surfaces some OEM
    builds focus while still being "at home" (Samsung's App Fetch, the app
    drawer), because those are not apps the user is using.
    """
    if not package:
        return False
    if package in HOME_PACKAGES:
        return True
    lowered = package.lower()
    return any(hint in lowered for hint in _HOME_HINTS)


# --------------------------------------------------------------------------- #
# Presence client
# --------------------------------------------------------------------------- #

#: Kept only as documentation of a dead end: Discord does *not* accept an
#: emoji where an asset is expected. It looks the name up, misses, and paints
#: the grey "?" -- see :func:`is_image_reference`, which rejects them.
EMOJI_PHONE = "\U0001F4F1"  # mobile phone
EMOJI_APP = "\U0001F4F6"  # package
EMOJI_GAME = "\U0001F3AE"  # game controller
EMOJI_HOME = "\U0001F3E1"  # house

def is_image_reference(icon: str) -> bool:
    """True when Discord will actually be able to render ``icon``.

    A url, and *only* a url. That was measured against the running client
    rather than read out of the docs, because the two disagree. The ``Activity``
    spec says an uploaded asset is referenced as ``{application_asset_id}``,
    but the desktop presence layer drops any asset reference it cannot resolve
    in its own store -- silently, with no error on either side. So an asset id
    (and an asset key) both produce the grey "?" this function exists to
    prevent, while a url round-trips as::

        mp:external/e5DF1EE11Do1-d4vG-wM9QVkMYrWIGnx0Gi-zj2aa-U/https/…

    That ``mp:`` prefix is the client reporting it fetched and proxied the
    image, which makes a url the only reference known to render.

    Refusing the other shapes matters as much as accepting this one: an
    unresolvable value is *worse* than no value, because Discord paints "?"
    instead of falling back to the application's own icon.
    """
    return icon.startswith(("http://", "https://"))

#: Package prefixes that mean "this is a game" -- gets the controller emoji.
_GAME_HINTS = (
    "com.roblox",
    "com.mojang",
    "com.miHoYo",
    "com.pubg",
    "com.tencent.ig",
    "com.activision",
    "com.garena",
    "com.dts",
    "com.supercell",
    "com.ea.gp",
    "com.nianticlabs",
    "com.unity3d",
    "com.epicgames",
)

HOME_DETAILS = "Using mobile phone"

#: The picture for the home screen -- the state that reads "Using mobile phone".
#:
#: A url, not an uploaded asset: a url is the only reference the desktop client
#: will resolve (see :func:`is_image_reference` for the measurement). The picture
#: is ``idle-screen.jpg`` squashed into a square with its paper background
#: punched out to **transparency** -- the card behind it is dark, and a white
#: tile there reads as a broken image. The square comes out at
#: ``icons/idle-screen-square.png``, rebuilt by ``make_idle_square.py``; upload
#: that and point this line at the result.
IDLE_IMAGE_URL = "https://files.catbox.moe/27a0pi.png"


class DiscordPresence:
    """A Rich Presence connection that can always be switched off safely.

    Every Discord failure mode (not running, no id, library missing, socket
    refused) collapses into "presence stays off" -- this must never take the
    mirror down with it.
    """

    def __init__(self) -> None:
        self._rpc: Any = None
        self._client_id: str = ""
        self._last: tuple[str, str] | None = None  # (details, state)

    # -- configuration ---------------------------------------------------- #

    @property
    def configured(self) -> bool:
        """True when an application id has been supplied."""
        return bool(self._client_id)

    @property
    def connected(self) -> bool:
        return self._rpc is not None

    @property
    def client_id(self) -> str:
        return self._client_id

    @property
    def status(self) -> str:
        """A short human-readable state, for the UI to surface.

        Without this, "the Discord button is lit" looks identical to "the
        presence is actually being published" -- and when something else is
        writing to the same application id, the difference between the two is
        exactly what the user needs to see.
        """
        if not self._client_id:
            return "no application id"
        if self._rpc is None:
            return "not connected"
        return "connected"

    def configure(self, client_id: str, enabled: bool = True) -> bool:
        """Set the Discord application id (and connect if enabled).

        Returns True when a live connection exists afterwards. Calling it
        again with the same id is a no-op, so a settings reload is harmless.
        """
        client_id = (client_id or "").strip()
        if client_id == self._client_id and self._rpc is not None:
            return True
        self.close()
        self._client_id = client_id
        if not (enabled and client_id):
            return False
        return self._connect()

    def _connect(self) -> bool:
        try:
            from pypresence import Presence  # imported late: optional dependency
        except Exception:
            return False  # pypresence not installed -- quietly off
        try:
            rpc = Presence(self._client_id)
            rpc.connect()
        except Exception:
            # Discord not running, id invalid, socket refused: all "off".
            self._rpc = None
            return False
        self._rpc = rpc
        self._last = None
        return True

    # -- updates ----------------------------------------------------------- #

    def _update(
        self,
        details: str,
        state: str,
        icon: str,
        start: Any,
        force: bool,
    ) -> None:
        key = (details, state, icon)
        if key == self._last and not force:
            return  # nothing changed; do not spam the IPC socket
        self._last = key
        payload: dict[str, Any] = {
            "details": details,
            "state": state,
            "large_text": details,
        }
        # large_image must be something Discord can actually resolve: a url, or
        # the numeric asset id from one of our uploads. An emoji -- and a
        # *key* rather than an id -- is neither: Discord looks it up, misses,
        # and paints the grey "?" placeholder. Omitting the field instead
        # falls back to the application's own icon, which does resolve.
        if is_image_reference(icon):
            payload["large_image"] = icon
        if start is not None:
            payload["start"] = start
        try:
            self._rpc.update(**payload)
        except Exception:
            # Discord went away mid-session (closed it). Drop the connection
            # and stay quiet rather than raising on every poll.
            self._rpc = None

    def update_app(
        self,
        package: str | None,
        *,
        state: str = "",
        start: Any = None,
        image: str | None = None,
        force: bool = False,
    ) -> None:
        """Publish what the device is showing right now.

        ``image`` is a url to the app's real icon, fetched online. When we have
        one it wins, and Discord fetches and proxies the picture itself.

        The home screen has no app behind it, so it goes out with
        :data:`IDLE_IMAGE_URL` instead. Either way an *omitted* field is the
        fallback of last resort -- Discord then draws the application's own icon,
        which is what the emoji constants used to prevent, because
        :func:`is_image_reference` drops an emoji (like an asset id) on the way
        out and Discord paints the grey "?" for it. So the emoji only ever
        decide which value gets discarded; nothing is drawn from them.
        """
        if self._rpc is None:
            return
        if is_home(package) or not package:
            # The home screen is not an app, so there is no store page to fetch
            # an icon from -- its picture is :data:`IDLE_IMAGE_URL`. A per-state
            # image still wins if the caller has one; the emoji survives only as
            # the last resort for an emptied-out url, and is dropped on the way
            # out anyway (see :func:`is_image_reference`).
            self._update(
                HOME_DETAILS,
                state or "DroidMirror",
                image or IDLE_IMAGE_URL or EMOJI_HOME,
                start,
                force,
            )
            return
        label = app_label(package)
        chosen = image or (
            EMOJI_GAME if package.startswith(_GAME_HINTS) else EMOJI_APP
        )
        self._update(label, state or "DroidMirror", chosen, start, force)

    def clear(self) -> None:
        """Hide the presence (called on disconnect)."""
        self.close()

    def close(self) -> None:
        if self._rpc is not None:
            try:
                self._rpc.clear()
                self._rpc.close()
            except Exception:
                pass
        self._rpc = None
        self._last = None