"""Icon machinery for the Discord presence -- and the cache it depends on.

**The upload half of this module is retired.** Discord draws an image only when
it is given a *url*; it fetches and proxies that itself. Measured against the
running client, which echoes back the ``large_image`` it accepted:

    "https://play-lh.googleusercontent.com/…"  ->  mp:external/<hash>/https/…
    "<asset id>"                                ->  None
    "<asset key>"                               ->  None

``mp:`` is the client reporting that it fetched the image; ``None`` is the
value being discarded, after which the card paints its grey "?" over the
application's icon. A locally drawn or extracted PNG has no url, so uploading
it to Discord buys nothing: the presence hands over a url from
:mod:`droidmirror.siteicons` and never downloads the image at all.

Everything below that exists to *obtain or make* a local PNG -- the APK
extraction, the upload, and the two drawn glyphs -- is consequently no longer
called by anything. It is left in place rather than deleted; the module is not
under version control, so removing it is a deliberate step rather than a
by-product of fixing the icon.

What is live, and the reason this module is still imported, is :class:`IconCache`:
it maps a package to its resolved reference (a url now) on disk, together with
its provenance, so each app is looked up once rather than on every poll.
"""

from __future__ import annotations

import hashlib
import json
import re
import struct
from pathlib import Path
from typing import TYPE_CHECKING
from urllib import request as urlrequest

if TYPE_CHECKING:
    from .adb import Adb

#: Discord's asset endpoints.
ASSET_URL = "https://discord.com/api/v10/applications/{app_id}/assets"

#: Step 1 of the upload: reserve a slot, get back a pre-signed upload url.
#: The sibling ``/assets/uploads`` route answers 405; this is the real one.
ASSET_UPLOAD_URL = (
    "https://discord.com/api/v10/applications/{app_id}/assets/upload"
)

#: Discord rejects a request without a versioned User-Agent (Cloudflare 1xxx).
USER_AGENT = "DiscordBot (https://github.com/, 1.0)"

#: Discord validates the asset key: 1-50 chars of this charset. Anything else
#: comes back as STRING_TYPE_REGEX / BASE_TYPE_BAD_LENGTH. Note there is no
#: dot -- "com.example.app" is rejected, "com-example-app" is accepted.
_ASSET_KEY_RE = re.compile(r"[a-z0-9_-]{1,50}")

#: The launcher icon is a square PNG; anything smaller is a notification dot
#: or a badge.
MIN_ICON_PX = 48
MAX_ICON_BYTES = 400_000

#: Launcher icons are shipped at these exact pixel sizes (the Android density
#: buckets). A square PNG matching one of them is almost certainly the real
#: icon -- while simply "the largest square" is often promotional artwork or a
#: splash image bundled in the same APK, which would put the *wrong* picture
#: on someone's profile.
ICON_SIZES = (432, 192, 144, 96, 72, 48)

#: How many entries to probe per APK. Each probe is a 24-byte read, so this
#: stays in the tens of kilobytes.
MAX_PROBES = 400

#: How far into the end of the APK we read for the central directory.
CENTRAL_DIR_TAIL = 1_048_576

#: adb's own exec-out keeps binary clean; a plain `shell` mangles some bytes.
_READ_TIMEOUT = 60.0


# --------------------------------------------------------------------------- #
# Pure ZIP parsing (testable with no device)
# --------------------------------------------------------------------------- #


def parse_central_directory(blob: bytes, base_offset: int = 0) -> dict[str, tuple[int, int]]:
    """``name -> (local header offset, size)`` for STORED entries.

    ``blob`` is only the *tail* of the file, so ``base_offset`` is the file
    position that ``blob[0]`` represents. The directory's own offset is
    absolute in the archive and has to be rebased onto the window we hold --
    without that, every lookup lands past the end of the buffer.

    STORED only: Android APKs deflate resources, and inflating them would
    mean shipping a decompressor -- but the launcher icons we want are stored
    uncompressed often enough, and we verify the PNG header before trusting
    anything, so a miss costs nothing but a fallback to the emoji.
    """
    idx = blob.rfind(b"PK\x05\x06")
    if idx < 0:
        return {}
    end = blob[idx : idx + 22]
    if len(end) < 22:
        return {}
    (count,) = struct.unpack("<H", end[10:12])
    (cd_size,) = struct.unpack("<I", end[12:16])
    (cd_off,) = struct.unpack("<I", end[16:20])
    if count == 0xFFFF or cd_off == 0xFFFFFFFF:
        return {}  # zip64 -- not produced for APKs this size
    start = cd_off - base_offset
    if start < 0 or start + cd_size > len(blob):
        return {}  # the directory is outside the window we were given

    entries: dict[str, tuple[int, int]] = {}
    pos = start
    while pos + 46 <= len(blob) and blob[pos : pos + 4] == b"PK\x01\x02":
        (method,) = struct.unpack("<H", blob[pos + 10 : pos + 12])
        (csize,) = struct.unpack("<I", blob[pos + 20 : pos + 24])
        nlen, elen, clen = struct.unpack("<HHH", blob[pos + 28 : pos + 34])
        (lho,) = struct.unpack("<I", blob[pos + 42 : pos + 46])
        name = blob[pos + 46 : pos + 46 + nlen].decode("utf-8", "replace")
        if method == 0 and 0 < csize <= MAX_ICON_BYTES:
            entries[name] = (lho, csize)
        pos += 46 + nlen + elen + clen
    return entries


def png_dimensions(data: bytes) -> tuple[int, int] | None:
    """(width, height) from a PNG header, or None if it is not a PNG."""
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    if data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def local_data_offset(header: bytes) -> int | None:
    """Where a local file header's payload starts (name+extra skipped)."""
    if len(header) < 30 or header[:4] != b"PK\x03\x04":
        return None
    nlen, elen = struct.unpack("<HH", header[26:30])
    return 30 + nlen + elen


def icon_score(dims: tuple[int, int] | None) -> tuple[int, int]:
    """Rank a probed PNG as a launcher icon. Higher is better.

    A square at a standard Android density size is the icon; a square at an
    odd size is probably artwork; anything not square is not an icon at all.
    Within the accepted sizes a *bigger* icon wins (192 over 72), which is
    what makes a phone look crisp in someone's Discord profile.
    """
    if not dims:
        return (0, 0)
    width, height = dims
    if width != height or width < MIN_ICON_PX:
        return (0, 0)
    if width in ICON_SIZES:
        return (2, width)
    return (1, width)


# --------------------------------------------------------------------------- #
# Device side
# --------------------------------------------------------------------------- #


def apk_paths(adb: "Adb", package: str) -> list[str]:
    """Every APK path the device holds for ``package`` (base + splits).

    ``run`` decodes to text normally, but a caller may hand back bytes; both
    are accepted here so the caller never has to care.
    """
    try:
        out = adb.run(
            ["shell", "pm", "path", package], check=False, timeout=15.0
        ).stdout or ""
    except Exception:
        return []
    if isinstance(out, bytes):
        out = out.decode("utf-8", "replace")
    return [
        line.split(":", 1)[1].strip()
        for line in out.splitlines()
        if line.startswith("package:") and ":" in line
    ]


def _remote(adb: "Adb", path: str, offset: int, count: int) -> bytes:
    """Read ``count`` bytes at ``offset`` from a file on the device.

    ``tail -c +N`` seeks straight to the offset and ``head -c`` caps the read,
    so both are O(1) in the file size -- ``dd bs=1 skip=N`` would be O(N), and
    a 240 MB APK would take minutes for a single read.
    """
    return _remote_script(
        adb,
        f"tail -c +{offset + 1} {path} 2>/dev/null | head -c {count}",
        count,
    )


def _remote_script(adb: "Adb", script: str, expected: int) -> bytes:
    """Run a shell script on the device and hand back its stdout verbatim."""
    try:
        data = (
            adb.run(
                ["exec-out", "sh", "-c", script],
                check=False,
                timeout=_READ_TIMEOUT,
                binary=True,
            )
            .stdout
            or b""
        )
    except Exception:
        return b""
    return data if isinstance(data, bytes) else str(data).encode("latin-1", "replace")


def _remote_size(adb: "Adb", path: str) -> int:
    try:
        out = (
            adb.run(
                ["shell", "stat", "-c", "%s", path], check=False, timeout=15.0
            ).stdout
            or ""
        ).strip()
        return int(out.split()[0]) if out.split() and out.split()[0].isdigit() else 0
    except Exception:
        return 0


def extract_icon_png(adb: "Adb", package: str) -> bytes | None:
    """The app's launcher icon as PNG bytes, or None when we cannot be sure.

    Only an entry that is *named* like an icon is accepted
    (``ic_launcher``/``mipmap``/``app_icon``). Modern release APKs obfuscate
    their resource names and ship adaptive icons composed from several layers,
    so pattern-matching "the biggest square PNG" is a coin flip -- it once
    picked a 48x48 tick mark, and before that a Meta Horizon promo picture,
    for Instagram. Being wrong in someone's Discord profile is worse than the
    emoji fallback, so an obfuscated APK simply yields None.
    """
    paths = apk_paths(adb, package)
    if not paths:
        return None
    ordered = [p for p in paths if "split_config" in p] + [
        p for p in paths if p.endswith("base.apk")
    ]

    for path in ordered:
        size = _remote_size(adb, path)
        if size <= 0:
            continue
        start = max(0, size - CENTRAL_DIR_TAIL)
        tail = _remote(adb, path, start, CENTRAL_DIR_TAIL)
        entries = parse_central_directory(tail, base_offset=start)
        if not entries:
            start = max(0, size - 4 * CENTRAL_DIR_TAIL)
            tail = _remote(adb, path, start, 4 * CENTRAL_DIR_TAIL)
            entries = parse_central_directory(tail, base_offset=start)
            if not entries:
                continue

        # Named icon entries only -- that is the whole correctness argument.
        names = [
            n
            for n in entries
            if n.lower().endswith(".png")
            and re.search(r"(ic_launcher|app_icon|mipmap)", n, re.I)
        ]
        if not names:
            continue  # obfuscated: do not guess
        # Prefer the biggest standard size present.
        names.sort(key=lambda n: -entries[n][1])
        for name in names[:MAX_PROBES]:
            offset, entry_size = entries[name]
            header = _remote(adb, path, offset, 30)
            data_at = local_data_offset(header)
            if data_at is None:
                continue
            probe = _remote(adb, path, offset + data_at, 24)
            score = icon_score(png_dimensions(probe))
            if score[0] < 2:
                continue  # not a standard-size square; keep looking
            blob = _remote(adb, path, offset + data_at, entry_size)
            if icon_score(png_dimensions(blob)) == score:
                return blob
    return None


# --------------------------------------------------------------------------- #
# Discord side
# --------------------------------------------------------------------------- #


def _asset_key(package: str, png: bytes | None = None) -> str:
    """A legal asset key derived from a package name (and the image itself).

    Discord is strict here: 1-50 characters from a narrow charset, and a dot
    is **not** in it -- a package name has to lose its dots or the upload is
    rejected with STRING_TYPE_REGEX before any byte is sent. (Found the hard
    way: ``droidmirror-home`` uploaded fine, ``com.instagram.android`` did
    not, and the error names no field.)

    The image's digest is folded in when we have it. Discord will not let a
    key be re-registered, so a key derived only from the package would pin
    whatever icon was uploaded *first* forever -- which is how a corrected
    icon could never replace an early bad one. Identical bytes give an
    identical key, so a correct icon is still uploaded exactly once.
    """
    cleaned = re.sub(r"[^a-z0-9_-]", "-", package.lower())[:40].strip("-_")
    if not cleaned or not _ASSET_KEY_RE.fullmatch(cleaned):
        cleaned = "icon"
    if png:
        cleaned = f"{cleaned}-{hashlib.sha1(png).hexdigest()[:8]}"[:50].strip("-_")
    return cleaned if _ASSET_KEY_RE.fullmatch(cleaned) else "icon"


def _existing_assets(
    client_id: str, token: str, timeout: float = 20.0
) -> dict[str, str]:
    """``key -> asset_id`` for every asset already on this application.

    One read serves two purposes, which is why they share a function:

    * **Idempotence.** Registering a key that already exists is an error, so
      without this a second run for the same app would fail forever -- and the
      on-disk cache is not something we can trust to survive a reinstall or a
      wiped icons folder.
    * **The id.** ``large_image`` needs the asset *id*, and for something
      already uploaded this list is the only place left to learn it.

    ``asset_id`` is the field the API returns today; ``id`` is accepted too
    because this object has been reshaped before. An asset missing both is
    skipped rather than guessed at.
    """
    req = urlrequest.Request(
        ASSET_URL.format(app_id=client_id),
        method="GET",
        headers={
            "Authorization": token,
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urlrequest.urlopen(req, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except Exception:
        return {}
    if not isinstance(payload, list):
        return {}
    found: dict[str, str] = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        key = item.get("key")
        asset_id = item.get("asset_id") or item.get("id")
        if isinstance(key, str) and isinstance(asset_id, str) and asset_id:
            found[key] = asset_id
    return found


def upload_icon(
    client_id: str, token: str, package: str, png: bytes, timeout: float = 20.0
) -> str | None:
    """Upload one PNG and return the asset **id** Discord assigns it.

    **Retired -- nothing calls this.** It is kept because the flow below is
    awkward to rediscover, not because it works: the reference it returns does
    not render. Handed to ``SET_ACTIVITY``, an asset id comes back from the
    running client as ``None``, i.e. dropped (see the module docstring).

    This is three requests, not one. The old shape -- a single ``PATCH
    /assets`` with a multipart body -- is gone; it answers 405 Method Not
    Allowed now. The current flow is:

    1. ``POST /assets/upload`` with ``{filename, file_size}`` reserves a slot
       and returns a pre-signed ``upload_url`` plus an ``upload_filename``.
    2. ``PUT upload_url`` sends the raw bytes. That url carries its own
       signature, so it must go out **without** an Authorization header.
    3. ``POST /assets`` with ``{file_name, upload_filename, key}`` registers it
       and returns the asset -- a snowflake ``asset_id``, the ``key`` we asked
       for, and a metadata/visibility block.

    The id, not the key, is the value worth holding, since the key is not even
    something Discord's CDN resolves:

        /app-assets/<app>/1560714379794579618.png  -> 200 image/png
        /app-assets/<app>/home-51417e8a.png        -> 404

    That CDN check is why this looked like the answer for so long. It is not:
    a 200 from the CDN says the image is stored, not that the presence layer
    will draw it.

    An already-registered key is answered from the asset list without
    re-uploading -- registering a key twice is an error -- and that same list
    is where the id comes from in that case, since an asset uploaded by an
    earlier version of this code only stored its key.

    None on any failure -- bad token, no network, API change. This was never a
    hard dependency, and now it is not a dependency at all.
    """
    key = _asset_key(package, png)
    existing = _existing_assets(client_id, token, timeout)
    if key in existing:
        return existing[key]

    name = f"{key}.png"
    headers = {
        "Authorization": token,
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }

    try:
        # 1. Reserve the upload slot.
        reserve = urlrequest.Request(
            ASSET_UPLOAD_URL.format(app_id=client_id),
            data=json.dumps(
                {"filename": name, "file_size": len(png)}
            ).encode(),
            method="POST",
            headers=headers,
        )
        with urlrequest.urlopen(reserve, timeout=timeout) as response:
            ticket = json.loads(response.read().decode("utf-8", "replace"))
        upload_url = ticket["upload_url"]
        upload_filename = ticket["upload_filename"]

        # 2. Send the bytes. Signed url: no Authorization header.
        put = urlrequest.Request(upload_url, data=png, method="PUT")
        with urlrequest.urlopen(put, timeout=max(timeout, 60.0)):
            pass

        # 3. Register it as an application asset.
        register = urlrequest.Request(
            ASSET_URL.format(app_id=client_id),
            data=json.dumps(
                {
                    "file_name": name,
                    "upload_filename": upload_filename,
                    "key": key,
                }
            ).encode(),
            method="POST",
            headers=headers,
        )
        with urlrequest.urlopen(register, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except Exception:
        return None

    if isinstance(payload, dict):
        asset_id = payload.get("asset_id") or payload.get("id")
        if isinstance(asset_id, str) and asset_id:
            return asset_id
    # Registration succeeded but answered with a shape we do not recognise.
    # Re-reading the list is the authoritative way to learn the id, and it
    # costs one request on an operation that runs once per app.
    return _existing_assets(client_id, token, timeout).get(key)


# --------------------------------------------------------------------------- #
# Cache: package -> Discord asset id, and the PNGs we already pulled
# --------------------------------------------------------------------------- #


class IconCache:
    """Remembers which Discord asset reference belongs to which package.

    Uploads are one-per-app-for-all-time, so this is the difference between a
    single API call ever and one call every few minutes.

    It also records *where* each icon came from. That matters because a
    generic drawn fallback must never be allowed to look like a finished
    result: the icon sources have improved over time (website favicons, then
    the Play store), and without provenance a stale fallback would be served
    from cache forever and the improvement would never reach an existing
    install. :meth:`get` therefore reports a fallback as a miss.
    """

    #: Icons drawn locally because nothing better was found. Treated as a miss
    #: by :meth:`get`, so they are re-resolved whenever a better source exists.
    FALLBACK_SOURCE = "fallback"

    #: Recorded for a cached entry whose origin is not being tracked. Trusted,
    #: unlike an *absent* source -- absence means "written before provenance
    #: existed", which is exactly the stale-fallback case worth re-resolving.
    UNKNOWN_SOURCE = "cached"

    def __init__(self, root: Path) -> None:
        self._root = root
        #: The map was called ``discord_icons.json`` while it held asset ids
        #: and keys, and this name is from when it held the ids. It now holds
        #: **urls**, which is the only shape that renders, and the ids left in
        #: it are refused as unusable rather than migrated -- by the caller,
        #: ``main_window._icon_url_for``, so the rule follows the value's
        #: meaning instead of the file's name. A rename makes the bulk
        #: invalidation unconditional, with no version stamp to forget to
        #: check; the PNGs beside it are simply no longer read.
        self._map_path = root / "discord_assets.json"
        self._map: dict[str, str] = {}
        self._sources_path = root / "icon_sources.json"
        self._sources: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self._map_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._map = {str(k): str(v) for k, v in raw.items()}
        except (OSError, ValueError):
            self._map = {}
        try:
            raw = json.loads(self._sources_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._sources = {str(k): str(v) for k, v in raw.items()}
        except (OSError, ValueError):
            self._sources = {}

    def _save(self) -> None:
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            self._map_path.write_text(
                json.dumps(self._map, indent=2), encoding="utf-8"
            )
        except OSError:
            pass  # a cache that cannot be written is just a cold cache

    def _save_sources(self) -> None:
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            self._sources_path.write_text(
                json.dumps(self._sources, indent=2), encoding="utf-8"
            )
        except OSError:
            pass

    def source(self, package: str) -> str:
        """Where this package's cached icon came from.

        Unknown packages -- and everything cached before provenance was
        recorded -- report the fallback, so they are re-resolved once.
        """
        return self._sources.get(package, self.FALLBACK_SOURCE)

    def is_fallback(self, package: str) -> bool:
        return self.source(package) == self.FALLBACK_SOURCE

    def get(self, package: str) -> str | None:
        """The cached asset reference, or None when nothing worth reusing."""
        if self.is_fallback(package):
            return None  # re-resolve instead of serving a stale fallback
        return self._map.get(package)

    def put(self, package: str, reference: str, source: str = "") -> None:
        source = source or self.UNKNOWN_SOURCE
        if (
            self._map.get(package) == reference
            and self._sources.get(package) == source
        ):
            return
        self._map[package] = reference
        self._sources[package] = source
        self._save()
        self._save_sources()

    def png_path(self, package: str) -> Path:
        return self._root / f"{package.replace('.', '_')}.png"

    def load_png(self, package: str) -> bytes | None:
        """The cached PNG, unless it is a fallback that could be done better.

        Same rule as :meth:`get`: a locally drawn fallback is not a finished
        answer, so it is reported as a miss and the real sources get a turn.
        Without this a single bad resolution was cached forever.
        """
        if self.is_fallback(package):
            return None
        try:
            return self.png_path(package).read_bytes()
        except OSError:
            return None

    def store_png(self, package: str, data: bytes, source: str = "") -> None:
        """Cache the PNG for ``package`` and record where it came from.

        The source is written here, not only in :meth:`put`, so any icon that
        went through this cache has provenance. An icon with *no* recorded
        source was written before provenance existed, and is treated as a
        fallback so it gets one chance to resolve properly.
        """
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            self.png_path(package).write_bytes(data)
        except OSError:
            pass
        self._sources[package] = source or self.UNKNOWN_SOURCE
        self._save_sources()


# --------------------------------------------------------------------------- #
# Built-in icons (the states that are not an app)
# --------------------------------------------------------------------------- #


def home_screen_icon(size: int = 256) -> bytes:
    """A drawn phone glyph, as PNG bytes.

    Discord only renders assets uploaded to its own CDN, so an emoji in
    ``large_image`` just earns a grey "?" placeholder. Drawing the icon here
    and uploading it once gives the home screen a real picture without
    needing an emoji to resolve.
    """
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QBrush, QColor, QImage, QPainter, QPen

    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    # Rounded backdrop so the glyph reads on Discord's dark and light cards.
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(QColor(58, 122, 216, 235)))
    inset = size * 0.06
    painter.drawRoundedRect(
        QRectF(inset, inset, size - 2 * inset, size - 2 * inset),
        size * 0.18,
        size * 0.18,
    )

    # The handset.
    body_w, body_h = size * 0.34, size * 0.56
    body_x = (size - body_w) / 2
    body_y = (size - body_h) / 2
    body = QRectF(body_x, body_y, body_w, body_h)
    painter.setBrush(QBrush(QColor(255, 255, 255)))
    painter.setPen(QPen(QColor(20, 20, 30), max(2.0, size * 0.012)))
    painter.drawRoundedRect(body, size * 0.09, size * 0.09)

    # Screen.
    screen = body.adjusted(body_w * 0.11, body_h * 0.11, -body_w * 0.11, -body_h * 0.20)
    painter.setBrush(QBrush(QColor(226, 236, 252)))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(screen, size * 0.02, size * 0.02)

    # Home bar.
    bar_w = body_w * 0.3
    painter.setBrush(QBrush(QColor(150, 160, 180)))
    painter.drawRoundedRect(
        QRectF(
            (size - bar_w) / 2,
            body.bottom() - body_h * 0.10,
            bar_w,
            max(2.0, size * 0.014),
        ),
        size * 0.01,
        size * 0.01,
    )
    painter.end()

    from PySide6.QtCore import QBuffer, QByteArray

    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QBuffer.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    buffer.close()
    return bytes(data)


def app_icon(size: int = 256) -> bytes:
    """A drawn app glyph, as PNG bytes.

    The fallback for apps whose real icon we cannot pull. A modern release APK
    obfuscates every resource name and folder (``res/9p.png``, not
    ``res/mipmap-hdpi/ic_launcher.png``) and ships an *adaptive* icon composed
    from layers that no single PNG contains, so there is genuinely no file in
    the APK that is "the" icon.

    Without a token the presence falls back to the emoji, which Discord cannot
    resolve, so the user sees its grey "?". Drawing a neutral app glyph and
    uploading it once means the picture is always deliberate -- and one
    deliberate icon beats Discord's placeholder on every screen.
    """
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QBrush, QColor, QImage, QPainter

    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(QColor(72, 78, 96, 235)))
    inset = size * 0.06
    painter.drawRoundedRect(
        QRectF(inset, inset, size - 2 * inset, size - 2 * inset),
        size * 0.18,
        size * 0.18,
    )

    # A 2x2 grid: reads as "an app" at 32px and never looks like the phone.
    tile = size * 0.26
    gap = size * 0.06
    block = tile * 2 + gap
    origin = (size - block) / 2
    painter.setBrush(QBrush(QColor(238, 241, 248)))
    for row in range(2):
        for col in range(2):
            painter.drawRoundedRect(
                QRectF(
                    origin + col * (tile + gap),
                    origin + row * (tile + gap),
                    tile,
                    tile,
                ),
                size * 0.07,
                size * 0.07,
            )
    painter.end()

    from PySide6.QtCore import QBuffer, QByteArray

    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QBuffer.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    buffer.close()
    return bytes(data)