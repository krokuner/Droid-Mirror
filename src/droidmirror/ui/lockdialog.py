"""App-side screen-lock panel: PIN pad, pattern, password, swipe.

A secure keyguard can mirror as a plain black surface (or doze to black)
while still registering touches, so fumbling through the mirrored lock
screen blind is hopeless. This dialog draws the lock UI here and injects
the equivalent events instead -- digits as key events, password as text,
pattern as one continuous finger drag mapped onto the real pattern view
(read from uiautomator bounds), swipe as an up-flick.

The one piece of device I/O the dialog does itself -- reading the keyguard's
own widget bounds so the pattern pad lines up -- runs on a worker thread and
hands the result back through :attr:`LockDialog._bounds_ready`. Qt queues that
connection into the GUI thread's event loop, so the three attributes below are
only ever written on the GUI thread. Everything else reaches the device through
callbacks supplied by the main window, so the dialog is testable with plain
recorders.
"""

from __future__ import annotations

import threading
from typing import Callable

from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..lockscreen import fetch_display_size, parse_node_bounds, scale_rect
from ..protocol import POINTER_ID_MOUSE, POINTER_ID_VIRTUAL_FINGER, MotionAction

# android.view.KeyEvent codes (stable since API 1).
KEYCODE_0 = 7  # KEYCODE_0 .. KEYCODE_9 = 7..16
KEYCODE_DEL = 67
KEYCODE_ENTER = 66
KEYCODE_WAKEUP = 224

#: uiautomator resource-id fragments on the keyguard.
OK_NODE = "key_enter_text"
PATTERN_NODE = "pattern"

#: Where the pattern grid sits when uiautomator cannot tell us (fractions
#: of the screen: the Samsung keyguard keeps it in the middle band).
DEFAULT_PATTERN_RECT = (0.24, 0.28, 0.76, 0.70)

_TAB_FOR_TYPE = {"pin": 0, "pattern": 1, "password": 2, "swipe": 3}


class _PatternPad(QWidget):
    """3x3 dot grid; collects a normalized polyline of the drawn pattern."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.points: list[tuple[float, float]] = []
        self.setMinimumSize(240, 240)
        self.setMaximumSize(320, 320)

    def clear(self) -> None:
        self.points.clear()
        self.update()

    def _local(self, pos) -> tuple[float, float]:
        return (
            min(max(pos.x() / max(1.0, self.width()), 0.0), 1.0),
            min(max(pos.y() / max(1.0, self.height()), 0.0), 1.0),
        )

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self.points.clear()
        self.points.append(self._local(event.position()))
        self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if not self.points:
            return
        self.points.append(self._local(event.position()))
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(QColor(255, 255, 255, 60), 1))
        painter.setBrush(QColor(255, 255, 255, 18))
        painter.drawRoundedRect(self.rect(), 12, 12)
        dot_r = self.width() * 0.045
        painter.setBrush(QColor(170, 200, 255, 220))
        painter.setPen(Qt.PenStyle.NoPen)
        for gy in (0.2, 0.5, 0.8):
            for gx in (0.2, 0.5, 0.8):
                painter.drawEllipse(
                    QPointF(gx * self.width(), gy * self.height()), dot_r, dot_r
                )
        if len(self.points) >= 2:
            painter.setPen(QPen(QColor(120, 230, 170, 235), 3))
            for a, b in zip(self.points, self.points[1:]):
                painter.drawLine(
                    QPointF(a[0] * self.width(), a[1] * self.height()),
                    QPointF(b[0] * self.width(), b[1] * self.height()),
                )


class LockDialog(QDialog):
    """The unlock panel. Non-modal: the mirror keeps running behind it."""

    #: Carries the keyguard-bounds probe back onto the GUI thread.
    #: (ok_rect, pattern_rect, display) -- any element may be None.
    _bounds_ready = Signal(object, object, object)

    def __init__(
        self,
        parent,
        *,
        send_key: Callable[[int], None],
        send_text: Callable[[str], None],
        send_touch: Callable[..., None],
        frame_size: Callable[[], tuple[int, int]],
        adb=None,
    ) -> None:
        super().__init__(parent)
        self._send_key = send_key
        self._send_text = send_text
        self._send_touch = send_touch
        self._frame_size = frame_size
        self._adb = adb

        #: Fetched in the background from uiautomator / wm size (display px).
        #: Written on the GUI thread only, from _on_bounds_ready().
        self._ok_rect = None
        self._pattern_rect = None
        self._display: tuple[int, int] | None = None
        self._bounds_busy = False
        self._bounds_ready.connect(self._on_bounds_ready)
        #: Digits typed here so far, mirrored as dots (the phone echoes
        #: nothing back, so otherwise you enter a PIN blind).
        self._entered = ""

        self.setWindowTitle("Unlock phone")
        self.setModal(False)
        self.setMinimumWidth(340)
        self.setStyleSheet(
            """
            QDialog { background: #14141c; }
            QLabel { color: #cfd4e4; }
            QTabWidget::pane { border: 1px solid #2c2c3a; }
            QTabBar::tab { color: #9aa0b8; background: #1b1b26;
                           padding: 7px 16px; }
            QTabBar::tab:selected { color: #ffffff; background: #2a2a3c;
                                    border-bottom: 2px solid #7ab8ff; }
            QPushButton { background: #232331; color: #e8eaf2;
                          border: 1px solid #34344a; border-radius: 8px;
                          padding: 9px; font-size: 17px; font-weight: 600; }
            QPushButton:hover { background: #2e2e40; }
            QPushButton:pressed { background: #3a3a52; }
            QPushButton#accent { background: #2f6fd8; border: none;
                                 color: white; }
            QPushButton#accent:hover { background: #3d7fe8; }
            QPushButton#wide { font-size: 14px; padding: 7px; }
            QPushButton#link { background: transparent; color: #7ab8ff;
                               border: none; padding: 6px 8px;
                               font-size: 12px; }
            QPushButton#link:hover { color: #a8d0ff; }
            QLineEdit { background: #101018; color: #ffffff; border: 1px
                        solid #34344a; border-radius: 8px; padding: 10px;
                        font-size: 16px; letter-spacing: 2px; }
            """
        )

        outer = QVBoxLayout(self)

        head = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(0)
        self._title_label = QLabel("Phone is locked")
        self._title_label.setStyleSheet(
            "font-size: 16px; font-weight: 600; color: #ffffff;"
        )
        titles.addWidget(self._title_label)
        self._type_label = QLabel("")
        self._type_label.setStyleSheet("color: #8f96ad; font-size: 12px;")
        titles.addWidget(self._type_label)
        head.addLayout(titles)
        head.addStretch(1)
        # The tab bar is hidden while we know the device's lock type -- four
        # tabs for a phone that only accepts one was the confusing part. It
        # is still one click away for when detection is wrong.
        self._more_btn = QPushButton("Other methods")
        self._more_btn.setObjectName("link")
        self._more_btn.setCheckable(True)
        self._more_btn.toggled.connect(self._on_more_toggled)
        head.addWidget(self._more_btn)
        wake = QPushButton("Wake")
        wake.setObjectName("wide")
        wake.setToolTip("Send a wake-up key to the phone")
        wake.clicked.connect(self._wake)
        head.addWidget(wake)
        outer.addLayout(head)

        self._status_label = QLabel("")
        self._status_label.setWordWrap(True)
        self._status_label.setStyleSheet("color: #7d8299; font-size: 12px;")
        outer.addWidget(self._status_label)

        self._tabs = QTabWidget()
        self._tabs.addTab(self._build_pin_tab(), "PIN")
        self._tabs.addTab(self._build_pattern_tab(), "Pattern")
        self._tabs.addTab(self._build_password_tab(), "Password")
        self._tabs.addTab(self._build_swipe_tab(), "Swipe")
        outer.addWidget(self._tabs)

    # -- tabs ------------------------------------------------------------- #

    def _build_pin_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        # Mirrors what has been typed. The device echoes nothing back, so
        # without this you are entering a PIN blind.
        self._pin_echo = QLabel("")
        self._pin_echo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._pin_echo.setStyleSheet(
            "font-size: 22px; letter-spacing: 8px; color: #cfd4e4; padding: 2px;"
        )
        layout.addWidget(self._pin_echo)
        grid = QGridLayout()
        grid.setSpacing(8)
        self._pin_buttons: dict[str, QPushButton] = {}
        rows = (("1", "2", "3"), ("4", "5", "6"), ("7", "8", "9"))
        for r, row in enumerate(rows):
            for c, label in enumerate(row):
                button = QPushButton(label)
                button.setFixedHeight(52)
                button.clicked.connect(lambda _=False, k=label: self._digit(k))
                self._pin_buttons[label] = button
                grid.addWidget(button, r, c)
        wake_btn = QPushButton("W")
        wake_btn.setFixedHeight(52)
        wake_btn.setToolTip("Wake the screen")
        wake_btn.clicked.connect(self._wake)
        grid.addWidget(wake_btn, 3, 0)
        zero = QPushButton("0")
        zero.setFixedHeight(52)
        zero.clicked.connect(lambda: self._digit("0"))
        self._pin_buttons["0"] = zero
        grid.addWidget(zero, 3, 1)
        delete = QPushButton("\u232b")
        delete.setFixedHeight(52)
        delete.clicked.connect(self._pin_backspace)
        grid.addWidget(delete, 3, 2)
        layout.addLayout(grid)
        ok = QPushButton("OK")
        ok.setObjectName("accent")
        ok.setFixedHeight(54)
        ok.setToolTip("Or just press Enter")
        ok.clicked.connect(self._submit_pin)
        layout.addWidget(ok)
        hint = QLabel("Digits are sent to the phone as real key events.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #7d8299; font-size: 12px;")
        layout.addWidget(hint)
        return page

    def _build_pattern_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self._pattern = _PatternPad()
        layout.addWidget(self._pattern, alignment=Qt.AlignmentFlag.AlignCenter)
        row = QHBoxLayout()
        clear = QPushButton("Clear")
        clear.clicked.connect(self._pattern.clear)
        draw = QPushButton("Unlock pattern")
        draw.setObjectName("accent")
        draw.clicked.connect(self._submit_pattern)
        row.addWidget(clear)
        row.addWidget(draw)
        layout.addLayout(row)
        hint = QLabel(
            "Draw the same shape as on the phone, then Unlock. The path is "
            "mapped onto the keyguard's real pattern view when it can be "
            "read; reveal the pad first if the device asks for a swipe."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #7d8299; font-size: 12px;")
        layout.addWidget(hint)
        return page

    def _build_password_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self._password = QLineEdit()
        self._password.setEchoMode(QLineEdit.EchoMode.Password)
        self._password.setPlaceholderText("Password")
        self._password.returnPressed.connect(self._submit_password)
        layout.addWidget(self._password)
        unlock = QPushButton("Unlock")
        unlock.setObjectName("accent")
        unlock.clicked.connect(self._submit_password)
        layout.addWidget(unlock)
        hint = QLabel("The password is injected as text into the keyguard's "
                      "field; it never leaves the device.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #7d8299; font-size: 12px;")
        layout.addWidget(hint)
        return page

    def _build_swipe_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        swipe = QPushButton("Swipe up to unlock")
        swipe.setObjectName("accent")
        swipe.setFixedHeight(64)
        swipe.clicked.connect(self._submit_swipe)
        layout.addWidget(swipe)
        hint = QLabel("For a lockscreen with no PIN/pattern/password.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #7d8299; font-size: 12px;")
        layout.addWidget(hint)
        return page

    # -- public ----------------------------------------------------------- #

    def open_for(self, lock_type: str | None) -> None:
        """Show with the right tab selected and refresh keyguard bounds.

        A secure keyguard often mirrors as a plain black surface with the pad
        hidden, so the panel brings the pad up itself on open. That is the
        whole point of the window -- not making the user discover that they
        have to wake and swipe before they can type anything.
        """
        kind = lock_type or "pin"
        self._title_label.setText("Phone is locked")
        self._type_label.setText(
            f"{lock_type.upper()} lock" if lock_type else "Unknown lock type"
        )
        self._tabs.setCurrentIndex(_TAB_FOR_TYPE.get(kind, 0))
        # One right answer -> hide the tab bar; it is one click away if this
        # guess turns out to be wrong.
        self._tabs.tabBar().setVisible(False)
        if self._more_btn.isChecked():
            self._more_btn.setChecked(False)
        self._entered = ""
        self._pin_echo.setText("")
        self._start_bounds_fetch()
        self.show()
        self.raise_()
        self._set_status("Waking the phone...")
        self._reveal()

    def _on_more_toggled(self, on: bool) -> None:
        self._tabs.tabBar().setVisible(bool(on))
        self._more_btn.setText("Fewer methods" if on else "Other methods")

    def _set_status(self, text: str) -> None:
        self._status_label.setText(text)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        """Enter submits whatever tab is showing, like the real keyguard."""
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            index = self._tabs.currentIndex()
            if index == 0:
                self._submit_pin()
            elif index == 2:
                self._submit_password()
            else:
                return super().keyPressEvent(event)
            event.accept()
            return
        if event.key() == Qt.Key.Key_Backspace and self._tabs.currentIndex() == 0:
            self._pin_backspace()
            event.accept()
            return
        super().keyPressEvent(event)

    # -- background bounds fetch ------------------------------------------ #

    def _start_bounds_fetch(self) -> None:
        if self._adb is None or self._bounds_busy:
            return
        self._bounds_busy = True
        threading.Thread(target=self._fetch_bounds, daemon=True).start()

    def _fetch_bounds(self) -> None:
        """One uiautomator dump + wm size; the pad may not be up yet, so the
        results only inform -- they never block a submit.

        Runs on a plain worker thread and touches nothing but locals. It used
        to assign ``_ok_rect`` / ``_pattern_rect`` / ``_display`` directly,
        which mutated a QDialog off the GUI thread -- the exact thing
        ``_ThreadBridge`` and ``_TaskBridge`` exist to prevent elsewhere in
        this package. The result now goes out through ``_bounds_ready``; Qt
        queues that into the GUI thread's event loop, so the assignments
        happen on the thread that owns the widgets.
        """
        try:
            adb = self._adb
            adb.shell(
                "uiautomator dump /sdcard/droidmirror_ui.xml",
                check=False,
                timeout=15.0,
            )
            result = adb.shell(
                "cat /sdcard/droidmirror_ui.xml", check=False, timeout=10.0
            )
            adb.shell(
                "rm -f /sdcard/droidmirror_ui.xml", check=False, timeout=5.0
            )
            xml = getattr(result, "stdout", "") or ""
            ok_rect = parse_node_bounds(xml, OK_NODE)
            pattern_rect = parse_node_bounds(xml, PATTERN_NODE)
            display = fetch_display_size(adb)
        except Exception:
            self._bounds_ready.emit(None, None, None)
            return
        self._bounds_ready.emit(ok_rect, pattern_rect, display)

    def _on_bounds_ready(self, ok_rect, pattern_rect, display) -> None:
        """GUI-thread slot: publish the fetched bounds (or clear them)."""
        self._ok_rect = ok_rect
        self._pattern_rect = pattern_rect
        self._display = display
        self._bounds_busy = False

    # -- actions ---------------------------------------------------------- #

    def _digit(self, label: str) -> None:
        self._send_key(KEYCODE_0 + int(label))
        self._entered += label
        self._pin_echo.setText("\u2022" * len(self._entered))

    def _pin_backspace(self) -> None:
        self._send_key(KEYCODE_DEL)
        if self._entered:
            self._entered = self._entered[:-1]
        self._pin_echo.setText("\u2022" * len(self._entered))

    def _reset_entry(self) -> None:
        self._entered = ""
        self._pin_echo.setText("")

    def _wake(self) -> None:
        self._send_key(KEYCODE_WAKEUP)

    def _frame(self) -> tuple[int, int]:
        fw, fh = self._frame_size()
        return (fw, fh) if fw > 0 and fh > 0 else (1, 1)

    def _tap(self, rect) -> bool:
        """Tap the center of a display-px rect through the stream's frame
        space. Returns False when the rect is unknown."""
        if rect is None or self._display is None:
            return False
        left, top, right, bottom = scale_rect(rect, self._display, self._frame())
        x = (left + right) // 2
        y = (top + bottom) // 2
        fw, fh = self._frame()
        x, y = min(max(x, 0), fw - 1), min(max(y, 0), fh - 1)
        args = (POINTER_ID_MOUSE, x, y, fw, fh, 1, 1)
        self._send_touch(MotionAction.DOWN, *args)
        self._send_touch(MotionAction.UP, POINTER_ID_MOUSE, x, y, fw, fh, 0, 0)
        return True

    def _submit_pin(self) -> None:
        tapped = self._tap(self._ok_rect)
        if not tapped:
            self._send_key(KEYCODE_ENTER)
        self._reset_entry()
        self._set_status("Sent — check the phone.")

    def _submit_password(self) -> None:
        text = self._password.text()
        if not text:
            return
        self._send_text(text)
        self._send_key(KEYCODE_ENTER)
        self._password.clear()
        self._set_status("Sent — check the phone.")

    def _pattern_point(self, nx: float, ny: float) -> tuple[int, int]:
        fw, fh = self._frame()
        if self._pattern_rect is not None and self._display is not None:
            left, top, right, bottom = scale_rect(
                self._pattern_rect, self._display, (fw, fh)
            )
            x = left + nx * (right - left)
            y = top + ny * (bottom - top)
        else:
            l, t, r, b = DEFAULT_PATTERN_RECT
            x = (l + nx * (r - l)) * fw
            y = (t + ny * (b - t)) * fh
        return int(min(max(x, 0.0), fw - 1)), int(min(max(y, 0.0), fh - 1))

    def _submit_pattern(self) -> None:
        points = self._pattern.points
        if len(points) < 2:
            self._set_status("Draw a pattern first.")
            return
        fw, fh = self._frame()
        first = self._pattern_point(*points[0])
        self._send_touch(
            MotionAction.DOWN, POINTER_ID_VIRTUAL_FINGER, first[0], first[1],
            fw, fh, 0, 0,
        )
        # ~24 samples: enough for the keyguard to register every dot.
        step = max(1, len(points) // 24)
        for nx, ny in points[::step][1:]:
            x, y = self._pattern_point(nx, ny)
            self._send_touch(
                MotionAction.MOVE, POINTER_ID_VIRTUAL_FINGER, x, y, fw, fh, 0, 0
            )
        last = self._pattern_point(*points[-1])
        self._send_touch(
            MotionAction.UP, POINTER_ID_VIRTUAL_FINGER, last[0], last[1],
            fw, fh, 0, 0,
        )
        # Clear so a retry draws from scratch instead of appending to a stale
        # path (which used to replay the previous attempt).
        self._pattern.clear()
        self._set_status("Sent — check the phone.")

    def _submit_swipe(self) -> None:
        self._swipe_path()
        self._set_status("Swiped — check the phone.")

    def _reveal(self) -> None:
        """Wake + swipe up: brings the secure pad (PIN/pattern) on screen."""
        self._swipe_path()

    def _swipe_path(self) -> None:
        self._wake()
        fw, fh = self._frame()
        cx = fw // 2
        self._send_touch(
            MotionAction.DOWN, POINTER_ID_VIRTUAL_FINGER, cx, int(fh * 0.8),
            fw, fh, 0, 0,
        )
        for frac in (0.6, 0.4):
            self._send_touch(
                MotionAction.MOVE, POINTER_ID_VIRTUAL_FINGER, cx, int(fh * frac),
                fw, fh, 0, 0,
            )
        self._send_touch(
            MotionAction.UP, POINTER_ID_VIRTUAL_FINGER, cx, int(fh * 0.4),
            fw, fh, 0, 0,
        )
