"""Site-icon lookup: domain derivation and response validation.

These are the pure parts. The HTTP call itself is stubbed -- a test suite that
depended on Google's favicon service being up would be a liability, not a test.
"""

from __future__ import annotations

import io
import struct
import zlib

import pytest

from droidmirror import siteicons
from droidmirror.siteicons import (
    APP_DOMAINS,
    MAX_ATTEMPTS,
    PLAY_READ_CAP,
    domain_candidates,
    fetch_site_icon,
    icon_for_package,
    play_store_icon,
    png_dimensions,
)


def _png(width: int, height: int) -> bytes:
    """A structurally valid PNG header (enough for png_dimensions)."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        raw = kind + data
        return (
            struct.pack(">I", len(data))
            + raw
            + struct.pack(">I", zlib.crc32(raw) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IEND", b"")


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# -- domain derivation ------------------------------------------------------ #


def test_curated_domains_win_over_derived_ones():
    """An explicit mapping must be the first candidate, not just one of many."""
    candidates = domain_candidates("com.instagram.android")
    assert candidates[0] == "instagram.com"


def test_vendor_and_apk_segments_are_not_tried_as_domains():
    """"com", "android", "client" name the shape of the APK, not the product."""
    candidates = domain_candidates("com.roblox.client")
    assert candidates[0] == "roblox.com"
    for noise in ("com.", "android.", "client."):
        assert not any(c.startswith(noise) for c in candidates)


def test_the_product_segment_beats_the_vendor_segment():
    candidates = domain_candidates("com.supercell.clashofclans")
    assert candidates[0] == "clashofclans.com"


def test_no_package_yields_no_candidates():
    assert domain_candidates("") == []
    assert domain_candidates("   ") == []
    assert domain_candidates("com.android") == []


def test_candidates_are_unique():
    candidates = domain_candidates("com.instagram.android")
    assert len(candidates) == len(set(candidates))


def test_every_curated_domain_is_reachable_from_its_package():
    """The map must not contain an entry domain_candidates would ignore."""
    for package, domain in APP_DOMAINS.items():
        assert domain in domain_candidates(package), (
            f"{package} -> {domain} is unreachable"
        )


# -- response validation ---------------------------------------------------- #


def test_png_dimensions_reads_a_header():
    assert png_dimensions(_png(128, 128)) == (128, 128)
    assert png_dimensions(_png(64, 32)) == (64, 32)


def test_png_dimensions_rejects_non_png():
    assert png_dimensions(b"") is None
    assert png_dimensions(b"GIF89a" + b"\x00" * 32) is None
    assert png_dimensions(b"\x89PNG\r\n\x1a\n" + b"\x00" * 10) is None


def test_fetch_accepts_a_real_icon(monkeypatch):
    body = _png(128, 128)
    monkeypatch.setattr(siteicons.urlrequest, "urlopen", lambda *_a, **_k: FakeResponse(body))
    assert fetch_site_icon("instagram.com") == body


def test_fetch_rejects_a_tracking_pixel(monkeypatch):
    """A 1x1 pixel is a placeholder, not an icon."""
    monkeypatch.setattr(siteicons.urlrequest, "urlopen", lambda *_a, **_k: FakeResponse(_png(1, 1)))
    assert fetch_site_icon("instagram.com") is None


def test_fetch_rejects_a_non_png(monkeypatch):
    monkeypatch.setattr(siteicons.urlrequest, "urlopen", lambda *_a, **_k: FakeResponse(b"<html>404</html>"))
    assert fetch_site_icon("instagram.com") is None


def test_fetch_swallows_a_network_error(monkeypatch):
    """An icon is cosmetic; no network must never raise into the worker."""

    def boom(*_a, **_k):
        raise OSError("network unreachable")

    monkeypatch.setattr(siteicons.urlrequest, "urlopen", boom)
    assert fetch_site_icon("instagram.com") is None


def test_fetch_asks_for_the_url_it_documents(monkeypatch):
    seen = {}

    def capture(request, **_k):
        # urlopen is handed a Request, so read the URL back off it.
        seen["url"] = request.full_url
        return FakeResponse(_png(128, 128))

    monkeypatch.setattr(siteicons.urlrequest, "urlopen", capture)
    fetch_site_icon("roblox.com", size=128)
    assert "roblox.com" in seen["url"]
    assert "sz=128" in seen["url"]


# -- the Play store source -------------------------------------------------- #


def test_play_store_returns_the_real_icon(monkeypatch):
    """The store page carries the actual launcher icon as its og:image."""
    body = _png(512, 512)
    html = (
        b'<!doctype html><html><head><meta property="og:image" '
        b'content="https://play-lh.googleusercontent.com/abc123"></head></html>'
    )
    calls = []

    def fake_fetch(url, **_k):
        calls.append(url)
        return html if "play.google.com" in url else body

    monkeypatch.setattr(siteicons, "_fetch", fake_fetch)
    assert play_store_icon("com.instagram.android") == body
    assert any("id=com.instagram.android" in c for c in calls)
    assert "play-lh.googleusercontent.com/abc123" in calls[-1]


def test_play_store_returns_none_for_a_missing_app(monkeypatch):
    monkeypatch.setattr(siteicons, "_fetch", lambda *_a, **_k: b"<html>nope</html>")
    assert play_store_icon("com.example.doesnotexist") is None


def test_play_store_tolerates_a_network_error(monkeypatch):
    monkeypatch.setattr(siteicons, "_fetch", lambda *_a, **_k: None)
    assert play_store_icon("com.instagram.android") is None


def test_play_store_ignores_an_empty_package(monkeypatch):
    monkeypatch.setattr(siteicons, "_fetch", lambda *_a, **_k: b"")
    assert play_store_icon("") is None
    assert play_store_icon("   ") is None


def test_play_store_rejects_a_non_image(monkeypatch):
    """og:image pointing at something that is not an icon must not be used."""
    html = (
        b'<html><meta property="og:image" content="https://example.com/logo.png">'
        b"</html>"
    )

    def fake_fetch(url, **_k):
        return html if "play.google.com" in url else b"<html>not an image</html>"

    monkeypatch.setattr(siteicons, "_fetch", fake_fetch)
    assert play_store_icon("com.example.app") is None


def test_play_read_cap_covers_a_real_store_page():
    """Regression: the cap once cut the page off before og:image.

    A Play store page is ~1.2 MB and the og:image tag sits around byte 941 kB,
    not up in <head>. A 400 kB cap made every lookup fail while the page was
    obviously fine.
    """
    assert PLAY_READ_CAP >= 1_200_000


# -- the composite lookup --------------------------------------------------- #


def test_play_is_tried_before_the_favicon(monkeypatch):
    """Play has the real icon and covers the favicon-service gaps."""
    order = []
    play_png = _png(512, 512)

    def fake_play(package, **_k):
        order.append(("play", package))
        return play_png

    monkeypatch.setattr(siteicons, "play_store_icon", fake_play)
    assert icon_for_package("com.instagram.android") == play_png
    assert order == [("play", "com.instagram.android")]


def test_favicon_is_the_fallback_when_play_has_nothing(monkeypatch):
    favicon = _png(128, 128)
    monkeypatch.setattr(siteicons, "play_store_icon", lambda *_a, **_k: None)

    def fake_fetch(domain, **_k):
        return favicon if domain == "roblox.com" else None

    monkeypatch.setattr(siteicons, "fetch_site_icon", fake_fetch)
    assert icon_for_package("com.roblox.client") == favicon


def test_first_hit_wins(monkeypatch):
    body = _png(128, 128)
    tried = []

    monkeypatch.setattr(siteicons, "play_store_icon", lambda *_a, **_k: None)

    def fake_fetch(domain, **_k):
        tried.append(domain)
        return body if domain == "roblox.com" else None

    monkeypatch.setattr(siteicons, "fetch_site_icon", fake_fetch)
    assert icon_for_package("com.roblox.client") == body
    assert tried == ["roblox.com"]


def test_a_package_with_no_website_gives_up_cleanly(monkeypatch):
    monkeypatch.setattr(siteicons, "play_store_icon", lambda *_a, **_k: None)
    monkeypatch.setattr(siteicons, "fetch_site_icon", lambda *_a, **_k: None)
    assert icon_for_package("com.example.totallyunknownthing") is None


def test_attempts_are_capped(monkeypatch):
    """Every miss is a round trip; a hopeless package must not cost seconds."""
    tried = []
    monkeypatch.setattr(siteicons, "play_store_icon", lambda *_a, **_k: None)
    monkeypatch.setattr(siteicons, "fetch_site_icon", lambda d, **_k: tried.append(d))
    icon_for_package("com.example.totallyunknownthing")
    assert len(tried) == MAX_ATTEMPTS


def test_a_mapped_domain_can_still_miss(monkeypatch):
    """A curated entry is a hint, not a promise -- the service 404s for some."""
    monkeypatch.setattr(siteicons, "play_store_icon", lambda *_a, **_k: None)
    monkeypatch.setattr(siteicons, "fetch_site_icon", lambda *_a, **_k: None)
    assert icon_for_package("com.supercell.clashofclans") is None


def test_helpers_are_importable():
    assert callable(icon_for_package)
    assert callable(fetch_site_icon)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))