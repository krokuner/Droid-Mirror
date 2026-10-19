"""Fetch an app's real icon off the internet.

Why this exists
---------------
The APK on the device is the "obvious" source, and for an ordinary app it
works: read the ZIP central directory, find ``res/mipmap-*/ic_launcher.png``,
pull it. But a modern release build obfuscates its resource names -- Meta's
bundler rewrites them to ``res/-e.png``, ``res/-f.png`` and so on. On a real
Instagram APK that leaves **zero** entries matching a name filter, and the
icon sits among 147 square PNGs with nothing to tell them apart. Reading the
compiled manifest and resource table instead is possible, but it is a lot of
binary-format code to maintain for a cosmetic detail.

Two web sources do the job far better, in order:

1. **Google Play.** Its store page carries the app's real launcher icon as an
   ``og:image`` tag, at 512x512. This is the *actual* icon the phone draws,
   not a lookalike, and it exists for every app that is on Play -- which is
   almost all of them.
2. **The site's favicon**, via Google's public favicon service. Catches the
   apps Play does not have, and anything sideloaded.

Both come in two shapes: as **bytes** (:func:`icon_for_package`, for callers
that need the image) and as a **url** (:func:`icon_url_for_package`). The
presence wants the url -- Discord fetches and proxies an external image on its
own, so nothing has to be downloaded here or hosted anywhere.

Only a package id (source 1) or a bare domain name (source 2) ever leaves the
machine. Nothing about the device, the user or the session is sent.

What this is not
----------------
Nothing here guesses. Every source either returns a real image or says no, and
the caller falls through to the next one.
"""

from __future__ import annotations

import re
import struct
from urllib import request as urlrequest

#: The Play store page for an app. Its <meta property="og:image"> is the real
#: launcher icon at 512x512.
PLAY_URL = (
    "https://play.google.com/store/apps/details?id={package}&hl=en&gl=us"
)

#: Google's public favicon service. Documented, free, no API key, and returns
#: 404 for a domain that has no icon -- which is what makes it safe to try
#: generically.
FAVICON_URL = "https://www.google.com/s2/favicons?domain={domain}&sz={size}"

#: Kept for callers that only care about the favicon path.
ICON_URL = FAVICON_URL

#: Discord's ``large_image`` wants at least 128px.
ICON_SIZE = 128

#: Below this, the result is a tracking pixel rather than an icon.
MIN_ICON_PX = 64

#: A Play store page is a ~1.2 MB bundle of inline data, and the og:image tag
#: sits at roughly the 940 kB mark -- not in <head> as you would hope. So the
#: cap has to be generous; it exists only to stop a bad package id pulling an
#: unbounded response, not to save bandwidth. The result is cached forever.
PLAY_READ_CAP = 4_000_000

#: Browser-ish User-Agent. Play and Google both serve a degraded page to an
#: obvious script, which would look exactly like "this app does not exist".
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

#: Seconds before a request is considered failed. Icons are cosmetic, so this
#: is deliberately short: a slow network must not stall the presence.
TIMEOUT_S = 8.0

#: How many domains to try before giving up on this app. A miss is a ~500ms
#: round trip, so walking every candidate for a package with no website would
#: cost seconds for nothing.
MAX_ATTEMPTS = 4

_OG_IMAGE_RE = re.compile(r'<meta\s+property="og:image"\s+content="([^"]+)"')

#: Package id -> the site whose logo represents it. Only needed where the
#: package name alone does not lead to the right domain (see
#: :func:`domain_candidates`). Keep this small and put the obvious ones here.
APP_DOMAINS: dict[str, str] = {
    "com.instagram.android": "instagram.com",
    "com.google.android.youtube": "youtube.com",
    "com.google.android.apps.youtube.tv": "youtube.com",
    "com.google.android.apps.youtube.vr.oculus": "youtube.com",
    "com.android.chrome": "chrome.google.com",
    "com.chrome.beta": "chrome.google.com",
    "com.google.android.gm": "mail.google.com",
    "com.microsoft.emmx": "microsoft.com",
    "com.facebook.katana": "facebook.com",
    "com.twitter.android": "x.com",
    "com.reddit.frontpage": "reddit.com",
    "com.linkedin.android": "linkedin.com",
    "com.spotify.music": "spotify.com",
    "com.netflix.mediaclient": "netflix.com",
    "com.amazon.avod": "amazon.com",
    "com.whatsapp": "whatsapp.com",
    "org.telegram.messenger": "telegram.org",
    "org.telegram.messenger.web": "web.telegram.org",
    "com.discord": "discord.com",
    "com.roblox.client": "roblox.com",
    "com.mojang.minecraftpe": "minecraft.net",
    "com.miHoYo.GenshinImpact": "genshin.hoyoverse.com",
    "com.miHoYo.Nap": "honkai-star-rail.hoyoverse.com",
    "com.HoYoverse.hkrpgoversea": "honkaiimpact3.hoyoverse.com",
    "com.pubg.krmobile": "pubgmobile.com",
    "com.tencent.ig": "pubgmobile.com",
    "com.activision.callofduty.shooter": "callofduty.com",
    "com.garena.game.codm": "callofduty.com",
    "com.dts.freefireth": "ff.garena.com",
    "com.dts.freefiremax": "ff.garena.com",
    "com.ea.gp.fifamobile": "ea.com",
    "com.dxco.pandavszombies": "pvz.com",
    "com.supercell.clashofclans": "clashofclans.com",
    "com.supercell.clashroyale": "clashroyale.com",
    "com.supercell.brawlstars": "brawlstars.com",
    "com.nianticlabs.pokemongo": "pokemongo.com",
    "com.microsoft.office.outlook": "outlook.com",
    "com.microsoft.teams": "teams.microsoft.com",
    "com.adobe.reader": "adobe.com",
    "com.obsproject.app": "obsproject.com",
    "com.sec.android.gallery3d": "samsung.com",
    "com.android.gallery3d": "google.com",
}

#: Package segments that describe the *vendor* or the APK shape rather than
#: the product, so they never make a good domain on their own.
_GENERIC_SEGMENTS = frozenset(
    {
        "android", "app", "apps", "client", "mobile", "core", "main", "ui",
        "launcher", "prod", "release", "def", "beta", "alpha", "dev", "debug",
        "com", "org", "net", "io", "co", "me", "tv", "wear", "auto", "common",
        "base", "shell", "system", "framework", "library", "modules",
    }
)


def domain_candidates(package: str) -> list[str]:
    """Web domains worth trying for ``package``, most likely first.

    A package id is not a domain, but its segments usually name the product,
    so ``com.roblox.client`` gives ``roblox.com``. Returns a de-duplicated,
    ordered list -- possibly empty, in which case the caller skips this path.
    """
    package = (package or "").strip()
    if not package:
        return []

    ordered: list[str] = []
    explicit = APP_DOMAINS.get(package)
    if explicit:
        ordered.append(explicit)

    segments = [s for s in package.lower().replace("_", "").split(".") if s]
    # Distinctive segments first: longer names are far more likely to be the
    # product than short ones like "app" or "tv".
    usable = [s for s in segments if s not in _GENERIC_SEGMENTS]
    usable.sort(key=len, reverse=True)

    for segment in usable:
        for suffix in (".com", ".io", ".app", ".net"):
            candidate = segment + suffix
            if candidate not in ordered:
                ordered.append(candidate)

    return ordered


def png_dimensions(data: bytes) -> tuple[int, int] | None:
    """(width, height) from a PNG header, or None if ``data`` is not a PNG."""
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return struct.unpack(">II", data[16:24])


def _fetch(
    url: str, timeout: float = TIMEOUT_S, cap: int | None = None
) -> bytes | None:
    """GET ``url``, reading at most ``cap`` bytes. None on any failure."""
    try:
        request = urlrequest.Request(url, headers={"User-Agent": USER_AGENT})
        with urlrequest.urlopen(request, timeout=timeout) as response:
            if cap is None:
                return response.read()
            chunks: list[bytes] = []
            remaining = cap
            while remaining > 0:
                chunk = response.read(min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            return b"".join(chunks)
    except Exception:
        return None


def _valid_png(data: bytes | None) -> bytes | None:
    """``data`` if it is a PNG big enough to be an icon, else None."""
    if not data:
        return None
    dims = png_dimensions(data)
    if dims is None:
        return None
    if dims[0] < MIN_ICON_PX or dims[1] < MIN_ICON_PX:
        return None  # a tracking pixel, not an icon
    return data


def play_store_icon_url(package: str, timeout: float = TIMEOUT_S) -> str | None:
    """The url of the app's real launcher icon on Play, or None.

    One request -- the store page -- and no image is downloaded. That is the
    point: the caller often does not want the bytes at all, because a *remote*
    client will fetch the picture itself (see
    :func:`icon_url_for_package`).
    """
    package = (package or "").strip()
    if not package:
        return None
    html = _fetch(
        PLAY_URL.format(package=package), timeout=timeout, cap=PLAY_READ_CAP
    )
    if not html:
        return None
    match = _OG_IMAGE_RE.search(html.decode("utf-8", "replace"))
    if not match:
        return None  # not on Play (or the page changed shape)
    image_url = match.group(1).replace("&amp;", "&")
    return image_url if image_url.startswith("http") else None


def site_icon_url(domain: str, size: int = ICON_SIZE) -> str:
    """The url Google's favicon service serves ``domain``'s icon at.

    Built, not fetched -- pair it with :func:`fetch_site_icon` to find out
    whether there is an icon behind it before showing it to anyone.
    """
    return FAVICON_URL.format(domain=domain, size=size)


def icon_url_for_package(
    package: str,
    size: int = ICON_SIZE,
    timeout: float = TIMEOUT_S,
    max_attempts: int = MAX_ATTEMPTS,
) -> str | None:
    """A url that will draw ``package``'s icon, or None.

    This is the form the presence actually wants. Discord does not need the
    image bytes and does not need it uploaded anywhere: it proxies a url
    itself (see ``discord_rpc._is_image_reference``), which is why nothing
    here has to be downloaded, stored or authenticated.

    Play first -- real icon, every app that is on it -- then the site favicon,
    but only once that favicon has been fetched and confirmed, because a url
    that 404s draws as a broken image and that is worse than drawing nothing.
    """
    url = play_store_icon_url(package, timeout=timeout)
    if url is not None:
        return url
    for domain in domain_candidates(package)[:max_attempts]:
        if fetch_site_icon(domain, size=size, timeout=timeout) is not None:
            return site_icon_url(domain, size=size)
    return None


def play_store_icon(
    package: str, timeout: float = TIMEOUT_S
) -> bytes | None:
    """The app's real launcher icon from its Google Play page, or None.

    This is the exact icon the phone draws, at 512x512. Two requests: the store
    page for the ``og:image`` tag, then the image itself.
    """
    url = play_store_icon_url(package, timeout=timeout)
    if url is None:
        return None
    return _valid_png(_fetch(url, timeout=timeout))


def fetch_site_icon(
    domain: str, size: int = ICON_SIZE, timeout: float = TIMEOUT_S
) -> bytes | None:
    """PNG bytes for ``domain``'s favicon, or None when there isn't one.

    None covers every failure mode -- no icon at the domain, a network error,
    a non-PNG response, something too small to be an icon -- because an icon
    is cosmetic and the caller always has a drawn fallback.
    """
    data = _fetch(FAVICON_URL.format(domain=domain, size=size), timeout=timeout)
    return _valid_png(data)


def icon_for_package(
    package: str,
    size: int = ICON_SIZE,
    timeout: float = TIMEOUT_S,
    max_attempts: int = MAX_ATTEMPTS,
) -> bytes | None:
    """The best icon obtainable for ``package``, or None.

    Play first -- real icon, every app that is on it -- then the app's website
    favicon for anything else. Each result is cached by the caller, so this
    costs two or three requests the first time and nothing after.
    """
    png = play_store_icon(package, timeout=timeout)
    if png is not None:
        return png
    for domain in domain_candidates(package)[:max_attempts]:
        data = fetch_site_icon(domain, size=size, timeout=timeout)
        if data is not None:
            return data
    return None