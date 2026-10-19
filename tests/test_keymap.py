"""Keybind model, persistence and injection-engine tests."""

from __future__ import annotations

import pytest

from droidmirror.keymap import (
    JOYSTICK_KEYS,
    MODE_HOLD,
    MODE_TAP,
    Binding,
    CameraAnchor,
    Joystick,
    KeymapEngine,
    Profile,
)
from droidmirror.protocol import MotionAction


class Recorder:
    """Captures every injected touch so we can assert on gestures."""

    def __init__(self) -> None:
        self.events: list[tuple] = []

    def __call__(self, action, pointer_id, x, y, w, h) -> None:
        self.events.append((action, pointer_id, x, y, w, h))

    @property
    def actions(self) -> list[MotionAction]:
        return [e[0] for e in self.events]

    @property
    def pointers(self) -> list[int]:
        return [e[1] for e in self.events]


def make_engine(profile, size=(101, 201)):
    rec = Recorder()
    eng = KeymapEngine(rec, profile, frame_size_provider=lambda: size)
    return eng, rec


# --------------------------------------------------------------------------- #
# Binding
# --------------------------------------------------------------------------- #


def test_binding_clamps_normalised_coordinates():
    assert Binding("a", -1.0, 5.0).nx == 0.0
    assert Binding("a", -1.0, 5.0).ny == 1.0


def test_binding_rejects_unknown_mode():
    with pytest.raises(ValueError):
        Binding("a", 0.5, 0.5, mode="teleport")


def test_to_frame_point_maps_corners_and_centre():
    assert Binding("a", 0.0, 0.0).to_frame_point(101, 201) == (0, 0)
    assert Binding("a", 1.0, 1.0).to_frame_point(101, 201) == (100, 200)
    assert Binding("a", 0.5, 0.5).to_frame_point(101, 201) == (50, 100)


def test_from_frame_point_roundtrips():
    b = Binding.from_frame_point("a", 50, 100, 101, 201)
    assert (b.nx, b.ny) == (0.5, 0.5)
    assert b.to_frame_point(101, 201) == (50, 100)


def test_coordinates_survive_resolution_change():
    """A point placed at 25%/75% must land proportionally after a resize."""
    b = Binding("a", 0.25, 0.75)
    x1, y1 = b.to_frame_point(1000, 2000)
    x2, y2 = b.to_frame_point(2000, 4000)

    # Proportional to within a pixel; the exact fraction shifts slightly because
    # pixels are clamped to the inclusive range [0, size - 1].
    assert abs(x1 / 1000 - x2 / 2000) < 0.002
    assert abs(y1 / 2000 - y2 / 4000) < 0.002
    assert (x1, y1) == (250, 1499)
    assert (x2, y2) == (500, 2999)


def test_coordinates_never_land_outside_the_frame():
    for w, h in [(1, 1), (2, 2), (1080, 1920), (1920, 1080)]:
        for nx in (0.0, 0.5, 1.0):
            for ny in (0.0, 0.5, 1.0):
                x, y = Binding("a", nx, ny).to_frame_point(w, h)
                assert 0 <= x <= w - 1
                assert 0 <= y <= h - 1


def test_display_label_falls_back_to_key():
    assert Binding("w", 0.1, 0.1).display_label == "W"
    assert Binding("w", 0.1, 0.1, label="Jump").display_label == "Jump"


# --------------------------------------------------------------------------- #
# Profile
# --------------------------------------------------------------------------- #


def test_binding_for_key_is_case_insensitive_and_respects_enabled():
    p = Profile(bindings=[Binding("W", 0.1, 0.1), Binding("a", 0.2, 0.2, enabled=False)])
    assert p.binding_for_key("w") is not None
    assert p.binding_for_key("W") is not None
    assert p.binding_for_key("a") is None
    assert p.binding_for_key("z") is None


def test_key_in_use_and_remove_key():
    b = Binding("w", 0.1, 0.1)
    p = Profile(bindings=[b, Binding("a", 0.2, 0.2)])
    assert p.key_in_use("w")
    assert not p.key_in_use("w", exclude=b)
    p.remove_key("w")
    assert [x.key for x in p.bindings] == ["a"]


def test_profile_json_roundtrip(tmp_path):
    p = Profile(
        name="My Game",
        package="com.example.game",
        bindings=[
            Binding("w", 0.1, 0.2, label="Up", mode=MODE_HOLD),
            Binding("space", 0.5, 0.9),
        ],
    )
    path = p.save(tmp_path / "p.json")
    loaded = Profile.load(path)
    assert loaded.name == "My Game"
    assert loaded.package == "com.example.game"
    assert loaded.bindings == p.bindings


def test_profile_name_is_slugified_for_filename(tmp_path):
    path = Profile(name="My Game! / v2").save(tmp_path / "x.json")
    assert path.name == "x.json"


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #


def test_tap_sends_down_then_up_on_the_same_pointer():
    eng, rec = make_engine(Profile(bindings=[Binding("space", 0.5, 0.5)]))
    assert eng.press("space") is True
    assert rec.actions == [MotionAction.DOWN, MotionAction.UP]
    assert rec.pointers[0] == rec.pointers[1]
    assert eng.held_keys == frozenset()


def test_hold_keeps_finger_down_until_release():
    eng, rec = make_engine(Profile(bindings=[Binding("w", 0.5, 0.5, mode=MODE_HOLD)]))
    assert eng.press("w") is True
    assert rec.actions == [MotionAction.DOWN]
    assert eng.held_keys == frozenset({"w"})

    assert eng.release("w") is True
    assert rec.actions == [MotionAction.DOWN, MotionAction.UP]
    assert eng.held_keys == frozenset()


def test_auto_repeat_is_ignored_while_key_is_held():
    eng, rec = make_engine(Profile(bindings=[Binding("w", 0.5, 0.5, mode=MODE_HOLD)]))
    eng.press("w")
    assert eng.press("w") is False
    assert rec.actions == [MotionAction.DOWN]


def test_unmapped_key_is_ignored():
    eng, rec = make_engine(Profile(bindings=[Binding("w", 0.5, 0.5)]))
    assert eng.press("q") is False
    assert eng.release("q") is False
    assert rec.events == []


def test_two_held_keys_get_distinct_fingers():
    """Simultaneous holds must be separate pointers for multitouch to work."""
    profile = Profile(
        bindings=[
            Binding("w", 0.2, 0.2, mode=MODE_HOLD),
            Binding("j", 0.8, 0.8, mode=MODE_HOLD),
        ]
    )
    eng, rec = make_engine(profile)
    eng.press("w")
    eng.press("j")
    downs = [e for e in rec.events if e[0] == MotionAction.DOWN]
    assert len(downs) == 2
    assert downs[0][1] != downs[1][1]
    assert eng.held_keys == frozenset({"w", "j"})

    eng.release("w")
    eng.release("j")
    ups = [e for e in rec.events if e[0] == MotionAction.UP]
    assert {u[1] for u in ups} == {downs[0][1], downs[1][1]}


def test_pointer_ids_are_reused_after_release():
    eng, rec = make_engine(Profile(bindings=[Binding("a", 0.1, 0.1, mode=MODE_HOLD)]))
    eng.press("a")
    first = rec.pointers[0]
    eng.release("a")
    eng.press("a")
    assert rec.pointers[-1] == first


def test_release_all_lifts_every_finger():
    profile = Profile(
        bindings=[
            Binding("w", 0.2, 0.2, mode=MODE_HOLD),
            Binding("j", 0.8, 0.8, mode=MODE_HOLD),
        ]
    )
    eng, rec = make_engine(profile)
    eng.press("w")
    eng.press("j")
    rec.events.clear()
    eng.release_all()
    assert rec.actions == [MotionAction.UP, MotionAction.UP]
    assert eng.held_keys == frozenset()


def test_swapping_profile_releases_held_keys():
    old = Profile(bindings=[Binding("w", 0.1, 0.1, mode=MODE_HOLD)])
    eng, rec = make_engine(old)
    eng.press("w")
    eng.set_profile(Profile(bindings=[Binding("q", 0.9, 0.9)]))
    assert rec.actions[-1] == MotionAction.UP
    assert eng.held_keys == frozenset()


def test_injection_uses_current_frame_size():
    size = [1000, 1000]
    rec = Recorder()
    eng = KeymapEngine(rec, Profile(bindings=[Binding("a", 0.5, 0.5)]), lambda: tuple(size))
    eng.press("a")
    assert rec.events[0][2:6] == (500, 500, 1000, 1000)
    # Rotate: the same binding must follow the new frame.
    size[:] = [2000, 500]
    eng.press("a")
    assert rec.events[2][2:6] == (1000, 250, 2000, 500)


def test_injection_is_skipped_before_the_first_frame():
    rec = Recorder()
    eng = KeymapEngine(rec, Profile(bindings=[Binding("a", 0.5, 0.5)]), lambda: (0, 0))
    assert eng.press("a") is True
    assert rec.events == []


# --------------------------------------------------------------------------- #
# On-screen joystick (GameLoop-style WASD pad)
# --------------------------------------------------------------------------- #
# Frame for these: 101x201, so centre (0.5, 0.5) = (50, 100) and a radius of
# 0.1 reaches 0.1 * min(101, 201) = 10.1 px.


def make_joystick_engine(radius=0.1, size=(101, 201)):
    profile = Profile(joystick=Joystick(nx=0.5, ny=0.5, radius=radius))
    return make_engine(profile, size=size)


def test_pad_press_deflects_up_and_release_lifts_from_centre():
    eng, rec = make_joystick_engine()
    assert eng.press("w") is True
    # DOWN at the centre first (floating sticks take their base from the
    # initial touch), then slide out to the deflection.
    assert rec.actions == [MotionAction.DOWN, MotionAction.MOVE]
    assert rec.events[0][2:4] == (50, 100)  # neutral down
    assert rec.events[1][2:4] == (50, 90)  # centre minus reach, straight up
    assert eng.held_keys == frozenset({"w"})

    assert eng.release("w") is True
    assert rec.actions == [MotionAction.DOWN, MotionAction.MOVE, MotionAction.UP]
    assert rec.events[2][2:4] == (50, 100)  # neutral lift, not a snap from the rim
    assert eng.held_keys == frozenset()


def test_diagonal_is_normalised_to_the_same_reach():
    eng, rec = make_joystick_engine()
    eng.press("w")
    eng.press("a")
    assert rec.actions == [MotionAction.DOWN, MotionAction.MOVE, MotionAction.MOVE]
    x, y = rec.events[-1][2:4]
    reach = ((x - 50) ** 2 + (y - 100) ** 2) ** 0.5
    # Normalised: W+A must travel the same distance as a single key.
    assert reach == pytest.approx(10.1, abs=0.5)
    assert eng.held_keys == frozenset({"w", "a"})


def test_partial_release_slides_before_the_final_lift():
    eng, rec = make_joystick_engine()
    eng.press("w")
    eng.press("a")
    eng.release("w")  # still steering left with A
    assert rec.actions == [
        MotionAction.DOWN,
        MotionAction.MOVE,  # w deflection
        MotionAction.MOVE,  # w+a diagonal
        MotionAction.MOVE,  # a alone after releasing w
    ]
    x, y = rec.events[-1][2:4]
    assert (x, y) == (40, 100)  # slide left, back to centre's y
    assert eng.held_keys == frozenset({"a"})

    eng.release("a")
    assert rec.actions[-1] == MotionAction.UP
    assert rec.events[-1][2:4] == (50, 100)
    assert eng.held_keys == frozenset()


def test_pad_owns_wasd_even_with_a_stray_marker_underneath():
    """Placing the pad retires bindings, but even a forgotten one must lose."""
    profile = Profile(
        bindings=[Binding("w", 0.1, 0.9, mode=MODE_HOLD)],
        joystick=Joystick(nx=0.5, ny=0.5, radius=0.1),
    )
    eng, rec = make_engine(profile)
    assert eng.press("w") is True
    # The MOVE lands at the pad's deflection (50, 90), not the marker
    # (10, 180) -- the pad's finger owns the key.
    assert rec.events[-1][2:4] == (50, 90)


def test_without_a_pad_wasd_still_uses_its_binding():
    profile = Profile(bindings=[Binding("w", 0.1, 0.9, mode=MODE_HOLD)])
    eng, rec = make_engine(profile)
    assert eng.press("w") is True
    assert rec.events[0][2:4] == (10, 180)


def test_pad_auto_repeat_is_ignored():
    eng, rec = make_joystick_engine()
    eng.press("w")
    count = len(rec.events)
    assert eng.press("w") is False
    assert len(rec.events) == count  # no extra packets on repeat


def test_release_all_lifts_the_pad_finger():
    eng, rec = make_joystick_engine()
    eng.press("w")
    first = rec.pointers[0]
    rec.events.clear()
    eng.release_all()
    assert rec.actions == [MotionAction.UP]
    assert eng.held_keys == frozenset()
    # The freed pointer goes back in the pool for the next gesture.
    eng.press("d")
    assert rec.pointers[-1] == first


def test_pad_press_is_skipped_before_the_first_frame():
    rec = Recorder()
    eng = KeymapEngine(rec, Profile(joystick=Joystick()), lambda: (0, 0))
    assert eng.press("w") is False
    assert rec.events == []


def test_joystick_clamps_position_and_radius():
    joy = Joystick(nx=2.0, ny=-1.0, radius=9.0)
    assert (joy.nx, joy.ny, joy.radius) == (1.0, 0.0, 0.5)
    assert Joystick(radius=0.001).radius == 0.02


def test_deflection_never_leaves_the_frame():
    """A pad in a corner must not push its finger off-screen."""
    for nx, ny in [(0.0, 0.0), (1.0, 1.0), (0.0, 1.0), (1.0, 0.0)]:
        joy = Joystick(nx=nx, ny=ny, radius=0.5)
        for keys in [{"a"}, {"d"}, {"w"}, {"s"}, {"w", "a"}, set(JOYSTICK_KEYS)]:
            x, y = joy.deflect_px(keys, 101, 201)
            assert 0 <= x <= 100
            assert 0 <= y <= 200


def test_joystick_survives_profile_json_roundtrip(tmp_path):
    p = Profile(name="game", joystick=Joystick(nx=0.3, ny=0.7, radius=0.2))
    loaded = Profile.load(p.save(tmp_path / "p.json"))
    assert loaded.joystick == Joystick(nx=0.3, ny=0.7, radius=0.2)
    assert p.to_dict()["joystick"] == {"nx": 0.3, "ny": 0.7, "radius": 0.2}


def test_profiles_written_before_joystick_existed_still_load(tmp_path):
    """Old keymap files have no joystick key; loading must not choke."""
    import json

    path = tmp_path / "old.json"
    path.write_text(
        json.dumps(
            {
                "name": "old",
                "package": "",
                "bindings": [
                    {"key": "a", "nx": 0.1, "ny": 0.1, "label": "", "mode": "tap", "enabled": True}
                ],
            }
        ),
        encoding="utf-8",
    )
    loaded = Profile.load(path)
    assert loaded.joystick is None
    assert len(loaded.bindings) == 1


# --------------------------------------------------------------------------- #
# Camera Movement anchor (where the x-key look drag plants)
# --------------------------------------------------------------------------- #


def test_camera_defaults_to_the_middle():
    cam = CameraAnchor()
    assert (cam.nx, cam.ny, cam.locked) == (0.5, 0.5, False)
    assert cam.to_frame_point(1001, 1601) == (500, 800)  # size - 1, like Binding


def test_camera_clamps_out_of_range_points():
    cam = CameraAnchor(nx=7.5, ny=-2.0)
    assert (cam.nx, cam.ny) == (1.0, 0.0)
    cam2 = CameraAnchor(locked="yes")
    assert cam2.locked is True


def test_camera_to_frame_point_maps_corners():
    assert CameraAnchor(0.0, 0.0).to_frame_point(101, 201) == (0, 0)
    assert CameraAnchor(1.0, 1.0).to_frame_point(101, 201) == (100, 200)
    assert CameraAnchor(0.2, 0.2).to_frame_point(101, 201) == (20, 40)


def test_profile_has_a_camera_by_default():
    p = Profile()
    assert isinstance(p.camera, CameraAnchor)
    assert (p.camera.nx, p.camera.ny) == (0.5, 0.5)


def test_camera_survives_profile_json_roundtrip(tmp_path):
    p = Profile(name="game", camera=CameraAnchor(nx=0.8, ny=0.2, locked=True))
    loaded = Profile.load(p.save(tmp_path / "p.json"))
    assert loaded.camera == CameraAnchor(nx=0.8, ny=0.2, locked=True)
    assert p.to_dict()["camera"] == {"nx": 0.8, "ny": 0.2, "locked": True}


def test_profiles_written_before_camera_existed_still_load(tmp_path):
    """Old keymap files have no camera key; loading must not choke."""
    import json

    path = tmp_path / "old.json"
    path.write_text(
        json.dumps({"name": "old", "package": "", "bindings": []}),
        encoding="utf-8",
    )
    loaded = Profile.load(path)
    assert loaded.camera == CameraAnchor()  # the default middle spot
