"""Translate Qt key codes into stable, human-readable key names.

Binding a key to a screen point must survive the "key" being represented the
same way everywhere -- in profiles on disk, in the UI, and in the keymap
engine. This module is the single source of truth for that spelling. Mouse
buttons are bindable too; ``MOUSE_BUTTON_NAMES`` is their spelling.
"""

from __future__ import annotations

from PySide6.QtCore import Qt

#: Keys that have no printable single character representation.
_SPECIAL: dict[int, str] = {
    Qt.Key.Key_Space: "space",
    Qt.Key.Key_Return: "enter",
    Qt.Key.Key_Enter: "enter",
    Qt.Key.Key_Tab: "tab",
    Qt.Key.Key_Backtab: "tab",
    Qt.Key.Key_Escape: "escape",
    Qt.Key.Key_Backspace: "backspace",
    Qt.Key.Key_Delete: "delete",
    Qt.Key.Key_Insert: "insert",
    Qt.Key.Key_Home: "home",
    Qt.Key.Key_End: "end",
    Qt.Key.Key_PageUp: "pageup",
    Qt.Key.Key_PageDown: "pagedown",
    Qt.Key.Key_Shift: "shift",
    Qt.Key.Key_Control: "ctrl",
    Qt.Key.Key_Alt: "alt",
    Qt.Key.Key_Meta: "meta",
    Qt.Key.Key_Up: "up",
    Qt.Key.Key_Down: "down",
    Qt.Key.Key_Left: "left",
    Qt.Key.Key_Right: "right",
    Qt.Key.Key_CapsLock: "capslock",
}
_SPECIAL.update({getattr(Qt.Key, f"Key_F{i}"): f"f{i}" for i in range(1, 25)})

#: Modifier keys are not useful bind targets -- the user is usually holding them.
MODIFIER_KEYS = frozenset(
    {"shift", "ctrl", "alt", "meta", "capslock", "numlock", "scrolllock"}
)

#: The application hotkey that switches between mouse and keybind control.
#: Matched on the *physical* key code so it works regardless of layout, since
#: Shift turns it into "~".
TOGGLE_KEY_CODE = Qt.Key.Key_QuoteLeft
TOGGLE_KEY_NAME = "`"

#: Window-level hotkeys owned by the app: latency HUD (F9), fullscreen (F11),
#: keybind-visibility toggle (F7).
HUD_KEY_CODE = Qt.Key.Key_F9
FULLSCREEN_KEY_CODE = Qt.Key.Key_F11
HUD_KEY_NAME = "f9"
FULLSCREEN_KEY_NAME = "f11"
#: Toggle showing/hiding the keybind markers (and pad) drawn over the mirror.
OVERLAY_KEY_CODE = Qt.Key.Key_F7
OVERLAY_KEY_NAME = "f7"

#: Mouse lock toggle for gaming: hides the cursor and keeps it inside the
#: mirror window. Deliberately *not* reserved -- it stays bindable, and an
#: explicit x binding wins (the engine runs before the lock branch); the lock
#: only fires on an unbound x.
LOCK_KEY_NAME = "x"

#: Names the toggle key can produce. Reserved so a binding can never fight the
#: hotkey for the same press. F7/F9/F11 are reserved for the same reason: Qt
#: resolves shortcuts before delivering key events, so an unreserved binding
#: on them would silently never fire.
RESERVED_KEYS = frozenset(
    {TOGGLE_KEY_NAME, "~", HUD_KEY_NAME, FULLSCREEN_KEY_NAME, OVERLAY_KEY_NAME}
)

#: Mouse buttons that can be bound, and how they read in the UI. The recorder
#: captures a left/right click the same way it captures a physical key press,
#: and profiles store them under these names so the engine's key lookup works
#: unchanged.
MOUSE_BUTTON_NAMES = {"mouse_left": "Left Click", "mouse_right": "Right Click"}


def is_toggle_key(key: int) -> bool:
    """Whether a Qt key code is the control-mode toggle hotkey."""
    return key == TOGGLE_KEY_CODE


def is_hud_key(key: int) -> bool:
    """Whether a Qt key code is the latency-HUD hotkey."""
    return key == HUD_KEY_CODE


def is_fullscreen_key(key: int) -> bool:
    """Whether a Qt key code is the fullscreen hotkey."""
    return key == FULLSCREEN_KEY_CODE


def mouse_button_name(button) -> str | None:
    """Canonical binding name for a mouse button, or None if it can't bind."""
    if button == Qt.MouseButton.LeftButton:
        return "mouse_left"
    if button == Qt.MouseButton.RightButton:
        return "mouse_right"
    return None


def key_name(key: int, text: str = "") -> str | None:
    """Return the canonical name for a key, or None if it can't be bound.

    ``key`` is a ``Qt.Key`` value and ``text`` is ``QKeyEvent.text()``.
    """
    if key in _SPECIAL:
        return _SPECIAL[key]

    # Letters and digits, derived from the code point so it works regardless of
    # the keyboard layout's produced text.
    if Qt.Key.Key_A <= key <= Qt.Key.Key_Z:
        return chr(key).lower()
    if Qt.Key.Key_0 <= key <= Qt.Key.Key_9:
        return chr(key)

    # Punctuation and anything else that produced a printable character.
    if text and len(text) == 1 and text.isprintable():
        return text.lower()

    return None


def is_bindable(name: str | None) -> bool:
    """Whether a key name is a sensible binding target."""
    if not name:
        return False
    return name not in MODIFIER_KEYS and name not in RESERVED_KEYS


def pretty_name(name: str) -> str:
    """Display form of a key name."""
    if name in MOUSE_BUTTON_NAMES:
        return MOUSE_BUTTON_NAMES[name]
    if len(name) == 1:
        return name.upper()
    return name.replace("_", " ").title()
