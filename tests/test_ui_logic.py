"""Tests for the pure UI logic: mirror layout maths and key naming."""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt

from droidmirror.ui import keys, layout
from droidmirror.ui.layout import Rect

# --------------------------------------------------------------------------- #
# Layout
# --------------------------------------------------------------------------- #


def test_fit_rect_letterboxes_a_wide_frame_in_a_tall_view():
    r = layout.fit_rect(1920, 1080, 800, 800)
    assert r.w == pytest.approx(800)
    assert r.h == pytest.approx(450)
    assert r.x == pytest.approx(0)
    assert r.y == pytest.approx(175)


def test_fit_rect_letterboxes_a_tall_frame_in_a_wide_view():
    r = layout.fit_rect(1080, 1920, 800, 600)
    assert r.h == pytest.approx(600)
    assert r.w == pytest.approx(337.5)
    assert r.y == pytest.approx(0)
    assert r.x == pytest.approx(231.25)


def test_fit_rect_is_the_identity_when_aspects_match():
    r = layout.fit_rect(100, 50, 400, 200)
    assert (r.x, r.y, r.w, r.h) == (0.0, 0.0, 400.0, 200.0)


def test_fit_rect_degrades_gracefully_on_bad_input():
    assert layout.fit_rect(0, 0, 100, 50) == Rect(0, 0, 100, 50)


def test_point_roundtrip_through_normalised_space():
    r = layout.fit_rect(1000, 1000, 500, 500)
    for px, py in [(0, 0), (250, 250), (499, 499), (123, 456)]:
        nx, ny = layout.point_to_normalised(px, py, r)
        bx, by = layout.normalised_to_point(nx, ny, r)
        assert (bx, by) == pytest.approx((px, py))


def test_normalised_coordinates_are_clamped_to_the_rect():
    r = Rect(10, 10, 100, 100)
    assert layout.point_to_normalised(-500, -500, r) == (0.0, 0.0)
    assert layout.point_to_normalised(9999, 9999, r) == (1.0, 1.0)


def test_corners_map_to_frame_extremes():
    r = layout.fit_rect(100, 200, 100, 200)
    assert layout.point_to_frame(0, 0, r, 100, 200) == (0, 0)
    assert layout.point_to_frame(100, 200, r, 100, 200) == (99, 199)


def test_point_to_frame_never_leaves_the_frame():
    r = layout.fit_rect(50, 50, 400, 400)
    for px, py in [(-9999, -9999), (9999, 9999), (0, 0), (400, 400)]:
        x, y = layout.point_to_frame(px, py, r, 50, 50)
        assert 0 <= x <= 49
        assert 0 <= y <= 49


def test_contains():
    r = Rect(0, 0, 10, 10)
    assert layout.contains(r, 5, 5)
    assert layout.contains(r, 0, 0)
    assert layout.contains(r, 10, 10)
    assert not layout.contains(r, 11, 5)
    assert not layout.contains(r, 5, -1)


def test_rect_helpers():
    r = Rect(2, 3, 10, 20)
    assert (r.right, r.bottom) == (12, 23)
    assert r.center == (7, 13)


# --------------------------------------------------------------------------- #
# Key naming
# --------------------------------------------------------------------------- #


def test_letters_and_digits():
    assert keys.key_name(Qt.Key.Key_W) == "w"
    assert keys.key_name(Qt.Key.Key_A) == "a"
    assert keys.key_name(Qt.Key.Key_0) == "0"
    assert keys.key_name(Qt.Key.Key_9) == "9"


def test_special_keys():
    assert keys.key_name(Qt.Key.Key_Space) == "space"
    assert keys.key_name(Qt.Key.Key_Return) == "enter"
    assert keys.key_name(Qt.Key.Key_Up) == "up"
    assert keys.key_name(Qt.Key.Key_Escape) == "escape"
    assert keys.key_name(Qt.Key.Key_F5) == "f5"
    assert keys.key_name(Qt.Key.Key_F12) == "f12"


def test_punctuation_uses_the_produced_text():
    assert keys.key_name(Qt.Key.Key_Comma, ",") == ","
    assert keys.key_name(Qt.Key.Key_Period, ".") == "."


def test_unmappable_keys_return_none():
    assert keys.key_name(0, "") is None
    assert keys.key_name(Qt.Key.Key_unknown, "") is None


def test_modifiers_are_not_bindable():
    assert not keys.is_bindable("shift")
    assert not keys.is_bindable("ctrl")
    assert not keys.is_bindable("alt")
    assert not keys.is_bindable(None)
    assert keys.is_bindable("w")
    assert keys.is_bindable("space")


def test_pretty_name():
    assert keys.pretty_name("w") == "W"
    assert keys.pretty_name("space") == "Space"
    assert keys.pretty_name("pageup") == "Pageup"
