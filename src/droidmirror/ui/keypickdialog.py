"""The "press a key to bind" recorder.

*Add binding* used to arm a key silently and leave a line of text in the
dock, which is easy to miss and easy to get wrong (arm the wrong key and you
never notice until a tap goes missing). This panel grabs the next physical key
press -- or mouse button; left and right click are bindable too -- shows it
the moment it lands, and hands the name back -- so what you bind is what you
saw.

It is a floating tool panel, not a modal dialog: clicking outside must never
dismiss it (a dialog that vanishes the moment you look away is useless for
recording a key). It stays up until a key is recorded, until Escape, or until
the toolbar button is clicked again -- which is why *Add binding* is a toggle.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QRect, Qt, QTimer
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QLabel,
    QVBoxLayout,
)

from . import keys as keynames

#: Pressed on their own, these mean "I am holding a modifier", not "bind me".
#: The same set already exists as keys.MODIFIER_KEYS; this used to be a second
#: hand-written copy, which is exactly the kind of duplication that rots when
#: somebody adds a modifier to one of them.
_ALONE = set(keynames.MODIFIER_KEYS)

_HINT = (
    "Any key -- or a mouse click anywhere in the app -- becomes the binding.\n"
    "Escape cancels. Bindings keep working as normal."
)


class KeyCaptureDialog(QDialog):
    """Floating single-key recorder. Returns the key name via :meth:`key_name`."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add binding")
        # A tool window floats above the mirror and is not dismissed by
        # clicking elsewhere -- the whole point of a recorder panel.
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.CustomizeWindowHint
        )
        self.setModal(False)
        self.setMinimumWidth(420)
        # Hide, never destroy: this panel is reused for every binding.
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)

        self._key_name: str | None = None
        #: While recording, clicks land in :meth:`eventFilter` first. A press
        #: inside this rect passes through instead of recording -- it is the
        #: *Add key* button itself, whose second click puts the recorder away.
        self._ignore_rect: QRect | None = None
        #: A recorded press must be consumed together with its release, or
        #: the widget underneath sees a release for a press it never got.
        self._swallow_release = False
        self._accept_timer = QTimer(self)
        self._accept_timer.setSingleShot(True)
        self._accept_timer.setInterval(220)
        self._accept_timer.timeout.connect(self.accept)

        layout = QVBoxLayout(self)

        prompt = QLabel("Press the key or mouse button you want to bind")
        prompt.setWordWrap(True)
        prompt.setStyleSheet("font-size: 14px;")
        layout.addWidget(prompt)

        self._reading = QLabel("Recording\u2026")
        self._reading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._reading.setStyleSheet(
            "font-size: 26px; font-weight: bold; color: #ffbe3c; padding: 22px;"
        )
        layout.addWidget(self._reading)

        self._hint = QLabel(_HINT)
        self._hint.setWordWrap(True)
        self._hint.setStyleSheet("color: #7d8299; font-size: 12px;")
        layout.addWidget(self._hint)

        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def key_name(self) -> str | None:
        """The captured key, or None if the recorder was dismissed."""
        return self._key_name

    def set_ignore_rect(self, rect: QRect | None) -> None:
        """A screen area where clicks pass through instead of recording."""
        self._ignore_rect = rect

    def reset(self) -> None:
        """Clear the previous result and get ready to record again."""
        self._accept_timer.stop()
        self._key_name = None
        self._reading.setText("Recording\u2026")
        self._hint.setText(_HINT)

    def reject(self) -> None:  # noqa: N802
        """Cancel recording, even if a key was just captured during the beat."""
        self._accept_timer.stop()
        self._key_name = None
        super().reject()

    def begin_recording(self) -> None:
        """Show the panel and take the keyboard straight away."""
        self.reset()
        app = QApplication.instance()
        if app is not None:
            # A click can land on any widget -- the mirror, the dock, this
            # panel itself -- and it must be *recorded*, not delivered.
            app.installEventFilter(self)
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus(Qt.FocusReason.OtherFocusReason)

    def _record(self, name: str) -> None:
        """Lock in a capture, show it, and close after one visible beat.

        The first capture wins: keys and clicks arriving while the result is
        already on screen do not overwrite it (one *Add binding* press is one
        recorded binding).
        """
        if self._key_name:
            return
        self._key_name = name
        self._reading.setText(keynames.pretty_name(name))
        self._accept_timer.start()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.reject()
            return
        if event.isAutoRepeat():
            return  # the first press already recorded it
        name = keynames.key_name(event.key(), event.text())
        if not name or name in _ALONE:
            self._reading.setText("Keep holding\u2026")  # only a modifier so far
            return
        if not keynames.is_bindable(name):
            # Reserved (F7/F9/F11, the ` toggle): say so instead of silently
            # binding a key the app is about to intercept anyway.
            self._reading.setText(f"{name.upper()} is reserved")
            self._hint.setText(
                "That key is used by the app itself. Press another key, "
                "or Escape to cancel."
            )
            return
        self._record(name)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        """Record mouse buttons anywhere in the app while the panel is up.

        Keys arrive via :meth:`keyPressEvent` -- the panel holds focus -- but
        a click can land on any widget, so filtering the application is the
        only way to hear it. Presses on the *Add key* button itself pass
        through: clicking it again is how the recorder is toggled off.
        """
        kind = event.type()
        if kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick):
            name = keynames.mouse_button_name(event.button())
            if name is None:  # middle/back/forward: not bind targets
                return False
            pos = event.globalPosition().toPoint()
            if self._ignore_rect is not None and self._ignore_rect.contains(pos):
                return False  # the toggle button -- its click must land
            self._swallow_release = True
            self._record(name)
            return True  # recorded, and never delivered to the widget below
        if kind == QEvent.Type.MouseButtonRelease and self._swallow_release:
            self._swallow_release = False
            return True
        return super().eventFilter(obj, event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        """Fallback path for a click that reached the panel unfiltered."""
        name = keynames.mouse_button_name(event.button())
        if name is not None:
            self._record(name)
            return
        super().mousePressEvent(event)

    def hideEvent(self, event) -> None:  # noqa: N802
        # Off screen = not recording, whatever the reason (record or cancel).
        self._accept_timer.stop()
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        self._swallow_release = False
        super().hideEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802
        # Closing just parks the panel; it is reused next time.
        self.hide()
        event.ignore()
