"""App-icon extraction: ZIP parsing, honest selection, and the asset cache."""

from __future__ import annotations

import json
import re
import struct

import pytest

from droidmirror import appicons
from droidmirror.appicons import (
    IconCache,
    _asset_key,
    apk_paths,
    extract_icon_png,
    icon_score,
    local_data_offset,
    parse_central_directory,
    png_dimensions,
)
from droidmirror.discord_rpc import is_image_reference


def _png(width: int, height: int, payload: bytes = b"\x00") -> bytes:
    """A minimal but real PNG header (enough for the dimension probe)."""
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + ihdr
        + payload
    )


def _build_zip(entries: list[tuple[str, bytes]], base_offset: int = 0) -> bytes:
    """A STORED-only zip, plus the EOCD the parser expects.

    ``base_offset`` pretends the buffer starts that far into a file, which is
    exactly the situation when only the tail of an APK has been read.
    """
    body = b""
    directory = b""
    for name, data in entries:
        offset = base_offset + len(body)
        name_b = name.encode()
        body += (
            b"PK\x03\x04"
            + struct.pack("<HHH", 0, 0, 0)   # version, flags, method (stored)
            + struct.pack("<HH", 0, 0)       # mod time, mod date
            + struct.pack("<I", 0)            # crc
            + struct.pack("<II", len(data), len(data))
            + struct.pack("<HH", len(name_b), 0)  # name length, extra length
            + name_b + data
        )
        directory += (
            b"PK\x01\x02"
            + struct.pack("<HHH", 0, 0, 0)  # version made, version needed, flags
            + struct.pack("<H", 0)            # method: stored
            + struct.pack("<HH", 0, 0)        # mod time, mod date
            + struct.pack("<I", 0)            # crc
            + struct.pack("<II", len(data), len(data))
            + struct.pack("<HHH", len(name_b), 0, 0)  # name, extra, comment
            + struct.pack("<HH", 0, 0)        # disk, internal attrs
            + struct.pack("<I", 0)            # external attrs
            + struct.pack("<I", offset)       # local header offset
            + name_b
        )
    cd_off = base_offset + len(body)
    eocd = (
        b"PK\x05\x06" + struct.pack("<HHHH", 0, 0, len(entries), len(entries))
        + struct.pack("<II", len(directory), cd_off)
        + struct.pack("<H", 0)
    )
    return body + directory + eocd


# --------------------------------------------------------------------------- #
# ZIP parsing
# --------------------------------------------------------------------------- #


def test_central_directory_is_found_when_the_buffer_is_the_file_start():
    blob = _build_zip([("res/a.png", _png(192, 192, b"x" * 64))])
    entries = parse_central_directory(blob)

    assert "res/a.png" in entries
    assert entries["res/a.png"][1] == len(_png(192, 192, b"x" * 64))


def test_central_directory_is_found_in_a_tail_buffer():
    """The real case: only the last MB of a 200 MB APK is in hand."""
    # 100 KB of leading filler, then the entries -- so the directory's
    # absolute offset is far beyond the buffer.
    filler = b"\x00" * 100_000
    base = len(filler)
    blob = filler + _build_zip(
        [("res/ic_launcher.png", _png(96, 96, b"y" * 40))], base_offset=base
    )
    # Re-slice to the tail, as the device read would.
    tail = blob[base:]
    entries = parse_central_directory(tail, base_offset=base)

    assert "res/ic_launcher.png" in entries
    # The recorded offset is absolute, so it points into the real file rather
    # than into the slice we happen to be holding.
    assert entries["res/ic_launcher.png"][0] >= base


def test_a_directory_outside_the_window_is_refused():
    blob = _build_zip([("res/a.png", _png(48, 48))], base_offset=500_000)
    assert parse_central_directory(blob, base_offset=0) == {}


def test_a_buffer_with_no_end_record_is_refused():
    assert parse_central_directory(b"not a zip at all") == {}


def test_zip64_is_refused_rather_than_misread():
    blob = bytearray(_build_zip([("res/a.png", _png(48, 48))]))
    # Mark the entry-count/offset fields as the zip64 sentinels.
    blob[-12:-8] = struct.pack("<I", 0xFFFFFFFF)
    assert parse_central_directory(bytes(blob)) == {}


def test_local_data_offset_skips_the_name_and_extra():
    header = b"PK\x03\x04" + b"\x00" * 22 + struct.pack("<HH", 7, 3)
    assert local_data_offset(header) == 30 + 7 + 3


def test_a_bad_local_header_has_no_data_offset():
    assert local_data_offset(b"NOPE") is None
    assert local_data_offset(b"PK\x03\x04") is None


# --------------------------------------------------------------------------- #
# PNG probing + honest scoring
# --------------------------------------------------------------------------- #


def test_png_dimensions_reads_the_header():
    assert png_dimensions(_png(192, 192)) == (192, 192)


def test_png_dimensions_rejects_other_bytes():
    assert png_dimensions(b"") is None
    assert png_dimensions(b"\x89PNG\r\n\x1a\n" + b"\x00" * 4) is None
    assert png_dimensions(b"GIF89a" + b"\x00" * 32) is None


def test_standard_icon_sizes_are_accepted():
    for size in (48, 72, 96, 144, 192, 432):
        assert icon_score((size, size))[0] == 2, size


def test_odd_squares_score_below_standard_ones():
    assert icon_score((714, 660))[0] == 0, "not square"
    assert icon_score((200, 200))[0] == 1, "square but not an icon size"
    assert icon_score((32, 32))[0] == 0, "too small to be an icon"
    assert icon_score(None)[0] == 0


def test_bigger_standard_icons_rank_higher():
    assert icon_score((192, 192)) > icon_score((48, 48))


# --------------------------------------------------------------------------- #
# Extraction (device calls faked)
# --------------------------------------------------------------------------- #


class FakeFile:
    """One APK's bytes, served through the adb calls the code makes."""

    def __init__(self, entries: list[tuple[str, bytes]]) -> None:
        self.blob = _build_zip(entries)

    def read(self, offset: int, count: int) -> bytes:
        return self.blob[offset : offset + count]

    def size(self) -> int:
        return len(self.blob)


class FakeAdb:
    """Answers only the handful of commands extract_icon_png issues."""

    def __init__(self, files: dict[str, FakeFile]) -> None:
        self._files = files

    def run(self, args, **kwargs):
        joined = [str(a) for a in args]
        script = " ".join(joined)

        def out(data: bytes):
            class R:
                stdout = data

            return R()

        if "pm" in joined and "path" in joined:
            listed = "".join(f"package:{p}\n" for p in self._files)
            return out(listed.encode())
        if "stat" in joined:
            path = joined[-1]
            return out(str(self._files[path].size()).encode())
        if "exec-out" in joined:
            # The script is `tail -c +START PATH ... | head -c LENGTH`.
            match = re.search(
                r"tail -c \+(\d+) (\S+).*?head -c (\d+)", script, re.S
            )
            if match is None:
                return out(b"")
            start = int(match.group(1)) - 1
            path, length = match.group(2), int(match.group(3))
            return out(self._files[path].read(start, length))
        return out(b"")


def test_a_named_icon_entry_is_extracted():
    apk = "/data/app/x/base.apk"
    files = {apk: FakeFile([("res/mipmap-xxhdpi/ic_launcher.png",
                             _png(192, 192, b"icon" * 40))])}

    png = extract_icon_png(FakeAdb(files), "com.example.app")

    assert png is not None
    assert png_dimensions(png) == (192, 192)


def test_an_obfuscated_apk_yields_nothing_rather_than_a_guess():
    """Instagram-style: no entry is named like an icon, so we decline.

    Guessing here once produced a tick mark, and before that a promo picture,
    both under the app's name. None is the correct answer.
    """
    apk = "/data/app/x/base.apk"
    files = {apk: FakeFile([("res/a1b2.png", _png(192, 192, b"x" * 40))])}

    assert extract_icon_png(FakeAdb(files), "com.instagram.android") is None


def test_a_missing_apk_yields_nothing():
    assert extract_icon_png(FakeAdb({}), "com.example.app") is None


def test_apk_paths_are_parsed():
    class Result:
        stdout = "package:/data/app/a/base.apk\npackage:/data/app/a/split.apk\n"

    class FakeAdb:
        def run(self, args, **kwargs):
            return Result()

    assert apk_paths(FakeAdb(), "com.example.app") == [
        "/data/app/a/base.apk",
        "/data/app/a/split.apk",
    ]


# --------------------------------------------------------------------------- #
# Upload
# --------------------------------------------------------------------------- #


def test_an_asset_key_is_legal_but_is_not_renderable():
    """An upload returns two identifiers, and they are not interchangeable.

    Discord validates the ``key`` as a 1-50 char slug with *no dots* -- a
    dotted one is rejected outright as STRING_TYPE_REGEX -- so a package name
    has to lose its dots on the way in. But the key is only a *name*: the
    value ``large_image`` has to carry is the sibling ``asset_id``. Sending
    the key is accepted by the API and then renders as Discord's grey "?".
    """
    assert _asset_key("com.instagram.android") == "com-instagram-android"
    assert _asset_key("droidmirror-home") == "droidmirror-home"
    assert _asset_key("A.Weird..Package!!Name") == "a-weird--package--name"

    long_key = _asset_key("x" * 200)
    assert 1 <= len(long_key) <= 50

    for package in (
        "com.instagram.android",
        "com.google.android.youtube",
        "A..b!!",
        "x" * 200,
        "!!!",
    ):
        key = _asset_key(package)
        assert re.fullmatch(r"[a-z0-9_-]{1,50}", key), (package, key)
        assert "." not in key
        assert not is_image_reference(key), (package, key)


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        import json as _json

        return _json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_the_upload_follows_the_three_step_api(monkeypatch):
    """reserve -> PUT the bytes -> register.

    This is the shape Discord accepts today. The old one-shot
    ``PATCH /assets`` multipart now answers 405, which is exactly how the
    icons silently stopped appearing.
    """
    calls = []

    def fake_urlopen(request, timeout=None):
        calls.append((request.get_method(), request.full_url, request.data))
        if request.get_method() == "GET":
            return _FakeResponse([])  # nothing registered yet
        if request.full_url.endswith("/assets/upload"):
            return _FakeResponse(
                {
                    "upload_url": "https://cdn.example/signed",
                    "upload_filename": "uuid/droidmirror-home.png",
                }
            )
        return _FakeResponse(
            {
                "key": "droidmirror-home",
                "asset_id": "1560714379794579618",
                "visibility": "private",
            }
        )

    monkeypatch.setattr(appicons.urlrequest, "urlopen", fake_urlopen)

    got = appicons.upload_icon(
        "123", "token", "droidmirror-home", b"\x89PNG-fake"
    )

    # The id, not the key it was registered under: only the id resolves.
    assert got == "1560714379794579618"
    assert [c[0] for c in calls] == ["GET", "POST", "PUT", "POST"]
    assert calls[1][1].endswith("/applications/123/assets/upload")
    assert calls[2][1] == "https://cdn.example/signed"
    assert calls[3][1].endswith("/applications/123/assets")
    # the bytes go up raw, on a signed url, so no Authorization header
    assert calls[2][2] == b"\x89PNG-fake"


def test_an_unrecognised_register_response_falls_back_to_the_asset_list(
    monkeypatch,
):
    """The registered asset object has been reshaped before, so the id is
    re-read from the list rather than assumed -- one extra call, once per app.
    """
    calls = []

    def fake_urlopen(request, timeout=None):
        calls.append(request.get_method())
        if request.get_method() == "GET":
            # empty before the upload, the new asset after it
            return _FakeResponse(
                []
                if len([c for c in calls if c == "GET"]) == 1
                else [
                    {
                        "key": _asset_key("com.discord", b"png"),
                        "asset_id": "42",
                    }
                ]
            )
        if request.full_url.endswith("/assets/upload"):
            return _FakeResponse(
                {"upload_url": "https://cdn.example/signed", "upload_filename": "u"}
            )
        return _FakeResponse({"key": "com-discord"})  # no id anywhere

    monkeypatch.setattr(appicons.urlrequest, "urlopen", fake_urlopen)

    assert appicons.upload_icon("123", "token", "com.discord", b"png") == "42"


def test_an_already_registered_icon_is_not_uploaded_again(monkeypatch):
    """Re-registering a key is an error, so a second run must short-circuit.

    The list is also the only place an asset uploaded by an older build can
    still yield a usable id, since that build cached the key.
    """
    calls = []
    png = b"the-bytes"

    def fake_urlopen(request, timeout=None):
        calls.append(request.get_method())
        # the key Discord stored is the sanitised one plus the content digest
        return _FakeResponse(
            [{"key": _asset_key("com.discord", png), "asset_id": "1560714379794579618"}]
        )

    monkeypatch.setattr(appicons.urlrequest, "urlopen", fake_urlopen)

    assert (
        appicons.upload_icon("123", "token", "com.discord", png)
        == "1560714379794579618"
    )
    assert calls == ["GET"], "must stop after the lookup"


def test_the_asset_key_changes_when_the_image_changes():
    """A corrected icon must be able to replace an earlier bad upload.

    Discord refuses to re-register a key, so a key derived only from the
    package would pin whatever was uploaded first forever. Identical bytes
    still give an identical key, so a good icon is uploaded exactly once.
    """
    a = _asset_key("com.instagram.android", b"first-icon")
    b = _asset_key("com.instagram.android", b"second-icon")
    assert a != b, "a different image must not reuse the old key"
    assert _asset_key("com.instagram.android", b"same") == _asset_key(
        "com.instagram.android", b"same"
    ), "identical bytes must still collapse to one key"


def test_the_asset_key_is_still_legal_after_hashing():
    import re as _re

    for package in ("com.instagram.android", "com.a", "x", "com.supercell.clashofclans"):
        key = _asset_key(package, b"png-bytes")
        assert _re.fullmatch(r"[a-z0-9_-]{1,50}", key), f"illegal key {key!r}"


def test_a_failed_upload_returns_nothing_rather_than_raising(monkeypatch):
    def boom(request, timeout=None):
        raise OSError("network down")

    monkeypatch.setattr(appicons.urlrequest, "urlopen", boom)
    assert appicons.upload_icon("123", "token", "com.discord", b"png") is None


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #


def test_the_cache_remembers_the_uploaded_asset(tmp_path):
    cache = IconCache(tmp_path)
    assert cache.get("com.discord") is None

    cache.put("com.discord", "1560714379794579618")

    assert cache.get("com.discord") == "1560714379794579618"
    # A fresh instance reads it back -- this is what makes it once-per-app.
    assert IconCache(tmp_path).get("com.discord") == "1560714379794579618"


def test_the_cache_survives_a_corrupt_file(tmp_path):
    (tmp_path / "discord_assets.json").write_text("{not json", encoding="utf-8")
    assert IconCache(tmp_path).get("com.discord") is None


def test_cached_asset_keys_from_the_old_format_are_abandoned(tmp_path):
    """The map used to hold asset *keys*, every one of which is a 404.

    Reusing them would pin the grey "?" forever, so the old file has to be
    ignored rather than merged. What must survive is everything else: the
    PNG and its provenance live in their own files, so abandoning the map
    costs one asset-list read per package and no re-fetch at all.
    """
    cache = IconCache(tmp_path)
    cache.store_png("com.discord", b"a-real-icon", source="web")
    (tmp_path / "discord_icons.json").write_text(
        json.dumps({"com.discord": "com-discord-a5d0a956"}), encoding="utf-8"
    )

    reopened = IconCache(tmp_path)

    assert reopened.get("com.discord") is None, "the old key must not be reused"
    assert not reopened.is_fallback("com.discord")
    assert reopened.load_png("com.discord") == b"a-real-icon", (
        "the icon itself is still good -- only the reference has to be redone"
    )

    reopened.put("com.discord", "1560714379794579618")
    assert IconCache(tmp_path).get("com.discord") == "1560714379794579618"


def test_pngs_are_stored_and_loaded(tmp_path):
    cache = IconCache(tmp_path)
    assert cache.load_png("com.discord") is None

    cache.store_png("com.discord", _png(192, 192, b"z" * 32))

    assert png_dimensions(cache.load_png("com.discord")) == (192, 192)


def test_an_unwritable_cache_is_only_a_cold_cache(tmp_path, monkeypatch):
    """A read-only checkout must not break anything -- the map just goes cold."""
    cache = IconCache(tmp_path)

    def boom(*_a, **_k):
        raise OSError("read-only file system")

    monkeypatch.setattr("pathlib.Path.write_text", boom)
    cache.put("com.discord", "https://cdn/x.png")  # must not raise

    assert cache.get("com.discord") == "https://cdn/x.png"

def _png_bytes(width: int, height: int) -> bytes:
    """A structurally valid PNG of the given size."""
    import struct as _struct
    import zlib as _zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        raw = kind + data
        return (
            _struct.pack(">I", len(data))
            + raw
            + _struct.pack(">I", _zlib.crc32(raw) & 0xFFFFFFFF)
        )

    ihdr = _struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IEND", b"")

def test_an_icon_with_no_recorded_source_is_treated_as_stale(tmp_path):
    """Anything written before provenance existed gets one chance to redo."""
    cache = appicons.IconCache(tmp_path)
    # Simulate the old on-disk state: a PNG and a url, no provenance file.
    cache.png_path("com.instagram.android").write_bytes(b"stale")
    cache._map["com.instagram.android"] = "cdn-key"
    cache._save()
    assert cache.source("com.instagram.android") == IconCache.FALLBACK_SOURCE
    assert cache.is_fallback("com.instagram.android")
    assert cache.get("com.instagram.android") is None


def test_an_entry_written_without_a_source_is_still_trusted(tmp_path):
    """put()/store_png() with no explicit source means "from somewhere good"."""
    cache = appicons.IconCache(tmp_path)
    cache.store_png("com.discord", b"png-bytes")
    cache.put("com.discord", "cdn-key")
    assert not cache.is_fallback("com.discord")
    assert cache.get("com.discord") == "cdn-key"
    assert cache.load_png("com.discord") == b"png-bytes"
