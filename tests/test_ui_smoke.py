"""Offscreen GUI smoke tests.

Runs the real Qt widgets with the ``offscreen`` platform plugin, so the widget
tree, signal wiring and the full key -> touch path are exercised without a
display (and without a phone).
"""

from __future__ import annotations

import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent, QMouseEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QStyle, QStyleOptionSlider  # noqa: E402

import threading  # noqa: E402
import time  # noqa: E402

from droidmirror.adb import Device  # noqa: E402
from droidmirror.decoder import DecodedFrame  # noqa: E402
from droidmirror.keymap import MODE_TAP, Binding, profile_path  # noqa: E402
from droidmirror.protocol import (  # noqa: E402
    POINTER_ID_GENERIC_FINGER,
    POINTER_ID_MOUSE,
    POINTER_ID_VIRTUAL_FINGER,
    AndroidKey,
    KeyAction,
    MotionAction,
)
from droidmirror.ui import keys as keynames  # noqa: E402
from droidmirror.ui import main_window as main_window_module  # noqa: E402
from droidmirror.ui.glview import (  # noqa: E402
    DRAG_HELD_BUTTONS,
    DRAG_POINTER_ID,
    DRAG_PRESS_BUTTON,
    MirrorView,
)
from droidmirror.ui.main_window import MainWindow  # noqa: E402
from droidmirror.volume import VolumeState  # noqa: E402
from droidmirror.ui.lockdialog import (  # noqa: E402
    KEYCODE_0,
    KEYCODE_ENTER,
    KEYCODE_WAKEUP,
    LockDialog,
)


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def isolated_profiles(tmp_path, monkeypatch):
    """Keep profile auto-saves out of the repository."""
    monkeypatch.setenv("DROIDMIRROR_HOME", str(tmp_path))
    yield


@pytest.fixture
def window(qt_app):
    w = MainWindow(adb=None)
    # Drive the background adb flow inline so tests assert synchronously.
    w._sync_workers = True
    # A scratch name of its own: the built-in "default" profile is read-only
    # and would refuse every edit these tests exercise.
    w._profile.name = "smoke"
    yield w
    w.close()


class FakeSession:
    """Captures everything the UI tries to inject."""

    def __init__(self) -> None:
        self.touches: list[tuple] = []
        self.keys: list[tuple] = []
        self.raw: list[bytes] = []
        self.texts: list[str] = []
        self.clipboard_sets: list[tuple[str, bool]] = []
        self.device_pastes = 0
        self.frames_decoded = 0
        #: Mirrors MirrorSession.device_locked (None = unknown).
        self.device_locked: bool | None = None
        #: Mirrors MirrorSession.lock_type (pin/pattern/password/swipe).
        self.lock_type: str | None = None
        #: Mirrors MirrorSession.device_volume(); None = unknown, as on a
        #: device that will not report it.
        self.volume_state = None
        #: Every absolute index the UI tried to write, in order.
        self.volume_sets: list[int] = []

    def device_volume(self):
        return self.volume_state

    def set_device_volume(self, index: int) -> bool:
        self.volume_sets.append(index)
        return True

    def inject_touch(self, *args) -> bool:
        self.touches.append(args)
        return True

    def inject_keycode(self, action, keycode) -> bool:
        self.keys.append((action, keycode))
        return True

    def inject_text(self, text: str) -> bool:
        self.texts.append(text)
        return True

    def send_control(self, message: bytes) -> bool:
        self.raw.append(message)
        return True

    def set_clipboard(self, text: str, paste: bool = False) -> bool:
        self.clipboard_sets.append((text, paste))
        return True

    def paste_device_clipboard(self) -> bool:
        self.device_pastes += 1
        return True

    def set_muted(self, muted: bool) -> None:
        self.muted = muted

    @property
    def fps(self) -> float:
        return 0.0

    def stop(self) -> None:
        pass


def attach_frame(window: MainWindow, width: int = 100, height: int = 100) -> None:
    """Give the view a frame so it reports a usable size."""
    ch, cw = height // 2, width // 2
    frame = DecodedFrame(
        y=np.zeros((height, width), np.uint8),
        u=np.zeros((ch, cw), np.uint8),
        v=np.zeros((ch, cw), np.uint8),
        width=width,
        height=height,
    )
    window._view.set_frame(frame)


def mouse_event(
    kind, x: float, y: float, button=Qt.MouseButton.LeftButton
) -> QMouseEvent:
    pos = QPointF(x, y)
    return QMouseEvent(
        kind,
        pos,
        pos,
        pos,
        button,
        button,
        Qt.KeyboardModifier.NoModifier,
    )


def use_keybinds(window: MainWindow) -> None:
    window._view.set_control_target(MirrorView.TARGET_KEYMAP)


def timed_frame(
    recv_ns: int, pts_us: int, decode_ms: float = 0.0, width: int = 100, height: int = 100
) -> DecodedFrame:
    """A frame carrying the latency stamps the decoder would record."""
    ch, cw = height // 2, width // 2
    return DecodedFrame(
        y=np.zeros((height, width), np.uint8),
        u=np.zeros((ch, cw), np.uint8),
        v=np.zeros((ch, cw), np.uint8),
        width=width,
        height=height,
        recv_ns=recv_ns,
        decoded_ns=recv_ns + 2_000_000,
        pts_us=pts_us,
        decode_ms=decode_ms,
    )


# --------------------------------------------------------------------------- #


def test_window_constructs_without_adb(window):
    assert window.windowTitle().startswith("DroidMirror")
    assert window._table.columnCount() == 4
    assert not window._connected


def test_placing_a_binding_updates_profile_table_and_engine(window):
    window._on_binding_placed("w", 0.25, 0.5)

    assert len(window._profile.bindings) == 1
    binding = window._profile.bindings[0]
    assert (binding.nx, binding.ny) == (0.25, 0.5)
    assert binding.mode == MODE_TAP

    assert window._table.item(0, 0).text() == "W"
    assert window._table.item(0, 1).text() == "25.0"
    assert window._table.item(0, 2).text() == "50.0"

    # The engine must see the new binding.
    assert window._engine.profile.binding_for_key("w") is binding


def test_placing_the_same_key_twice_moves_it(window):
    window._on_binding_placed("w", 0.1, 0.1)
    window._on_binding_placed("w", 0.9, 0.9)
    assert len(window._profile.bindings) == 1
    assert window._profile.bindings[0].nx == 0.9


def test_dragging_a_binding_updates_its_position(window):
    window._on_binding_placed("a", 0.5, 0.5)
    window._on_binding_moved("a", 0.2, 0.8)
    binding = window._profile.bindings[0]
    assert (binding.nx, binding.ny) == (0.2, 0.8)


def test_toggle_mode_flips_tap_and_hold(window):
    window._on_binding_placed("space", 0.5, 0.9)
    window._table.setCurrentCell(0, 0)
    window._toggle_selected_mode()
    assert window._profile.bindings[0].mode == "hold"
    window._toggle_selected_mode()
    assert window._profile.bindings[0].mode == MODE_TAP


def test_control_mode_keypress_injects_a_tap(window):
    """The whole path: key event -> engine -> session.inject_touch."""
    session = FakeSession()
    window._session = session
    attach_frame(window)
    use_keybinds(window)
    window._on_binding_placed("w", 0.25, 0.5)

    window._on_key_pressed("w")

    assert len(session.touches) == 2
    down, up = session.touches
    assert down[0] == MotionAction.DOWN
    assert up[0] == MotionAction.UP
    # 100px frame -> 0.25*99 = 25, 0.5*99 = 50
    assert (down[2], down[3]) == (25, 50)
    assert (down[4], down[5]) == (100, 100)
    # A tap completes immediately; nothing stays held.
    assert window._engine.held_keys == frozenset()


def test_hold_mode_keypress_injects_down_until_release(window):
    session = FakeSession()
    window._session = session
    attach_frame(window)
    use_keybinds(window)
    window._on_binding_placed("w", 0.5, 0.5)
    window._profile.bindings[0].mode = "hold"
    window._engine.set_profile(window._profile)

    window._on_key_pressed("w")
    assert [t[0] for t in session.touches] == [MotionAction.DOWN]

    window._on_key_released("w")
    assert [t[0] for t in session.touches] == [
        MotionAction.DOWN,
        MotionAction.UP,
    ]


def test_map_mode_arms_a_key_instead_of_injecting(window):
    session = FakeSession()
    window._session = session
    attach_frame(window)
    window._on_binding_placed("w", 0.5, 0.5)

    window._mode_action.setChecked(True)  # enter edit mode
    window._on_key_pressed("w")

    assert session.touches == []
    assert window._pending_key == "w"


def test_hardware_button_sends_a_keycode(window):
    session = FakeSession()
    window._session = session
    from droidmirror.protocol import AndroidKey

    window._send_key(AndroidKey.BACK)
    assert len(session.keys) == 2
    assert session.keys[0][1] == int(AndroidKey.BACK)


def test_rotate_sends_the_control_message(window):
    session = FakeSession()
    window._session = session
    window._rotate_device()
    assert session.raw == [bytes([11])]


def test_clear_bindings_empties_everything(window, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    window._on_binding_placed("w", 0.1, 0.1)
    window._on_binding_placed("a", 0.2, 0.2)
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes
    )
    window._clear_bindings()
    assert window._profile.bindings == []
    assert window._table.rowCount() == 0


def test_profile_roundtrips_through_save_and_load(window, tmp_path):
    window._profile.name = "smoke"
    window._on_binding_placed("j", 0.3, 0.7)
    window._profile.save()

    from droidmirror.keymap import Profile

    reloaded = Profile.load(profile_path("smoke"))
    assert reloaded.name == "smoke"
    assert len(reloaded.bindings) == 1
    assert reloaded.bindings[0].key == "j"


def test_view_render_path_is_reported(window):
    assert window._view.render_path in {"GPU", "CPU", "starting"}


# --------------------------------------------------------------------------- #
# Mouse <-> keybind control toggle
# --------------------------------------------------------------------------- #


def test_default_target_is_mouse(window):
    assert window._view.control_target == MirrorView.TARGET_MOUSE
    assert window._input_label.text() == "input: mouse"
    assert not window._keybind_action.isChecked()


def test_hotkey_toggles_between_mouse_and_keybinds(window):
    window._on_toggle_hotkey()
    assert window._view.control_target == MirrorView.TARGET_KEYMAP
    assert window._input_label.text() == "input: keybinds"
    assert window._keybind_action.isChecked()

    window._on_toggle_hotkey()
    assert window._view.control_target == MirrorView.TARGET_MOUSE
    assert window._input_label.text() == "input: mouse"
    assert not window._keybind_action.isChecked()


def test_toolbar_action_drives_the_same_state(window):
    window._keybind_action.setChecked(True)
    assert window._view.control_target == MirrorView.TARGET_KEYMAP
    assert window._input_label.text() == "input: keybinds"
    window._keybind_action.setChecked(False)
    assert window._view.control_target == MirrorView.TARGET_MOUSE


def test_toggle_shortcut_is_registered(window):
    qt_key = window._toggle_shortcut.key()
    assert keynames.is_toggle_key(qt_key)
    assert qt_key.toString() == keynames.TOGGLE_KEY_NAME


def test_bound_keys_fire_in_mouse_mode_too(window):
    """Regression: bindings were gated to keybind mode only, so the hotkeys
    you armed stayed dead while the mouse drove the camera -- you had to
    press ` and lose camera movement just to fire an ability."""
    session = FakeSession()
    window._session = session
    attach_frame(window)
    window._on_binding_placed("w", 0.5, 0.5)
    assert window._view.control_target == MirrorView.TARGET_MOUSE

    window._on_key_pressed("w")
    assert [t[0] for t in session.touches] == [
        MotionAction.DOWN,
        MotionAction.UP,
    ]  # tap fires with the camera still live

    # ...and the camera mouse keeps working alongside it.
    window._view.mousePressEvent(mouse_event(QEvent.Type.MouseButtonPress, 40, 40))
    assert session.touches[-1][0] == MotionAction.DOWN


def test_mouse_drives_the_device_in_mouse_mode(window):
    """Regression: the view used to check a different attribute than the one
    MainWindow assigns, so mouse injection silently did nothing."""
    session = FakeSession()
    window._session = session
    attach_frame(window)
    view = window._view
    view.set_control_target(MirrorView.TARGET_MOUSE)

    view.mousePressEvent(mouse_event(QEvent.Type.MouseButtonPress, 40, 40))
    assert len(session.touches) == 1
    action, pointer, x, y, w, h, _ab, _btns = session.touches[0]
    assert action == MotionAction.DOWN
    assert pointer == POINTER_ID_MOUSE
    assert (w, h) == (100, 100)

    view.mouseMoveEvent(mouse_event(QEvent.Type.MouseMove, 60, 60))
    assert session.touches[-1][0] == MotionAction.MOVE

    view.mouseReleaseEvent(mouse_event(QEvent.Type.MouseButtonRelease, 60, 60))
    assert session.touches[-1][0] == MotionAction.UP
    assert len(session.touches) == 3


def test_mouse_is_inert_in_keybind_mode(window):
    session = FakeSession()
    window._session = session
    attach_frame(window)
    use_keybinds(window)

    window._view.mousePressEvent(mouse_event(QEvent.Type.MouseButtonPress, 40, 40))
    window._view.mouseReleaseEvent(mouse_event(QEvent.Type.MouseButtonRelease, 40, 40))
    assert session.touches == [], "mouse must not inject in keybind mode"


def test_switching_to_mouse_lifts_a_held_keybind(window):
    session = FakeSession()
    window._session = session
    attach_frame(window)
    use_keybinds(window)
    window._on_binding_placed("w", 0.5, 0.5)
    window._profile.bindings[0].mode = "hold"
    window._engine.set_profile(window._profile)

    window._on_key_pressed("w")
    assert [t[0] for t in session.touches] == [MotionAction.DOWN]

    window._on_toggle_hotkey()  # switch to mouse while W is held
    assert [t[0] for t in session.touches] == [MotionAction.DOWN, MotionAction.UP]
    assert window._engine.held_keys == frozenset()


def test_switching_away_mid_click_lifts_the_mouse(window):
    session = FakeSession()
    window._session = session
    attach_frame(window)
    view = window._view

    view.mousePressEvent(mouse_event(QEvent.Type.MouseButtonPress, 40, 40))
    assert session.touches[-1][0] == MotionAction.DOWN

    use_keybinds(window)  # handed control over without releasing
    assert session.touches[-1][0] == MotionAction.UP


def test_editing_bindings_still_works_alongside_control_target(window):
    session = FakeSession()
    window._session = session
    attach_frame(window)
    use_keybinds(window)

    window._mode_action.setChecked(True)  # edit mode
    window._on_key_pressed("j")
    assert window._pending_key == "j"
    assert session.touches == []

    window._on_binding_placed("j", 0.5, 0.5)
    assert window._profile.binding_for_key("j") is not None

    # Leaving edit mode keeps the previously selected target.
    window._mode_action.setChecked(False)
    assert window._view.control_target == MirrorView.TARGET_KEYMAP


def test_toggle_key_cannot_be_bound_to_a_screen_point(window):
    assert not keynames.is_bindable(keynames.TOGGLE_KEY_NAME)
    assert not keynames.is_bindable("~")  # Shift+` produces this


def test_view_rejects_an_unknown_control_target(window):
    with pytest.raises(ValueError):
        window._view.set_control_target("telepathy")


def test_fps_label_reflects_connection_state(window):
    window._connected = False
    window._session = None
    window._update_fps()
    assert window._fps_label.text() == "fps: -"

    session = FakeSession()
    window._session = session
    window._connected = True
    window._update_fps()
    assert window._fps_label.text().endswith(" fps")

    # Disconnecting must reset the window so the next session starts clean.
    window._connected = False
    window._update_fps()
    assert window._fps_label.text() == "fps: -"
    assert window._fps_counter.rate == 0.0


def test_lock_label_tracks_the_keyguard_state(window):
    window._connected = False
    window._session = None
    window._update_fps()
    assert window._lock_label.text() == "phone: -", "nothing known offline"

    session = FakeSession()
    window._session = session
    window._connected = True
    window._update_fps()
    assert window._lock_label.text() == "phone: -", "unknown until first poll"

    session.device_locked = False
    window._update_fps()
    assert window._lock_label.text() == "phone: unlocked"

    session.device_locked = True
    window._update_fps()
    assert window._lock_label.text() == "phone: LOCKED"
    assert "lockscreen" in window._status.text(), "the flip announces itself"

    # Flapping back must not re-announce every second.
    window._status.setText("quiet")
    session.device_locked = True
    window._update_fps()
    assert window._status.text() == "quiet"

    window._connected = False
    window._update_fps()
    assert window._lock_label.text() == "phone: -"


# --------------------------------------------------------------------------- #
# Unlock panel (pops at the lockscreen, tabs follow the detected lock type)
# --------------------------------------------------------------------------- #


def make_dialog():
    """A LockDialog wired to recorders instead of a live session."""
    keys: list[int] = []
    texts: list[str] = []
    touches: list[tuple] = []
    dialog = LockDialog(
        None,
        send_key=keys.append,
        send_text=texts.append,
        send_touch=lambda *args: touches.append(args),
        frame_size=lambda: (1000, 1600),
        adb=None,
    )
    return dialog, keys, texts, touches


def test_dialog_opens_on_the_tab_matching_the_lock_type(qt_app):
    dialog, keys, texts, touches = make_dialog()
    try:
        for kind, expected in (
            ("pin", 0),
            ("pattern", 1),
            ("password", 2),
            ("swipe", 3),
            (None, 0),  # unknown: PIN is the safest default
        ):
            dialog.open_for(kind)
            assert dialog._tabs.currentIndex() == expected, kind
            assert dialog.isVisible()
    finally:
        dialog.close()


def test_pin_digits_press_real_keycodes(qt_app):
    dialog, keys, texts, touches = make_dialog()
    try:
        dialog._pin_buttons["5"].click()
        dialog._digit("0")
        assert keys == [KEYCODE_0 + 5, KEYCODE_0 + 0]
    finally:
        dialog.close()


def test_ok_taps_the_keyguard_button_through_the_frame(qt_app):
    """uiautomator bounds are display px; injection must speak frame px."""
    dialog, keys, texts, touches = make_dialog()
    try:
        dialog._ok_rect = (687, 1662, 777, 1752)
        dialog._display = (1200, 1920)
        dialog._submit_pin()

        assert keys == [], "a known bounds rect taps, it does not press Enter"
        assert len(touches) == 2
        down, up = touches
        # [687..777] x 1000/1200 -> [572..647]; [1662..1752] x 1600/1920
        # -> [1385..1460]; the tap lands on their centers.
        assert down[0] == MotionAction.DOWN
        assert down[1] == POINTER_ID_MOUSE
        assert down[2:4] == (609, 1422)
        assert down[4:6] == (1000, 1600)
        assert down[7] == 1, "mouse tap carries its button held"
        assert up[0] == MotionAction.UP
        assert up[7] == 0
    finally:
        dialog.close()


def test_ok_falls_back_to_enter_when_bounds_unknown(qt_app):
    dialog, keys, texts, touches = make_dialog()
    try:
        dialog._submit_pin()
        assert keys == [KEYCODE_ENTER]
        assert touches == []
    finally:
        dialog.close()


def test_password_unlock_sends_text_then_enter(qt_app):
    dialog, keys, texts, touches = make_dialog()
    try:
        dialog._password.setText("hunter2")
        dialog._submit_password()
        assert texts == ["hunter2"]
        assert keys == [KEYCODE_ENTER]
        assert dialog._password.text() == "", "the field never keeps it"

        dialog._submit_password()
        assert texts == ["hunter2"], "an empty field sends nothing"
    finally:
        dialog.close()


def test_pattern_unlock_is_one_continuous_finger_drag(qt_app):
    dialog, keys, texts, touches = make_dialog()
    try:
        dialog._pattern.points = [(0.0, 0.0), (0.5, 0.5), (1.0, 1.0)]
        dialog._submit_pattern()

        assert len(touches) >= 3
        assert touches[0][0] == MotionAction.DOWN
        assert touches[0][1] == POINTER_ID_VIRTUAL_FINGER
        assert all(t[0] == MotionAction.MOVE for t in touches[1:-1])
        assert touches[-1][0] == MotionAction.UP
        # No bounds fetched -> the default screen band is used:
        # x 0.24..0.76 of 1000, y 0.28..0.70 of 1600.
        assert touches[0][2:4] == (240, 448)
        assert touches[-1][2:4] == (760, 1120)
    finally:
        dialog.close()


def test_pattern_points_map_onto_the_real_pattern_view(qt_app):
    dialog, keys, texts, touches = make_dialog()
    try:
        dialog._pattern_rect = (300, 500, 900, 1500)
        dialog._display = (1200, 1920)
        dialog._pattern.points = [(0.0, 0.0), (1.0, 1.0)]
        dialog._submit_pattern()

        # (300,500)->(250,416) and (900,1500)->(750,1250) after scaling.
        assert touches[0][2:4] == (250, 416)
        assert touches[-1][2:4] == (750, 1250)
    finally:
        dialog.close()


def test_swipe_unlock_wakes_then_flicks_up(qt_app):
    dialog, keys, texts, touches = make_dialog()
    try:
        dialog._submit_swipe()
        assert keys == [KEYCODE_WAKEUP]
        assert touches[0][0] == MotionAction.DOWN
        assert touches[-1][0] == MotionAction.UP
        assert touches[0][1] == POINTER_ID_VIRTUAL_FINGER
        assert touches[0][3] > touches[-1][3], "end above start = upward flick"
    finally:
        dialog.close()


def test_lock_flip_pops_the_panel_and_unlock_closes_it(window):
    session = FakeSession()
    window._session = session
    window._connected = True

    session.device_locked = True
    session.lock_type = "pin"
    window._update_fps()
    assert window._lock_dialog is not None
    assert window._lock_dialog.isVisible()
    assert window._lock_dialog._tabs.currentIndex() == 0
    assert window._lock_label.text() == "phone: LOCKED (PIN)"

    # Still locked on later polls: same dialog, still up, never re-opened.
    popped = window._lock_dialog
    session.device_locked = True
    window._update_fps()
    assert window._lock_dialog is popped
    assert window._lock_dialog.isVisible()

    session.device_locked = False
    window._update_fps()
    assert not window._lock_dialog.isVisible(), "unlocking closes the panel"

    # Locked again (screen off, timed out): a fresh open on the new edge.
    session.device_locked = True
    window._update_fps()
    assert window._lock_dialog.isVisible()

    window._connected = False
    window._update_fps()
    assert not window._lock_dialog.isVisible(), "a dead link closes it too"


def test_clicking_the_lock_chip_opens_the_panel_by_hand(window):
    session = FakeSession()
    window._session = session
    window._connected = True
    session.device_locked = False
    window._update_fps()
    assert window._lock_dialog is None, "unlocked: nothing pops by itself"

    window._lock_label.clicked.emit()  # the chip itself
    assert window._lock_dialog is not None
    assert window._lock_dialog.isVisible()


# --------------------------------------------------------------------------- #
# Latency HUD (toggleable, hotkey F9)
# --------------------------------------------------------------------------- #


def test_hud_is_off_by_default(window):
    assert not window._view.hud_enabled
    assert not window._hud_action.isChecked()


def test_hud_action_toggles_the_overlay(window):
    window._hud_action.setChecked(True)
    assert window._view.hud_enabled
    window._hud_action.setChecked(False)
    assert not window._view.hud_enabled


def test_hud_state_change_keeps_the_action_in_sync(window):
    window._view.toggle_hud()
    assert window._view.hud_enabled
    assert window._hud_action.isChecked()

    window._view.set_hud(False)
    assert not window._hud_action.isChecked()

    # Re-setting the same value must be a no-op (no signal loop).
    window._view.set_hud(False)
    assert not window._hud_action.isChecked()


def test_hud_hotkey_and_fullscreen_hotkey_are_registered(window):
    assert window._hud_action.shortcut().toString() == "F9"
    # F11 lives on a window-level QShortcut, not the toolbar action: the
    # action's shortcut dies when fullscreen hides the toolbar.
    assert window._fullscreen_shortcut.key() == Qt.Key.Key_F11
    assert window._fullscreen_action.shortcut().isEmpty(), \
        "a second F11 on the action would make the key ambiguous"
    # Both are app hotkeys, so they must never be bindable as keybinds.
    assert not keynames.is_bindable(keynames.HUD_KEY_NAME)
    assert not keynames.is_bindable(keynames.FULLSCREEN_KEY_NAME)
    assert keynames.is_hud_key(keynames.HUD_KEY_CODE)
    assert keynames.is_fullscreen_key(keynames.FULLSCREEN_KEY_CODE)


def test_hud_reports_latency_breakdown_in_ms(window):
    view = window._view
    view.set_hud(True)

    # No frame yet: the HUD must still render something sensible.
    assert any("waiting" in line for line in view.hud_lines())

    base = time.perf_counter_ns()
    view.set_frame(timed_frame(base, 1_000_000, decode_ms=4.25))
    view.set_frame(timed_frame(base + 16_600_000, 1_016_000, decode_ms=5.5))
    text = "\n".join(view.hud_lines())

    assert "render" in text and "paint" in text
    assert "backend" in text
    assert "5.50" in text, "decode ms missing"
    assert "16.6" in text, "client inter-arrival missing"
    assert "16.0" in text, "device pts delta missing"


def test_hud_backend_is_fed_from_the_session(window):
    class BackendSession(FakeSession):
        decoder_backend = "amf"

    window._session = BackendSession()
    window._poll_frame()
    assert window._view.hud_backend == "amf"


class RecordingGL:
    """Fake GL module: records calls, no context needed.

    Constants become no-op callables via __getattr__, which is fine -- the
    only calls we assert on are recorded explicitly.
    """

    def __init__(self, unpack_alignment: int = 4) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self._unpack = unpack_alignment

    def glGetIntegerv(self, name):
        self.calls.append(("glGetIntegerv", (name,)))
        return self._unpack

    def glPixelStorei(self, name, value):
        self.calls.append(("glPixelStorei", (name, value)))

    def __getattr__(self, name):
        if name.startswith("GL_"):
            return 100  # a plain int: constants get used in arithmetic

        def noop(*args, **kwargs):
            self.calls.append((name, args))
            return 0

        return noop


def _gl_ready_view(window):
    """A view with a frame and the GL handles the upload path touches."""
    attach_frame(window)
    view = window._view
    view._textures = [1, 2, 3]
    view._program = 7
    view._u_locs = {"tex_y": 0, "tex_u": 1, "tex_v": 2}
    view._attrs = {"position": 0, "texcoord": 1}
    view._vbo = 5
    view._gl_ready = True
    return view


def test_gl_upload_restores_unpack_alignment(window, monkeypatch):
    """Regression: leaving GL_UNPACK_ALIGNMENT at 1 smeared all overlay text.

    Qt's QPainter uploads glyph textures assuming the default alignment of 4
    and does not reset it in endNativePainting(), so the video upload must put
    it back -- otherwise the latency HUD and keybind labels rasterise as
    overlapping rows.
    """
    from droidmirror.ui import glview

    view = _gl_ready_view(window)
    fake = RecordingGL(unpack_alignment=4)
    monkeypatch.setattr(glview, "GL", fake)

    view._paint_video_gl()

    stores = [args[1] for name, args in fake.calls if name == "glPixelStorei"]
    assert stores == [1, 4], "set to 1 for our planes, restored to 4 after"


def test_gl_upload_restores_whatever_alignment_was_there(window, monkeypatch):
    from droidmirror.ui import glview

    view = _gl_ready_view(window)
    fake = RecordingGL(unpack_alignment=8)
    monkeypatch.setattr(glview, "GL", fake)

    view._paint_video_gl()

    stores = [args[1] for name, args in fake.calls if name == "glPixelStorei"]
    assert stores == [1, 8], "restores the prior value, not a hard-coded one"


# --------------------------------------------------------------------------- #
# Fullscreen (hotkey F11) -- the phone picture letterboxes to the display
# --------------------------------------------------------------------------- #


def test_fullscreen_manages_the_esc_exit_shortcut(window):
    """Esc exits fullscreen, and only while fullscreen.

    Renamed: this used to share a name with the fullscreen test further down the
    file, so the later definition silently shadowed this one and its assertions
    never ran. What is unique here is the Esc shortcut's enable/disable
    lifecycle -- leaving Esc live outside fullscreen would break the 'escape'
    keybind, which has its own meaning on the device.
    """
    bar, dock, status = window._bar, window._dock, window.statusBar()
    assert not bar.isHidden() and not dock.isHidden() and not status.isHidden()

    window.set_fullscreen(True)
    assert window.is_fullscreen
    assert bar.isHidden() and dock.isHidden() and status.isHidden()
    assert window._exit_fullscreen_shortcut.isEnabled(), "Esc must exit"

    window.set_fullscreen(False)
    assert not window.is_fullscreen
    assert not bar.isHidden() and not dock.isHidden() and not status.isHidden()
    assert not window._exit_fullscreen_shortcut.isEnabled(), \
        "Esc must stay inert so the 'escape' keybind still works"


def test_fullscreen_toggle_flips_state(window):
    window.toggle_fullscreen()
    assert window.is_fullscreen
    window.toggle_fullscreen()
    assert not window.is_fullscreen
    # Setting the same state again must not re-hide anything.
    window.set_fullscreen(False)
    assert not window.is_fullscreen


def test_fullscreen_action_triggers_the_toggle(window):
    window._fullscreen_action.trigger()
    assert window.is_fullscreen
    window._fullscreen_action.trigger()
    assert not window.is_fullscreen


def test_f11_press_enters_and_exits_fullscreen(window):
    """The real key, both directions -- including with the toolbar hidden.

    Regression: F11 used to live on the toolbar action's shortcut, which
    stopped firing the moment fullscreen hid the toolbar, trapping the user
    (only Esc could get them out).
    """
    window.show()
    QApplication.instance().processEvents()

    QTest.keyClick(window, Qt.Key.Key_F11)
    assert window.is_fullscreen, "F11 must enter fullscreen"
    assert window._bar.isHidden(), "toolbar gone -> old action shortcut is dead now"

    QTest.keyClick(window, Qt.Key.Key_F11)
    assert not window.is_fullscreen, "F11 must also exit fullscreen"
    assert not window._bar.isHidden()

    # Esc remains a second way out while fullscreen is active.
    QTest.keyClick(window, Qt.Key.Key_F11)
    assert window.is_fullscreen
    QTest.keyClick(window, Qt.Key.Key_Escape)
    assert not window.is_fullscreen


def test_fullscreen_keeps_the_mobile_aspect_ratio(window):
    """The video rect must stay proportional to the phone screen, centred."""
    attach_frame(window, width=100, height=200)  # portrait, like a phone
    window.set_fullscreen(True)
    window._view.resize(1920, 1080)
    rect = window._view.frame_rect()
    assert rect.w > 0 and rect.h > 0
    assert abs((rect.w / rect.h) - (100 / 200)) < 1e-6
    # Letterboxed and centred, never stretched.
    assert abs(rect.x - (1920 - rect.w) / 2) < 1e-6
    assert abs(rect.y - (1080 - rect.h) / 2) < 1e-6


def test_hw_decode_option_reaches_the_session(window, monkeypatch):
    from droidmirror.ui import main_window as mw

    captured: dict = {}

    class CapturingSession(FakeSession):
        def __init__(self, adb, **kwargs):
            super().__init__()
            captured.update(kwargs)

        def start(self, device=None) -> None:
            pass

    monkeypatch.setattr(mw, "MirrorSession", CapturingSession)
    window._adb = object()
    window._device_combo.clear()
    window._device_combo.addItem("fake device", object())
    window._hw_decode = "auto"

    window._connect()

    assert captured.get("hw_decode") == "auto"
    window._disconnect()


def _wifi_connect_setup(window, monkeypatch, choice: str) -> list:
    """Wireless device selected with a USB device also attached.

    ``choice`` is what the warning dialog would return. Returns the list the
    (fake) session appends the device it was asked to start.
    """
    from droidmirror.ui import main_window as mw

    started: list = []

    class CapturingSession(FakeSession):
        def __init__(self, adb, **kwargs):
            super().__init__()

        def start(self, device=None) -> None:
            started.append(device)

    monkeypatch.setattr(mw, "MirrorSession", CapturingSession)
    adb = FakeAdb(Device(serial="USB123", state="device"))
    adb._wireless = Device(serial="10.0.0.9:5555", state="device")
    window._adb = adb
    window._filter_action.setChecked(True)  # wireless mode: wireless selected
    monkeypatch.setattr(window, "_wireless_choice", lambda d: choice)
    return started


def test_wifi_connect_warns_and_can_use_the_cable(window, monkeypatch):
    started = _wifi_connect_setup(window, monkeypatch, "usb")

    window._connect()

    assert started and started[0].serial == "USB123", "cable path must be used"
    assert not window._filter_action.isChecked(), "filter follows to USB"
    window._disconnect()


def test_wifi_connect_can_proceed_on_wifi(window, monkeypatch):
    started = _wifi_connect_setup(window, monkeypatch, "wifi")

    window._connect()

    assert started and started[0].serial == "10.0.0.9:5555"
    window._disconnect()


def test_wifi_connect_can_be_cancelled(window, monkeypatch):
    started = _wifi_connect_setup(window, monkeypatch, "cancel")

    window._connect()

    assert started == [], "cancelling must not open a session"
    assert window._session is None
    assert "cancel" in window._status.text().lower()


def test_usb_connect_never_shows_the_wifi_warning(window, monkeypatch):
    from droidmirror.ui import main_window as mw

    started: list = []

    class CapturingSession(FakeSession):
        def __init__(self, adb, **kwargs):
            super().__init__()

        def start(self, device=None) -> None:
            started.append(device)

    monkeypatch.setattr(mw, "MirrorSession", CapturingSession)
    window._adb = FakeAdb(Device(serial="USB123", state="device"))
    window._refresh_devices()
    asked: list = []
    monkeypatch.setattr(window, "_wireless_choice", lambda d: asked.append(d) or "cancel")

    window._connect()

    assert asked == [], "a cable connect must not be interrupted"
    assert started and started[0].serial == "USB123"
    window._disconnect()


# --------------------------------------------------------------------------- #
# On-screen joystick (GameLoop-style WASD pad)
# --------------------------------------------------------------------------- #


def test_add_joystick_arms_placement_and_click_drops_the_pad(window):
    attach_frame(window)
    window._on_binding_placed("w", 0.1, 0.1)  # stray marker under the pad spot

    window._begin_add_joystick()
    assert window._view.edit_mode
    assert window._view.pending_joystick

    window._view.mousePressEvent(mouse_event(QEvent.Type.MouseButtonPress, 40, 40))
    joy = window._profile.joystick
    assert joy is not None
    assert 0.0 <= joy.nx <= 1.0 and 0.0 <= joy.ny <= 1.0
    # The pad owns WASD from now on.
    assert window._profile.binding_for_key("w") is None
    assert not window._view.pending_joystick
    assert window._view.joystick is joy


def test_key_and_pad_armings_are_mutually_exclusive(window):
    window._mode_action.setChecked(True)  # edit mode

    window._begin_add_joystick()
    assert window._view.pending_joystick
    window._on_key_pressed("j")  # arming a key takes the arming away from the pad
    assert window._pending_key == "j"
    assert not window._view.pending_joystick

    window._begin_add_joystick()  # and the pad takes it back from the key
    assert window._pending_key is None
    assert window._view.pending_joystick

    window._mode_action.setChecked(False)  # leaving edit mode cancels both
    assert not window._view.pending_joystick
    assert window._pending_key is None


def test_dragging_the_pad_repositions_it(window):
    attach_frame(window)
    window._on_joystick_placed(0.5, 0.5)  # placement itself leaves edit mode
    window._mode_action.setChecked(True)  # re-enter to drag it around
    view = window._view
    joy = window._profile.joystick

    centre, _radius = view._joystick_geom()
    view.mousePressEvent(
        mouse_event(QEvent.Type.MouseButtonPress, centre.x(), centre.y())
    )
    assert view._joy_selected

    view.mouseMoveEvent(
        mouse_event(QEvent.Type.MouseMove, centre.x() + 3, centre.y() + 4)
    )
    assert (joy.nx, joy.ny) != (0.5, 0.5)  # dragged off the original spot
    assert view.joystick is joy  # the view tracks the same object

    view.mouseReleaseEvent(
        mouse_event(QEvent.Type.MouseButtonRelease, centre.x() + 3, centre.y() + 4)
    )
    assert not view._dragging_joy


def test_remove_joystick_clears_the_pad_everywhere(window):
    window._on_joystick_placed(0.4, 0.4)
    assert window._view.joystick is not None

    window._remove_joystick()
    assert window._profile.joystick is None
    assert window._view.joystick is None

    window._remove_joystick()  # removing nothing is a no-op, not a crash
    assert window._profile.joystick is None


def test_joystick_keys_drive_touches_end_to_end(window):
    session = FakeSession()
    window._session = session
    attach_frame(window)
    use_keybinds(window)
    window._on_joystick_placed(0.5, 0.5)

    window._on_key_pressed("w")
    window._on_key_released("w")

    # DOWN at centre -> slide to deflection -> UP at centre.
    actions = [t[0] for t in session.touches]
    assert actions == [MotionAction.DOWN, MotionAction.MOVE, MotionAction.UP]
    assert window._engine.held_keys == frozenset()


def test_joystick_drives_in_mouse_mode_too(window):
    """The pad owns WASD regardless of the mouse/keybind toggle."""
    session = FakeSession()
    window._session = session
    attach_frame(window)
    window._on_joystick_placed(0.5, 0.5)
    # control target stays at its default (mouse)
    assert window._view.control_target == MirrorView.TARGET_MOUSE

    window._on_key_pressed("w")
    window._on_key_released("w")
    assert [t[0] for t in session.touches] == [
        MotionAction.DOWN,
        MotionAction.MOVE,
        MotionAction.UP,
    ]


def test_placing_the_pad_leaves_edit_mode_so_wasd_isnt_armed(window):
    """Regression: edit mode stayed on after placing, so WASD armed a
    binding instead of driving the pad -- the joystick 'did nothing'."""
    attach_frame(window)
    window._begin_add_joystick()
    assert window._view.edit_mode

    window._view.mousePressEvent(mouse_event(QEvent.Type.MouseButtonPress, 40, 40))
    assert not window._view.edit_mode
    assert window._pending_key is None
    assert window._profile.joystick is not None


# --------------------------------------------------------------------------- #
# Resizing the WASD pad
# --------------------------------------------------------------------------- #


def test_the_size_slider_appears_only_with_a_pad(window):
    assert window._joy_size_slider.isHidden()

    window._on_joystick_placed(0.5, 0.5)
    window._refresh_bindings()

    assert not window._joy_size_slider.isHidden()
    assert window._joy_size_slider.isEnabled()

    window._remove_joystick()
    window._refresh_bindings()
    assert window._joy_size_slider.isHidden(), "no pad, no size control"


def test_the_slider_shows_and_sets_the_pad_radius(window):
    window._on_joystick_placed(0.5, 0.5)
    window._profile.joystick.radius = 0.25
    window._refresh_bindings()
    assert window._joy_size_slider.value() == 25

    window._joy_size_slider.setValue(30)

    assert window._profile.joystick.radius == pytest.approx(0.30)


def test_dragging_the_pad_rim_resizes_it(window):
    attach_frame(window)
    window._on_joystick_placed(0.5, 0.5)
    window._mode_action.setChecked(True)  # re-enter to drag it
    view = window._view
    joy = window._profile.joystick
    before = joy.radius

    centre, radius = view._joystick_geom()
    # Grab the ring itself, not the middle of the pad.
    rim_x = centre.x() + radius * 0.85
    rim_y = centre.y()
    assert view._joystick_radius_at(QPointF(rim_x, rim_y)), "the rim is a handle"

    view.mousePressEvent(mouse_event(QEvent.Type.MouseButtonPress, rim_x, rim_y))
    assert view._resizing_joy
    assert not view._dragging_joy, "the rim resizes; it does not move"

    view.mouseMoveEvent(mouse_event(QEvent.Type.MouseMove, rim_x + 25, rim_y))
    assert joy.radius > before, "dragging outward grows the pad"

    view.mouseReleaseEvent(
        mouse_event(QEvent.Type.MouseButtonRelease, rim_x + 25, rim_y)
    )
    assert not view._resizing_joy


def test_dragging_the_middle_of_the_pad_still_moves_it(window):
    attach_frame(window)
    window._on_joystick_placed(0.5, 0.5)
    window._mode_action.setChecked(True)
    view = window._view
    joy = window._profile.joystick
    before_radius = joy.radius

    centre, _radius = view._joystick_geom()
    view.mousePressEvent(
        mouse_event(QEvent.Type.MouseButtonPress, centre.x(), centre.y())
    )
    assert view._dragging_joy, "the middle still moves the pad"
    assert not view._resizing_joy

    view.mouseMoveEvent(
        mouse_event(QEvent.Type.MouseMove, centre.x() + 4, centre.y() + 4)
    )
    assert (joy.nx, joy.ny) != (0.5, 0.5), "moved"
    assert joy.radius == pytest.approx(before_radius), "but not resized"


# --------------------------------------------------------------------------- #
# The "press a key to bind" recorder
# --------------------------------------------------------------------------- #


def _key_capture():
    from droidmirror.ui.keypickdialog import KeyCaptureDialog

    return KeyCaptureDialog(None)


def _key_event(key, text="", auto_repeat=False):
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtCore import QEvent, Qt

    return QKeyEvent(
        QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier, text
    )


def test_the_recorder_captures_the_next_key(qt_app):
    dialog = _key_capture()
    try:
        dialog.keyPressEvent(_key_event(Qt.Key.Key_W, "w"))
        assert dialog.key_name() == "w"
        assert "W" in dialog._reading.text(), "shows what it caught"
    finally:
        dialog.close()


def test_escape_cancels_the_recording(qt_app):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QDialog

    dialog = _key_capture()
    try:
        dialog.keyPressEvent(_key_event(Qt.Key.Key_Escape))
        assert dialog.key_name() is None
        assert dialog.result() == int(QDialog.DialogCode.Rejected)
    finally:
        dialog.close()


def test_a_lone_modifier_does_not_get_recorded(qt_app):
    from PySide6.QtCore import Qt

    dialog = _key_capture()
    try:
        dialog.keyPressEvent(_key_event(Qt.Key.Key_Shift))
        dialog.keyPressEvent(_key_event(Qt.Key.Key_Control))
        assert dialog.key_name() is None, "still waiting for a real key"
    finally:
        dialog.close()


def test_a_reserved_key_is_refused_with_a_reason(qt_app):
    from PySide6.QtCore import Qt

    dialog = _key_capture()
    try:
        dialog.keyPressEvent(_key_event(Qt.Key.Key_F7))
        assert dialog.key_name() is None, "F7 belongs to the app"
        assert "reserved" in dialog._reading.text().lower()
    finally:
        dialog.close()


def test_the_recorder_captures_a_mouse_button(qt_app):
    from PySide6.QtCore import QEvent, Qt

    dialog = _key_capture()
    try:
        grabbed = dialog.eventFilter(
            None,
            mouse_event(
                QEvent.Type.MouseButtonPress, 500, 500, Qt.MouseButton.RightButton
            ),
        )
        assert grabbed, "the click is consumed, not delivered to the widget"
        assert dialog.key_name() == "mouse_right"
        assert "Right Click" in dialog._reading.text(), "shows what it caught"
    finally:
        dialog.close()


def test_a_click_on_the_add_key_button_is_not_recorded(qt_app):
    from PySide6.QtCore import QEvent, QRect

    dialog = _key_capture()
    try:
        dialog.set_ignore_rect(QRect(460, 460, 80, 80))  # the toggle button
        grabbed = dialog.eventFilter(
            None, mouse_event(QEvent.Type.MouseButtonPress, 500, 500)
        )
        assert not grabbed and dialog.key_name() is None, "its click must land"
    finally:
        dialog.close()


def test_adding_a_binding_opens_the_recorder_and_arms_its_key(window):
    """The panel records a key; a match closes it and arms the binding."""
    from PySide6.QtCore import Qt

    window._begin_add_binding()

    dialog = window._key_dialog
    assert dialog is not None and dialog.isVisible(), "the recorder is up"
    assert window._add_action.isChecked(), "the button reads as a toggle"
    assert window._view.edit_mode

    dialog.keyPressEvent(_key_event(Qt.Key.Key_J, "j"))
    assert dialog.key_name() == "j", "the panel caught the key"
    dialog.accept()  # the panel closes itself after a beat

    assert window._pending_key == "j", "armed straight from the recording"
    assert window._view.pending_key == "j"
    assert "J" in window._pending_label.text()
    assert not window._add_action.isChecked(), "button released after recording"


def test_the_add_key_button_toggles_the_recorder_off(window):
    window._begin_add_binding()
    assert window._key_dialog.isVisible()

    window._begin_add_binding()  # second click: put it away

    assert not window._key_dialog.isVisible()
    assert not window._add_action.isChecked()
    assert window._pending_key is None
    assert not window._view.edit_mode, "cancelling restores the pre-edit state"


def test_escape_cancels_the_recording_without_binding(window):
    from PySide6.QtCore import Qt

    window._begin_add_binding()
    window._key_dialog.keyPressEvent(_key_event(Qt.Key.Key_Escape))

    assert window._pending_key is None
    assert window._view.pending_key is None
    assert "cancel" in window._pending_label.text().lower()
    assert not window._add_action.isChecked()
    assert not window._view.edit_mode, "cancelling restores the pre-edit state"


def test_a_recorded_key_can_then_be_placed_on_the_phone_screen(window):
    from PySide6.QtCore import Qt

    window._begin_add_binding()
    window._key_dialog.keyPressEvent(_key_event(Qt.Key.Key_J, "j"))
    window._key_dialog.accept()
    assert window._pending_key == "j"

    attach_frame(window)
    rect = window._view.frame_rect()
    window._view.mousePressEvent(
        mouse_event(
            QEvent.Type.MouseButtonPress, rect.x + 0.5 * rect.w, rect.y + 0.5 * rect.h
        )
    )

    assert [b.key for b in window._profile.bindings] == ["j"], "the binding landed"
    # One-shot, like the pad and the camera spot: edit mode ends with the
    # placement, so the next "j" press fires the binding instead of arming.
    assert not window._view.edit_mode
    assert not window._add_action.isChecked()


def test_a_bound_mouse_button_fires_its_keybind(window):
    """A recorded left/right click is a keybind like any other key."""
    session = FakeSession()
    window._session = session
    attach_frame(window)
    use_keybinds(window)
    window._on_binding_placed("mouse_right", 0.5, 0.5)

    rect = window._view.frame_rect()
    window._view.mousePressEvent(
        mouse_event(
            QEvent.Type.MouseButtonPress,
            rect.x + 0.5 * rect.w,
            rect.y + 0.5 * rect.h,
            Qt.MouseButton.RightButton,
        )
    )
    window._view.mouseReleaseEvent(
        mouse_event(
            QEvent.Type.MouseButtonRelease,
            rect.x + 0.5 * rect.w,
            rect.y + 0.5 * rect.h,
            Qt.MouseButton.RightButton,
        )
    )

    assert [t[0] for t in session.touches] == [MotionAction.DOWN, MotionAction.UP]


# --------------------------------------------------------------------------- #
# The read-only default profile
# --------------------------------------------------------------------------- #


def test_the_default_profile_refuses_edits(window):
    """The shipped preset never changes; the copy is the way in."""
    from droidmirror.keymap import profile_path

    window._profile.name = "default"

    window._begin_add_binding()  # refused at the door
    assert window._key_dialog is None
    assert not window._view.edit_mode
    assert "read-only" in window._status.text()

    window._mode_action.setChecked(True)  # edit mode bounces too
    assert not window._mode_action.isChecked()
    assert not window._view.edit_mode
    assert "read-only" in window._status.text()

    path = profile_path("default")
    path.unlink(missing_ok=True)
    window._auto_save()
    assert not path.exists(), "nothing ever reaches the preset's file"


def test_default_name_guard_covers_casing_and_padding():
    from droidmirror.keymap import is_default_profile_name

    assert is_default_profile_name("Default")
    assert is_default_profile_name(" default ")
    assert not is_default_profile_name("default copy")


# --------------------------------------------------------------------------- #
# Camera Movement (the x-key look drag's plant point)
# --------------------------------------------------------------------------- #


def test_camera_label_starts_at_the_middle(window):
    assert "50.0% / 50.0%" in window._cam_label.text()
    assert not window._cam_lock_btn.isChecked()


def test_setting_the_camera_spot_moves_it_and_saves(window):
    attach_frame(window)
    window._begin_set_camera()
    assert window._view.pending_camera
    assert window._view.edit_mode

    # Click through the letterboxed frame rect: (0.8, 0.2) of the frame.
    rect = window._view.frame_rect()
    window._view.mousePressEvent(
        mouse_event(
            QEvent.Type.MouseButtonPress,
            rect.x + 0.8 * rect.w,
            rect.y + 0.2 * rect.h,
        )
    )

    cam = window._profile.camera
    assert (round(cam.nx, 2), round(cam.ny, 2)) == (0.8, 0.2)
    assert not window._view.pending_camera  # one-shot, same as the pad
    assert not window._view.edit_mode  # and it leaves edit mode too
    assert "80.0% / 20.0%" in window._cam_label.text()


def test_dragging_the_camera_spot_updates_it(window):
    attach_frame(window)
    window._on_camera_placed(0.5, 0.5)
    window._mode_action.setChecked(True)  # re-enter to drag it around
    view = window._view
    cam = window._profile.camera

    centre, _radius = view._camera_geom()
    view.mousePressEvent(
        mouse_event(QEvent.Type.MouseButtonPress, centre.x(), centre.y())
    )
    assert view._cam_selected

    view.mouseMoveEvent(
        mouse_event(QEvent.Type.MouseMove, centre.x() + 3, centre.y() + 4)
    )
    assert (cam.nx, cam.ny) != (0.5, 0.5)  # dragged off the original spot
    assert view.camera is cam  # the view tracks the same object
    assert "Camera Movement" in window._cam_label.text()

    view.mouseReleaseEvent(
        mouse_event(QEvent.Type.MouseButtonRelease, centre.x() + 3, centre.y() + 4)
    )
    assert not view._dragging_cam


def test_locked_camera_spot_refuses_to_be_dragged(window):
    attach_frame(window)
    window._on_camera_placed(0.5, 0.5)
    window._on_camera_lock_toggled(True)
    window._mode_action.setChecked(True)
    view = window._view
    cam = window._profile.camera

    centre, _radius = view._camera_geom()
    locked_clicks = []
    view.camera_locked_click.connect(lambda: locked_clicks.append(True))
    view.mousePressEvent(
        mouse_event(QEvent.Type.MouseButtonPress, centre.x(), centre.y())
    )
    assert not view._cam_selected
    assert not view._dragging_cam
    assert locked_clicks, "the click must explain the lock, not drag"

    view.mouseMoveEvent(
        mouse_event(QEvent.Type.MouseMove, centre.x() + 5, centre.y() + 5)
    )
    assert (cam.nx, cam.ny) == (0.5, 0.5), "a locked spot stays put"
    assert "(locked)" in window._cam_label.text()


def test_placing_is_refused_while_the_spot_is_locked(window):
    attach_frame(window)
    window._on_camera_placed(0.3, 0.3)
    window._on_camera_lock_toggled(True)

    window._begin_set_camera()
    window._view.mousePressEvent(mouse_event(QEvent.Type.MouseButtonPress, 90, 90))

    assert (window._profile.camera.nx, window._profile.camera.ny) == (0.3, 0.3)
    assert not window._view.pending_camera


def test_camera_lock_survives_a_profile_refresh(window):
    window._on_camera_lock_toggled(True)
    window._refresh_bindings()  # e.g. after loading a profile
    assert window._profile.camera.locked
    assert window._cam_lock_btn.isChecked()


def test_camera_anchor_plants_in_frame_pixels_and_clears_the_edge(window):
    attach_frame(window)  # 100x100 frame
    # Default middle: nx * (size - 1), same mapping as a binding.
    assert window._camera_anchor_px() == (50, 50)

    window._on_camera_placed(0.8, 0.2)
    x, y = window._camera_anchor_px()
    assert (x, y) == (79, 20)

    window._on_camera_placed(0.0, 0.0)  # jammed in the corner...
    from droidmirror.ui.main_window import _edge_band

    x, y = window._camera_anchor_px()
    band_x, band_y = _edge_band(100, 100)
    assert x > band_x and y > band_y  # ...still dragged clear of the edge


def test_camera_anchor_falls_back_to_none_without_a_frame(window):
    window._view.clear()
    assert window._camera_anchor_px() is None


# --------------------------------------------------------------------------- #
# F7: show/hide the keybind markers
# --------------------------------------------------------------------------- #


def test_markers_are_visible_by_default(window):
    assert window._view.overlay_visible
    assert window._overlay_action.isChecked()


def test_the_markers_action_flips_the_overlay(window):
    window._overlay_action.setChecked(False)

    assert not window._view.overlay_visible

    window._overlay_action.setChecked(True)
    assert window._view.overlay_visible


def test_the_toolbar_follows_a_view_side_toggle(window):
    """F7 routed through the view still leaves the button showing the truth."""
    window._view.toggle_overlay()

    assert window._view.overlay_visible is False
    assert window._overlay_action.isChecked() is False


def test_toggling_the_overlay_announces_itself(window):
    window._overlay_action.setChecked(False)
    assert "hidden" in window._status.text()


def test_hiding_markers_leaves_injection_untouched(window):
    """Purely cosmetic: the binding still fires with the markers hidden."""
    session = FakeSession()
    window._session = session
    attach_frame(window)
    use_keybinds(window)
    window._profile.bindings.append(Binding("w", 0.5, 0.5))
    window._engine.set_profile(window._profile)

    window._overlay_action.setChecked(False)
    window._on_key_pressed("w")
    window._on_key_released("w")

    assert [t[0] for t in session.touches] == [MotionAction.DOWN, MotionAction.UP]


def test_f7_cannot_be_bound_as_a_keybind(window, monkeypatch):
    """The hotkey must never arm a binding -- it belongs to the app."""
    from droidmirror.ui import keys as keynames

    assert "f7" in keynames.RESERVED_KEYS
    assert not keynames.is_bindable("f7")


# --------------------------------------------------------------------------- #
# x never steals the mouse while the tablet's keyboard is open
# --------------------------------------------------------------------------- #


def test_camera_lock_is_skipped_while_the_tablet_keyboard_is_open(window, monkeypatch):
    window._mouse_locked = False
    fake = FakeSession()
    fake.keyboard_shown = True
    window._session = fake
    monkeypatch.setattr(
        window, "_toggle_mouse_lock", lambda: pytest.fail("must not lock")
    )

    window._on_key_pressed("x")

    assert not window._mouse_locked
    # x is left completely alone so the letter can be typed: no binding, no
    # lock, nothing consumed.
    assert not window._key_fired


def test_a_bound_x_types_its_letter_while_the_tablet_keyboard_is_open(window):
    """Typing beats a binding.

    The mouse-lock hotkey owns an unbound x, and it was easy to bind x to
    something else. While the tablet's keyboard is up the user is in a text
    field, so a bound x must still type -- otherwise "xylophone" arrives as
    "ylophone" with the letter silently eaten.
    """
    session = FakeSession()
    session.keyboard_shown = True
    window._session = session
    attach_frame(window)
    use_keybinds(window)
    window._profile.bindings.append(Binding("x", 0.5, 0.5))
    window._engine.set_profile(window._profile)

    window._on_key_pressed("x")
    window._on_key_released("x")

    assert [t[0] for t in session.touches] == [], "the binding must not fire"
    assert not window._key_fired

    window._on_text_input("x")
    assert session.texts == ["x"]


def test_the_pad_keys_type_too_while_the_tablet_keyboard_is_open(window):
    """wasd drive the stick, but not while the user is typing a word."""
    session = FakeSession()
    session.keyboard_shown = True
    window._session = session
    window._profile.bindings.append(Binding("w", 0.5, 0.5))
    window._engine.set_profile(window._profile)

    window._on_text_input("w")

    assert session.texts == ["w"], "typing wins over the stick"


def test_named_keys_still_fire_bindings_while_the_tablet_keyboard_is_open(window):
    """Arrows are never part of a word, so they keep working."""
    session = FakeSession()
    session.keyboard_shown = True
    window._session = session
    attach_frame(window)
    use_keybinds(window)
    window._profile.bindings.append(Binding("up", 0.5, 0.5))
    window._engine.set_profile(window._profile)

    window._on_key_pressed("up")

    assert MotionAction.DOWN in [t[0] for t in session.touches], (
        "a named key must still fire its binding while typing"
    )


def test_a_bound_x_still_binds_when_the_tablet_keyboard_is_closed(window):
    """The guard only applies while typing is happening."""
    session = FakeSession()
    session.keyboard_shown = False
    window._session = session
    attach_frame(window)
    use_keybinds(window)
    window._profile.bindings.append(Binding("x", 0.5, 0.5))
    window._engine.set_profile(window._profile)

    window._on_key_pressed("x")
    window._on_key_released("x")

    assert [t[0] for t in session.touches] == [MotionAction.DOWN, MotionAction.UP]


def test_camera_lock_still_works_when_the_tablet_keyboard_is_closed(window, monkeypatch):
    window._mouse_locked = False
    fake = FakeSession()
    fake.keyboard_shown = False
    window._session = fake
    locked = []
    monkeypatch.setattr(window, "_toggle_mouse_lock", lambda: locked.append(True))

    window._on_key_pressed("x")

    assert locked == [True], "nothing about typing should disable the camera lock"


def test_an_unknown_keyboard_state_does_not_block_the_lock(window, monkeypatch):
    window._mouse_locked = False
    fake = FakeSession()
    fake.keyboard_shown = None  # could not read the device
    window._session = fake
    locked = []
    monkeypatch.setattr(window, "_toggle_mouse_lock", lambda: locked.append(True))

    window._on_key_pressed("x")

    assert locked == [True], "unknown must not silently disable the hotkey"


def test_an_explicit_x_binding_still_wins_over_the_keyboard_guard(window):
    """A real binding on x is the user asking for a touch -- always honoured."""
    session = FakeSession()
    window._session = session
    attach_frame(window)
    use_keybinds(window)
    window._profile.bindings.append(Binding("x", 0.5, 0.5))
    window._engine.set_profile(window._profile)

    window._on_key_pressed("x")
    window._on_key_released("x")

    assert [t[0] for t in session.touches] == [MotionAction.DOWN, MotionAction.UP]


def test_mouse_lock_never_touches_the_chrome(window):
    """x hides the cursor; it must not cost the user their keybind controls.

    Not even in fullscreen -- that is F11's business, and Esc brings it back.
    """
    for fullscreen in (False, True):
        window._fullscreen = fullscreen
        window._dock.setVisible(True)
        window.statusBar().setVisible(True)

        window._apply_mouse_lock(True)
        assert not window._dock.isHidden(), f"dock hidden (fullscreen={fullscreen})"
        assert not window.statusBar().isHidden()

        window._apply_mouse_lock(False)
        assert not window._dock.isHidden(), "unlock must not re-show a closed dock"
        assert not window.statusBar().isHidden()


def test_unlock_does_not_reopen_a_closed_dock(window):
    """If the user closed the keybind panel themselves, x leaves it closed."""
    window._dock.setVisible(False)

    window._apply_mouse_lock(True)
    window._apply_mouse_lock(False)

    assert window._dock.isHidden(), "the user's own choice must survive x"


# --------------------------------------------------------------------------- #
# Latency HUD: lag line
# --------------------------------------------------------------------------- #


def test_hud_stays_silent_about_lag_until_it_is_known(window):
    attach_frame(window)
    window._view.hud_lag_s = -1.0
    assert not any(line.startswith("lag") for line in window._view.hud_lines())


def test_hud_reports_staleness_in_seconds(window):
    attach_frame(window)
    window._view.hud_lag_s = 1.25
    lines = window._view.hud_lines()
    assert any(line.startswith("lag") and "1.25" in line for line in lines)


def test_poll_frame_syncs_lag_from_the_session(window):
    attach_frame(window)
    session = FakeSession()
    session.video_lag_s = 7.5
    window._session = session

    window._poll_frame()
    assert window._view.hud_lag_s == 7.5


# --------------------------------------------------------------------------- #
# Laptop keyboard -> device typing
# --------------------------------------------------------------------------- #


class FakeKeyEvent:
    """Minimal QKeyEvent stand-in.

    PySide6 exposes QKeyEvent.isAutoRepeat() but no setter, and auto-repeat
    is exactly what the typing tests need to simulate.
    """

    def __init__(
        self,
        key,
        text="",
        autorepeat=False,
        modifiers=Qt.KeyboardModifier.NoModifier,
    ) -> None:
        self._key = key
        self._text = text
        self._rep = autorepeat
        self._mods = modifiers

    def key(self):
        return self._key

    def text(self):
        return self._text

    def modifiers(self):
        return self._mods

    def isAutoRepeat(self):
        return self._rep

    def accept(self) -> None:
        pass


def press_view_key(
    view,
    key,
    text="",
    autorepeat=False,
    modifiers=Qt.KeyboardModifier.NoModifier,
) -> tuple[list, list]:
    """Drive view.keyPressEvent directly; returns (names, texts) captured."""
    names: list[str] = []
    texts: list[str] = []
    view.key_pressed.connect(names.append)
    view.text_input.connect(texts.append)
    view.keyPressEvent(FakeKeyEvent(key, text, autorepeat, modifiers))
    return names, texts


def test_view_emits_composed_text_for_printable_keys(window):
    names, texts = press_view_key(window._view, Qt.Key.Key_A, "a")
    assert names == ["a"]
    assert texts == ["a"]

    # Shift+1 composes "!" -- Qt hands us the character, no meta math needed.
    _names, texts = press_view_key(window._view, Qt.Key.Key_Exclam, "!")
    assert texts == ["!"]


def test_view_repeats_text_but_not_binding_presses(window):
    """Holding Backspace must keep deleting (text repeats), while a held
    game key still fires its binding exactly once."""
    names, texts = press_view_key(window._view, Qt.Key.Key_Backspace, "\b", autorepeat=True)
    assert names == []  # auto-repeat suppressed for bindings...
    assert texts == ["\b"]  # ...but not for typing


def test_typing_printable_chars_sends_text(window):
    session = FakeSession()
    window._session = session

    window._on_key_pressed("q")  # unbound: nothing consumes the keypress
    window._on_text_input("q")

    assert session.texts == ["q"]


def test_typing_works_in_mouse_mode(window):
    session = FakeSession()
    window._session = session
    assert window._view.control_target == MirrorView.TARGET_MOUSE

    window._on_text_input("h")
    window._on_text_input("i")
    assert session.texts == ["h", "i"]


def test_enter_backspace_and_tab_become_keycodes(window):
    from droidmirror.protocol import AndroidKey, KeyAction

    session = FakeSession()
    window._session = session

    window._on_text_input("\n")
    window._on_text_input("\b")
    window._on_text_input("\t")

    sent = [(action, code) for action, code in session.keys]
    assert sent == [
        (KeyAction.DOWN, AndroidKey.ENTER),
        (KeyAction.UP, AndroidKey.ENTER),
        (KeyAction.DOWN, AndroidKey.DEL),
        (KeyAction.UP, AndroidKey.DEL),
        (KeyAction.DOWN, AndroidKey.TAB),
        (KeyAction.UP, AndroidKey.TAB),
    ]
    assert session.texts == []


def test_edit_mode_blocks_typing(window):
    session = FakeSession()
    window._session = session
    window._mode_action.setChecked(True)  # arming a bind, not typing

    window._on_key_pressed("j")  # arms "j"
    window._on_text_input("j")

    assert session.texts == []
    assert window._pending_key == "j"


def test_bound_key_drives_its_binding_instead_of_typing(window):
    session = FakeSession()
    window._session = session
    attach_frame(window)
    use_keybinds(window)
    window._on_binding_placed("j", 0.5, 0.5)

    window._on_key_pressed("j")  # tap fires on press
    window._on_text_input("j")  # same keystroke's text must be swallowed

    assert session.texts == []
    assert len(session.touches) == 2  # tap: DOWN + UP


def test_unbound_key_types_in_keybind_mode(window):
    session = FakeSession()
    window._session = session
    use_keybinds(window)

    window._on_key_pressed("q")
    window._on_text_input("q")
    assert session.texts == ["q"]


def test_wasd_never_types_while_the_pad_exists(window):
    """Pad keys are movement, not text -- in any case.

    Trade-off, deliberate: while a pad exists the letters a/s/d/w (and their
    Shift forms) drive the stick instead of typing. Everything else types.
    """
    session = FakeSession()
    window._session = session
    window._on_joystick_placed(0.5, 0.5)

    window._on_text_input("w")
    window._on_text_input("A")
    window._on_text_input("s")
    assert session.texts == []

    window._on_text_input("hello")  # other words type fine
    assert session.texts == ["hello"]


def _fake_clipboard(monkeypatch, text: str) -> None:
    """Make QGuiApplication.clipboard().text() return ``text``."""
    from droidmirror.ui import main_window as mw

    class FakeClipboard:
        def text(self) -> str:
            return text

    class FakeApp:
        @staticmethod
        def clipboard() -> FakeClipboard:
            return FakeClipboard()

        @staticmethod
        def platformName() -> str:
            # MainWindow asks this to decide whether the OS cursor is real.
            return "offscreen"

    monkeypatch.setattr(mw, "QGuiApplication", FakeApp)


def test_ctrl_v_pastes_this_computers_clipboard(window, monkeypatch):
    session = FakeSession()
    window._session = session
    _fake_clipboard(monkeypatch, "hello from the laptop")

    names, texts = press_view_key(
        window._view,
        Qt.Key.Key_V,
        "\x16",
        modifiers=Qt.KeyboardModifier.ControlModifier,
    )

    assert names == [] and texts == [], "Ctrl+V is a hotkey, not a key or text"
    assert session.clipboard_sets == [("hello from the laptop", True)]
    assert "laptop" in window._status.text()


def test_ctrl_shift_v_pastes_the_devices_own_clipboard(window):
    session = FakeSession()
    window._session = session

    names, texts = press_view_key(
        window._view,
        Qt.Key.Key_V,
        "\x16",
        modifiers=(
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
        ),
    )

    assert names == [] and texts == []
    assert session.device_pastes == 1
    assert session.clipboard_sets == [], "must not touch the computer clipboard"
    assert "device" in window._status.text().lower()


def test_ctrl_v_with_an_empty_clipboard_says_so(window, monkeypatch):
    session = FakeSession()
    window._session = session
    _fake_clipboard(monkeypatch, "")

    press_view_key(
        window._view,
        Qt.Key.Key_V,
        "\x16",
        modifiers=Qt.KeyboardModifier.ControlModifier,
    )

    assert session.clipboard_sets == []
    assert "empty" in window._status.text().lower()


def test_clipboard_hotkeys_are_inert_without_a_session(window, monkeypatch):
    window._session = None
    _fake_clipboard(monkeypatch, "something")

    press_view_key(
        window._view,
        Qt.Key.Key_V,
        "\x16",
        modifiers=Qt.KeyboardModifier.ControlModifier,
    )

    assert "not connected" in window._status.text().lower()


def test_plain_v_still_types_and_binds(window):
    """Only the Ctrl-modified V is a hotkey; bare V is untouched."""
    session = FakeSession()
    window._session = session

    names, texts = press_view_key(window._view, Qt.Key.Key_V, "v")

    assert names == ["v"]
    assert texts == ["v"]


def test_arrows_navigate_the_device_without_a_binding(window):
    from droidmirror.protocol import AndroidKey, KeyAction

    session = FakeSession()
    window._session = session

    window._on_key_pressed("up")
    assert [(a, k) for a, k in session.keys] == [
        (KeyAction.DOWN, AndroidKey.DPAD_UP),
        (KeyAction.UP, AndroidKey.DPAD_UP),
    ]


# --------------------------------------------------------------------------- #
# Mouse lock (x)
# --------------------------------------------------------------------------- #


def test_x_toggles_the_mouse_lock(window):
    window._on_key_pressed("x")
    assert window._mouse_locked
    assert window._view.cursor().shape() == Qt.CursorShape.BlankCursor, "hidden"
    assert window._look_poll.isActive(), "warp-diff loop running"

    window._on_key_pressed("x")
    assert not window._mouse_locked
    assert window._view.cursor().shape() == Qt.CursorShape.ArrowCursor
    assert not window._look_poll.isActive()


def test_x_toggles_over_and_over(window):
    """x must be a real toggle, not a one-shot lock."""
    for expected in (True, False, True, False, True):
        window._on_key_pressed("x")
        assert window._mouse_locked is expected


def test_lock_survives_a_chrome_change_during_setup(window, monkeypatch):
    """Hiding the dock/status bar can emit a transient ActivationChange.

    changeEvent() must not read that as an alt-tab and undo the lock -- that
    is what made x behave like a one-shot toggle.
    """
    from PySide6.QtCore import QEvent

    original = window._dock.setVisible

    def hide(visible):
        if not visible:
            window.changeEvent(QEvent(QEvent.Type.ActivationChange))
        original(visible)

    monkeypatch.setattr(window._dock, "setVisible", hide)

    window._on_key_pressed("x")
    assert window._mouse_locked, "a chrome change must not cancel the lock"

    window._on_key_pressed("x")
    assert not window._mouse_locked


def test_locking_keeps_keyboard_focus_on_the_mirror(window, monkeypatch):
    focused: list = []
    monkeypatch.setattr(window._view, "setFocus", lambda *a: focused.append(True))

    window._on_key_pressed("x")

    assert focused, "focus must stay on the mirror or x stops being received"


def test_fullscreen_hides_the_chrome_and_restores_it(window):
    """F11 is the only thing that removes the toolbar, dock and status bar."""
    window._dock.setVisible(True)
    window.statusBar().setVisible(True)

    window.set_fullscreen(True)
    assert window._dock.isHidden()
    assert window.statusBar().isHidden()
    assert not window._bar.isVisible()

    window.set_fullscreen(False)
    assert not window._dock.isHidden()
    assert not window.statusBar().isHidden()
    assert window._bar.isVisible()


def test_lock_key_is_consumed_and_never_types(window):
    session = FakeSession()
    window._session = session

    window._on_key_pressed("x")
    window._on_text_input("x")

    assert window._mouse_locked
    assert session.texts == [], "x is the lock key, not a character"


def test_bound_x_fires_the_binding_instead_of_locking(window):
    """Precedence: an explicit x binding wins over the lock shortcut."""
    session = FakeSession()
    window._session = session
    attach_frame(window)
    use_keybinds(window)
    window._on_binding_placed("x", 0.5, 0.5)

    window._on_key_pressed("x")

    assert not window._mouse_locked
    assert [t[0] for t in session.touches] == [MotionAction.DOWN, MotionAction.UP]


def test_edit_mode_still_arms_x(window):
    window._mode_action.setChecked(True)

    window._on_key_pressed("x")

    assert window._pending_key == "x"  # arming beats the lock shortcut
    assert not window._mouse_locked


def test_lock_releases_on_disconnect(window):
    window._on_key_pressed("x")
    assert window._mouse_locked

    window._disconnect()

    assert not window._mouse_locked
    assert window._view.cursor().shape() == Qt.CursorShape.ArrowCursor


def test_lock_releases_on_close(window):
    from PySide6.QtGui import QCloseEvent

    window._on_key_pressed("x")
    assert window._mouse_locked

    window.closeEvent(QCloseEvent())

    assert not window._mouse_locked
    assert window._view.cursor().shape() == Qt.CursorShape.ArrowCursor


def test_lock_key_stays_bindable_in_keynames():
    """The lock key must remain a normal bindable name (not RESERVED)."""
    from droidmirror.ui import keys as keynames

    assert keynames.LOCK_KEY_NAME == "x"
    assert keynames.is_bindable("x")


# --------------------------------------------------------------------------- #
# Mouse-look (x holds the camera drag; the pointer stays parked and hidden)
# --------------------------------------------------------------------------- #


def _look_setup(window, park=(50, 50)):
    """Lock on with a frame attached and a deterministic park point.

    No real pointer exists under the offscreen platform, so the drag's home
    position is pinned to the middle of the 100x100 test frame.
    """
    session = FakeSession()
    window._session = session
    attach_frame(window)
    window._cursor_global = lambda: None  # no pointer to read
    window._centre_point = lambda: park
    window._on_key_pressed("x")
    assert window._mouse_locked
    assert window._look_park == park
    # Pin the drag's home so the expected frame pixels are exact arithmetic
    # (the real one lands on the middle of the letterboxed frame, give or
    # take the rounding of a widget-sized rectangle).
    assert abs(window._look_anchor[0] - 50) <= 1, window._look_anchor
    window._look_anchor = (50, 50)
    return session


def test_raw_look_steers_camera_without_the_user_pressing_anything(window):
    session = _look_setup(window)

    window._on_raw_look(40, 10)

    assert [t[0] for t in session.touches] == [MotionAction.DOWN, MotionAction.MOVE]
    action, pointer, x, y, fw, fh, action_button, buttons = session.touches[0]
    assert pointer == DRAG_POINTER_ID, "the same drag the real mouse makes"
    assert action_button == DRAG_PRESS_BUTTON, "the app holds the button down"
    assert buttons == DRAG_HELD_BUTTONS, "and keeps it held for the game"
    assert (x, y) == window._look_anchor, "the drag starts on the anchor"

    down_x = session.touches[0][2]
    window._on_raw_look(40, 0)

    moves = [t for t in session.touches if t[0] == MotionAction.MOVE]
    assert len(moves) == 2
    gain = 100 / max(1, window._view.width())  # frame 100 wide -> screen px
    assert moves[0][2] == min(int(50 + 40 * gain), 99), "delta scaled to frame px"
    assert moves[-1][2] == min(int(50 + 80 * gain), 99), "deltas accumulate"
    assert moves[0][2] >= down_x

    window._lift_look_finger()
    assert [t[0] for t in session.touches][-1] == MotionAction.UP
    assert not window._look_down


def test_the_camera_look_replays_the_exact_left_button_drag(window):
    """x must inject the gesture the game already answers, byte for byte.

    A look that only *resembled* the drag -- a generic finger with no button
    held -- was quietly ignored by the game while a real left-button drag
    rotated it. The two must never be able to drift apart again.
    """
    session = _look_setup(window)
    view = window._view

    window._on_raw_look(40, 10)
    window._lift_look_finger()
    look_gesture = [(t[0], t[1], t[6], t[7]) for t in session.touches]

    assert look_gesture == [
        (MotionAction.DOWN, DRAG_POINTER_ID, DRAG_PRESS_BUTTON, DRAG_HELD_BUTTONS),
        (MotionAction.MOVE, DRAG_POINTER_ID, 0, DRAG_HELD_BUTTONS),
        (MotionAction.UP, DRAG_POINTER_ID, 0, 0),
    ], "button held for every step, released only at the end"

    window._on_key_pressed("x")  # unlock: the mouse is a mouse again
    assert not window._mouse_locked
    session.touches.clear()

    # The very drag the user would make by hand.
    view.mousePressEvent(mouse_event(QEvent.Type.MouseButtonPress, 20.0, 20.0))
    view.mouseMoveEvent(mouse_event(QEvent.Type.MouseMove, 40.0, 24.0))
    view.mouseReleaseEvent(mouse_event(QEvent.Type.MouseButtonRelease, 40.0, 24.0))
    drag_gesture = [(t[0], t[1], t[6], t[7]) for t in session.touches]

    assert look_gesture == drag_gesture, "the look IS that drag"


def test_the_drag_anchor_sits_clear_of_the_edge_band(window):
    """An anchor *inside* the band re-plants the drag on every single step.

    8% of 1920 is 153.6, so the band and the anchor clamp have to be whole
    pixels that agree, or the camera never gets anywhere.
    """
    from droidmirror.ui.main_window import _edge_band

    for fw, fh in ((100, 100), (1200, 1920), (12, 9)):
        attach_frame(window, width=fw, height=fh)
        x, y = window._frame_centre()
        band_x, band_y = _edge_band(fw, fh)
        assert band_x < x < fw - band_x, (fw, fh, x)
        assert band_y < y < fh - band_y, (fw, fh, y)


def test_a_planted_drag_on_a_real_frame_travels(window):
    """The whole point: plant once, then move -- never plant, re-plant."""
    session = FakeSession()
    window._session = session
    attach_frame(window, width=1200, height=1920)
    window._centre_point = lambda: (50, 50)
    window._on_key_pressed("x")
    window._look_down = False  # nothing planted yet
    session.touches.clear()

    window._on_raw_look(0, 0)

    assert [t[0] for t in session.touches] == [MotionAction.DOWN, MotionAction.MOVE], (
        "plant, then move -- not plant, re-plant"
    )


def test_a_drag_planted_on_the_anchor_travels_instead_of_re_planting(window):
    session = _look_setup(window)

    window._on_raw_look(0, 0)  # the step that used to trigger a re-plant
    window._on_raw_look(9, 0)
    window._on_raw_look(9, 0)

    actions = [t[0] for t in session.touches]
    assert actions.count(MotionAction.DOWN) == 1, "planted once, then moved"
    xs = [t[2] for t in session.touches if t[0] == MotionAction.MOVE]
    assert xs == sorted(xs) and len(set(xs)) == 3, f"the drag must travel: {xs}"


def test_raw_look_does_nothing_while_unlocked(window):
    session = FakeSession()
    window._session = session
    attach_frame(window)

    window._on_raw_look(40, 10)

    assert session.touches == []
    assert not window._look_down


def test_raw_look_clamps_to_the_screen(window):
    session = _look_setup(window)

    window._on_raw_look(10_000_000, 10_000_000)

    x, y = session.touches[-1][2], session.touches[-1][3]
    assert (x, y) == (99, 99), "a wild flick stops at the frame edge"


def test_unlock_lifts_the_look_finger(window):
    session = _look_setup(window)
    window._on_raw_look(30, 30)
    assert window._look_down

    window._on_key_pressed("x")  # unlock

    assert not window._look_down
    assert session.touches[-1][0] == MotionAction.UP, "no finger left behind"


def test_a_locked_left_click_owns_the_camera_drag(window):
    """Holding the left button yourself is the drag, not a stray tap."""
    session = _look_setup(window)
    view = window._view

    view.mousePressEvent(mouse_event(QEvent.Type.MouseButtonPress, 40, 40))

    assert window._look_press, "the press is remembered"
    assert session.touches[0][0] == MotionAction.DOWN
    assert session.touches[0][6] == DRAG_PRESS_BUTTON
    assert session.touches[0][2:4] == window._look_anchor, "planted on the anchor"

    window._on_raw_look(20, 0)  # movement goes into the user's own drag
    assert session.touches[-1][0] == MotionAction.MOVE

    view.mouseReleaseEvent(mouse_event(QEvent.Type.MouseButtonRelease, 40, 40))

    assert not window._look_press
    assert session.touches[-1][0] == MotionAction.UP, "their release lifts it"
    assert not window._look_down


def test_a_held_button_keeps_the_drag_down(window):
    """While the user holds the button, movement must not re-plant it."""
    session = _look_setup(window)
    window._view.mousePressEvent(mouse_event(QEvent.Type.MouseButtonPress, 40, 40))
    session.touches.clear()

    for _ in range(5):
        window._on_raw_look(6, 0)

    assert [t[0] for t in session.touches] == [MotionAction.MOVE] * 5, (
        "one DOWN, then pure movement -- no tapping in the middle of a drag"
    )


def test_lock_lifts_any_half_finished_drag(window):
    """Engaging the lock mid-LMB-drag must not leave a touch held down."""
    session = _look_setup(window)
    window._on_key_pressed("x")  # unlock again: back to plain mouse mode
    session.touches.clear()

    window._view.mousePressEvent(mouse_event(QEvent.Type.MouseButtonPress, 40, 40))
    assert [t[0] for t in session.touches] == [MotionAction.DOWN]

    window._on_key_pressed("x")  # lock while still holding the button

    assert [t[0] for t in session.touches] == [MotionAction.DOWN, MotionAction.UP]
    assert not window._view._mouse_active


def test_hidden_cursor_moves_steer_the_camera(window):
    """The actual runtime path: Qt mouse moves while the cursor is hidden."""
    session = _look_setup(window)
    warped = []
    window._set_cursor_pos = lambda x, y: warped.append((x, y))
    view = window._view
    cx, cy = view.rect().center().x(), view.rect().center().y()

    view.mouseMoveEvent(mouse_event(QEvent.Type.MouseMove, cx, cy))  # sync
    view.mouseMoveEvent(mouse_event(QEvent.Type.MouseMove, cx + 30, cy - 10))

    assert [t[0] for t in session.touches] == [MotionAction.DOWN, MotionAction.MOVE]
    gain_x = 100 / max(1, view.width())
    gain_y = 100 / max(1, view.height())
    assert session.touches[1][2] == min(int(50 + 30 * gain_x), 99)
    assert session.touches[1][3] == int(min(max(50 - 10 * gain_y, 0.0), 99))
    assert warped == [(50, 50)], "and the pointer is put back on its park"


def test_hidden_cursor_sits_on_center_and_moves_nothing(window):
    """The warp-back event itself must not inject anything."""
    session = _look_setup(window)
    view = window._view
    cx, cy = view.rect().center().x(), view.rect().center().y()

    view.mouseMoveEvent(mouse_event(QEvent.Type.MouseMove, cx, cy))

    assert session.touches == []
    assert view._look_last_pos is not None, "position tracked for deltas"


def test_qt_fallback_ignores_the_warp_echo(window):
    """Windows replays SetCursorPos as a move event; it is not a gesture."""
    session = _look_setup(window)
    view = window._view
    cx, cy = view.rect().center().x(), view.rect().center().y()

    view.mouseMoveEvent(mouse_event(QEvent.Type.MouseMove, cx, cy))
    view.mouseMoveEvent(mouse_event(QEvent.Type.MouseMove, cx + 40, cy))
    injected = len(session.touches)

    # The same position the app just warped the cursor to, arriving back.
    view.mouseMoveEvent(mouse_event(QEvent.Type.MouseMove, cx, cy))

    assert len(session.touches) == injected, "the echo must not steer"
    assert view._look_last_pos == QPointF(cx, cy), "but it does re-baseline"


def test_the_poll_reads_the_pointers_travel_and_puts_it_back(window):
    """The guaranteed delta source: where the pointer went since last tick."""
    session = _look_setup(window)
    moved = []
    window._set_cursor_pos = lambda x, y: moved.append((x, y))
    window._pixel_scale = lambda: (1.0, 1.0)
    window._get_cursor_pos = lambda: (90, 55)  # 40 right, 5 down of the park

    window._look_poll_tick()

    assert moved == [(50, 50)], "put straight back on the park"
    assert [t[0] for t in session.touches] == [MotionAction.DOWN, MotionAction.MOVE]
    view = window._view
    gain_x, gain_y = 100 / view.width(), 100 / view.height()
    assert session.touches[-1][2] == min(int(50 + 40 * gain_x), 99)
    assert session.touches[-1][3] == min(int(50 + 5 * gain_y), 99)


def test_the_poll_injects_nothing_when_the_pointer_has_not_moved(window):
    session = _look_setup(window)
    window._pixel_scale = lambda: (1.0, 1.0)
    window._get_cursor_pos = lambda: (50, 50)  # still on the park

    window._look_poll_tick()

    assert session.touches == [], "a still mouse is not movement"


def test_raw_deltas_win_over_the_pointer_so_movement_is_not_doubled(window):
    """Raw and the pointer report the same travel; only one may count."""
    session = _look_setup(window)
    raw = FakeRawLook(batches=[(60, 0)])
    window._raw_look = raw
    window._raw_active = True
    window._pixel_scale = lambda: (1.0, 1.0)
    window._get_cursor_pos = lambda: (110, 50)  # the same 60px, seen twice

    window._look_poll_tick()

    view = window._view
    gain_x = 100 / view.width()
    assert session.touches[-1][2] == min(int(50 + 60 * gain_x), 99), "counted once"


# Sound (toolbar Mute)


def test_mute_button_silences_the_running_session(window):
    session = FakeSession()
    window._session = session

    window._mute_action.setChecked(True)

    assert window._muted
    assert session.muted is True
    assert "muted" in window._status.text().lower()

    window._mute_action.setChecked(False)

    assert not window._muted
    assert session.muted is False


def test_mute_is_remembered_between_launches(window):
    window._mute_action.setChecked(True)

    assert window._settings.get("muted") is True
    assert MainWindow()._mute_action.isChecked(), "the next launch starts muted"


def test_connecting_carries_the_mute_toggle_to_the_session(window, monkeypatch):
    from droidmirror.ui import main_window as mw

    class SilentSession(FakeSession):
        def __init__(self, adb, **kwargs) -> None:
            super().__init__()

        def start(self, device=None) -> None:
            pass

    monkeypatch.setattr(mw, "MirrorSession", SilentSession)
    window._mute_action.setChecked(True)
    window._adb = object()
    window._device_combo.clear()
    window._device_combo.addItem("fake device", object())

    window._connect()

    assert window._session.muted is True, "the new stream starts silenced"


# -- tablet volume --------------------------------------------------------- #
# The tablet's media volume is a different control from Mute above: muting
# silences this computer, these change the device itself.


def test_the_volume_slider_is_dead_until_a_level_has_been_read(window):
    """Never let it be dragged against a level nobody has read yet."""
    assert not window._vol_slider.isEnabled()
    assert window._vol_readout.text() == "-"


def test_a_reading_drives_the_slider_and_its_range(window):
    session = FakeSession()
    session.volume_state = VolumeState(index=7, maximum=15, muted=False)
    window._session = session

    window._refresh_volume()

    assert window._vol_slider.isEnabled()
    assert window._vol_slider.minimum() == 0
    assert window._vol_slider.maximum() == 15, "the device's own range, not a guess"
    assert window._vol_slider.value() == 7
    assert window._vol_readout.text() == "7/15"


def test_a_muted_tablet_is_still_shown_with_its_level(window):
    session = FakeSession()
    session.volume_state = VolumeState(index=0, maximum=15, muted=True)
    window._session = session

    window._refresh_volume()

    assert "(muted)" in window._vol_slider.toolTip()
    assert window._vol_readout.text() == "0/15"


def test_an_unreadable_level_disables_the_slider_rather_than_showing_zero(window):
    """A blank is honest; `0/15` would claim the tablet had been turned down."""
    session = FakeSession()  # volume_state stays None
    window._session = session

    window._refresh_volume()

    assert not window._vol_slider.isEnabled()
    assert window._vol_readout.text() == "-"


def test_reading_the_level_is_never_written_back_to_the_device(window):
    """Setting the slider from a reading fires valueChanged; it must not send."""
    session = FakeSession()
    session.volume_state = VolumeState(index=9, maximum=15, muted=False)
    window._session = session

    window._refresh_volume()

    assert session.volume_sets == [], "a read is not a write"


def test_a_drag_sends_the_tablets_own_volume_keys(window):
    """The fix for the lag. No debounce, no adb round trip, one press a step.

    The slider used to wait out a 180 ms debounce and *then* spend a further
    ~193 ms (measured) on an absolute write, so the tablet sat still for over a
    third of a second while the slider already showed the new number. The keys
    go over the control socket the session already holds open, so they land
    while the finger is still moving.
    """
    session = FakeSession()
    session.volume_state = VolumeState(index=5, maximum=15, muted=False)
    window._session = session
    window._refresh_volume()

    for value in range(6, 12):  # a drag emits one event per pixel
        window._vol_slider.setValue(value)

    press = int(AndroidKey.VOLUME_UP)
    assert session.keys == [(KeyAction.DOWN, press), (KeyAction.UP, press)] * 6
    assert session.volume_sets == [], "the fast path must not also spend a write"
    assert window._vol_readout.text() == "11/15"


def test_a_drag_back_down_sends_the_other_key(window):
    session = FakeSession()
    session.volume_state = VolumeState(index=11, maximum=15, muted=False)
    window._session = session
    window._refresh_volume()

    window._vol_slider.setValue(9)

    down = int(AndroidKey.VOLUME_DOWN)
    assert session.keys == [(KeyAction.DOWN, down), (KeyAction.UP, down)] * 2
    assert session.volume_sets == []


def test_a_drag_with_no_reading_sends_nothing(window):
    """Without a reading there is no distance to send, only a guess."""
    session = FakeSession()  # volume_state stays None
    window._session = session

    window._vol_slider.setValue(4)

    assert session.keys == []
    assert session.volume_sets == []


def slider_x_for(slider, value: int) -> float:
    """The x the slider draws ``value`` at.

    Worked out here rather than asked of the widget, so a broken mapping cannot
    agree with itself; the style is only consulted for the groove's geometry.
    """
    option = QStyleOptionSlider()
    slider.initStyleOption(option)
    groove = slider.style().subControlRect(
        QStyle.ComplexControl.CC_Slider,
        option,
        QStyle.SubControl.SC_SliderGroove,
        slider,
    )
    handle = slider.style().subControlRect(
        QStyle.ComplexControl.CC_Slider,
        option,
        QStyle.SubControl.SC_SliderHandle,
        slider,
    )
    fraction = (value - slider.minimum()) / (slider.maximum() - slider.minimum())
    return groove.x() + handle.width() / 2 + (groove.width() - handle.width()) * fraction


def a_live_volume_bar(window, level: int = 15) -> FakeSession:
    """A window whose volume slider is live, showing ``level``/15."""
    session = FakeSession()
    session.volume_state = VolumeState(index=level, maximum=15, muted=False)
    window._session = session
    window._refresh_volume()
    window.show()
    return session


def test_clicking_the_volume_bar_lands_on_the_point_clicked(window):
    """The bar answered a click with a page step, not with the position.

    QSlider moves by ``pageStep`` on a press in the groove -- 10 by default --
    so on this device's 0..15 range a click anywhere left of the handle landed
    on ``15 - 10 = 5``, and anywhere right of it jumped to the top, whichever
    part of the bar was aimed at. Every target below answers 5 to a page step,
    so none of them can pass by accident.
    """
    session = a_live_volume_bar(window)

    slider = window._vol_slider
    assert slider.width() > 40, "too narrow to tell two positions apart"

    for target in (11, 2):
        x = slider_x_for(slider, target)
        slider.mousePressEvent(
            mouse_event(QEvent.Type.MouseButtonPress, x, slider.height() / 2)
        )
        slider.mouseReleaseEvent(
            mouse_event(QEvent.Type.MouseButtonRelease, x, slider.height() / 2)
        )

        assert slider.value() == target, f"a click at {target}/15 landed on {slider.value()}"

    # And the jump goes out as the tablet's own volume keys -- 15 -> 11 and
    # 11 -> 2, all downward -- not as an absolute write.
    down = int(AndroidKey.VOLUME_DOWN)
    assert session.keys == [(KeyAction.DOWN, down), (KeyAction.UP, down)] * 13
    assert session.volume_sets == []
    assert window._vol_readout.text() == "2/15"


def test_a_press_on_the_volume_bar_still_hands_over_to_a_drag(window):
    """Clicking jumps, and dragging on from there keeps working.

    The jump happens *before* the base class sees the press, so the base class
    finds the handle under the cursor and starts a drag from it. If that
    hand-off broke, the press would page-step and this drag would not track.
    """
    session = a_live_volume_bar(window, level=9)

    slider = window._vol_slider
    x = slider_x_for(slider, 9)
    slider.mousePressEvent(mouse_event(QEvent.Type.MouseButtonPress, x, slider.height() / 2))
    assert slider.value() == 9, "the press itself did not land"

    session.keys.clear()
    x = slider_x_for(slider, 13)
    slider.mouseMoveEvent(mouse_event(QEvent.Type.MouseMove, x, slider.height() / 2))
    slider.mouseReleaseEvent(
        mouse_event(QEvent.Type.MouseButtonRelease, x, slider.height() / 2)
    )

    up = int(AndroidKey.VOLUME_UP)
    assert slider.value() == 13, "the drag stopped tracking after the click"
    assert session.keys == [(KeyAction.DOWN, up), (KeyAction.UP, up)] * 4


def test_a_drag_that_lands_short_is_written_exactly_once(window):
    """The safety net. A keycode moves the tablet one step from where it *was*.

    If the device was not where we believed -- its own volume buttons pressed,
    another app changing it -- the drag lands on the wrong number. The reading
    after the drag says so, and the number is then written exactly, once, so
    the slider always ends up on the value it was put on.
    """
    session = FakeSession()
    session.volume_state = VolumeState(index=5, maximum=15, muted=False)
    window._session = session
    window._refresh_volume()

    window._vol_slider.setValue(8)  # three steps up, sent as keys
    session.keys.clear()
    assert session.volume_sets == [], "the drag itself writes nothing"

    session.volume_state = VolumeState(index=6, maximum=15, muted=False)  # only got to 6
    window._refresh_volume()  # the read that follows a drag

    assert session.volume_sets == [8], "written exactly, and only once"
    assert window._vol_slider.value() == 8
    assert window._vol_readout.text() == "8/15"


def test_a_drag_that_lands_where_it_was_aimed_costs_no_write(window):
    session = FakeSession()
    session.volume_state = VolumeState(index=5, maximum=15, muted=False)
    window._session = session
    window._refresh_volume()

    window._vol_slider.setValue(8)
    session.volume_state = VolumeState(index=8, maximum=15, muted=False)
    window._refresh_volume()

    assert session.volume_sets == [], "the keys did their job; no second command"


def test_a_refused_correction_says_so_instead_of_pretending(window):
    class RefusingSession(FakeSession):
        def set_device_volume(self, index: int) -> bool:
            return False

    session = RefusingSession()
    session.volume_state = VolumeState(index=5, maximum=15, muted=False)
    window._session = session
    window._refresh_volume()

    window._vol_slider.setValue(8)
    session.volume_state = VolumeState(index=6, maximum=15, muted=False)
    window._refresh_volume()

    assert "did not accept" in window._status.text()


def test_a_step_moves_the_readout_on_faith(window):
    """A keycode reports nothing back, so the belief is what the UI shows."""
    session = FakeSession()
    session.volume_state = VolumeState(index=5, maximum=15, muted=False)
    window._session = session
    window._refresh_volume()

    window._step_volume(1)

    assert window._volume_level == 6
    assert window._vol_slider.value() == 6, "and the slider follows it"
    assert window._vol_readout.text() == "6/15"


def test_a_step_below_the_bottom_does_not_go_negative(window):
    session = FakeSession()
    session.volume_state = VolumeState(index=0, maximum=15, muted=False)
    window._session = session
    window._refresh_volume()

    window._step_volume(-1)

    assert window._volume_level == 0
    assert window._vol_readout.text() == "0/15"
    assert session.keys, "the tablet still gets its key -- it may be out of step"


def test_a_step_cancels_a_drag_it_cannot_honour(window):
    """A step is relative, so a number from a drag is stale the moment it lands.

    Without this, the sync read after the step would see the tablet one off
    where the slider claimed and "correct" it back to the dragged value --
    undoing the step the user just made with a write they did not ask for.
    """
    session = FakeSession()
    session.volume_state = VolumeState(index=5, maximum=15, muted=False)
    window._session = session
    window._refresh_volume()

    window._vol_slider.setValue(8)  # a drag: slider says 8, target 8
    window._step_volume(1)  # then the +/- button
    assert window._volume_target is None

    session.volume_state = VolumeState(index=9, maximum=15, muted=False)
    window._refresh_volume()

    assert session.volume_sets == [], "no write for a number nobody asked for"


def test_disconnecting_drops_the_level_and_cancels_the_sync(window):
    session = FakeSession()
    session.volume_state = VolumeState(index=5, maximum=15, muted=False)
    window._session = session
    window._refresh_volume()

    window._vol_slider.setValue(8)
    assert window._vol_sync_timer.isActive()

    window._disconnect()

    assert not window._vol_sync_timer.isActive(), "no read with no device left"
    assert not window._vol_slider.isEnabled()
    assert window._vol_readout.text() == "-"
    assert session.volume_sets == []
    assert window._volume_level is None, "the belief goes with the device"
    assert window._volume_target is None


def test_the_volume_slider_is_kept_out_of_the_toolbar(window):
    """The toolbar's overflow contract cannot hold a slider.

    Everything in the toolbar that does not fit slides into the » menu and
    stays reachable. A slider has no menu form, so a permanent one there either
    vanishes with no way back or silently eats the width _rebuild_overflow()
    had budgeted for buttons -- measured, the row drew over itself by 25px at a
    1200px window. Hence the status bar, which has no such arithmetic.
    """
    in_bar = [
        window._bar_layout.itemAt(i).widget()
        for i in range(window._bar_layout.count())
    ]
    assert window._vol_slider not in in_bar
    assert window._vol_slider.parent() is not window._bar


# -- volume up/down buttons ------------------------------------------------ #
# Discrete steps rather than a level: the tablet's *own* volume key over the
# control socket, which is what the slider sends too. Instant, and the device
# raises its volume panel, so the change is visible on the tablet itself.


def test_the_up_down_buttons_are_dead_until_something_is_connected(window):
    assert not window._vol_plus.isEnabled()
    assert not window._vol_minus.isEnabled()


def test_volume_up_sends_the_tablets_own_volume_key(window):
    session = FakeSession()
    window._session = session

    window._step_volume(1)

    press = int(AndroidKey.VOLUME_UP)
    assert session.keys == [(KeyAction.DOWN, press), (KeyAction.UP, press)]
    assert session.volume_sets == [], "the keycode path must not also write"


def test_volume_down_sends_the_other_key(window):
    session = FakeSession()
    window._session = session

    window._step_volume(-1)

    press = int(AndroidKey.VOLUME_DOWN)
    assert session.keys == [(KeyAction.DOWN, press), (KeyAction.UP, press)]


def test_a_step_refreshes_the_level_once_the_presses_stop(window):
    """A keycode reports nothing back, so the readout would drift."""
    session = FakeSession()
    session.volume_state = VolumeState(index=5, maximum=15, muted=False)
    window._session = session
    window._refresh_volume()

    window._step_volume(1)

    assert window._vol_sync_timer.isActive(), "the shown level is now stale"


def test_the_buttons_still_work_when_the_level_cannot_be_read(window):
    """This is the one control that survives a device that will not report."""
    session = FakeSession()  # volume_state stays None: the read failed
    window._session = session
    window._refresh_volume()

    assert not window._vol_slider.isEnabled(), "the slider needs a reading"

    window._vol_plus.setEnabled(True)  # what a connect does
    window._step_volume(1)

    assert session.keys, "still able to change the volume"


def test_a_step_with_no_session_is_harmless(window):
    window._session = None
    window._step_volume(1)  # must not raise
    assert not window._vol_sync_timer.isActive()


def test_disconnecting_locks_the_up_down_buttons_too(window):
    session = FakeSession()
    window._session = session
    window._vol_minus.setEnabled(True)
    window._vol_plus.setEnabled(True)

    window._step_volume(1)
    assert window._vol_sync_timer.isActive()

    window._disconnect()

    assert not window._vol_minus.isEnabled()
    assert not window._vol_plus.isEnabled()
    assert not window._vol_sync_timer.isActive()


def test_a_minimized_window_never_flings_the_cursor_anywhere(window, monkeypatch):
    """A minimized window reports a 0x0 client area, whose 'center' is (0,0).

    That is the top-left corner of the desktop, and it is exactly where the
    pointer used to appear on its own -- so an unmeasurable window must mean
    "leave the cursor alone", never "assume it is at the origin".
    """
    session = FakeSession()
    window._session = session
    attach_frame(window)
    window._cursor_global = lambda: None
    moved = []
    window._set_cursor_pos = lambda x, y: moved.append((x, y))
    monkeypatch.setattr(window, "isMinimized", lambda: True)

    window._on_key_pressed("x")  # locking must not fling it either
    window._warp_to_center()
    window._park_cursor()
    window._release_stale_clip()

    assert moved == [], "no guess: the pointer stays where the user left it"


def test_the_cursor_guard_rejects_points_off_the_desktop(window, monkeypatch):
    """Only real screen coordinates may be passed to SetCursorPos."""
    monkeypatch.setattr(window, "_virtual_screen", lambda: (100, 100, 800, 600))

    assert window._on_screen(500, 400), "middle of the desktop"
    assert window._on_screen(100, 100), "its top-left, inclusive"
    assert window._on_screen(899, 699), "its bottom-right, inclusive"

    assert not window._on_screen(0, 0), "the corner the pointer kept jumping to"
    assert not window._on_screen(99, 400), "left of the leftmost monitor"
    assert not window._on_screen(900, 400)
    assert not window._on_screen(500, 700)


def test_set_cursor_pos_refuses_a_point_outside_the_desktop(window, monkeypatch):
    """The guard has to be on the call itself, not only on its callers."""
    import ctypes
    from types import SimpleNamespace

    from droidmirror.ui import main_window as mw

    moved = []
    fake_user32 = SimpleNamespace(
        SetCursorPos=lambda x, y: (moved.append((x, y)), 1)[1]
    )
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(user32=fake_user32))
    monkeypatch.setattr(
        mw, "QGuiApplication", SimpleNamespace(platformName=lambda: "windows")
    )
    monkeypatch.setattr(window, "_virtual_screen", lambda: (100, 100, 800, 600))

    window._set_cursor_pos(0, 0)  # the old fling-to-the-corner
    window._set_cursor_pos(500, 400)

    assert moved == [(500, 400)]


def test_unlock_parks_the_cursor_back_on_center(window):
    _look_setup(window)
    warped = []
    window._set_cursor_pos = lambda x, y: warped.append((x, y))

    window._on_key_pressed("x")  # unlock: cursor reappears in the middle

    assert warped[-1] == (50, 50), "back on the middle of the mirror"


def test_look_relocates_the_finger_at_the_device_edge(window):
    """Hitting the far side of the device screen must NOT stop the drag.

    The finger is lifted and re-planted at the center, so the next step
    keeps rotating -- infinite 360-degree camera spin.
    """
    session = _look_setup(window)

    window._on_raw_look(1000, 0)  # a long drag straight to the right edge

    actions = [t[0] for t in session.touches]
    assert actions == [
        MotionAction.DOWN,   # planted at the center
        MotionAction.UP,     # lifted at the edge
        MotionAction.DOWN,   # re-planted at the center
        MotionAction.MOVE,   # and travelling again
    ]
    # Both plants are on center; the drag did not simply stall at the edge.
    assert (session.touches[0][2], session.touches[0][3]) == (50, 50)
    assert (session.touches[2][2], session.touches[2][3]) == (50, 50)
    assert session.touches[-1][2] > 50, "still moving right after the warp"

    # A second long drag relocates again instead of wedging at the edge.
    before = len(session.touches)
    window._on_raw_look(1000, 0)
    assert MotionAction.UP in [t[0] for t in session.touches[before:]]
    assert session.touches[-1][2] > 50


class FakeRawLook:
    """Stands in for the Win32 raw-input collector."""

    def __init__(self, batches=None) -> None:
        self.batches = list(batches or [])
        self.active = False
        self.started_with = None

    def start(self, hwnd) -> bool:
        self.active = True
        self.started_with = hwnd
        return True

    def stop(self) -> None:
        self.active = False

    def take(self):
        return self.batches.pop(0) if self.batches else (0, 0)


def _locked_with_pointer(window, geometry=(0, 0, 200, 200)):
    """Lock on with a pointer we can actually move, read and park.

    The mirror rect is 200x200 physical while the widget is wider than that,
    so the pointer's travel also exercises the DPI scaling.
    """
    session = FakeSession()
    window._session = session
    attach_frame(window)
    window._look_geometry = lambda: geometry
    window._cursor_global = lambda: None  # start from the middle of the mirror
    middle = (geometry[2] // 2, geometry[3] // 2)
    cursor = [middle[0], middle[1]]
    moved: list[tuple[int, int]] = []

    def get():
        return (cursor[0], cursor[1])

    def put(x, y):
        cursor[0], cursor[1] = x, y
        moved.append((x, y))

    window._get_cursor_pos = get
    window._set_cursor_pos = put
    window._on_key_pressed("x")

    assert not window._raw_active, "raw input has no window to register with"
    assert window._view.look_move is None, "Qt moves must not double-count"
    park = window._look_park
    assert abs(park[0] - middle[0]) <= 1 and abs(park[1] - middle[1]) <= 1, park
    cursor[:] = list(park)
    window._look_anchor = (50, 50)  # exact arithmetic for the frame deltas
    return session, cursor, moved


def test_locking_parks_the_pointer_in_the_middle_of_the_mirror(window):
    """The pointer is held still and hidden, not left to roam to the edge."""
    session, _, moved = _locked_with_pointer(window)

    assert moved[-1] == window._look_park, "put back on its park"
    assert session.touches == [], "parking is not movement"


def test_the_pointer_is_put_back_on_its_park_every_tick(window):
    """Movement is read, then the pointer is returned: it never travels."""
    session, cursor, moved = _locked_with_pointer(window)
    park = window._look_park
    moved.clear()

    cursor[0] = park[0] + 20  # the user moved 20 physical px right
    window._look_poll_tick()

    assert moved == [park], "put straight back on its park"
    assert tuple(cursor) == park
    assert [t[0] for t in session.touches] == [MotionAction.DOWN, MotionAction.MOVE]
    # 20 physical px across a 200px-wide mirror == 10% of the 100px frame.
    assert session.touches[-1][2] == 60


def test_the_pointer_travel_maps_through_the_dpi_scale(window):
    """Physical pixels are not view pixels at other DPI settings."""
    session, cursor, _ = _locked_with_pointer(window, geometry=(0, 0, 400, 400))
    park = window._look_park

    cursor[0] = park[0] + 20  # 20 physical px out of 400 == 5% of the frame
    window._look_poll_tick()

    assert session.touches[-1][2] == min(int(50 + 20 * (100 / 400)), 99)


def test_lock_uses_the_devices_own_deltas_when_they_arrive(window):
    """Raw input, when the OS delivers it, is the cleanest source of all."""
    session = FakeSession()
    window._session = session
    attach_frame(window)
    raw = FakeRawLook(batches=[(0, 0), (300, 0), (0, 400)])
    window._raw_look = raw
    window._cursor_global = lambda: None
    window._centre_point = lambda: (50, 50)

    window._on_key_pressed("x")
    assert window._raw_active and raw.active

    window._look_poll_tick()
    assert session.touches == [], "a quiet mouse injects nothing"

    window._look_poll_tick()

    assert session.touches[0][0] == MotionAction.DOWN, "the drag is planted"
    first_move = next(t for t in session.touches if t[0] == MotionAction.MOVE)
    assert first_move[2] > session.touches[0][2], "steered right"
    assert raw.batches == [(0, 400)], "drained"

    window._look_poll_tick()

    assert session.touches[-1][3] > first_move[3], "and down on the next batch"

    window._on_key_pressed("x")  # unlock
    assert not raw.active, "and it unregisters raw input"


def test_the_pointer_is_never_moved_off_the_mirror(window, monkeypatch):
    """The reported jump: the pointer turned up at an odd screen corner.

    Whatever computes a target, the one call that moves the pointer checks it
    against the mirror first -- so a bad coordinate can never fling it out.
    """
    import ctypes
    from types import SimpleNamespace

    from droidmirror.ui import main_window as mw

    moved = []
    monkeypatch.setattr(
        ctypes,
        "windll",
        SimpleNamespace(
            user32=SimpleNamespace(
                SetCursorPos=lambda x, y: (moved.append((x, y)), 1)[1]
            )
        ),
    )
    monkeypatch.setattr(
        mw, "QGuiApplication", SimpleNamespace(platformName=lambda: "windows")
    )
    monkeypatch.setattr(window, "_virtual_screen", lambda: (0, 0, 1920, 1080))
    monkeypatch.setattr(window, "_mirror_rect", lambda: (300, 200, 1125, 875))

    window._set_cursor_pos(0, 0)       # the screen's top-left corner
    window._set_cursor_pos(2000, 100)  # off the desktop entirely
    window._set_cursor_pos(862, 637)   # the middle of the mirror

    assert moved == [(862, 637)], "only a point that IS the mirror"


def test_the_pointer_is_left_alone_while_raw_deltas_arrive(window):
    """When the device reports its own deltas, nothing touches the pointer."""
    session = _look_setup(window)
    window._raw_look = FakeRawLook(batches=[(25, 0)])
    window._raw_active = True
    moved = []
    window._set_cursor_pos = lambda x, y: moved.append((x, y))
    window._get_cursor_pos = lambda: (999, 999)  # somewhere else entirely

    window._look_poll_tick()

    assert window._raw_seen
    assert session.touches[-1][0] == MotionAction.MOVE
    assert moved == [], "no pointer movement at all while raw is delivering"


def test_it_falls_back_to_the_pointer_when_raw_stays_silent(window):
    """No raw delivery + a moving pointer means the pointer is the source."""
    session = _look_setup(window)
    window._raw_look = FakeRawLook()  # registered, but never says anything
    window._raw_active = True
    window._pixel_scale = lambda: (1.0, 1.0)
    park = window._look_park
    moved = []
    window._set_cursor_pos = lambda x, y: moved.append((x, y))
    window._get_cursor_pos = lambda: (park[0] + 40, park[1])

    window._look_poll_tick()

    assert window._pointer_driven, "raw is silent and the pointer is moving"
    assert moved == [park], "and the pointer is put back on its park"
    assert session.touches[-1][2] > session.touches[0][2], "the travel steered"

    # If the raw stream wakes up later it must not be counted on top: the same
    # movement would be added twice.
    before = len(session.touches)
    window._raw_look.batches.append((500, 0))
    window._get_cursor_pos = lambda: park
    window._look_poll_tick()

    assert len(session.touches) == before, "one source of deltas, not two"


def test_the_pointer_is_put_back_only_once_it_has_left_the_mirror(window):
    _look_setup(window)
    moved = []
    window._set_cursor_pos = lambda x, y: moved.append((x, y))
    window._inside_mirror = lambda point: point == window._look_park

    window._get_cursor_pos = lambda: window._look_park
    window._keep_pointer_on_the_mirror()
    assert moved == [], "still on the mirror: don't churn it"

    window._get_cursor_pos = lambda: (999, 999)
    window._keep_pointer_on_the_mirror()
    assert moved == [window._look_park], "wandered off: bring it back"


def test_lock_still_reads_qt_moves_when_the_pointer_cannot_be_read(window):
    class DeadRaw(FakeRawLook):
        def start(self, hwnd) -> bool:
            return False

    window._raw_look = DeadRaw()
    window._get_cursor_pos = lambda: None  # no pointer we can read or park
    session = _look_setup(window)

    assert not window._raw_active
    assert window._view.look_move == window._on_look_move

    window._view.mouseMoveEvent(
        mouse_event(QEvent.Type.MouseMove, 50.0, 50.0)
    )
    cx, cy = window._view.rect().center().x(), window._view.rect().center().y()
    window._view.mouseMoveEvent(mouse_event(QEvent.Type.MouseMove, cx + 30, cy))

    assert session.touches[-1][0] == MotionAction.MOVE


def test_unlock_restores_the_plain_mouse(window):
    window._look_geometry = lambda: (0, 0, 100, 100)
    window._get_cursor_pos = lambda: (50, 50)
    window._on_key_pressed("x")
    assert window._view.look_move is None, "the poll is the source while locked"

    window._on_key_pressed("x")  # unlock

    assert window._view.look_move == window._on_look_move
    assert window._look_park is None
    assert window._look_anchor is None


# --------------------------------------------------------------------------- #
# On-screen key chips (label + hit area sized to the text)
# --------------------------------------------------------------------------- #


def _chip(window, key: str, nx: float = 0.5, ny: float = 0.5):
    attach_frame(window)
    window._on_binding_placed(key, nx, ny)
    return next(b for b in window._view._bindings if b.key == key)


def test_key_chip_is_sized_to_its_label(window):
    space = _chip(window, "space")
    w_key = _chip(window, "w", 0.7, 0.3)
    view = window._view

    space_rect = view._marker_rect(space)
    w_rect = view._marker_rect(w_key)

    assert space_rect.width() > w_rect.width(), "longer label = wider chip"
    assert space_rect.height() == w_rect.height(), "one chip height for all"
    assert w_rect.width() >= 42.0, "short labels keep the old footprint"


def test_click_lands_on_the_whole_wide_chip(window):
    """SPACE's chip sticks out past the old 21px circle -- clicks hit it."""
    space = _chip(window, "space")
    view = window._view
    rect = view._marker_rect(space)
    outside_old_circle = QPointF(rect.left() + 4, rect.center().y())

    hit = view._binding_at(outside_old_circle)

    assert hit is not None and hit.key == "space"


def test_click_past_the_chip_still_misses(window):
    space = _chip(window, "space")
    view = window._view
    rect = view._marker_rect(space)

    hit = view._binding_at(QPointF(rect.left() - 30, rect.center().y()))

    assert hit is None


# --------------------------------------------------------------------------- #
# Scroll wheel -> touch flick
# --------------------------------------------------------------------------- #


def _wheel(window, notches: float, x: float = 50, y: float = 50):
    """Drive one wheel step and return the session it injects into."""
    session = FakeSession()
    window._session = session
    attach_frame(window)
    window._view._note_wheel(notches, QPointF(x, y))
    return session, window._view


def test_wheel_flicks_a_finger_to_scroll(window):
    session, view = _wheel(window, 1.0)

    # The stroke opens with a finger DOWN at the cursor -- generic finger id,
    # no mouse buttons, so Android treats it as a plain touch.
    assert len(session.touches) == 1
    action, pointer, x, y, w, h, action_button, buttons = session.touches[0]
    assert action == MotionAction.DOWN
    assert pointer == POINTER_ID_GENERIC_FINGER
    assert (action_button, buttons) == (0, 0)

    view._finish_stroke()  # MOVE + UP complete the flick
    assert [t[0] for t in session.touches] == [
        MotionAction.DOWN,
        MotionAction.MOVE,
        MotionAction.UP,
    ]
    down_y = session.touches[0][3]
    move_x, move_y = session.touches[1][2:4]
    # One notch = 5% of screen height. Wheel up (positive) -> content moves
    # down (+y) -- the same direction a finger would push it.
    assert move_y == pytest.approx(down_y + 0.05 * h, abs=1)
    assert move_x == session.touches[0][2]
    assert session.touches[2][2:4] == (move_x, move_y)  # lands where it moved
    assert not view._wheel_active  # stroke fully drained


def test_wheel_up_and_down_scroll_in_opposite_directions(window):
    session, view = _wheel(window, -1.0)
    down_y = session.touches[0][3]
    view._finish_stroke()
    assert session.touches[1][3] == pytest.approx(down_y - 0.05 * 100, abs=1)


def test_wheel_bursts_chain_into_capped_strokes(window):
    """A fast spin coalesces: notches bank mid-stroke, each follow-up stroke
    flicks at most 3 notches so the list scrolls far, not teleport-far."""
    session, view = _wheel(window, 10.0)

    # Stroke 1 carries 3 of the 10 notches; the other 7 are banked.
    assert [t[0] for t in session.touches] == [MotionAction.DOWN]

    for expected_events in (4, 7, 10):
        view._finish_stroke()  # MOVE + UP, then a chained DOWN for the bank
        assert len(session.touches) == expected_events
    view._finish_stroke()  # last stroke (1 notch): MOVE + UP, nothing banked
    assert len(session.touches) == 12

    actions = [t[0] for t in session.touches]
    assert actions.count(MotionAction.DOWN) == 4  # 3+3+3+1 notches
    assert actions.count(MotionAction.MOVE) == 4
    assert actions.count(MotionAction.UP) == 4
    assert not view._wheel_active
    assert view._wheel_accum == 0.0


def test_wheel_is_ignored_in_edit_mode(window):
    from PySide6.QtCore import QPointF

    session = FakeSession()
    window._session = session
    attach_frame(window)
    window._mode_action.setChecked(True)

    window._view._note_wheel(1.0, QPointF(50, 50))
    assert session.touches == [], "arranging markers must not scroll"


# --------------------------------------------------------------------------- #
# Wireless (adb over Wi-Fi)
# --------------------------------------------------------------------------- #


class FakeAdb:
    """Just enough adb for the wireless toolbar flow."""

    def __init__(self, usb=None, address="192.168.1.42:5555", connect_ok=True) -> None:
        self._usb = usb
        self._wireless = None
        self._address = address
        self._connect_ok = connect_ok
        self.serial = usb.serial if usb is not None else None
        self.enable_calls: list[int] = []
        self.connect_calls: list[str] = []

    def devices(self, timeout=30.0):
        # Mirrors Adb.devices(timeout=...): the GUI passes a short one so a
        # wedged adb cannot freeze the window.
        return [d for d in (self._usb, self._wireless) if d is not None]

    def enable_wireless(self, port=5555):
        self.enable_calls.append(port)
        self._wireless = Device(serial=self._address, state="device")
        self.serial = self._address
        return self._address

    def connect(self, address, **kwargs):
        self.connect_calls.append(address)
        if self._connect_ok:
            self._wireless = Device(serial=address, state="device")
        return "connected"

    def is_connected(self, address):
        return any(d.serial == address and d.is_ready for d in self.devices())


def test_switch_to_wifi_button_is_gone(window):
    """The separate action was merged into the transport (filter) button."""
    assert not hasattr(window, "_wireless_action")


def test_filter_click_without_adb_still_plainly_flips(window, monkeypatch):
    asked = []
    monkeypatch.setattr(window, "_ask_wifi_address", lambda *a: asked.append(a))

    window._filter_action.trigger()

    assert asked == [], "no adb: nothing to connect, so no prompt"
    assert window._filter_action.isChecked(), "the plain flip still happens"


def test_wireless_offers_the_usb_device_address_and_connects(window, monkeypatch):
    """With USB attached we tcpip first and connect the discovered address.

    ``enable_wireless`` only returns once the device is reachable, so this
    path never has to ask the user for anything.
    """
    usb = Device(serial="USB123", state="device")
    adb = FakeAdb(usb)
    window._adb = adb
    window._refresh_devices()
    asked = []
    monkeypatch.setattr(
        window, "_ask_wifi_address", lambda prefill, history: asked.append(prefill)
    )

    window._connect_wireless()

    assert adb.enable_calls == [5555]
    assert asked == [], "a device we just switched needs no address from the user"
    selected = window._device_combo.currentData()
    assert selected.serial == "192.168.1.42:5555"
    assert "192.168.1.42:5555" in window._status.text()
    # The list is filtered by transport, so going wireless flips it over.
    assert window._filter_action.isChecked()
    assert not window._usb_action.isChecked()


def test_wireless_prompts_for_an_address_when_no_usb_device(window, monkeypatch):
    adb = FakeAdb(None, address="10.0.0.9:5555")
    window._adb = adb
    monkeypatch.setattr(
        window, "_ask_wifi_address", lambda prefill, history: "10.0.0.9"
    )

    window._connect_wireless()

    # A bare IP gets the standard adb TCP port appended.
    assert adb.connect_calls == ["10.0.0.9:5555"]
    assert adb.enable_calls == []
    assert window._device_combo.currentData().serial == "10.0.0.9:5555"


def test_wireless_cancelled_prompt_does_nothing(window, monkeypatch):
    adb = FakeAdb(None)
    window._adb = adb
    monkeypatch.setattr(window, "_ask_wifi_address", lambda prefill, history: None)

    window._connect_wireless()
    assert adb.connect_calls == []


# -- the Add button -------------------------------------------------------- #
# Auto-discovery (the Wi-Fi button) only ever finds the device the app already
# knows about -- so until Add existed there was no way to bring in a *second*
# tablet or phone, or the same one on an address that changed.


def test_add_is_faint_and_dead_in_usb_mode(window, monkeypatch):
    """Faint in USB mode, as asked: there is nothing to add by hand there.

    A cabled device appears in the list the moment it is plugged in, so Add
    would have nothing to do.
    """
    adb = FakeAdb(Device(serial="USB123", state="device"))
    window._adb = adb
    window._set_wireless_mode(False)
    asked = []
    monkeypatch.setattr(window, "_ask_wifi_address", lambda *a: asked.append(a))

    assert not window._add_device_action.isEnabled(), "Qt draws it faint"

    # A disabled QAction still fires triggered() when called on directly, so
    # the handler has to refuse as well -- the button is not the only guard.
    window._add_device_action.trigger()

    assert asked == [], "no prompt from a button that is meant to be dead"


def test_add_lights_up_in_wireless_mode(window):
    window._adb = FakeAdb(None)
    window._set_wireless_mode(True)

    assert window._add_device_action.isEnabled()


def test_add_needs_adb_like_everything_else(window):
    window._adb = None
    window._set_wireless_mode(True)

    assert not window._add_device_action.isEnabled()


def test_add_asks_for_an_address_and_connects_it(window, monkeypatch):
    """The whole point: a device that is not the one already in the list."""
    adb = FakeAdb(None, address="10.0.1.7:5555")
    window._adb = adb
    window._set_wireless_mode(True)
    asked = []
    monkeypatch.setattr(
        window,
        "_ask_wifi_address",
        lambda prefill, history: asked.append(prefill) or "10.0.1.7",
    )

    window._add_device_action.trigger()

    assert asked == [""], "a fresh field: an old address is history, not a default"
    assert adb.connect_calls == ["10.0.1.7:5555"]
    assert window._device_combo.currentData().serial == "10.0.1.7:5555"
    assert window._wireless_mode is True, "adding must not drop the filter to USB"
    assert not window._wireless_busy


def test_add_keeps_the_wireless_filter_when_cancelled(window, monkeypatch):
    """Unlike the automatic search, Add never flipped the list -- so no restore.

    The search flips to USB while it looks and has to put that back; Add is
    entered from Wi-Fi mode and must leave the transport buttons alone.
    """
    adb = FakeAdb(None, address="10.0.0.9:5555")
    adb._wireless = Device(serial="10.0.0.9:5555", state="device")
    window._adb = adb
    window._set_wireless_mode(True)
    monkeypatch.setattr(window, "_ask_wifi_address", lambda prefill, history: None)

    window._add_device_action.trigger()

    assert window._wireless_mode is True, "cancelling Add knocked us to USB"
    assert window._filter_action.isChecked()
    assert window._add_device_action.isEnabled(), "still addable: we are on Wi-Fi"
    assert not window._wireless_busy, "the flow stands down instead of wedging"


def test_add_remembers_an_address_that_worked(window, monkeypatch):
    adb = FakeAdb(None)
    window._adb = adb
    window._set_wireless_mode(True)
    monkeypatch.setattr(
        window, "_ask_wifi_address", lambda prefill, history: "10.0.1.7"
    )

    window._add_device_action.trigger()

    assert window._settings.get("wireless_ips") == ["10.0.1.7:5555"]


def test_add_shows_up_in_front_of_refresh(window):
    """Where he asked for it."""
    order = [action.text() for _button, action in window._bar_items]

    assert order.index("Add") + 1 == order.index("Refresh")


def test_the_add_button_is_drawn_faint_when_it_cannot_be_used(window, qt_app):
    """He asked for it translucent in USB mode, not merely disabled.

    A disabled QAction is only drawn faded if the *stylesheet* says so, and
    every button in this row is styled by _build_toolbar's sheet: a sheet that
    sets a colour with no ``:disabled`` variant leaves a dead button looking
    exactly like a live one, which is why the check that matters here is the
    rendered ink and not ``isEnabled()``. The tests run on the offscreen
    plugin, so nothing appears on screen.
    """
    window._adb = object()  # _sync_filter_label only asks whether adb exists
    button = next(b for b, action in window._bar_items if action.text() == "Add")
    window.show()

    def ink() -> int:
        image = button.grab().toImage()
        values = [
            (pixel.red() * 299 + pixel.green() * 587 + pixel.blue() * 114) // 1000
            for y in range(image.height())
            for x in range(image.width())
            if (pixel := image.pixelColor(x, y)).alpha() >= 40
        ]
        assert values, "the button rendered no text to measure"
        return max(values)

    window._wireless_mode = True
    window._sync_filter_label()
    qt_app.processEvents()
    live = ink()

    window._wireless_mode = False
    window._sync_filter_label()
    qt_app.processEvents()
    faint = ink()

    assert not button.isEnabled(), "USB mode still offers Add"
    assert live != faint, "the dead button is drawn exactly like the live one"


def test_wireless_rejects_a_non_address(window, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    adb = FakeAdb(None)
    window._adb = adb
    monkeypatch.setattr(
        window, "_ask_wifi_address", lambda prefill, history: "not an ip"
    )
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a))

    window._connect_wireless()

    assert adb.connect_calls == []
    assert warned, "the user must be told the address is bad"


def test_clicking_toward_wireless_asks_for_the_ip(window, monkeypatch):
    """The transport button IS the switch-to-Wi-Fi flow now."""
    adb = FakeAdb(None, address="10.0.0.7:5555")
    window._adb = adb
    seen = {}
    monkeypatch.setattr(
        window,
        "_ask_wifi_address",
        lambda prefill, history: seen.update(prefill=prefill, history=history)
        or "10.0.0.7",
    )

    window._filter_action.trigger()  # the user click: USB -> Wireless

    assert adb.connect_calls == ["10.0.0.7:5555"]
    assert seen["prefill"] == "", "first time: nothing to pre-fill yet"
    assert window._filter_action.isChecked()
    assert not window._usb_action.isChecked()
    assert window._filter_action.text() == "Wi-Fi"


def test_cancelling_the_asked_ip_stays_on_usb(window, monkeypatch):
    adb = FakeAdb(None)
    window._adb = adb
    monkeypatch.setattr(window, "_ask_wifi_address", lambda prefill, history: None)

    window._filter_action.trigger()

    assert adb.connect_calls == []
    assert not window._filter_action.isChecked(), "cancel leaves the mode as it was"
    assert not window._wireless_mode
    # A cancelled search must leave the USB button lit, not a half-lit pair.
    assert window._usb_action.isChecked()


def test_a_failed_search_leaves_the_transport_buttons_unmixed(window, monkeypatch):
    """The 'blue USB' bug: an error must not leave both buttons lit."""
    from PySide6.QtWidgets import QMessageBox

    adb = FakeAdb(None, address="10.0.0.9:5555", connect_ok=False)
    window._adb = adb
    monkeypatch.setattr(
        window, "_ask_wifi_address", lambda prefill, history: "10.0.0.9"
    )
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: None)

    window._filter_action.trigger()

    assert not window._filter_action.isChecked(), "Wi-Fi button must not stay lit"
    assert window._usb_action.isChecked(), "USB button back to normal"
    assert window._wireless_mode is False


def test_the_prompt_opens_on_the_last_ip_that_worked(window, monkeypatch):
    window._adb = FakeAdb(None)
    window._settings.set("wireless_ips", ["10.0.0.9:5555", "192.168.1.4:5555"])
    seen = {}
    monkeypatch.setattr(
        window,
        "_ask_wifi_address",
        lambda prefill, history: seen.update(prefill=prefill, history=history),
    )

    window._connect_wireless()

    assert seen["prefill"] == "10.0.0.9:5555", "pre-entered, ready to confirm"
    assert seen["history"] == ["10.0.0.9:5555", "192.168.1.4:5555"], (
        "the whole past is handed to the sliding bar"
    )


def test_a_successful_connect_moves_its_ip_to_the_front(window, monkeypatch):
    adb = FakeAdb(None, address="10.0.0.9:5555")
    window._adb = adb
    window._settings.set("wireless_ips", ["192.168.1.4:5555"])
    monkeypatch.setattr(
        window, "_ask_wifi_address", lambda prefill, history: "10.0.0.9"
    )

    window._connect_wireless()

    assert window._settings.get("wireless_ips") == [
        "10.0.0.9:5555",
        "192.168.1.4:5555",
    ]
    # Next time around the device is already on the network, so the flow finds
    # it by itself; the prompt is only reached once that stops working.
    adb._wireless = None  # pretend it dropped off the network again
    seen = {}
    monkeypatch.setattr(
        window,
        "_ask_wifi_address",
        lambda prefill, history: seen.update(prefill=prefill) or "",
    )
    window._connect_wireless()
    assert seen["prefill"] == "10.0.0.9:5555"


def test_a_failed_connect_is_not_remembered(window, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    adb = FakeAdb(None, address="10.0.0.9:5555", connect_ok=False)
    window._adb = adb
    monkeypatch.setattr(
        window, "_ask_wifi_address", lambda prefill, history: "10.0.0.9"
    )
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: None)

    window._connect_wireless()

    assert not window._settings.get("wireless_ips"), "only working addresses count"


def test_wireless_survives_an_unexpected_exception(window, monkeypatch):
    """Whatever blows up inside the flow must be reported, not fatal -- the
    whole app used to die with the console on run.bat."""
    from PySide6.QtWidgets import QMessageBox

    adb = FakeAdb(None)
    window._adb = adb

    def boom(port=5555):
        raise RuntimeError("tcpip exploded")

    monkeypatch.setattr(adb, "enable_wireless", boom)
    # No USB device here, so force the USB branch for the raise.
    adb._usb = Device(serial="USB123", state="device")
    boxes = []
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: boxes.append(a))
    monkeypatch.setattr("droidmirror.ui.main_window._log_flow_error", lambda *a: None)

    window._connect_wireless()  # must not raise

    assert boxes, "the failure is reported to the user"
    assert "tcpip exploded" in boxes[0][2]
    assert window._status.text() == "Wireless setup failed"
    assert not window._wireless_busy, "the flow stands down instead of wedging"
    assert not window._filter_action.isChecked(), "still cleanly on USB"


def test_wifi_prompt_failure_is_reported_not_fatal(window, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    window._adb = FakeAdb(None)
    boxes = []
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: boxes.append(a))
    monkeypatch.setattr("droidmirror.ui.main_window._log_flow_error", lambda *a: None)

    def broken_dialog(*a, **k):
        raise RuntimeError("dialog ctor exploded")

    monkeypatch.setattr("droidmirror.ui.main_window.WifiConnectDialog", broken_dialog)

    assert window._ask_wifi_address("", []) is None
    assert boxes, "the user sees why the prompt did not open"


def test_the_adb_flow_never_blocks_the_caller(window, qt_app):
    """The AppHang killer: a slow adb must run off the GUI thread.

    ``_run_worker`` returns immediately and delivers the result through the
    event loop, so a stalled device can't freeze (and get killed) the app.
    """
    window._sync_workers = False  # the real, threaded path
    delivered = []

    def slow_adb():
        time.sleep(0.3)
        return "done"

    def on_done(result):
        delivered.append(result)

    started = time.perf_counter()
    window._run_worker(slow_adb, on_done)
    elapsed = time.perf_counter() - started

    assert elapsed < 0.1, "the caller returned while adb was still running"
    assert not delivered, "nothing arrives until the event loop spins"

    deadline = time.perf_counter() + 3.0
    while not delivered and time.perf_counter() < deadline:
        qt_app.processEvents()
        time.sleep(0.01)
    assert delivered == ["done"]


def test_a_stalled_adb_worker_reports_an_error_instead_of_dying(window, qt_app):
    window._sync_workers = False
    results = []

    def boom():
        raise RuntimeError("adb blew up")

    window._run_worker(boom, lambda r: results.append(r))

    deadline = time.perf_counter() + 3.0
    while not results and time.perf_counter() < deadline:
        qt_app.processEvents()
        time.sleep(0.01)
    assert results and isinstance(results[0], RuntimeError)


def test_a_second_wireless_click_while_busy_is_ignored(window, monkeypatch):
    """The flow is single-flight: no two concurrent adb flows."""
    window._wireless_busy = True
    started = []
    monkeypatch.setattr(
        window, "_wireless_stage1", lambda h: started.append(h) or ("", False)
    )
    window._connect_wireless()
    assert started == [], "a click during a busy flow must be ignored"


# --------------------------------------------------------------------------- #
# Wi-Fi address prompt (history slides open as a bar)
# --------------------------------------------------------------------------- #


def _wifi_dialog(**kwargs):
    from droidmirror.ui.wifidialog import WifiConnectDialog

    return WifiConnectDialog(None, **kwargs)


def test_wifi_dialog_reports_the_typed_address_stripped(qt_app):
    dialog = _wifi_dialog(prefill="10.0.0.9:5555", history=[])
    try:
        assert dialog.address() == "10.0.0.9:5555"
        dialog._address.setText("  192.168.1.4  ")
        assert dialog.address() == "192.168.1.4"
    finally:
        dialog.close()


def test_wifi_dialog_history_bar_slides_open_and_fills_the_field(qt_app):
    dialog = _wifi_dialog(
        prefill="",
        history=["10.0.0.9:5555", "192.168.1.4:5555"],
    )
    try:
        assert not dialog._history.isHidden(), "the bar is offered"
        assert dialog._history.count() == 2
        assert dialog._history.currentIndex() == -1, "placeholder until picked"

        dialog._history.activated.emit(1)  # the slide-down pick

        assert dialog.address() == "192.168.1.4:5555"
    finally:
        dialog.close()


def test_wifi_dialog_hides_the_bar_when_there_is_no_history(qt_app):
    dialog = _wifi_dialog(history=[])
    try:
        assert dialog._history.isHidden()
    finally:
        dialog.close()


def test_wifi_dialog_connect_button_accepts(qt_app):
    from PySide6.QtWidgets import QDialog, QDialogButtonBox

    dialog = _wifi_dialog(prefill="10.0.0.5", history=[])
    box = dialog.findChild(QDialogButtonBox)
    assert box is not None
    box.button(QDialogButtonBox.StandardButton.Ok).click()

    assert dialog.result() == int(QDialog.DialogCode.Accepted)
    assert dialog.address() == "10.0.0.5"


def test_wifi_dialog_enter_and_cancel_paths(qt_app):
    from PySide6.QtWidgets import QDialog, QDialogButtonBox

    dialog = _wifi_dialog(prefill="10.0.0.5", history=[])
    QTest.keyClick(dialog._address, Qt.Key.Key_Return)
    assert dialog.result() == int(QDialog.DialogCode.Accepted)

    dialog = _wifi_dialog(prefill="10.0.0.5", history=[])
    box = dialog.findChild(QDialogButtonBox)
    box.button(QDialogButtonBox.StandardButton.Cancel).click()
    assert dialog.result() == int(QDialog.DialogCode.Rejected)
    assert dialog.address() == "10.0.0.5", "cancel still leaves the text readable"


def test_help_action_shows_the_wireless_guide(window, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from droidmirror.ui import main_window as mw

    boxes: list = []
    monkeypatch.setattr(
        QMessageBox, "exec", lambda self: boxes.append(self) or 0
    )

    window.show_help()

    assert len(boxes) == 1, "Help must open exactly one dialog"
    text = boxes[0].text()
    assert "IP address" in text, "must explain where to find the device IP"
    assert "tcpip" in text, "must say the device has to be in TCP mode"
    assert "Wi-Fi" in text
    # The guide lives in a module constant so it can be reviewed/tested.
    assert mw.HELP_TEXT == text


def test_help_action_is_wired_to_the_dialog(window, monkeypatch):
    calls: list = []
    monkeypatch.setattr(window, "show_help", lambda: calls.append(True))

    window._help_action.trigger()

    assert calls == [True]


def test_help_is_available_without_adb(window):
    """Help has nothing to do with adb being present."""
    window._adb = None
    window._refresh_devices()
    assert window._help_action.isEnabled()


def test_link_label_shows_usb_for_a_cable_device(window):
    window._adb = FakeAdb(Device(serial="USB123", state="device"))
    window._refresh_devices()

    assert window._link_label.text() == "link: USB"


def test_link_label_shows_wifi_for_a_network_device(window):
    window._adb = FakeAdb(Device(serial="10.0.0.9:5555", state="device"))
    window._filter_action.setChecked(True)  # wireless mode

    assert window._link_label.text() == "link: Wi-Fi (10.0.0.9:5555)"


def _both_transports(window):
    """A USB device and a wireless one attached at the same time."""
    adb = FakeAdb(Device(serial="USB123", state="device", model="SM-X210"))
    adb._wireless = Device(serial="10.0.0.9:5555", state="device", model="SM-X210")
    window._adb = adb
    window._refresh_devices()
    return adb


def test_filter_shows_only_usb_devices(window):
    _both_transports(window)

    assert window._filter_action.text() == "Wi-Fi"
    assert window._usb_action.isChecked(), "USB button lit in USB mode"
    assert not window._filter_action.isChecked(), "Wi-Fi button dark"
    serials = [
        window._device_combo.itemData(i).serial
        for i in range(window._device_combo.count())
    ]
    assert serials == ["USB123"], "wireless devices must not appear in USB mode"


def test_filter_switch_shows_only_wireless_devices(window):
    _both_transports(window)

    window._filter_action.setChecked(True)  # flip to wireless

    assert window._filter_action.isChecked(), "Wi-Fi button lit"
    assert not window._usb_action.isChecked(), "USB button dark"
    serials = [
        window._device_combo.itemData(i).serial
        for i in range(window._device_combo.count())
    ]
    assert serials == ["10.0.0.9:5555"], "USB devices must not appear in wireless mode"


def test_filter_switch_back_lists_usb_again(window):
    _both_transports(window)
    window._filter_action.setChecked(True)
    window._filter_action.setChecked(False)

    assert window._usb_action.isChecked()
    assert not window._filter_action.isChecked()
    assert window._device_combo.count() == 1
    assert window._device_combo.itemData(0).serial == "USB123"


def test_filter_switch_refreshes_automatically(window):
    """Toggling re-scans adb (the user must not have to press Refresh)."""
    adb = FakeAdb(Device(serial="USB123", state="device"))
    window._adb = adb
    window._refresh_devices()

    calls = []
    original = adb.devices
    adb.devices = lambda *a, **k: calls.append(1) or original(*a, **k)

    window._filter_action.setChecked(True)

    assert calls, "switching modes must refresh the device list"


def test_empty_mode_says_which_transport_is_missing(window):
    window._adb = FakeAdb(Device(serial="USB123", state="device"))
    window._refresh_devices()

    window._filter_action.setChecked(True)  # no wireless device attached

    assert "no wireless device" in window._device_combo.itemText(0)
    assert not window._connect_action.isEnabled()


def test_filter_mode_is_remembered_between_launches(window, qt_app):
    window._filter_action.setChecked(True)  # choose wireless
    window.close()

    again = MainWindow(adb=None)
    try:
        assert again._wireless_mode is True
        assert again._filter_action.isChecked()
        assert not again._usb_action.isChecked()
    finally:
        again.close()


def test_remembered_usb_mode_survives_a_wireless_foray(window, qt_app):
    window._filter_action.setChecked(True)
    window._filter_action.setChecked(False)  # back to USB
    window.close()

    again = MainWindow(adb=None)
    try:
        assert again._wireless_mode is False
        assert again._usb_action.isChecked(), "USB button lit again"
        assert not again._filter_action.isChecked()
    finally:
        again.close()


def test_first_launch_picks_wireless_when_only_wireless_is_attached(qt_app):
    """No saved preference: start on whatever transport is present."""
    adb = FakeAdb(Device(serial="10.0.0.9:5555", state="device"))
    fresh = MainWindow(adb=adb)
    try:
        assert fresh._wireless_mode is True
        assert fresh._filter_action.isChecked()
    finally:
        fresh.close()


def test_link_label_resets_without_a_device(window):
    window._adb = None
    window._refresh_devices()

    assert window._link_label.text() == "link: -"


def test_device_list_tags_each_entry_with_its_transport(window):
    window._adb = FakeAdb(
        Device(serial="USB123", state="device", model="SM-X210")
    )
    window._refresh_devices()

    assert window._device_combo.itemText(0).endswith("- USB")


def test_wireless_reports_a_failed_connection(window, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    adb = FakeAdb(None, connect_ok=False)
    window._adb = adb
    monkeypatch.setattr(window, "_ask_wifi_address", lambda prefill, history: "10.0.0.9")
    shown = []
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: shown.append(a))

    window._connect_wireless()

    assert shown, "a connection that never came up must be surfaced"
    assert "failed" in window._status.text().lower()


# --------------------------------------------------------------------------- #
# Toolbar: one row, ever (plus the Keys panel toggle)
# --------------------------------------------------------------------------- #


def _settled(window):
    """Resize + let the layout actually re-lay out, as the app does."""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    window.resize(window.width(), 720)
    if app is not None:
        app.processEvents()
    window._rebuild_overflow()


def test_the_toolbar_row_never_wraps_to_a_second_line(window):
    """A wrapped row used to be clipped, taking half the buttons with it.

    A QHBoxLayout cannot wrap, so the bar's height must stay at one row at any
    window width -- that is the permanent fix, not another round of shortening
    labels.
    """
    window.show()  # an unshown window has no layout to measure
    for width in (1600, 1200, 900, 700):
        window.resize(width, 720)
        _settled(window)
        assert window._bar.height() <= 46, f"wrapped at {width}px"


def test_f7_works_in_fullscreen_where_the_toolbar_is_gone(window):
    """The toolbar is hidden in fullscreen, and a hidden toolbar's action
    shortcut never fires -- F7 used to do nothing there."""
    window.show()
    window.set_fullscreen(True)
    app = QApplication.instance()
    if app is not None:
        app.processEvents()
    assert not window._bar.isVisible(), "precondition: no toolbar in fullscreen"

    f7 = QKeyEvent(
        QEvent.Type.KeyPress, Qt.Key.Key_F7, Qt.KeyboardModifier.NoModifier, ""
    )
    window._view.keyPressEvent(f7)
    assert window._view.overlay_visible is False, "F7 must still work"

    window._view.keyPressEvent(f7)
    assert window._view.overlay_visible is True, "and toggle back"
    assert window._overlay_action.isChecked(), "the toolbar button stays in sync"


def test_f9_latency_hud_also_works_in_fullscreen(window):
    """Same defect as F7: the HUD is a toolbar action too."""
    window.show()
    window.set_fullscreen(True)
    assert not window._bar.isVisible()

    f9 = QKeyEvent(
        QEvent.Type.KeyPress, Qt.Key.Key_F9, Qt.KeyboardModifier.NoModifier, ""
    )
    window._view.keyPressEvent(f9)

    assert window._view.hud_enabled is True
    assert window._hud_action.isChecked()


def test_one_f7_press_toggles_exactly_once(window):
    """A single press must flip it once -- not zero, not twice."""
    window.show()
    f7 = lambda: QKeyEvent(  # noqa: E731
        QEvent.Type.KeyPress, Qt.Key.Key_F7, Qt.KeyboardModifier.NoModifier, ""
    )
    assert window._view.overlay_visible is True
    window._view.keyPressEvent(f7())
    assert window._view.overlay_visible is False
    window._view.keyPressEvent(f7())
    assert window._view.overlay_visible is True


def test_the_disconnect_label_is_never_elided(window):
    """Connect -> Disconnect must widen the button.

    The width was measured once while the button said "Connect" and never
    updated, so on connect Qt painted the longer word into the shorter box as
    "Disconn...".
    """
    window.resize(1900, 900)
    window.show()
    button = next(
        b for b, a in window._bar_items if a is window._connect_action
    )

    window._connect_action.setText("Connect")
    window._bar.layout().activate()
    window._connect_action.setText("Disconnect")
    window._bar.layout().activate()

    assert button.text() == "Disconnect"
    assert button.width() >= button.sizeHint().width(), (
        f"{button.text()!r} needs {button.sizeHint().width()}px but the "
        f"button is {button.width()}px, so Qt elides it"
    )


def test_no_toolbar_label_is_ever_clipped(window):
    """Regression: the row squeezed buttons below their text and Qt elided
    them -- "Remove pad" rendered as "Remove" and the row looked like the
    labels were overlapping. Every visible button must fit its own text."""
    window.show()
    window._on_panel_toggled(False)
    for width in (2200, 1920, 1600, 1400, 1200):
        window.resize(width, 720)
        _settled(window)
        for button, action in window._bar_items:
            if not button.isVisible():
                continue
            needed = button.fontMetrics().horizontalAdvance(action.text()) + 6
            assert button.width() >= needed, (
                f"{action.text()!r} clipped at {width}px "
                f"({button.width()} < {needed})"
            )


def test_toolbar_buttons_are_wide_enough_for_their_own_size_hint(window):
    """The "Re...sh" bug: width was text + a margin, but the style sheet's
    padding is reserved *inside* the button, so Qt elided the middle of the
    label. A button narrower than its sizeHint always elides."""
    window.show()
    window._on_panel_toggled(False)
    for width in (2200, 1920, 1600, 1200):
        window.resize(width, 720)
        _settled(window)
        for button, action in window._bar_items:
            if not button.isVisible():
                continue
            assert button.width() >= button.sizeHint().width(), (
                f"{action.text()!r} will elide at {width}px "
                f"({button.width()} < sizeHint {button.sizeHint().width()})"
            )


def test_nothing_is_lost_when_the_toolbar_overflows(window):
    """Whatever does not fit moves into the menu -- nothing disappears."""
    window.show()
    window.resize(600, 720)
    _settled(window)
    visible = {a.text() for b, a in window._bar_items if b.isVisible()}
    menu = (
        {a.text() for a in window._overflow.menu().actions()}
        if window._overflow is not None and window._overflow.isVisible()
        else set()
    )
    everything = {a.text() for _b, a in window._bar_items}

    assert visible | menu == everything, "a button vanished"
    assert not (visible & menu), "a button is in both places"


def test_the_toolbar_restores_buttons_when_there_is_room_again(window):
    """Widening brings the tail of the row back out of the menu."""
    window.show()
    window._on_panel_toggled(False)  # the dock itself takes ~600px of the row
    window.resize(700, 720)
    _settled(window)
    hidden_at_narrow = sum(1 for b, _a in window._bar_items if not b.isVisible())

    # Wide enough for the whole row (20 buttons with readable minimums).
    window.resize(2600, 720)
    _settled(window)

    assert hidden_at_narrow > 0, "the narrow case really did overflow"
    assert all(b.isVisible() for b, _a in window._bar_items), "they all came back"


def test_the_keys_panel_toggles_and_is_remembered(window):
    window.show()
    window._dock.setVisible(True)

    window._on_panel_toggled(False)
    assert window._dock.isHidden(), "panel hidden"
    assert window._settings.get("keys_panel") is False

    window._on_panel_toggled(True)
    assert not window._dock.isHidden(), "panel back"
    assert window._settings.get("keys_panel") is True


def test_a_hidden_panel_gives_the_toolbar_room(window):
    """The dock is ~600px wide; that is what starved the toolbar before."""
    window.show()
    window.resize(1600, 720)
    window._on_panel_toggled(True)
    _settled(window)
    with_panel = sum(1 for b, _a in window._bar_items if b.isVisible())

    window._on_panel_toggled(False)
    _settled(window)
    without_panel = sum(1 for b, _a in window._bar_items if b.isVisible())

    assert without_panel >= with_panel
    assert without_panel > 4, "the mirror gets the space"


# --------------------------------------------------------------------------- #
# Discord Rich Presence wiring
# --------------------------------------------------------------------------- #


def test_the_discord_toggle_comes_back_on_when_it_was_enabled(window, monkeypatch):
    """Enabled last launch must actually connect on the next Connect.

    setChecked() before connecting the signal never fires the handler, so the
    button could sit lit while nothing was connected.
    """
    window._settings.set("discord", True)
    window._settings.set("discord_app_id", "1560333515915132978")
    usb = Device(serial="USB123", state="device")
    fresh = MainWindow(adb=FakeAdb(usb))
    fresh._refresh_devices()
    try:
        assert fresh._selected_device() is not None, "a device is picked"
        assert fresh._discord_action.isChecked(), "the remembered choice is kept"

        attempts: list[str] = []
        fresh._connect_discord = lambda: attempts.append("connect")  # type: ignore[method-assign]

        class StubSession:
            """Stands in for MirrorSession so no device is needed."""

            def __init__(self, *a, **k) -> None:
                self.started_at = 0.0

            def start(self, device=None) -> None:
                pass

            def set_muted(self, muted) -> None:
                pass

            def device_volume(self):
                return None

            def set_device_volume(self, index) -> bool:
                return True

            def stop(self) -> None:
                pass

        monkeypatch.setattr("droidmirror.ui.main_window.MirrorSession", StubSession)
        fresh._connect()

        assert attempts == ["connect"], "brought up when the session starts"
    finally:
        fresh.close()


def test_turning_discord_off_clears_it_and_stops_the_timer(window):
    from droidmirror.ui import main_window as mw

    cleared: list[bool] = []
    fake = mw.DiscordPresence()
    fake.clear = lambda: cleared.append(True)  # type: ignore[method-assign]
    window._discord = fake
    window._discord_timer.start()

    window._on_discord_toggled(False)

    assert cleared == [True]
    assert not window._discord_timer.isActive()
    assert window._settings.get("discord") is False


def test_connecting_never_asks_for_a_discord_token(window, monkeypatch):
    """Icons are resolved online and handed over as a url, so the presence
    needs no Discord login credential -- and must never ask for one.

    It used to prompt for a *user token* to upload icons. That credential is
    the whole account, it had to be written to settings.json in plain text,
    and the uploads it paid for were dropped by the client anyway.
    """
    asked: list[str] = []

    class Connected:
        connected = True
        client_id = "1560333515915132978"

        def configure(self, *a, **k):
            return True

        def update_app(self, *a, **k):
            pass

    window._discord = Connected()  # type: ignore[assignment]
    window._settings.set("discord_app_id", "1560333515915132978")
    monkeypatch.setattr(
        main_window_module.QInputDialog,
        "getText",
        lambda *a, **k: asked.append("prompt") or ("", False),
    )

    window._connect_discord()
    window._connect_discord()

    assert asked == [], "connecting must not prompt for anything"


def test_enabling_discord_starts_the_refresh_timer(window, monkeypatch):
    """Discord can be switched on mid-session; without this the presence
    stayed frozen on whatever it first sent and never noticed the app on the
    tablet changing."""
    window.show()

    class Connected:
        connected = True
        client_id = "1560333515915132978"

        def configure(self, *a, **k):
            return True

        def update_app(self, *a, **k):
            pass

    window._discord = Connected()  # type: ignore[assignment]
    window._settings.set("discord_app_id", "1560333515915132978")
    window._discord_timer.stop()

    window._connect_discord()

    assert window._discord_timer.isActive(), "the refresh must be running"


def test_the_presence_follows_the_app_on_the_phone(window):
    """The tick must re-send when the foreground app changes."""
    pushed: list[str | None] = []

    class FakePresence:
        connected = True
        client_id = "1"

        def update_app(self, package, **kwargs):
            pushed.append(package)

    class FakeSession:
        def __init__(self) -> None:
            self.foreground_app = "com.instagram.android"
            self.started_at = 0.0

        def stop(self) -> None:
            pass

    window._discord = FakePresence()  # type: ignore[assignment]
    session = FakeSession()
    window._session = session  # type: ignore[assignment]

    window._push_discord(force=True)
    assert pushed == ["com.instagram.android"]

    session.foreground_app = "com.whatsapp"
    window._push_discord()
    assert pushed[-1] == "com.whatsapp", "the app changing must be published"


def test_the_presence_tick_reads_the_session_not_the_device(window):
    pushed: list[str | None] = []

    class FakePresence:
        connected = True
        client_id = "1"

        def update_app(self, package, **kwargs):
            pushed.append(package)

    window._session = FakeSession()
    window._session.foreground_app = "com.roblox.client"
    window._discord = FakePresence()  # type: ignore[assignment]
    window._adb = None

    window._push_discord()

    assert pushed == ["com.roblox.client"]


# --------------------------------------------------------------------------- #
# Toolbar layout
# --------------------------------------------------------------------------- #


def test_toolbar_never_overlaps_at_any_width(window, qt_app):
    """No toolbar item may overlap the one before it, at any window width.

    Regression test. _rebuild_overflow() decides how many buttons to slide into
    the » menu by budgeting the untracked widgets (the "Device:" label, the
    device combo, the separators) at their *sizeHint*. But the combo pins
    setMinimumWidth(180) while its sizeHint is only ~101, so the row was
    under-budgeted by ~79px. Too few buttons were hidden, Qt laid the combo
    out over its 180px minimum anyway, and it drew straight over the Refresh
    button beside it. addSpacing() also contributed nothing at all, because
    itemAt(i).widget() is None for a spacer item.
    """
    layout = window._bar_layout
    combo = layout.itemAt(1).widget()
    assert combo is not None
    window.show()  # isVisible() is False until the window is on screen

    def widgets():
        return [
            layout.itemAt(i).widget()
            for i in range(layout.count())
            if layout.itemAt(i).widget() is not None
        ]

    for width in (2400, 1900, 1500, 1200, 1000, 900, 800, 700, 600):
        window.resize(width, 720)
        for _ in range(4):
            qt_app.processEvents()

        visible = [w for w in widgets() if w.isVisible() and w.width() > 0]
        # The combo must not run into the next visible widget.
        assert combo.isVisible(), f"device combo hidden at width {width}"
        next_to_combo = None
        for w in visible:
            if w is combo:
                next_to_combo = "pending"
            elif next_to_combo == "pending":
                next_to_combo = w
                break
        assert next_to_combo is not None, f"nothing follows the combo at {width}"
        gap = next_to_combo.x() - (combo.x() + combo.width())
        assert gap >= 0, (
            f"{type(next_to_combo).__name__} overlaps the device combo by "
            f"{-gap}px at window width {width}"
        )


def test_loading_a_profile_does_not_rewrite_it(window, monkeypatch):
    """Syncing the Camera-Movement Lock button must not re-enter its handler.

    Regression test. _refresh_bindings() calls setChecked() on that button. The
    button's toggled signal was connected to a handler ending in _auto_save(),
    so syncing a button whose state actually changed wrote the profile straight
    back to disk and repopulated the profile combo. A comment claimed signals
    were blocked; they were not.
    """
    window._profile.camera.locked = False

    # Force the button to disagree with the profile -- the state a profile load
    # can land in.
    window._cam_lock_btn.blockSignals(True)
    window._cam_lock_btn.setChecked(True)
    window._cam_lock_btn.blockSignals(False)
    assert window._cam_lock_btn.isChecked() is True
    assert window._profile.camera.locked is False

    saves = []
    monkeypatch.setattr(window, "_auto_save", lambda: saves.append(1))

    window._refresh_bindings()

    assert saves == [], f"_refresh_bindings() saved the profile {len(saves)} time(s)"
    # ...and the button still ends up in sync with the profile.
    assert window._cam_lock_btn.isChecked() is False
    assert window._profile.camera.locked is False

    # A real user click must still save.
    window._cam_lock_btn.setChecked(True)
    assert len(saves) == 1, "the Lock toggle stopped working"
    assert window._profile.camera.locked is True


def test_clicking_the_lit_wifi_button_does_not_wedge_the_filter(window):
    """Re-scanning from Wi-Fi must not knock the mode back to USB.

    Regression test. Both transport buttons are QActions wired to the same
    `_wireless_mode`, and the old code drove that mode *through* the Wi-Fi
    action's checked state: click -> setChecked(False) -> toggled -> mode=USB,
    then on success setChecked(True) -> mode=wireless.

    But `trigger()` on an already-checked checkable QAction emits
    `toggled(False)` *before* `triggered(False)`. So clicking the lit Wi-Fi
    button silently switched the mode to USB and emptied the device list --
    and the recovery `setChecked(True)` could not work, because the action was
    already checked and therefore emitted nothing. The window was left with
    both buttons lit, no devices listed, Connect disabled, and a wireless
    tablet actually connected; no click could get out of it without a restart.
    """
    adb = FakeAdb(Device(serial="USB123", state="device", model="SM-X210"))
    adb._wireless = Device(serial="10.0.0.9:5555", state="device", model="SM-X210")
    window._adb = adb

    # Settle on Wi-Fi first, as a successful connect would.
    window._set_wireless_mode(True)
    assert window._wireless_mode is True
    assert window._connect_action.isEnabled()

    # Now click the already-lit Wi-Fi button -- the re-scan path.
    window._filter_action.trigger()

    assert window._wireless_mode is True, "re-scan knocked the mode back to USB"
    assert window._filter_action.isChecked(), "Wi-Fi button went dark"
    assert not window._usb_action.isChecked(), "USB button stayed lit"
    assert window._usb_action.isChecked() is False, "both buttons lit = desync"

    serials = [
        window._device_combo.itemData(i).serial
        for i in range(window._device_combo.count())
    ]
    assert serials == ["10.0.0.9:5555"], "the wireless device vanished from the list"
    assert window._connect_action.isEnabled(), "Connect was left disabled"
    assert window._settings.get("device_filter") == "wireless"


def test_usb_click_switches_the_filter_back(window):
    """Clicking USB lists cable devices only, and stays consistent."""
    adb = FakeAdb(Device(serial="USB123", state="device", model="SM-X210"))
    adb._wireless = Device(serial="10.0.0.9:5555", state="device", model="SM-X210")
    window._adb = adb
    window._set_wireless_mode(True)

    window._usb_action.trigger()

    assert window._wireless_mode is False
    assert window._usb_action.isChecked() and not window._filter_action.isChecked()
    serials = [
        window._device_combo.itemData(i).serial
        for i in range(window._device_combo.count())
    ]
    assert serials == ["USB123"]
    assert window._settings.get("device_filter") == "usb"

    # ...and back again, which is how a user recovers.
    window._filter_action.trigger()
    assert window._wireless_mode is True
    assert window._filter_action.isChecked() and not window._usb_action.isChecked()


# --------------------------------------------------------------------------- #
# Icon resolution: a failure must never disable icons for the session
# --------------------------------------------------------------------------- #


def _wait_until_resolve_clears(qt_app, in_flight, key, timeout=5.0):
    """Block until ``key`` leaves ``in_flight``, or the deadline passes.

    ``qt_app.processEvents()`` on its own does *not* wait for a worker thread --
    it returns immediately when nothing is queued -- so spinning on it is not a
    wait, and makes this test race the thread it is asserting about. This
    sleeps for real, giving the worker time to start, raise, and run its
    finally.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qt_app.processEvents()
        if key not in in_flight:
            return
        time.sleep(0.01)


def test_icon_resolution_recovers_after_a_worker_failure(window, qt_app, monkeypatch):
    """A raising icon worker must not wedge icon resolution permanently.

    Regression test. ``_resolve_icon`` cleared ``_icon_resolving`` at two
    hand-placed points rather than in a ``finally``. One failure left the flag
    set, and since every caller returns early while it is set, no icon of any
    kind could ever resolve again for the rest of the session.
    """
    window._discord.configure("123456789", enabled=False)

    def boom(*_a, **_k):
        raise RuntimeError("simulated lookup failure")

    monkeypatch.setattr(main_window_module, "icon_url_for_package", boom)

    # Provoking a thread exception is the point; pytest escalates those to
    # errors, so swallow them for the duration of this test only.
    original_hook = threading.excepthook
    monkeypatch.setattr(threading, "excepthook", lambda _a: None)

    try:
        for package in ("com.example.app", "com.other.app"):
            assert window._icon_url_for(package) is None
            _wait_until_resolve_clears(qt_app, window._icon_resolving, package)
            assert package not in window._icon_resolving, (
                f"{package} stayed marked as in-flight after its worker raised"
            )
    finally:
        monkeypatch.setattr(threading, "excepthook", original_hook)


def test_the_home_screen_never_starts_an_icon_lookup(window, qt_app, monkeypatch):
    """Home is not an app, so there is no store page to look up.

    It used to get a locally drawn phone glyph instead. That could never be
    shown: Discord drops any image that is not a url (see
    ``discord_rpc.is_image_reference``), so the drawing only ever produced
    the "?" it was meant to avoid. Now nothing is resolved and no image is
    sent, which lets Discord draw the application's own icon.
    """
    window._discord.configure("123456789", enabled=False)
    looked_up: list[str] = []
    monkeypatch.setattr(
        main_window_module,
        "icon_url_for_package",
        lambda package, **_k: looked_up.append(package) or "https://x/y.png",
    )

    assert window._icon_url_for("com.sec.android.app.launcher") is None

    for _ in range(20):
        qt_app.processEvents()
    assert looked_up == [], "no online icon exists for the home screen"
    assert window._icon_resolving == set(), "and nothing may be left in flight"


def test_a_cached_asset_id_is_re_resolved_as_a_url(window, qt_app, monkeypatch):
    """A cached value that cannot render must never be served as final.

    The cache on disk holds asset ids, written by the previous scheme. Discord
    drops any image reference that is not a url (see
    ``discord_rpc.is_image_reference``), so those entries paint nothing at all
    -- and since a cached hit does not start a lookup, an app that had already
    been "resolved" would show "?" for ever with nothing ever retrying it.
    """
    window._discord.configure("123456789", enabled=False)
    window._icon_cache.put(
        "com.instagram.android", "1560714282906161254", source="web"
    )

    looked_up: list[str] = []
    monkeypatch.setattr(
        main_window_module,
        "icon_url_for_package",
        lambda package, **_k: looked_up.append(package) or None,
    )

    assert window._icon_url_for("com.instagram.android") is None, (
        "an asset id is not a url, so it must not be handed to Discord"
    )
    _wait_until_resolve_clears(qt_app, window._icon_resolving, "com.instagram.android")
    assert looked_up == ["com.instagram.android"], "the stale value was never retried"

    # A url in the cache *is* a finished answer, and is served straight back
    # -- no second lookup, which is what keeps the network quiet.
    window._icon_cache.put("com.instagram.android", "https://x/y.png", source="web")
    assert window._icon_url_for("com.instagram.android") == "https://x/y.png"


def test_icon_in_flight_is_tracked_per_package(window, qt_app, monkeypatch):
    """One slow lookup must not block every other app's icon."""
    window._discord.configure("123456789", enabled=False)

    started = threading.Event()
    release = threading.Event()

    def slow(*_a, **_k):
        started.set()
        release.wait(timeout=5.0)
        return None

    # The online lookup is the one that has to block here.
    monkeypatch.setattr(main_window_module, "icon_url_for_package", slow)
    try:
        window._icon_url_for("com.slow.app")
        assert started.wait(timeout=2.0), "worker never started"
        assert "com.slow.app" in window._icon_resolving

        # A different package must still be free to start its own resolve.
        window._icon_url_for("com.other.app")
        assert "com.other.app" in window._icon_resolving, (
            "a second package was blocked by the first one's in-flight resolve"
        )
    finally:
        release.set()
        for _ in range(20):
            qt_app.processEvents()


def test_foreground_polling_follows_the_discord_toggle(window):
    """The foreground probe only runs when Discord is on.

    It exists solely to feed Rich Presence, but it used to fire for the whole
    session regardless, on a device already decoding video.
    """
    class FakeSession:
        foreground_polling = False

        def stop(self):
            pass  # the window calls this on teardown

    window._session = FakeSession()
    window._set_foreground_polling(True)
    assert window._session.foreground_polling is True

    window._set_foreground_polling(False)
    assert window._session.foreground_polling is False


def test_app_names_has_no_duplicate_packages():
    """A repeated key silently shadows the entry above it."""
    import ast
    import collections

    from droidmirror import discord_rpc as drpc

    tree = ast.parse(open(drpc.__file__, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "APP_NAMES":
            keys = [k.value for k in node.value.keys]
            dupes = [k for k, c in collections.Counter(keys).items() if c > 1]
            assert not dupes, f"duplicate packages in APP_NAMES: {dupes}"
            return
    pytest.fail("APP_NAMES not found")


def _png_bytes(width, height):
    """A structurally valid PNG of the given size."""
    import struct as _struct
    import zlib as _zlib

    def chunk(kind, data):
        raw = kind + data
        return (_struct.pack(">I", len(data)) + raw
                + _struct.pack(">I", _zlib.crc32(raw) & 0xFFFFFFFF))

    ihdr = _struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IEND", b"")


def test_a_cached_fallback_is_re_resolved(window, qt_app, monkeypatch):
    """A generic fallback must never be served as a finished result.

    Regression test. IconCache returned the cached PNG unconditionally, so the
    first time an app resolved to the drawn glyph -- because the network was
    down, or because no source existed yet -- that glyph was cached forever
    and every later improvement was unreachable. The user saw exactly this:
    Instagram permanently showing the generic 2x2 square.

    The cache now records provenance, and a fallback (or anything written
    before provenance existed) reads as a miss.
    """
    from droidmirror.appicons import IconCache

    glyph = _png_bytes(512, 512)
    window._icon_cache.store_png("com.instagram.android", glyph)
    window._icon_cache.put(
        "com.instagram.android", "cdn-key", source=IconCache.FALLBACK_SOURCE
    )

    assert window._icon_cache.is_fallback("com.instagram.android")
    assert window._icon_cache.get("com.instagram.android") is None, (
        "a cached fallback was served as a finished icon"
    )
    assert window._icon_cache.load_png("com.instagram.android") is None, (
        "the fallback PNG was returned from cache"
    )

    # A real icon, once resolved, is trusted and reused.
    real = _png_bytes(512, 512)
    window._icon_cache.store_png("com.instagram.android", real, source="web")
    window._icon_cache.put("com.instagram.android", "cdn-key-2", source="web")
    assert window._icon_cache.get("com.instagram.android") == "cdn-key-2"
    assert window._icon_cache.load_png("com.instagram.android") == real


# --------------------------------------------------------------------------- #
# Regression tests for bug audit fixes
# --------------------------------------------------------------------------- #


def test_set_discord_app_id_passes_current_id_without_type_error(window, monkeypatch):
    seen_prefills: list[str] = []

    def fake_get_text(_parent, _title, _label, text=""):
        seen_prefills.append(text)
        return ("9876543210", True)

    monkeypatch.setattr(main_window_module.QInputDialog, "getText", fake_get_text)

    class FakePresence:
        connected = True
        status = "connected"
        client_id = "9876543210"

        def clear(self):
            pass

        def configure(self, client_id, enabled=True):
            self.client_id = client_id
            return True

        def update_app(self, *a, **k):
            pass

    window._discord = FakePresence()  # type: ignore[assignment]
    window._settings.set("discord_app_id", "111222333")
    window._set_discord_app_id()

    assert seen_prefills == ["111222333"]
    assert window._settings.get("discord_app_id") == "9876543210"


def test_dragging_a_binding_schedules_auto_save_and_respects_read_only(window):
    window._on_binding_placed("a", 0.5, 0.5)
    window._joy_save_timer.stop()

    window._on_binding_moved("a", 0.3, 0.7)
    assert window._joy_save_timer.isActive(), "moving a binding must schedule a save"
    window._joy_save_timer.stop()

    # On the read-only default profile, moving, toggling, removing, and clearing
    # must all be refused.
    window._profile.name = "default"
    window._on_binding_moved("a", 0.9, 0.9)
    assert (window._profile.bindings[0].nx, window._profile.bindings[0].ny) == (0.3, 0.7)

    window._table.setCurrentCell(0, 0)
    window._toggle_selected_mode()
    assert window._profile.bindings[0].mode == MODE_TAP

    window._remove_selected_binding()
    assert len(window._profile.bindings) == 1

    window._clear_bindings()
    assert len(window._profile.bindings) == 1


def test_overlapping_background_workers_deliver_to_their_own_callbacks(window, qt_app):
    window._sync_workers = False
    first_started = threading.Event()
    release_first = threading.Event()
    results_1: list = []
    results_2: list = []

    def work_1():
        first_started.set()
        release_first.wait(timeout=3.0)
        return "from_worker_1"

    def work_2():
        return "from_worker_2"

    window._run_worker(work_1, results_1.append)
    assert first_started.wait(timeout=2.0)

    # Start worker 2 while worker 1 is still in flight; this used to overwrite
    # self._task_bridge and route worker 1's result into results_2.
    window._run_worker(work_2, results_2.append)
    release_first.set()

    deadline = time.perf_counter() + 3.0
    while (not results_1 or not results_2) and time.perf_counter() < deadline:
        qt_app.processEvents()
        time.sleep(0.01)

    assert results_1 == ["from_worker_1"]
    assert results_2 == ["from_worker_2"]


def test_escape_during_recorder_post_capture_delay_cancels_cleanly(window):
    window._begin_add_binding()
    dialog = window._key_dialog
    assert dialog is not None

    dialog.keyPressEvent(_key_event(Qt.Key.Key_J, "j"))
    assert dialog.key_name() == "j"
    assert dialog._accept_timer.isActive()

    # Pressing Escape before the 220ms timer fires must clear the key and stop
    # the timer so the key is not armed.
    dialog.keyPressEvent(_key_event(Qt.Key.Key_Escape))
    assert dialog.key_name() is None
    assert not dialog._accept_timer.isActive()
    assert window._pending_key is None


def test_disabled_binding_does_not_intercept_clicks_in_edit_mode(window):
    attach_frame(window)
    b = Binding("k", 0.5, 0.5, enabled=False)
    window._profile.bindings.append(b)
    window._refresh_bindings()

    rect = window._view.frame_rect()
    pos = QPointF(rect.x + 0.5 * rect.w, rect.y + 0.5 * rect.h)
    assert window._view._binding_at(pos) is None


def test_close_event_stops_discord_timer_and_clears_presence(qt_app):
    w = MainWindow(adb=None)
    cleared: list[bool] = []
    w._discord.clear = lambda: cleared.append(True)  # type: ignore[method-assign]
    w._discord_timer.start()

    w.close()
    assert not w._discord_timer.isActive()
    assert cleared == [True]
