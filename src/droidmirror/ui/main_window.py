"""Main application window.

Layout: toolbar + mirror view, with a docked keybind table. Connection,
profiles and the keymapper all meet here.
"""

from __future__ import annotations

import ctypes
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QEvent,
    QObject,
    QPoint,
    QPointF,
    QRect,
    Qt,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtGui import QAction, QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDockWidget,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSlider,
    QStyle,
    QStyleOptionSlider,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..adb import (
    Adb,
    AdbError,
    Device,
    dedupe_wireless,
    looks_wireless,
    normalize_address,
    remember_ip,
)
from ..config import SCRCPY_VERSION, project_root
from ..appicons import IconCache
from ..decoder import HW_OFF, DecodedFrame
from ..discord_rpc import DiscordPresence, is_home, is_image_reference
from ..keymap import (
    JOYSTICK_KEYS,
    MODE_HOLD,
    MODE_TAP,
    Binding,
    Joystick,
    KeymapEngine,
    Profile,
    is_default_profile_name,
    profile_path,
)
from ..protocol import (
    AndroidKey,
    KeyAction,
    MotionAction,
    serialize_rotate_device,
)
from ..server import ServerOptions, wireless_options
from ..session import FpsCounter, MirrorSession
from ..settings import UiSettings
from ..siteicons import icon_url_for_package
from ..volume import VolumeState
from . import keys as keynames
from .glview import (
    DRAG_HELD_BUTTONS,
    DRAG_POINTER_ID,
    DRAG_PRESS_BUTTON,
    MirrorView,
)
from .rawlook import RawMouseLook
from .keypickdialog import KeyCaptureDialog
from .lockdialog import LockDialog
from .wifidialog import WifiConnectDialog


class _ClickableLabel(QLabel):
    """A statusbar readout you can click (the lock chip opens the panel)."""

    clicked = Signal()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class _ClickToPositionSlider(QSlider):
    """A slider that jumps to the point clicked instead of by a page step.

    QSlider answers a press on the groove with ``pageStep`` -- 10 by default --
    rather than with the position under the cursor. On the tablet's own 0..15
    range that meant a click anywhere left of the handle landed on
    ``15 - 10 = 5``, and anywhere right of it jumped to the top, whichever part
    of the bar was aimed at. He asked for the handle to end up where he clicked.
    """

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            # Move the handle first, then let QSlider see the press. The base
            # class starts a drag when the press lands on the *handle*, and it
            # is now under the cursor -- so a drag continues from the click and
            # the page step never comes into it.
            self.setValue(self._value_at(event.position().x()))
        super().mousePressEvent(event)

    def _value_at(self, x: float) -> int:
        """The value this slider draws at ``x`` -- Qt's own mapping."""
        option = QStyleOptionSlider()
        self.initStyleOption(option)
        groove = self.style().subControlRect(
            QStyle.ComplexControl.CC_Slider,
            option,
            QStyle.SubControl.SC_SliderGroove,
            self,
        )
        handle = self.style().subControlRect(
            QStyle.ComplexControl.CC_Slider,
            option,
            QStyle.SubControl.SC_SliderHandle,
            self,
        )
        # The handle centres on its position, so it travels the groove less its
        # own width, and a click is measured from the groove to the centre.
        span = groove.width() - handle.width()
        if span <= 0:  # narrower than the handle: no positions to tell apart
            return self.value()
        return self.style().sliderValueFromPosition(
            self.minimum(),
            self.maximum(),
            int(x - groove.x() - handle.width() / 2),
            span,
        )


class _ThreadBridge(QObject):
    """Carries reader-thread callbacks onto the GUI thread.

    A cross-thread signal emission is queued into the receiver's event loop,
    which is exactly what we need. Note ``QTimer.singleShot`` would *not* work
    here: called from the reader thread it targets that thread's loop, and the
    reader has none.
    """

    status = Signal(str)
    device_name = Signal(str)
    closed = Signal()


class _TaskBridge(QObject):
    """Hands a worker thread's result back to the GUI thread.

    Same trick as :class:`_ThreadBridge`, for the same reason: emitting a
    signal from a plain thread queues it into the receiver's event loop, so
    ``on_done`` always runs where widgets may be touched.
    """

    done = Signal(object)


class _IconBridge(QObject):
    """Lets the icon worker tell the GUI thread an upload finished."""

    #: package whose icon now has a Discord url
    discord_icon = Signal(str)


#: How long a single ``adb connect`` may take before we call it unreachable.
#: Kept short on purpose: a dead host must fail fast with a message instead
#: of leaving the user staring at a frozen window.
WIRELESS_CONNECT_TIMEOUT = 10.0

#: How long ``adb devices -l`` may take. This one runs synchronously on the
#: GUI thread (toolbar refresh, transport filter, serial selection), so a
#: wedged adb daemon must fail fast rather than hang the window -- see
#: Adb.devices(), which keeps the 30s default for CLI callers.
DEVICE_LIST_TIMEOUT_S = 5.0

#: How often the Discord presence is refreshed. The app on the tablet only
#: changes every few seconds, and each refresh is one IPC write, so this is
#: deliberately lazy.
DISCORD_POLL_MS = 5000

#: How often the GUI picks up the newest decoded frame. The decode itself runs
#: on the reader thread; this only samples the latest result, so a small poll
#: interval bounds added latency while making frame-drops impossible.
FRAME_POLL_MS = 4

#: How long the volume controls wait after the last change before the level is
#: read back. Both paths move the tablet with its *own* volume keycodes, which
#: report nothing back, so the slider and the readout would drift; one read once
#: the changes stop fixes that for the cost of a single dumpsys rather than one
#: per step.
#:
#: There is no debounce on the slider any more. It used to wait out 180 ms and
#: then spend a further ~193 ms (measured) on an adb round trip before the
#: tablet moved at all -- while a keycode is a write to the control socket the
#: session already holds open, and lands while the finger is still dragging.
VOLUME_SYNC_MS = 700

#: Shown by the Help action. Kept as HTML so the instructions stay scannable.
HELP_TEXT = """
<h3>Connect over USB</h3>
<ol>
  <li>On the device: Settings &gt; About &gt; tap <b>Build number</b> 7 times,
      then enable <b>USB debugging</b> in Developer options.</li>
  <li>Plug it in, accept the &quot;Allow USB debugging&quot; prompt.</li>
  <li>Pick the device in the toolbar list and click <b>Connect</b>.</li>
</ol>

<h3>Connect over Wi-Fi</h3>
<b>Easiest way:</b>
<ol>
  <li>Keep USB plugged in, click the transport button (it reads
      <b>USB</b>).</li>
  <li>It switches the device to Wi-Fi itself, then asks for the address
      with the device's own IP already filled in &mdash; confirm it.</li>
  <li>Once it says &quot;you can unplug USB now&quot;, unplug the cable.</li>
</ol>
Both devices must be on the <b>same Wi-Fi network</b>.
<br><br>
The status bar always shows which transport is live &mdash; <b>link: USB</b>
or <b>link: Wi-Fi (ip:port)</b> &mdash; and the device list tags each entry
with its link. <b>Wi-Fi adds input lag</b>: if the mirror feels sluggish,
check that readout first and use the cable for the lowest delay.
<br><br>
<b>The transport button beside the device list</b> shows the current mode
(<code>USB</code> / <code>Wireless</code>). Pointing it at
<code>Wireless</code> is the switch-to-Wi-Fi flow: it asks for the device's
address (the last one that worked comes pre-filled), connects, and only then
flips the list. Pointing it back at <code>USB</code> is a plain flip &mdash;
the list then shows only cable devices, and re-scans as it switches.
<br><br>
<b>Finding the IP address</b> (only needed if USB is not attached):
<ul>
  <li>Samsung: Settings &gt; Connections &gt; Wi-Fi &gt; tap the connected
      network &gt; <b>IP address</b>.</li>
  <li>Or: Settings &gt; About tablet &gt; <b>Status</b> &gt; IP address.</li>
  <li>Enter it in the prompt as e.g. <code>192.168.1.42</code>
      (the app appends adb's <code>:5555</code>). Every address that
      connects is remembered &mdash; the <b>History</b> bar beside the field
      slides open with the list, newest first, so a known device is one pick
      away.</li>
</ul>
The device must already be in TCP mode: plug in USB <b>once</b> and click
the transport button (it runs <code>adb tcpip 5555</code> for you), or run
<code>adb tcpip 5555</code> by hand.
<br><br>
On <b>Android 11+</b> you can skip USB entirely: enable Developer options &gt;
<b>Wireless debugging</b>, then pair with
<code>adb pair &lt;ip:port&gt; &lt;code&gt;</code>.

<h3>Keybinds</h3>
<ul>
  <li><b>Add binding</b> then press a key, then click the phone screen.</li>
  <li><b>Add joystick</b> then click over a game's virtual stick: W/A/S/D
      drive it.</li>
  <li>Double-click a row to switch tap (a quick press) and hold.</li>
</ul>

<h3>Clipboard</h3>
(with the mirror focused - click it first)
<ul>
  <li><code>Ctrl+V</code> &mdash; paste what you copied on this computer into
      the device's focused field.</li>
  <li><code>Ctrl+Shift+V</code> &mdash; paste what the <b>device</b> copied
      (the device's own clipboard), for when the text is already on the
      tablet.</li>
</ul>

<h3>Sound</h3>
The device's audio plays on this computer. <b>Mute</b> in the toolbar
silences it; the stream keeps running, so un-muting is instant. The choice is
remembered for the next connection.

<h3>Hotkeys</h3>
<ul>
  <li><code>`</code> &mdash; switch mouse &harr; keybind control</li>
  <li><code>x</code> &mdash; camera drag. A drag is planted on the profile's
      <b>Camera Movement</b> spot (the middle of the screen unless you move
      it) and held down for you. Mouse movement then turns the camera as far
      as you like, in any direction, with no screen edge to run into. Holding
      the left button yourself works too &mdash; it is the same drag. Press
      <code>x</code> again to release it.</li>
  <li><code>Ctrl+V</code> / <code>Ctrl+Shift+V</code> &mdash; paste from this
      computer / from the device</li>
  <li><code>F9</code> &mdash; latency HUD</li>
  <li><code>F7</code> &mdash; show / hide the keybind markers (the WASD pad
      and camera spot too). Handy for a clean view; the bindings keep
      working while hidden.</li>
  <li><code>F11</code> &mdash; fullscreen (<code>Esc</code> exits)</li>
</ul>

<h3>If it will not connect</h3>
<ul>
  <li>Both devices on the same Wi-Fi; no guest network / AP isolation.</li>
  <li>A VPN or firewall on this computer can block adb.</li>
  <li>Re-run the Wi-Fi switch (the transport button) if the device's IP
      address changed &mdash; the prompt remembers the old one for
      editing.</li>
</ul>
"""

#: How often the hidden cursor is sampled and put back where it is parked.
#: Each tick's displacement from the park point IS the look delta; the OS
#: cursor is the source of truth, so putting it back cannot echo as a
#: counter-rotation the way a Qt move event can.
LOOK_POLL_MS = 5

#: Mouse-device deltas are counts, not pixels: the OS pointer-acceleration
#: curve is not applied to them. Only used when the device's raw stream is
#: the delta source. Nudge up if the look feels sluggish, down if twitchy.
LOOK_RAW_GAIN = 1.0

#: Mouse-look keeps the virtual finger inside this fraction of the device
#: screen. On touching the band the finger is lifted and re-planted at the
#: center, so a drag can rotate forever instead of dying at a screen edge.
LOOK_EDGE_MARGIN = 0.08


def _edge_band(fw: int, fh: int) -> tuple[int, int]:
    """The no-go band around the device screen edge, in frame pixels.

    Whole pixels, and used by both the anchor clamp and the relocation test:
    a fractional band meant an anchor *on* the boundary was clamped to a
    position the test still called "at the edge", so every single step
    re-planted the drag and the camera never went anywhere.
    """
    return int(fw * LOOK_EDGE_MARGIN), int(fh * LOOK_EDGE_MARGIN)


def _log_flow_error(what: str, exc: BaseException) -> None:
    """Record a swallowed flow error next to the crash hook's own output."""
    from ..crashlog import _write

    _write(type(exc), exc, exc.__traceback__)
    # sys.stderr is None when there is no console -- pythonw.exe, or a launch
    # through DroidMirror.vbs. This is called from inside `except` blocks, so
    # an unguarded write would replace the error being reported with an
    # AttributeError raised out of the very handler meant to contain it. The
    # crash.log write above is the record that has to survive; the echo to a
    # console that may not exist is a courtesy.
    if sys.stderr is not None:
        sys.stderr.write(f"[{what}] {exc!r}\n")


def _cursor_api_available() -> bool:
    """Whether the OS cursor can be read/warped from here.

    True only on a real Windows desktop session. Two things rule it out:
    another platform has no ``SetCursorPos``/``GetCursorPos`` at all, and Qt's
    ``offscreen`` platform (what the test suite runs under) accepts the calls
    but has no real cursor, so every read returns a constant and warping is a
    no-op -- the camera lock would appear to do nothing.

    This guard was copy-pasted into eight call sites; they must agree, or some
    of them warp the pointer while others believe they failed.
    """
    return sys.platform == "win32" and QGuiApplication.platformName() != "offscreen"


def _is_printable_key(name: str) -> bool:
    """True when the key produces a character: a letter, digit or punctuation.

    Named keys ("up", "enter", "f7") are not printable, so bindings keep
    firing for them even while the tablet's own keyboard is up -- only real
    typing gets priority.
    """
    return len(name) == 1 and name.isprintable()


class MainWindow(QMainWindow):
    def __init__(
        self,
        adb: Adb | None = None,
        options: ServerOptions | None = None,
        profile: Profile | None = None,
        hw_decode: str = HW_OFF,
    ) -> None:
        super().__init__()
        self.setWindowTitle(f"DroidMirror - scrcpy {SCRCPY_VERSION}")
        self.resize(1180, 720)

        self._adb = adb
        self._options = options or ServerOptions()
        self._hw_decode = hw_decode or HW_OFF
        self._session: MirrorSession | None = None
        self._profile = profile or Profile()
        self._last_frame: DecodedFrame | None = None
        self._pending_key: str | None = None
        #: True while the keystroke currently arriving is being consumed by the
        #: keymap engine (a binding or the joystick pad fired). Set before the
        #: companion text_input signal arrives for the same event.
        self._key_fired = False
        self._connected = False
        #: Device-list filter: False = USB devices only, True = wireless only.
        self._wireless_mode = False
        #: True while the wireless flow runs in the background. A second
        #: click is ignored rather than starting a second concurrent flow.
        self._wireless_busy = False
        #: Tests set this to drive the background flow inline (no thread).
        self._sync_workers = False
        #: The live background worker (adb off the GUI thread). Held here so
        #: its bridge and result survive until the GUI thread runs them.
        self._task_bridge: _TaskBridge | None = None
        self._task_thread: threading.Thread | None = None
        self._active_workers: set[_TaskBridge] = set()
        #: Remembers small UI choices (the filter above) between launches.
        self._settings = UiSettings()
        self._render_path = ""
        self._fps_counter = FpsCounter(window_s=1.0)
        self._fullscreen = False
        #: Mouse-look state (x): cursor hidden, movement alone steers the
        #: camera (no button held, no cursor position involved).
        self._mouse_locked = False
        #: Device sound muted on this computer? Remembered between launches.
        self._muted = bool(self._settings.get("muted", False))
        #: The *tablet's* media volume, or None until it has been read (or when
        #: the device would not report it). A separate thing from _muted above:
        #: that silences this computer, this changes the device itself.
        self._volume_state: VolumeState | None = None
        #: Where the tablet's volume is believed to be *right now*: the last
        #: reading, moved by every step sent since. The keycode path reports
        #: nothing back, so without this the next slider move would compute its
        #: distance from a stale start and land somewhere the user did not ask
        #: for. None means "no idea", and then nothing is sent.
        self._volume_level: int | None = None
        #: The number the user dragged the slider to, until a reading confirms
        #: it. Only the slider has an absolute intent -- a step is relative, and
        #: clears this so a read never "corrects" a step into something else.
        self._volume_target: int | None = None
        #: True while the slider is being written *from* device state rather
        #: than by the user, so a refresh does not bounce straight back out as
        #: a write. Not blockSignals(): the readout still has to follow.
        self._volume_applying = False
        #: True while _set_mouse_lock is moving the chrome around, so the
        #: transient ActivationChange that causes cannot undo the lock.
        self._lock_changing = False
        #: Win32 raw mouse input: the device's own deltas, used when the OS
        #: delivers them. The cursor poll below is the guaranteed source.
        self._raw_look = RawMouseLook()
        self._raw_active = False
        #: Where the hidden pointer is held (physical px) and where the
        #: camera drag is planted (frame px). Both come from wherever the
        #: pointer was when the user locked, so the drag starts on the part
        #: of the game screen the user chose.
        self._look_park: tuple[int, int] | None = None
        self._look_anchor: tuple[int, int] | None = None
        #: True while the *user* is holding the left button themselves.
        self._look_press = False
        #: True once the device's raw stream has actually delivered a delta.
        self._raw_seen = False
        #: True once the pointer's own travel has been chosen as the source.
        self._pointer_driven = False
        #: True while the camera drag is down on the device.
        self._look_down = False
        self._look_x = 0.0
        self._look_y = 0.0
        self._look_poll = QTimer(self)
        self._look_poll.setInterval(LOOK_POLL_MS)
        self._look_poll.timeout.connect(self._look_poll_tick)
        #: The floating key recorder, built on first use and reused after that.
        self._key_dialog: KeyCaptureDialog | None = None
        #: Edit-mode state the recorder found when *Add binding* opened it,
        #: restored when a recording is cancelled so no crosshair outlives it.
        self._add_prev_edit = False
        #: (address, code) from the Wi-Fi prompt when the device needed
        #: pairing, or None. Declared here because _connect_wireless reads it
        #: back after the modal dialog returns; it used to be read through
        #: getattr() purely because __init__ never set it.
        self._wifi_pairing: tuple[str, str] | None = None
        #: Toolbar visibility saved when *fullscreen* hides it (F11 only --
        #: the x-key camera lock leaves the toolbar, dock and status bar be).
        self._dock_was_visible = True
        self._statusbar_was_visible = True

        self._view = MirrorView()
        self._view.injector = self._inject_mouse
        self._view.look_move = self._on_look_move
        self._view.look_button = self._on_look_button
        self._view.key_pressed.connect(self._on_key_pressed)
        self._view.key_released.connect(self._on_key_released)
        self._view.text_input.connect(self._on_text_input)
        self._view.paste_computer_clipboard.connect(self._paste_computer_clipboard)
        self._view.paste_device_clipboard.connect(self._paste_device_clipboard)
        self._view.binding_placed.connect(self._on_binding_placed)
        self._view.binding_moved.connect(self._on_binding_moved)
        self._view.binding_selected.connect(self._on_binding_selected)
        self._view.joystick_placed.connect(self._on_joystick_placed)
        self._view.joystick_moved.connect(self._on_joystick_moved)
        self._view.joystick_selected.connect(self._on_joystick_selected)
        self._view.joystick_resized.connect(self._on_joystick_resized)
        self._view.camera_placed.connect(self._on_camera_placed)
        self._view.camera_moved.connect(self._on_camera_moved)
        self._view.camera_selected.connect(self._on_camera_selected)
        self._view.camera_locked_click.connect(self._on_camera_locked_click)
        # The toolbar row and the mirror share one central widget. A
        # QHBoxLayout cannot wrap onto a second line the way QToolBar's flow
        # layout did, so the row is one row for good -- whatever does not fit
        # moves into the overflow menu instead of being clipped.
        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        self._bar = QWidget()
        self._bar.setObjectName("mainToolbar")
        # Ignored horizontally on purpose: the row must not impose a minimum
        # width on the window (the sum of every button's readable width would
        # force the whole app wider than the screen). The bar is given the
        # space it can get, and the overflow menu handles the rest.
        self._bar.setMinimumWidth(0)
        self._bar.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed
        )
        # Flat, borderless text buttons -- the plain toolbar look. The default
        # QToolButton styling drew a box around every one of them, which with
        # twenty of them read as a wall of outlines rather than a toolbar.
        self._bar.setStyleSheet(
            """
            QToolButton {
                border: none;
                border-radius: 3px;
                background: transparent;
                padding: 1px 3px;
                margin: 0px;
            }
            QToolButton:hover  { background: rgba(0, 0, 0, 55); }
            QToolButton:pressed, QToolButton:checked {
                background: #cfe2ff;
                color: #10203a;
            }
            QToolButton:disabled { color: #9a9a9a; }
            """
        )
        self._bar_layout = QHBoxLayout(self._bar)
        self._bar_layout.setContentsMargins(8, 3, 4, 3)
        self._bar_layout.setSpacing(2)
        #: Ordered (button, action) pairs; the tail of this list is what gets
        #: pushed into the overflow menu when the window is too narrow.
        self._bar_items: list[tuple[QToolButton, QAction]] = []
        #: (separator, index into _bar_items of the button just before it).
        #: The button just after it is always at that index + 1, because the
        #: toolbar adds one separator between two consecutive buttons and
        #: nothing else inserts into the bar. _rebuild_overflow() needs this to
        #: hide a separator whose group slid into the » menu.
        self._bar_seps: list[tuple[QFrame, int]] = []
        #: The "»" button that holds whatever did not fit (built on demand).
        self._overflow: QToolButton | None = None
        #: Rebuilds the overflow after the layout has settled (see resizeEvent).
        self._overflow_timer = QTimer(self)
        self._overflow_timer.setSingleShot(True)
        self._overflow_timer.timeout.connect(self._rebuild_overflow)
        central_layout.addWidget(self._bar)
        central_layout.addWidget(self._view, 1)
        self.setCentralWidget(central)

        # Dragging the pad fires at mouse rate; persist the profile once the
        # cursor has been still for a beat instead of writing JSON per pixel.
        self._joy_save_timer = QTimer(self)
        self._joy_save_timer.setSingleShot(True)
        self._joy_save_timer.setInterval(400)
        self._joy_save_timer.timeout.connect(self._auto_save)

        self._engine = KeymapEngine(
            inject=self._inject_touch,
            profile=self._profile,
            frame_size_provider=lambda: self._view.frame_size,
        )

        self._build_toolbar()
        self._build_keymap_dock()
        self._build_statusbar()
        self._view.hud_changed.connect(self._sync_hud_action)
        self._view.overlay_visibility_changed.connect(self._sync_overlay_action)

        # Wired after the status bar exists -- _bridge.status targets _status.
        self._bridge = _ThreadBridge(self)
        self._icon_bridge = _IconBridge(self)
        self._icon_bridge.discord_icon.connect(self._on_icon_ready)
        self._bridge.status.connect(self._status.setText)
        self._bridge.device_name.connect(
            lambda name: self.setWindowTitle(f"DroidMirror - {name}")
        )
        self._bridge.closed.connect(self._on_stream_closed)

        self._view.control_target_changed.connect(self._on_control_target_changed)

        # Window-level so it fires no matter which child widget has focus.
        self._toggle_shortcut = QShortcut(self)
        self._toggle_shortcut.setKey(
            QKeySequence(keynames.TOGGLE_KEY_CODE)
        )
        self._toggle_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self._toggle_shortcut.activated.connect(self._on_toggle_hotkey)

        # F11 gets its own window-level shortcut rather than living on the
        # toolbar action: fullscreen hides the bar, and a QAction's shortcut
        # stops firing with its widget -- which would strand the user in
        # fullscreen with no way back except Esc.
        self._fullscreen_shortcut = QShortcut(
            QKeySequence(keynames.FULLSCREEN_KEY_CODE), self
        )
        self._fullscreen_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self._fullscreen_shortcut.activated.connect(self.toggle_fullscreen)

        # Armed only while fullscreen, otherwise the "escape" keybind would be
        # swallowed by the shortcut everywhere.
        self._exit_fullscreen_shortcut = QShortcut(
            QKeySequence(Qt.Key.Key_Escape), self
        )
        self._exit_fullscreen_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self._exit_fullscreen_shortcut.setEnabled(False)
        self._exit_fullscreen_shortcut.activated.connect(self.leave_fullscreen)

        self._refresh_bindings()
        self._refresh_profiles()
        self._initial_filter()
        self._refresh_devices()
        # The remembered panel state is applied after the dock exists.
        self._dock.setVisible(bool(self._settings.get("keys_panel", True)))
        self._rebuild_overflow()

        self._frame_timer = QTimer(self)
        self._frame_timer.setInterval(FRAME_POLL_MS)
        self._frame_timer.timeout.connect(self._poll_frame)
        self._frame_timer.start()

    # ------------------------------------------------------------------ #
    # Construction
    # ------------------------------------------------------------------ #

    def _bar_add(self, action: QAction) -> QToolButton:
        """Put an action in the toolbar row as a button.

        Every button goes into a tracked list, because the overflow menu is
        rebuilt from it whenever the window is too narrow: the least-used tail
        slides under the » button rather than wrapping to a second, clipped
        line.
        """
        button = QToolButton(self._bar)
        button.setDefaultAction(action)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        # The button's own sizeHint is the only width that is guaranteed not
        # to elide: it already includes the style sheet's padding and the
        # style's frame. Measuring the text and adding a margin instead is
        # what produced "Re...sh" and "Remo... pad" -- Qt reserves that padding
        # inside the button, so the label ran out of room and it elided the
        # middle rather than wrapping or clipping.
        button.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )

        def _refit() -> None:
            # Width must follow the label. "Connect" becomes "Disconnect" on
            # connect, and a width captured once at build time then painted the
            # longer word into the shorter box as "Disconn...". sizeHint()
            # already tracks the current text, so re-pinning it here is enough.
            #
            # Fixed policy alone is NOT enough either: a Fixed button may still
            # be squeezed down to its minimumSizeHint when the row runs out of
            # room, which is how "Re...sh" and "Remo... pad" appeared. Hence
            # the fixed width stays -- it just is not a one-off measurement.
            width = button.sizeHint().width()
            if button.width() != width:
                button.setFixedWidth(width)
            self._schedule_overflow()

        button.setFixedWidth(button.sizeHint().width())
        action.changed.connect(_refit)
        button.setToolTip(action.toolTip() or action.text())
        self._bar_layout.addWidget(button)
        self._bar_items.append((button, action))
        return button

    def _on_panel_toggled(self, on: bool) -> None:
        """Show or hide the Keybinds dock, and remember the choice."""
        self._settings.set("keys_panel", bool(on))
        self._dock.setVisible(bool(on))
        # The dock changes how much room the mirror (and the toolbar) get.
        self._schedule_overflow()

    def _bar_sep(self) -> None:
        line = QFrame(self._bar)
        line.setFrameShape(QFrame.Shape.VLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        line.setFixedHeight(18)
        self._bar_layout.addWidget(line)
        self._bar_seps.append((line, len(self._bar_items) - 1))

    def _build_toolbar(self) -> None:
        # Fixed width, and no padding baked into the text: a QLabel's default
        # Preferred policy lets the layout hand it whatever space is going
        # spare, so on a wide window this label stretched from its 108px of
        # text to 182px and the visible gap before the combo was that slack
        # rather than anything to do with the text. The left-hand breathing
        # room now comes from the layout margin instead of a leading space.
        label = QLabel("Device:")
        label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        self._bar_layout.addWidget(label)
        self._device_combo = QComboBox()
        # Bounded on both sides. Preferred with only a minimum let the combo
        # grow to fit "no wireless device connected" and run its text straight
        # into the Refresh button beside it; capped, a long device name elides
        # instead ("SM_X210…") and the toolbar stays readable.
        self._device_combo.setMinimumWidth(180)
        self._device_combo.setMaximumWidth(250)
        self._device_combo.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        self._device_combo.currentIndexChanged.connect(self._update_link)
        self._bar_layout.addWidget(self._device_combo)
        # Breathing room so the combo's text never touches the next button.
        self._bar_layout.addSpacing(8)

        # Add another device. Auto-discovery (the Wi-Fi button) only ever finds
        # the device this app already knows about, so until this existed there
        # was no way to bring in a *second* tablet or phone -- the list was just
        # whatever adb already had. This opens the address prompt directly: type
        # the new device's IP:port, plus its pairing code on Android 11+.
        self._add_device_action = QAction("Add", self)
        self._add_device_action.setToolTip(
            "Add another device over Wi-Fi: type its IP address, and its "
            "pairing code on Android 11+"
        )
        self._add_device_action.triggered.connect(self._add_wireless_device)
        self._bar_add(self._add_device_action)

        self._refresh_action = QAction("Refresh", self)
        self._refresh_action.triggered.connect(self._refresh_devices)
        self._bar_add(self._refresh_action)

        self._connect_action = QAction("Connect", self)
        self._connect_action.triggered.connect(self._toggle_connection)
        self._bar_add(self._connect_action)

        # Exactly two ways to connect: USB or Wi-Fi (same network). The pair
        # behaves like radio buttons -- one is always lit. Clicking Wi-Fi *is*
        # the go-wireless flow: it finds the device on the network by itself
        # and only flips the list once something is connected. ``_filter_action``
        # is the Wi-Fi button (checked == wireless mode).
        self._usb_action = QAction("USB", self)
        self._usb_action.setCheckable(True)
        self._usb_action.setToolTip("Connect with the USB cable (lowest lag)")
        self._usb_action.triggered.connect(self._on_usb_clicked)
        self._bar_add(self._usb_action)

        self._filter_action = QAction("Wi-Fi", self)
        self._filter_action.setCheckable(True)
        self._filter_action.toggled.connect(self._on_filter_toggled)
        self._filter_action.triggered.connect(self._on_filter_clicked)
        self._bar_add(self._filter_action)
        self._sync_filter_label()

        self._bar_sep()

        # Short labels on purpose: with the device buttons, the transport pair
        # and the view toggles all on one bar, long names made the toolbar wrap
        # onto a second row that then got clipped. The tooltips carry the
        # detail instead.
        self._mode_action = QAction("Edit keys", self)
        self._mode_action.setCheckable(True)
        self._mode_action.setToolTip(
            "Edit keybinds: place markers on the phone screen"
        )
        self._mode_action.toggled.connect(self._on_mode_toggled)
        self._bar_add(self._mode_action)

        # Mirrors the ` hotkey so the current target is always visible.
        self._keybind_action = QAction("Keys", self)
        self._keybind_action.setCheckable(True)
        self._keybind_action.setToolTip(
            "Toggle mouse / keybind control (hotkey: `)"
        )
        self._keybind_action.toggled.connect(self._on_keybind_toggled)
        self._bar_add(self._keybind_action)

        self._add_action = QAction("Add key", self)
        self._add_action.setCheckable(True)
        self._add_action.setToolTip(
            "Open the key recorder, then click the phone screen to place it. "
            "Click again to close the recorder."
        )
        self._add_action.triggered.connect(self._begin_add_binding)
        self._bar_add(self._add_action)

        # GameLoop-style pad: click once over the game's virtual stick and the
        # WASD keys drive a finger inside it from then on.
        self._add_joy_action = QAction("Add pad", self)
        self._add_joy_action.setToolTip(
            "Then click over the game's stick to place the WASD pad"
        )
        self._add_joy_action.triggered.connect(self._begin_add_joystick)
        self._bar_add(self._add_joy_action)

        # Show/hide the whole Keybinds panel. It is a wide dock and the mirror
        # deserves the space -- especially with the toolbar needing room too.
        self._panel_action = QAction("Panel", self)
        self._panel_action.setCheckable(True)
        self._panel_action.setChecked(bool(self._settings.get("keys_panel", True)))
        self._panel_action.setToolTip(
            "Show or hide the Keybinds panel (remembered between launches)"
        )
        self._panel_action.toggled.connect(self._on_panel_toggled)
        self._bar_add(self._panel_action)

        self._remove_joy_action = QAction("Remove pad", self)
        self._remove_joy_action.setToolTip("Remove the WASD pad from this profile")
        self._remove_joy_action.triggered.connect(self._remove_joystick)
        self._bar_add(self._remove_joy_action)

        # Discord Rich Presence: advertises whichever app is in the foreground
        # on the tablet. Needs a Discord *application id* (see the prompt when
        # it is missing); everything stays quiet until one is set.
        self._discord = DiscordPresence()
        #: Real app icons, pulled from the device and uploaded to Discord.
        self._icon_cache = IconCache(project_root() / "icons")
        #: Packages whose icon is being pulled/uploaded right now. Per-package,
        #: not one global flag: a 240 MB APK takes several adb round trips, and
        #: a single shared flag made every *other* app fall back to the emoji
        #: for the duration. Entries are removed in a finally (see
        #: _resolve_icon) so a failure can never wedge one permanently.
        self._icon_resolving: set[str] = set()
        self._discord_action = QAction("Discord", self)
        self._discord_action.setCheckable(True)
        self._discord_action.setChecked(bool(self._settings.get("discord", False)))
        self._discord_action.setToolTip(
            "Show the app you are using on the tablet in Discord (needs a "
            "Discord application id)"
        )
        self._discord_action.toggled.connect(self._on_discord_toggled)
        discord_btn = self._bar_add(self._discord_action)
        # Right-click the button -> the settings the toggle alone cannot reach.
        # The application id is asked exactly once and then never again, so
        # without this there was no way to correct a wrong one -- or to move to
        # a fresh application when another tool turns out to share the old id.
        token_menu = QMenu(discord_btn)
        token_menu.addAction("Application ID...").triggered.connect(
            self._set_discord_app_id
        )
        discord_btn.setToolTip(
            self._discord_action.toolTip()
            + "\n\nRight-click to change the application id."
        )
        discord_btn.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        discord_btn.customContextMenuRequested.connect(
            lambda pos: token_menu.exec(discord_btn.mapToGlobal(pos))
        )

        self._bar_sep()
        # Text labels, as they have always been -- the fix for the clipped
        # second row was the layout, not these buttons.
        for label, keycode in (
            ("Back", AndroidKey.BACK),
            ("Home", AndroidKey.HOME),
            ("Apps", AndroidKey.APP_SWITCH),
        ):
            action = QAction(label, self)
            action.setToolTip(f"Send the {label.lower()} key to the phone")
            action.triggered.connect(
                lambda _=False, k=keycode: self._send_key(k)
            )
            self._bar_add(action)

        rotate = QAction("Rotate", self)
        rotate.setToolTip("Rotate the phone screen")
        rotate.triggered.connect(self._rotate_device)
        self._bar_add(rotate)

        # Silences the device sound this app plays on the computer. A toggle
        # rather than a one-shot, and deliberately without a hotkey so it can
        # never collide with a game binding.
        self._mute_action = QAction("Mute", self)
        self._mute_action.setCheckable(True)
        self._mute_action.setChecked(self._muted)
        self._mute_action.setToolTip(
            "Play no device sound on this computer (audio keeps streaming)"
        )
        self._mute_action.toggled.connect(self._on_mute_toggled)
        self._bar_add(self._mute_action)

        self._bar_sep()

        # Toggleable latency HUD (hotkey F9).
        self._hud_action = QAction("Latency", self)
        self._hud_action.setCheckable(True)
        self._hud_action.setShortcut(QKeySequence(keynames.HUD_KEY_CODE))
        self._hud_action.setToolTip(
            "Show decode / arrival / paint timings over the video (hotkey: F9)"
        )
        self._hud_action.toggled.connect(self._on_hud_toggled)
        self._bar_add(self._hud_action)

        # Toggle the on-screen keybind/joystick/camera markers (hotkey F7).
        # Purely cosmetic: hiding them still injects touches and still lets you
        # edit them -- it just gives you a clean feed to watch or record.
        self._overlay_action = QAction("Markers", self)
        self._overlay_action.setCheckable(True)
        self._overlay_action.setChecked(True)
        self._overlay_action.setShortcut(QKeySequence(keynames.OVERLAY_KEY_CODE))
        self._overlay_action.setToolTip(
            "Show or hide the keybind markers, WASD pad and camera spot "
            "(hotkey: F7)"
        )
        self._overlay_action.toggled.connect(self._on_overlay_toggled)
        self._bar_add(self._overlay_action)

        # Fills the display with the phone image, letterboxed to its aspect.
        # F11 itself is registered as a window-level QShortcut in __init__;
        # keeping it on the action too would make the key ambiguous (and the
        # action's copy dies anyway once fullscreen hides the toolbar).
        self._fullscreen_action = QAction("Full", self)
        self._fullscreen_action.setToolTip(
            "Fit the phone screen to your display (hotkey: F11, Esc to exit)"
        )
        self._fullscreen_action.triggered.connect(self.toggle_fullscreen)
        self._bar_add(self._fullscreen_action)

        self._bar_sep()

        self._help_action = QAction("Help", self)
        self._help_action.setToolTip(
            "How to connect (including finding the device's IP address)"
        )
        self._help_action.triggered.connect(self.show_help)
        self._bar_add(self._help_action)


    def _build_keymap_dock(self) -> None:
        dock = QDockWidget("Keybinds", self)
        dock.setAllowedAreas(
            Qt.DockWidgetArea.RightDockWidgetArea | Qt.DockWidgetArea.LeftDockWidgetArea
        )
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(6, 6, 6, 6)

        self._pending_label = QLabel(
            "Press 'Add binding', record a key, then click the phone screen."
        )
        self._pending_label.setWordWrap(True)
        self._pending_label.setStyleSheet("color: #888;")
        layout.addWidget(self._pending_label)

        # Joystick pad size (a real dial: the pad radius as a % of the short side).
        # Only shown once a pad exists; dragging the pad's rim does the same.
        joy_row = QHBoxLayout()
        self._joy_size_label = QLabel("Joystick size:")
        joy_row.addWidget(self._joy_size_label)
        self._joy_size_slider = QSlider(Qt.Orientation.Horizontal)
        self._joy_size_slider.setRange(3, 45)  # 3%..45% of the short side
        self._joy_size_slider.setValue(int(round(0.12 * 100)))
        self._joy_size_slider.setToolTip(
            "How big the WASD pad is. You can also drag the pad's rim."
        )
        self._joy_size_slider.valueChanged.connect(self._on_joystick_slider)
        joy_row.addWidget(self._joy_size_slider, 1)
        layout.addLayout(joy_row)

        # Camera Movement: where the x-key look drag plants its finger.
        # Always present (default: the middle of the screen); Set position
        # re-places it, Lock freezes it against edits.
        cam_row = QHBoxLayout()
        self._cam_label = QLabel("Camera Movement: 50.0% / 50.0%")
        self._cam_label.setToolTip(
            "Where the camera drag (x-key mouse-look) plants its finger"
        )
        cam_row.addWidget(self._cam_label, 1)
        self._cam_set_btn = QPushButton("Set position")
        self._cam_set_btn.setToolTip(
            "Click, then click the screen where the game's camera area is"
        )
        self._cam_set_btn.clicked.connect(self._begin_set_camera)
        cam_row.addWidget(self._cam_set_btn)
        self._cam_lock_btn = QPushButton("Lock")
        self._cam_lock_btn.setCheckable(True)
        self._cam_lock_btn.setToolTip(
            "Freeze the camera spot so editing keybinds cannot nudge it"
        )
        self._cam_lock_btn.toggled.connect(self._on_camera_lock_toggled)
        cam_row.addWidget(self._cam_lock_btn)
        layout.addLayout(cam_row)

        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(["Key", "X %", "Y %", "Mode"])
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.itemDoubleClicked.connect(self._on_table_double_clicked)
        layout.addWidget(self._table)

        buttons = QHBoxLayout()
        add_btn = QPushButton("Add binding")
        add_btn.clicked.connect(self._begin_add_binding)
        buttons.addWidget(add_btn)

        toggle_btn = QPushButton("Toggle tap/hold")
        toggle_btn.clicked.connect(self._toggle_selected_mode)
        buttons.addWidget(toggle_btn)

        remove_btn = QPushButton("Remove")
        remove_btn.clicked.connect(self._remove_selected_binding)
        buttons.addWidget(remove_btn)
        layout.addLayout(buttons)

        profiles = QHBoxLayout()
        self._profile_combo = QComboBox()
        self._profile_combo.setEditable(True)
        profiles.addWidget(self._profile_combo, 1)
        load_btn = QPushButton("Load")
        load_btn.clicked.connect(self._load_profile)
        profiles.addWidget(load_btn)
        save_btn = QPushButton("Save")
        save_btn.clicked.connect(self._save_profile)
        profiles.addWidget(save_btn)
        layout.addLayout(profiles)

        clear_btn = QPushButton("Clear all bindings")
        clear_btn.clicked.connect(self._clear_bindings)
        layout.addWidget(clear_btn)

        dock.setWidget(panel)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        self._dock = dock

    def _build_statusbar(self) -> None:
        self._status = QLabel("Not connected")
        #: Transport in use -- USB or Wi-Fi. Wi-Fi is the usual culprit when
        #: the mirror feels laggy, so it gets a permanent, visible readout.
        self._link_label = QLabel("link: -")
        self._link_label.setToolTip(
            "How the device is attached. USB is the lowest-latency option; "
            "Wi-Fi adds input lag."
        )
        self._input_label = QLabel("input: mouse")
        self._input_label.setToolTip("Press ` to switch between mouse and keybinds")
        self._lock_label = _ClickableLabel("phone: -")
        self._lock_label.setToolTip(
            "Lockscreen state, checked against the device every couple of "
            "seconds. Blank until connected. Click to open the unlock panel."
        )
        self._lock_label.clicked.connect(self._on_lock_label_clicked)
        self._render_label = QLabel("render: -")
        self._fps_label = QLabel("fps: -")
        #: Whether the Rich Presence is genuinely published. A lit Discord
        #: button only means "switched on" -- and if another tool shares the
        #: application id, this is the only readout that says which side is
        #: actually writing.
        self._presence_label = QLabel("presence: -")
        self._presence_label.setToolTip(
            "Discord Rich Presence state. 'connected' means this app is "
            "publishing. If two tools use the same Discord application id "
            "they overwrite each other and only the last writer wins -- give "
            "each one its own id in the developer portal."
        )
        #: Previous lock reading, so a flip to locked announces itself once.
        self._was_locked: bool | None = None
        #: Unlock panel, built the first time the phone actually locks.
        self._lock_dialog: LockDialog | None = None

        # The tablet's own media volume. It lives in the status bar rather than
        # the toolbar on purpose. The toolbar has an overflow contract: anything
        # that does not fit slides into the » menu and stays reachable, and
        # _rebuild_overflow() budgets the row accordingly. A slider has no menu
        # form, so putting one there breaks that contract whichever way it is
        # done -- mark it tracked and it disappears with no way back, leave it
        # untracked and it silently eats the width the arithmetic had set aside
        # for buttons, which Qt then draws straight over (measured: the row
        # overlapped itself at a 1200px window). The status bar has room and no
        # such contract: _status holds the stretch and elides when it runs out.
        # _ClickToPositionSlider, not a bare QSlider: a bare one answers a
        # click on the groove with a ten-step page jump, not with the position.
        self._vol_slider = _ClickToPositionSlider(Qt.Orientation.Horizontal)
        # Deliberately not "vol:" -- there is already a Mute button that
        # silences *this computer*, and the two are easy to confuse.
        self._vol_slider.setRange(0, 15)  # replaced by the device's own range
        self._vol_slider.setFixedWidth(96)
        self._vol_slider.setEnabled(False)
        self._vol_slider.setToolTip("Tablet media volume (read on connect)")
        self._vol_slider.valueChanged.connect(self._on_volume_slider)

        # Fixed width so the row cannot shuffle as the digits change.
        self._vol_readout = QLabel("-")
        self._vol_readout.setFixedWidth(40)
        self._vol_readout.setToolTip("Tablet media volume")

        self._vol_label = QLabel("tablet vol:")
        self._vol_label.setToolTip(
            "The tablet's media volume. Not the same as Mute, which only "
            "silences this computer."
        )

        vol_box = QWidget()
        vol_row = QHBoxLayout(vol_box)
        vol_row.setContentsMargins(0, 0, 8, 0)
        vol_row.setSpacing(4)
        vol_row.addWidget(self._vol_label)

        # Volume up/down, spelled out as buttons and flanking the slider.
        #
        # These inject the real KEYCODE_VOLUME_UP / VOLUME_DOWN over the scrcpy
        # control socket, which is exactly what the tablet's own volume keys
        # send: instant, no adb, and it makes the tablet raise its own volume
        # panel -- so the change is visible on the device, not just in this
        # readout. They also keep working when `dumpsys audio` cannot be read
        # at all, which is why they are enabled by *being connected* rather
        # than by having a level.
        #
        # The slider is the other kind of control: absolute, and a slower adb
        # round trip. Two different tools, both wanted.
        self._vol_minus = QToolButton()
        self._vol_minus.setText("\u2212")  # a real minus, not a hyphen
        self._vol_minus.setToolTip(
            "Volume down one step, like the tablet's own volume key"
        )
        self._vol_minus.clicked.connect(lambda _=False: self._step_volume(-1))
        self._vol_plus = QToolButton()
        self._vol_plus.setText("+")
        self._vol_plus.setToolTip(
            "Volume up one step, like the tablet's own volume key"
        )
        self._vol_plus.clicked.connect(lambda _=False: self._step_volume(1))
        for button in (self._vol_minus, self._vol_plus):
            button.setAutoRaise(True)
            button.setFixedWidth(22)
            button.setEnabled(False)  # until something is actually connected

        vol_row.addWidget(self._vol_minus)
        vol_row.addWidget(self._vol_slider)
        vol_row.addWidget(self._vol_plus)
        vol_row.addWidget(self._vol_readout)

        bar = self.statusBar()
        bar.addWidget(self._status, 1)
        bar.addPermanentWidget(vol_box)
        bar.addPermanentWidget(self._link_label)
        bar.addPermanentWidget(self._input_label)
        bar.addPermanentWidget(self._lock_label)
        bar.addPermanentWidget(self._render_label)
        bar.addPermanentWidget(self._presence_label)
        bar.addPermanentWidget(self._fps_label)

        self._fps_timer = QTimer(self)
        self._fps_timer.setInterval(1000)
        self._fps_timer.timeout.connect(self._update_fps)
        self._fps_timer.start()

        # Discord presence follows the app on the tablet, so it is refreshed
        # on its own slow timer rather than every frame.
        self._discord_timer = QTimer(self)
        self._discord_timer.setInterval(DISCORD_POLL_MS)
        self._discord_timer.timeout.connect(self._on_discord_tick)

        # Brings the slider back in line once the volume changes stop, for both
        # paths: neither the slider nor the up/down buttons get a reply from a
        # keycode, so how far the tablet actually moved is only known by asking.
        self._vol_sync_timer = QTimer(self)
        self._vol_sync_timer.setSingleShot(True)
        self._vol_sync_timer.setInterval(VOLUME_SYNC_MS)
        self._vol_sync_timer.timeout.connect(self._refresh_volume)

    # ------------------------------------------------------------------ #
    # Devices + connection
    # ------------------------------------------------------------------ #

    def show_help(self) -> None:
        """Pop up the setup guide (mainly: how to go wireless)."""
        box = QMessageBox(self)
        box.setWindowTitle("DroidMirror help")
        box.setIcon(QMessageBox.Icon.Information)
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(HELP_TEXT)
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.exec()

    def _sync_filter_label(self) -> None:
        """Light up whichever of the two buttons is the current mode."""
        wireless = self._wireless_mode
        self._filter_action.setText("Wi-Fi")
        self._filter_action.blockSignals(True)
        self._filter_action.setChecked(wireless)
        self._filter_action.blockSignals(False)
        self._usb_action.setChecked(not wireless)
        # Add is Wi-Fi only, and greys itself out otherwise. Over USB a device
        # appears the moment it is plugged in, so there is nothing to add by
        # hand; a disabled QAction is drawn faint by Qt, which is the look a
        # button that cannot do anything here should have.
        self._add_device_action.setEnabled(wireless and self._adb is not None)
        if wireless:
            self._filter_action.setToolTip(
                "Connected over Wi-Fi - click to look for the device again"
            )
        else:
            self._filter_action.setToolTip(
                "Connect over Wi-Fi - finds the device on the same network "
                "automatically"
            )

    def _on_usb_clicked(self, _checked: bool = False) -> None:
        """USB button: show cable devices (a lit button stays lit)."""
        self._usb_action.setChecked(True)
        self._set_wireless_mode(False)

    def _on_filter_clicked(self, _checked: bool = False) -> None:
        """Wi-Fi button: find the device on the network and connect.

        Always means the same thing, whether or not the button is already lit:
        go and find the device over Wi-Fi.

        The button's check mark is a *display* of ``_wireless_mode``, re-synced
        from it here -- never read back as the mode itself. Driving the mode
        off the QAction's checked state is what wedged this window: trigger()
        on a lit checkable action emits ``toggled(False)`` *before*
        ``triggered(False)``, so the click silently switched the mode back to
        USB and emptied the list. The later ``setChecked(True)`` could not undo
        that, because the action was already checked and therefore emitted
        nothing -- leaving both buttons lit, no devices listed, Connect
        disabled, and no way back except restarting.
        """
        if self._adb is None:
            # No adb: nothing to connect to, so this is just a filter change.
            self._set_wireless_mode(True)
            return
        # While the search runs, fall back to showing what is cabled. A failed
        # or cancelled search lands on USB by way of that, and a successful one
        # flips back in ready().
        self._set_wireless_mode(False)
        self._connect_wireless()

    def _on_filter_toggled(self, wireless: bool) -> None:
        """The action's check state changed: adopt it as the mode."""
        self._set_wireless_mode(wireless)

    def _set_wireless_mode(self, wireless: bool) -> None:
        """Switch the transport filter. ``_wireless_mode`` is the source of truth.

        Sets the mode directly instead of routing it through
        ``QAction.setChecked`` -> ``toggled`` -> back again. That round trip only
        works while the action's checked state actually changes: ``setChecked``
        on an already-checked action emits nothing, so a state that had drifted
        out of sync with the button could never be written back through it.
        """
        self._wireless_mode = bool(wireless)
        self._sync_filter_label()
        # Remember the choice so the next launch opens the same way.
        self._settings.set("device_filter", "wireless" if self._wireless_mode else "usb")
        self._refresh_devices()

    def _initial_filter(self) -> None:
        """Restore the remembered filter, else pick what is attached."""
        remembered = self._settings.get("device_filter")
        if remembered in ("usb", "wireless"):
            wireless = remembered == "wireless"
            self._wireless_mode = wireless
            self._filter_action.blockSignals(True)
            self._filter_action.setChecked(wireless)
            self._filter_action.blockSignals(False)
            self._sync_filter_label()
            return

        # No preference yet: start on whichever transport is present
        # (USB wins ties, since it is the lowest-latency option).
        ready = self._ready_devices()
        if ready and all(d.is_wireless for d in ready):
            self._wireless_mode = True
            self._filter_action.blockSignals(True)
            self._filter_action.setChecked(True)
            self._filter_action.blockSignals(False)
            self._sync_filter_label()

    def _refresh_devices(self) -> None:
        self._device_combo.clear()
        if self._adb is None:
            self._device_combo.addItem("adb not found", None)
            self._connect_action.setEnabled(False)
            self._status.setText(
                "adb not found - install Android platform-tools or set ANDROID_HOME"
            )
            return
        try:
            # Short timeout on purpose: this runs on the GUI thread, and adb
            # blocks for its full timeout when the daemon is wedged. Failing
            # fast and showing "error" beats freezing the window for 30s.
            devices = dedupe_wireless(self._adb.devices(timeout=DEVICE_LIST_TIMEOUT_S))
        except AdbError as exc:
            self._device_combo.addItem("error", None)
            self._status.setText(str(exc))
            self._connect_action.setEnabled(False)
            return

        # Only the selected transport is listed: USB mode never shows network
        # devices and vice versa.
        ready = [
            d for d in devices
            if d.is_ready and d.is_wireless == self._wireless_mode
        ]
        for device in ready:
            self._device_combo.addItem(device.display_name, device)
        unauthorized = [
            d for d in devices
            if d.is_unauthorized and d.is_wireless == self._wireless_mode
        ]
        for device in unauthorized:
            self._device_combo.addItem(
                f"{device.serial} - UNAUTHORIZED (accept the prompt)", None
            )
        if not ready and not unauthorized:
            kind = "wireless" if self._wireless_mode else "USB"
            self._device_combo.addItem(f"no {kind} device connected", None)
        self._connect_action.setEnabled(bool(ready))

    def _selected_device(self) -> Device | None:
        return self._device_combo.currentData()

    def _update_link(self) -> None:
        """Show how the selected device is attached: USB or Wi-Fi.

        Wi-Fi is the usual cause of a mirror that feels laggy, so this stays
        visible in the status bar rather than hiding behind a dialog.
        """
        device = self._selected_device()
        wireless = getattr(device, "is_wireless", None)
        if wireless is None:  # nothing selected, or a placeholder entry
            self._link_label.setText("link: -")
            return
        if wireless:
            self._link_label.setText(f"link: Wi-Fi ({device.serial})")
            self._link_label.setToolTip(
                "Connected over the network. Wi-Fi adds input lag - use the "
                "USB cable for the lowest delay."
            )
        else:
            self._link_label.setText("link: USB")
            self._link_label.setToolTip(
                "Connected over the USB cable - the lowest-latency option."
            )

    def _toggle_connection(self) -> None:
        if self._connected:
            self._disconnect()
        else:
            self._connect()

    def _ready_devices(self) -> list[Device]:
        if self._adb is None:
            return []
        try:
            return [d for d in self._adb.devices() if d.is_ready]
        except AdbError:
            return []

    def _select_serial(self, serial: str) -> None:
        """Refresh the list and make ``serial`` the selected device."""
        self._refresh_devices()
        for index in range(self._device_combo.count()):
            data = self._device_combo.itemData(index)
            if getattr(data, "serial", None) == serial:
                self._device_combo.setCurrentIndex(index)
                return

    def _ask_wifi_address(self, prefill: str, history: list[str]) -> str | None:
        """Open the address prompt; None means cancelled (or empty).

        The dialog slides the history open as a dropdown: picking an entry
        fills the field, hitting Connect dials whatever is in it.
        """
        self._wifi_pairing = None
        try:
            dialog = WifiConnectDialog(self, prefill=prefill, history=history)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return None
            # Optional Android 11+ pairing details, picked up by stage 2.
            self._wifi_pairing = dialog.pairing()
            return dialog.address() or None
        except Exception as exc:  # a broken prompt must not take the app down
            _log_flow_error("wireless prompt", exc)
            QMessageBox.critical(
                self,
                "Wireless",
                f"The address prompt failed:\n{exc}\n\n"
                "The details were saved to crash.log.",
            )
            return None

    def _run_worker(
        self, work: Callable[[], Any], on_done: Callable[[Any], None]
    ) -> None:
        """Run ``work`` (blocking adb calls) off the GUI thread.

        adb can stall for tens of seconds against a sleeping host -- running
        that inline froze the whole window until Windows killed us as an
        ``AppHang``. The result comes back through a queued signal, so
        ``on_done`` runs on the GUI thread where widgets may be touched.
        ``_sync_workers`` makes tests (and the smoke scripts) run inline.
        """
        if self._sync_workers:
            try:
                result = work()
            except Exception as exc:  # same contract as the threaded path
                result = exc
            on_done(result)
            return

        # The bridge and thread are parked on the window: a throwaway bridge
        # would be garbage collected the moment this returns, taking the
        # queued result (and the sender it is emitted from) with it. Each
        # worker binds its own local bridge so concurrent workers (e.g. volume
        # polling overlapping with wireless or connect) never overwrite each
        # other's callback target.
        bridge = _TaskBridge()
        self._task_bridge = bridge
        self._active_workers.add(bridge)

        def finish(result: Any) -> None:
            self._active_workers.discard(bridge)
            on_done(result)

        bridge.done.connect(finish)

        def runner() -> None:
            try:
                result = work()
            except Exception as exc:  # never let a worker die silently
                result = exc
            bridge.done.emit(result)

        thread = threading.Thread(
            target=runner, name="droidmirror-adb", daemon=True
        )
        self._task_thread = thread
        thread.start()

    def _wireless_stage1(self, history: list[str]) -> Any:
        """Worker: find the device on Wi-Fi without asking, if at all possible.

        In order: a device already connected over the network; one found on
        the same network through mDNS (Android 11+ Wireless debugging, or a
        device left in ``adb tcpip`` mode); an attached USB device switched to
        Wi-Fi. Returns ``(prefill, had_usb, connected_serial)`` -- the serial
        is None when the user has to be asked (e.g. first-time pairing), and
        ``prefill`` is then the best address to offer.
        """
        for device in dedupe_wireless(self._ready_devices()):
            if device.is_wireless:
                return device.serial, False, device.serial

        seen = None
        discover = getattr(self._adb, "discover_wireless", None)
        if discover is not None:
            serial, seen = discover()
            if serial:
                return serial, False, serial

        usb = [d for d in self._ready_devices() if not looks_wireless(d.serial)]
        if usb:
            # enable_wireless only returns once the address is connected.
            address = self._adb.enable_wireless() or ""
            if address:
                return address, True, address

        prefill = seen or (history[0] if history else "")
        return prefill, bool(usb), None

    def _wireless_stage2(
        self, address: str, pairing: tuple[str, str] | None = None
    ) -> Any:
        """Worker: (optionally pair, then) dial the address and confirm it.

        Returns ``None`` on success or a failure message string -- adb's own
        errors are turned into text so the GUI thread only ever shows a box.
        """
        if pairing:
            pair_addr, code = pairing
            if not pair_addr:
                # The open "pair with code" popup advertises itself on mDNS.
                finder = getattr(self._adb, "find_pairing_address", None)
                pair_addr = (finder() if finder else None) or ""
            pair_addr = normalize_address(pair_addr, default_port=0) or ""
            if not pair_addr or pair_addr.endswith(":0"):
                return (
                    "Could not find the pairing popup on the network.\n\n"
                    "On the tablet, open Wireless debugging > 'Pair device "
                    "with pairing code', keep it open, and try again (or type "
                    "the IP:port shown in the popup)."
                )
            try:
                self._adb.pair(pair_addr, code, timeout=WIRELESS_CONNECT_TIMEOUT * 2)
            except (AdbError, OSError) as exc:
                return str(exc)
        try:
            said = self._adb.connect(address, timeout=WIRELESS_CONNECT_TIMEOUT)
        except (AdbError, OSError) as exc:
            return str(exc)
        if not self._adb.is_connected(address):
            return (
                f"Could not connect to {address}.\n"
                f"adb said: {said or 'no output'}\n\n"
                "If the port came from Developer options > Wireless debugging "
                "(Android 11+), this computer must be paired first: tap 'Pair "
                "device with pairing code' on the tablet, then click Wireless "
                "again and fill in the pairing IP:port and code.\n\n"
                "Otherwise check that:\n"
                "- the tablet/phone is on the same Wi-Fi as this computer,\n"
                "- it is listening for adb over TCP - plug in USB once and "
                "click this button again (it switches the device "
                "automatically), or run 'adb tcpip 5555' while connected."
            )
        return None

    def _wireless_ready(self, address: str, had_usb: bool, history: list[str]) -> None:
        """A wireless device is connected: remember it, show it, select it.

        Shared by both ways in -- the automatic search and the Add button --
        since either can be the one that dials a new address, and both should
        leave the same trail behind.
        """
        # Worked: put it at the head so the next prompt opens on it.
        if ":" in address and "_adb-tls" not in address:
            self._settings.set("wireless_ips", remember_ip(history, address))
        # The list is filtered by transport: flip to wireless so the address we
        # just connected is visible (and selected).
        self._set_wireless_mode(True)
        self._select_serial(address)
        suffix = " - you can unplug USB now" if had_usb else ""
        self._status.setText(f"Wireless device ready: {address}{suffix}")

    def _add_wireless_device(self) -> None:
        """Add a device that is not the one already listed.

        The Wi-Fi button only ever finds the device this app already knows
        about, so there was no way to bring in a *second* tablet or phone -- or
        the same one on a changed address. This reaches the same pair-and-dial
        worker with no search in front of it.
        """
        # Also checks the mode, not just the button's enabled state: a disabled
        # QAction still fires triggered() if something calls trigger() on it,
        # and "Add" only means anything over Wi-Fi.
        if self._adb is None or self._wireless_busy or not self._wireless_mode:
            return
        self._wireless_busy = True
        history = self._settings.get("wireless_ips")
        if not isinstance(history, list):
            history = []
        self._status.setText("Add a device over Wi-Fi...")
        self._dial_wireless_address(
            "", history, had_usb=False, restore_usb_on_failure=False
        )

    def _dial_wireless_address(
        self,
        prefill: str,
        history: list[str],
        had_usb: bool,
        restore_usb_on_failure: bool,
    ) -> None:
        """Ask for an address, then pair and connect it on a worker.

        Assumes the caller holds ``_wireless_busy``; every exit releases it.
        ``restore_usb_on_failure`` is False for Add, which was entered from
        Wi-Fi mode and must leave the transport buttons as it found them. The
        automatic search needs it True: it has already flipped the list to USB
        while it looked, and a cancelled or failed prompt has to put that back.
        """
        def failed(message: str) -> None:
            self._wireless_busy = False
            self._status.setText(message)
            if restore_usb_on_failure:
                self._restore_usb_after_failed_search()

        self._wifi_pairing = None
        text = self._ask_wifi_address(prefill, history)
        pairing = self._wifi_pairing
        if not text:  # cancelled: nothing to do, just stand down
            failed("Wireless setup cancelled")
            return
        address = normalize_address(text)
        if address is None:
            self._wireless_busy = False
            QMessageBox.warning(
                self, "Wireless", "That does not look like an IP address."
            )
            if restore_usb_on_failure:
                self._restore_usb_after_failed_search()
            return
        self._status.setText(f"Connecting to {address}...")

        def after_stage2(stage2: Any) -> None:
            self._wireless_busy = False
            if isinstance(stage2, Exception):
                _log_flow_error("wireless connect", stage2)
                QMessageBox.critical(self, "Wireless", str(stage2))
                self._status.setText("Wireless connection failed")
                if restore_usb_on_failure:
                    self._restore_usb_after_failed_search()
                return
            if stage2:  # a failure message from the worker
                QMessageBox.critical(self, "Wireless", stage2)
                self._status.setText("Wireless connection failed")
                if restore_usb_on_failure:
                    self._restore_usb_after_failed_search()
                return
            self._wireless_ready(address, had_usb, history)

        self._run_worker(
            lambda: self._wireless_stage2(address, pairing), after_stage2
        )

    def _connect_wireless(self) -> None:
        """Switch the device (and the list) over to Wi-Fi, without freezing.

        The flow is staged around a worker thread so no adb call ever runs on
        the GUI thread: (1) find the device by itself (already connected,
        mDNS on the same network, or tcpip an attached USB device); only if
        that fails (2) ask for the address / pairing code on the GUI thread
        and (3) pair + dial it on the worker. Then flip the list. Any failure
        is reported and logged, never fatal.
        """
        if self._adb is None or self._wireless_busy:
            return
        self._wireless_busy = True
        self._status.setText("Looking for the device on Wi-Fi...")
        history = self._settings.get("wireless_ips")
        if not isinstance(history, list):
            history = []

        def after_stage1(result: Any) -> None:
            if isinstance(result, Exception):
                self._wireless_busy = False
                _log_flow_error("wireless tcpip", result)
                QMessageBox.critical(self, "Wireless", str(result))
                self._status.setText("Wireless setup failed")
                self._restore_usb_after_failed_search()
                return
            prefill, had_usb, connected = (tuple(result) + (None,))[:3]
            if connected:  # found and connected without asking anything
                self._wireless_busy = False
                self._wireless_ready(connected, had_usb, history)
                return
            self._status.setText("Device not found automatically - asking...")
            self._dial_wireless_address(
                prefill, history, had_usb, restore_usb_on_failure=True
            )

        self._run_worker(lambda: self._wireless_stage1(history), after_stage1)

    def _restore_usb_after_failed_search(self) -> None:
        """Put both transport buttons back to a clean, non-blue USB state.

        A search that fails or is cancelled never went wireless, so the
        Wi-Fi button must not stay lit -- that is what made the toolbar look
        half-selected (the "blue USB" confusion). One call on every terminal
        path keeps the two buttons mutually exclusive.
        """
        self._filter_action.blockSignals(True)
        self._filter_action.setChecked(False)
        self._filter_action.blockSignals(False)
        self._wireless_mode = False
        self._sync_filter_label()
        self._refresh_devices()

    # Discord Rich Presence
    # ------------------------------------------------------------------ #

    def _on_discord_toggled(self, on: bool) -> None:
        """Turn the Rich Presence on/off, asking for an app id if needed."""
        self._settings.set("discord", bool(on))
        if not on:
            self._discord.clear()
            self._discord_timer.stop()
            self._set_foreground_polling(False)
            self._status.setText("Discord presence off")
            return
        self._connect_discord()

    def _connect_discord(self) -> None:
        """Connect the presence, prompting for whatever is missing."""
        client_id = (self._settings.get("discord_app_id") or "").strip()
        if not client_id:
            client_id = self._ask_discord_app_id()
            if not client_id:
                # Cancelled: put the button back where it was.
                self._discord_action.setChecked(False)
                return
        if not self._discord.configure(client_id, enabled=True):
            self._discord_action.setChecked(False)
            QMessageBox.warning(
                self,
                "Discord",
                "Could not reach Discord.\n\n"
                "Start Discord (it must be running) and check the "
                "application id is correct.",
            )
            return
        self._push_discord(force=True)
        self._set_foreground_polling(True)
        # Start (or restart) the refresh here, not only inside _connect():
        # Discord can be switched on while a session is already running, and
        # without this the presence stayed frozen on whatever was first sent
        # -- it never noticed the app on the tablet changing.
        self._discord_timer.start()
        # No token is requested any more. Icons are fetched online and handed
        # to Discord as a url, which Discord then fetches itself, so there is
        # nothing to upload -- and no Discord login credential has to be
        # written to disk for a cosmetic feature.
        self._status.setText("Discord presence on")

    def _on_icon_ready(self, _package: str = "") -> None:
        """An icon finished uploading: refresh the presence with it.

        No flag clearing here -- ``_resolve_icon`` owns that in its finally,
        and clearing it from this side as well would let a slow second upload
        be cancelled out early by a fast first one.
        """
        self._push_discord(force=True)

    def _set_discord_app_id(self) -> None:
        """Replace the Discord application id, and reconnect to the new one.

        Reachable only from here: the id is asked exactly once, on first use,
        and never again -- so a wrong id (or one shared with another tool,
        which makes both presences overwrite each other) used to be permanent
        short of hand-editing settings.json.
        """
        current = (self._settings.get("discord_app_id") or "").strip()
        client_id = self._ask_discord_app_id(current)
        if not client_id:
            self._status.setText("Discord application id unchanged")
            return

        self._settings.set("discord_app_id", client_id)
        # Drop the old socket before opening the new one, or the previous
        # application's presence would linger until it timed out.
        self._discord.clear()
        if not self._discord.configure(client_id, enabled=True):
            QMessageBox.warning(
                self,
                "Discord",
                "Could not connect with that application id.\n\n"
                "Start Discord (it must be running), then try again. The id "
                "has been saved, so the presence will pick it up whenever "
                "Discord is available.",
            )
            self._sync_discord_state()
            return
        self._discord_action.setChecked(True)
        self._set_foreground_polling(True)
        self._discord_timer.start()
        self._push_discord(force=True)
        self._sync_discord_state()
        self._status.setText(f"Discord presence on (application {client_id})")

    def _ask_discord_app_id(self, prefill: str = "") -> str:
        """Prompt for the Discord application id (created in the portal)."""
        text, ok = QInputDialog.getText(
            self,
            "Discord application id",
            "Paste your Discord Application ID\n\n"
            "Create one at discord.com/developers/applications\n"
            "(General Information > Application ID).",
            text=prefill,
        )
        if not ok:
            return ""
        client_id = text.strip()
        if not client_id.isdigit():
            QMessageBox.warning(
                self, "Discord", "An application id is a long number."
            )
            return ""
        self._settings.set("discord_app_id", client_id)
        return client_id

    def _set_foreground_polling(self, on: bool) -> None:
        """Tell the live session whether anything reads ``foreground_app``.

        Only Discord does. The probe behind it is a ``dumpsys window`` every
        ~8s, so it stays switched off for everyone who never turns the button
        on. Also cleared on disconnect so a stale session is not left polling.
        """
        session = self._session
        if session is not None:
            session.foreground_polling = bool(on)

    def _push_discord(self, force: bool = False) -> None:
        """Send the current app to Discord, if presence is connected."""
        if not self._discord.connected:
            return
        session = self._session
        package = getattr(session, "foreground_app", None) if session else None
        # A Unix timestamp, or nothing -- Discord draws the elapsed time from
        # it and a monotonic clock renders as a nonsense duration.
        start = getattr(session, "started_at", None) or None if session else None
        # The home screen is not an app, so there is no store page to look up:
        # its picture is a fixed url that the presence module supplies itself.
        # An app gets its real icon fetched online instead.
        image = (
            self._icon_url_for(package)
            if package and not is_home(package)
            else None
        )
        self._discord.update_app(
            package, state="DroidMirror", start=start, image=image,
            force=force,
        )

    def _start_icon_resolve(self, package: str) -> None:
        """Kick off an icon resolve for ``package``, unless one is in flight.

        Never blocks. Returns immediately; the presence goes out without a
        picture and :meth:`_on_icon_ready` sends a follow-up when the url
        lands, a moment later.
        """
        if package in self._icon_resolving:
            return  # already being looked up for this package
        if not package or is_home(package):
            return  # not an app, so there is no store page to look up
        self._icon_resolving.add(package)
        threading.Thread(
            target=self._resolve_icon,
            args=(package,),
            name=f"droidmirror-icon-{package}",
            daemon=True,
        ).start()

    def _icon_url_for(self, package: str) -> str | None:
        """The online url for this app's icon, once it has been resolved.

        A url and nothing else, because that is the only thing Discord will
        draw -- see ``discord_rpc.is_image_reference``, which is handed
        whatever this returns.

        A cached value that is *not* a url is therefore treated as a miss
        rather than served: it cannot render, so it is not a finished answer.
        That is what retires the asset ids left in the cache by the previous
        scheme, which the client silently dropped and which would otherwise
        sit there unpaintable for the life of the install.

        Never blocks and never raises: a miss starts a background resolve and
        returns None, so this tick simply goes out without an image.
        """
        cached = self._icon_cache.get(package)
        if cached and is_image_reference(cached):
            return cached
        self._start_icon_resolve(package)
        return None

    def _resolve_icon(self, package: str) -> None:
        """Look this app's icon up online and remember the url.

        Runs on a worker: the Play store page is a ~1 MB bundle and the
        favicon fallback is another round trip, so the presence goes out
        without a picture first and picks one up a moment later.

        Nothing is downloaded, uploaded or read off the device. Discord
        fetches the image from the url itself when it draws the card, which is
        the only reason a local icon can never work: a url is what Discord
        accepts, and a picture that only exists on this machine has none.

        The whole body is wrapped so ``_icon_resolving`` is always cleared.
        It used to be cleared at two hand-placed points instead, and one
        failure left the flag set for the rest of the session -- after which,
        since every caller bails out while it is set, no icon of any kind
        could resolve again.
        """
        try:
            url = icon_url_for_package(package)
            if url:
                self._icon_cache.put(package, url, source="web")
                self._icon_bridge.discord_icon.emit(package)
        finally:
            self._icon_resolving.discard(package)

    def _sync_discord_state(self) -> None:
        """Show whether the presence is actually live, not merely switched on.

        The Discord button is checkable and remembers its setting, so a lit
        button used to look identical to a working connection. When something
        else writes to the same application id -- Discord keys presence by
        application, not by client, so two tools sharing one id overwrite each
        other -- this is the only way to tell which side is publishing.
        """
        state = self._discord.status
        self._discord_action.setToolTip(
            "Show the app you are using on the tablet in Discord "
            "(needs a Discord application id)\n\n"
            f"Status: {state}"
        )
        label = self._presence_label
        if label is not None:
            label.setText(f"presence: {state}")
            label.setEnabled(state == "connected")

    def _on_discord_tick(self) -> None:
        # Cheap: reads the value the session's watcher already polled, so no
        # adb call happens on this timer.
        self._push_discord()
        self._sync_discord_state()

    def _first_usb_device(self) -> Device | None:
        for device in self._ready_devices():
            if not device.is_wireless:
                return device
        return None

    def _wireless_choice(self, device: Device) -> str:
        """Ask what to do about a wireless connect: 'wifi', 'usb' or 'cancel'.

        Wi-Fi adds input lag, so this is the moment to offer the cable while a
        USB device is still attached. Kept separate from the dialog-free flow
        so tests can drive each branch.
        """
        usb = self._first_usb_device()
        box = QMessageBox(self)
        box.setWindowTitle("Connect over Wi-Fi?")
        box.setIcon(QMessageBox.Icon.Warning)
        lines = [f"{device.serial} is a wireless link, which adds input lag."]
        if usb is not None:
            lines.append(
                f"A device is also attached by USB ({usb.serial}). "
                "Use the cable for the lowest delay?"
            )
        else:
            lines.append(
                "For the lowest delay, plug in the USB cable and reconnect."
            )
        box.setText("\n\n".join(lines))
        wifi_btn = box.addButton(
            "Connect over Wi-Fi", QMessageBox.ButtonRole.AcceptRole
        )
        usb_btn = None
        if usb is not None:
            usb_btn = box.addButton(
                "Use USB instead", QMessageBox.ButtonRole.AcceptRole
            )
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(wifi_btn)
        box.exec()
        clicked = box.clickedButton()
        if usb_btn is not None and clicked is usb_btn:
            return "usb"
        if clicked is wifi_btn:
            return "wifi"
        return "cancel"

    def _connect(self) -> None:
        device = self._selected_device()
        if self._adb is None or device is None:
            return
        if getattr(device, "is_wireless", False) and self._first_usb_device():
            # A cable is attached too: offer it before committing to Wi-Fi.
            choice = self._wireless_choice(device)
            if choice == "cancel":
                self._status.setText("Connection cancelled")
                return
            if choice == "usb":
                usb = self._first_usb_device()
                if usb is None:
                    self._status.setText("No USB device attached")
                    return
                self._set_wireless_mode(False)
                self._select_serial(usb.serial)
                device = usb
        self._status.setText("Connecting...")
        options = self._options
        if getattr(device, "is_wireless", False):
            # USB defaults (native res @ 16 Mbps) choke a Wi-Fi link.
            options = wireless_options(options)
        self._session = MirrorSession(
            self._adb,
            options=options,
            on_frame=self._on_frame_threadsafe,
            on_status=lambda s: self._bridge.status.emit(s.message),
            on_device_name=self._bridge.device_name.emit,
            on_closed=self._bridge.closed.emit,
            hw_decode=self._hw_decode,
        )
        self._session.set_muted(self._muted)  # carry the toolbar's toggle over
        # A fresh session starts with the foreground poll off; only switch it
        # on if Discord is live, or it would poll with nothing reading it.
        self._session.foreground_polling = self._discord.connected
        try:
            self._session.start(device=device)
        except (AdbError, RuntimeError, OSError) as exc:
            self._session = None
            QMessageBox.critical(self, "Connection failed", str(exc))
            self._status.setText("Connection failed")
            return
        self._connected = True
        self._connect_action.setText("Disconnect")
        self._device_combo.setEnabled(False)
        self._update_link()
        # Read the tablet's media volume once, now that there is a device to
        # ask. Until this lands the slider stays disabled, so it can never be
        # dragged against a level nobody has read yet. The up/down buttons need
        # no such reading -- they are keycodes -- so they open with the session.
        self._vol_minus.setEnabled(True)
        self._vol_plus.setEnabled(True)
        self._refresh_volume()
        if self._discord.connected:
            # Straight away, so the presence is not blank until the first tick.
            self._push_discord(force=True)
        elif self._discord_action.isChecked():
            # Enabled last launch but never connected (Discord was closed, or
            # the id was only just saved): bring it up now.
            self._connect_discord()

    def _disconnect(self) -> None:
        self._discord_timer.stop()
        self._discord.clear()
        # Stop a pending volume read first: it is scheduled, so one could still
        # be in flight with no device left to read from -- and a reading applies
        # itself to the slider, and can write, when it lands.
        self._vol_sync_timer.stop()
        self._vol_minus.setEnabled(False)
        self._vol_plus.setEnabled(False)
        self._set_volume_unknown("Tablet volume (connect to read it)")
        self._engine.release_all()
        self._set_mouse_lock(False)
        if self._session is not None:
            self._session.stop()
            self._session = None
        self._connected = False
        self._last_frame = None
        self._view.clear()
        self._connect_action.setText("Connect")
        self._device_combo.setEnabled(True)
        self._update_link()
        self._status.setText("Disconnected")

    # -- callbacks from the reader thread -------------------------------- #
    # These are handed to MirrorSession and run off the GUI thread, so they
    # only record state / hand off to Qt; they never touch widgets.

    def _on_frame_threadsafe(self, frame: DecodedFrame) -> None:
        # Plain attribute assignment; the GUI thread samples it on a timer. We
        # deliberately do not signal per frame -- that could queue stale frames.
        self._last_frame = frame

    # -- GUI-thread slots ------------------------------------------------- #

    def _poll_frame(self) -> None:
        frame = self._last_frame
        if frame is not None and frame is not self._view.current_frame:
            self._view.set_frame(frame)
        if self._session is not None:
            backend = getattr(self._session, "decoder_backend", "-")
            if backend != self._view.hud_backend:
                self._view.hud_backend = backend
            # Staleness vs the device clock. Repaint on change even while the
            # stream is being drained -- no new frames arrive during a drain.
            lag = getattr(self._session, "video_lag_s", -1.0)
            if lag != self._view.hud_lag_s:
                self._view.hud_lag_s = lag
                self._view.update()
        path = self._view.render_path
        if path != self._render_path:
            self._render_path = path
            self._render_label.setText(f"render: {path}")

    @Slot()
    def _on_stream_closed(self) -> None:
        if self._connected:
            self._disconnect()
            self._status.setText("Stream ended")

    @Slot()
    def _update_fps(self) -> None:
        self._update_lock_label()
        if self._session is None or not self._connected:
            self._fps_label.setText("fps: -")
            self._fps_counter.reset()
            return
        # Rolling window, not a session average -- otherwise idle screen time
        # drags the number down and it reads ~30fps even when running faster.
        rate = self._fps_counter.sample(self._session.frames_decoded)
        self._fps_label.setText(f"{rate:.0f} fps")

    def _update_lock_label(self) -> None:
        """Show the keyguard state; announce the moment it locks.

        The flip into locked is also what pops the unlock panel (edge
        triggered: one dialog per lock event, never one per poll), and any
        transition back to not-locked closes it -- unlock it, it goes away.
        """
        locked: bool | None = None
        if self._session is not None and self._connected:
            locked = self._session.device_locked
        if locked is None:
            self._lock_label.setText("phone: -")
            self._lock_label.setStyleSheet("color: #888888;")
        elif locked:
            kind = self._session.lock_type if self._session is not None else None
            suffix = f" ({kind.upper()})" if kind else ""
            self._lock_label.setText(f"phone: LOCKED{suffix}")
            self._lock_label.setStyleSheet("color: #ff7b7b; font-weight: bold;")
        else:
            self._lock_label.setText("phone: unlocked")
            self._lock_label.setStyleSheet("color: #8fd6a6;")
        if locked is True and self._was_locked is not True:
            self._status.setText("Phone is on the lockscreen")
            self._open_lock_dialog()
        elif locked is not True and self._was_locked is True:
            self._close_lock_dialog()
        self._was_locked = locked

    # ------------------------------------------------------------------ #
    # Unlock panel
    # ------------------------------------------------------------------ #

    def _open_lock_dialog(self) -> None:
        """Pop the unlock panel, already sitting on the detected lock kind."""
        if self._lock_dialog is None:
            self._lock_dialog = LockDialog(
                self,
                send_key=self._send_key,
                send_text=self._inject_text,
                send_touch=self._inject_mouse,
                frame_size=lambda: self._view.frame_size,
                adb=self._adb,
            )
        kind = self._session.lock_type if self._session is not None else None
        self._lock_dialog.open_for(kind)

    def _close_lock_dialog(self) -> None:
        if self._lock_dialog is not None:
            self._lock_dialog.hide()

    def _on_lock_label_clicked(self) -> None:
        # Clicking the chip always opens the panel; useful even mid-session
        # when the state poll lags a few seconds behind reality.
        self._open_lock_dialog()

    def _inject_text(self, text: str) -> None:
        if self._session is not None:
            self._session.inject_text(text)

    # ------------------------------------------------------------------ #
    # Latency HUD + fullscreen
    # ------------------------------------------------------------------ #

    def _on_hud_toggled(self, checked: bool) -> None:
        self._view.set_hud(checked)

    def _sync_hud_action(self, on: bool) -> None:
        # Keep the toolbar in sync without re-entering its handler.
        if self._hud_action.isChecked() != on:
            self._hud_action.blockSignals(True)
            self._hud_action.setChecked(on)
            self._hud_action.blockSignals(False)

    def _on_overlay_toggled(self, checked: bool) -> None:
        self._view.set_overlay_visible(checked)
        self._status.setText(
            "Keybind markers shown" if checked else "Keybind markers hidden"
        )

    def _sync_overlay_action(self, visible: bool) -> None:
        # The view is the source of truth if F7 ever toggles it directly.
        if self._overlay_action.isChecked() != visible:
            self._overlay_action.blockSignals(True)
            self._overlay_action.setChecked(visible)
            self._overlay_action.blockSignals(False)

    @property
    def is_fullscreen(self) -> bool:
        return self._fullscreen

    def toggle_fullscreen(self) -> None:
        self.set_fullscreen(not self._fullscreen)

    def set_fullscreen(self, on: bool) -> None:
        """Fill the display with the phone image.

        The video keeps its own aspect ratio (letterboxed to the mobile
        screen), so hiding the chrome gives the mirror the largest possible
        window without ever stretching the phone's picture.
        """
        on = bool(on)
        if on == self._fullscreen:
            return
        self._fullscreen = on
        if on:
            # isHidden() rather than isVisible(): the window itself may not be
            # shown yet, and that must not be read as "user closed the dock".
            self._dock_was_visible = not self._dock.isHidden()
            self._statusbar_was_visible = not self.statusBar().isHidden()
            self._bar.setVisible(False)
            self._dock.setVisible(False)
            self.statusBar().setVisible(False)
            self._exit_fullscreen_shortcut.setEnabled(True)
            self.showFullScreen()
        else:
            self.showNormal()
            self._bar.setVisible(True)
            self._dock.setVisible(self._dock_was_visible)
            self.statusBar().setVisible(self._statusbar_was_visible)
            self._exit_fullscreen_shortcut.setEnabled(False)

    def leave_fullscreen(self) -> None:
        self.set_fullscreen(False)

    # ------------------------------------------------------------------ #
    # Injection
    # ------------------------------------------------------------------ #

    def _inject_touch(self, action, pointer_id, x, y, width, height) -> None:
        if self._session is not None:
            self._session.inject_touch(action, pointer_id, x, y, width, height)

    def _inject_mouse(self, action, pointer_id, x, y, width, height, action_button, buttons) -> None:
        if self._session is not None:
            self._session.inject_touch(
                action, pointer_id, x, y, width, height, action_button, buttons
            )

    def _send_key(self, keycode: AndroidKey) -> None:
        if self._session is None:
            return
        self._session.inject_keycode(KeyAction.DOWN, int(keycode))
        self._session.inject_keycode(KeyAction.UP, int(keycode))

    def _rotate_device(self) -> None:
        if self._session is not None:
            self._session.send_control(serialize_rotate_device())

    def _on_mute_toggled(self, muted: bool) -> None:
        """Toolbar Mute: silence the device sound on this computer.

        Audio keeps streaming (only decoding/playback stops), so un-muting is
        instant, and the choice is remembered for the next connection too.
        """
        self._muted = muted
        self._settings.set("muted", muted)
        if self._session is not None:
            self._session.set_muted(muted)
        self._status.setText("Sound muted" if muted else "Sound on")

    # ------------------------------------------------------------------ #
    # Tablet volume
    # ------------------------------------------------------------------ #

    def _step_volume(self, delta: int) -> None:
        """One volume step -- the same thing the tablet's own volume key sends.

        Deliberately a keycode over the control socket rather than an absolute
        adb write: it lands instantly, and it makes the tablet show its own
        volume panel, so the change is visible on the device itself. It is also
        the only one of the two controls that still works when `dumpsys audio`
        cannot be read.

        The price is that nothing reports the new level back, so the slider and
        the readout are moved on faith and then re-read once the presses stop.
        """
        if self._session is None:
            return
        key = AndroidKey.VOLUME_UP if delta > 0 else AndroidKey.VOLUME_DOWN
        self._send_key(key)
        # A step is relative, so any number the user dragged the slider to is
        # now out of date -- and a reading must not drag the tablet back to it.
        self._volume_target = None
        self._nudge_level(delta)
        self._vol_sync_timer.start()

    def _on_volume_slider(self, value: int) -> None:
        """The slider moved: drive the tablet the way the buttons do.

        This used to send one absolute write over adb once the drag had settled,
        and that is what made it feel late: the write waited out a 180 ms
        debounce and then spent a further ~193 ms (measured) on the adb round
        trip, so the tablet sat still for over a third of a second while the
        slider was already showing the new number. A keycode is a write to the
        control socket the session already holds open -- it lands while the
        finger is still dragging, and the tablet raises its own volume panel.
        """
        state = self._volume_state
        if state is None:
            return
        self._vol_readout.setText(f"{value}/{state.maximum}")
        if self._volume_applying:
            return  # a reading, or our own step, wrote this -- not the user
        level = self._volume_level
        if level is None:
            return  # nothing has been read, so there is no distance to send
        self._send_volume_steps(value - level)
        self._volume_level = value
        self._volume_target = value
        self._vol_sync_timer.start()

    def _send_volume_steps(self, steps: int) -> None:
        """Press the tablet's volume key ``steps`` times, in one direction.

        The tablet moves exactly one index per press, so a drag is replayed as
        the presses its own volume buttons would send -- just as fast as the
        socket accepts them. ``steps`` is bounded by the slider's range, which
        is the device's own, so a burst is never longer than a full sweep.
        """
        if self._session is None or steps == 0:
            return
        key = AndroidKey.VOLUME_UP if steps > 0 else AndroidKey.VOLUME_DOWN
        for _ in range(abs(steps)):
            self._send_key(key)

    def _nudge_level(self, delta: int) -> None:
        """Move the belief, the slider and the readout together, on faith."""
        state = self._volume_state
        if state is None or self._volume_level is None:
            return
        self._volume_level = max(0, min(state.maximum, self._volume_level + delta))
        self._show_level(self._volume_level, state.maximum)

    def _show_level(self, level: int, maximum: int) -> None:
        """Put ``level`` on the slider and in the readout, without re-emitting.

        The setter fires valueChanged, which without the guard is
        indistinguishable from the user dragging it -- so a reading, or a press,
        would be sent straight back to the tablet as a redundant command.
        """
        self._volume_applying = True
        try:
            self._vol_slider.setValue(level)
        finally:
            self._volume_applying = False
        self._vol_readout.setText(f"{level}/{maximum}")

    def _refresh_volume(self) -> None:
        """Read the tablet's media volume -- one adb call, off the GUI thread.

        Runs on connect, and again ~700 ms after the last change (see
        VOLUME_SYNC_MS): a keycode reports nothing back, so the only way to know
        how far the tablet really moved is to ask. It is not polled otherwise --
        the read is a `dumpsys audio`, ~265 ms and ~1500 lines on a real tablet,
        and this app otherwise avoids asking adb for things it does not need
        (see the foreground poll in MirrorSession._lock_watch).
        """
        session = self._session
        if session is None:
            return
        self._run_worker(session.device_volume, self._on_volume_read)

    def _on_volume_read(self, result: Any) -> None:
        """Apply a reading, or a failure, from the volume worker.

        Also the moment the slider's promise is checked. A drag is sent as
        keycode steps, which move the tablet only as far as it actually was;
        if the reading disagrees with the number that was dragged to, the tablet
        was not where we thought it was, and the number is written exactly
        instead -- once, so a write that does not take cannot loop.
        """
        if isinstance(result, Exception):
            self._set_volume_unknown("Tablet volume: could not be read")
            return
        state: VolumeState | None = result
        if state is None:
            self._set_volume_unknown(
                "This device does not report its media volume"
            )
            return
        self._volume_state = state
        self._vol_slider.setRange(0, max(1, state.maximum))
        self._volume_level = state.index
        self._show_level(state.index, state.maximum)
        muted = " (muted)" if state.muted else ""
        self._vol_slider.setEnabled(True)
        self._vol_slider.setToolTip(
            f"Tablet media volume (0 - {state.maximum}){muted}"
        )

        target, self._volume_target = self._volume_target, None
        if target is None or target == state.index:
            return
        session = self._session
        if session is None:
            return

        def corrected(written: Any) -> None:
            if isinstance(written, Exception) or written is False:
                self._status.setText("Tablet volume: the device did not accept it")
                return
            # The write is absolute, so the number asked for is the number now.
            self._volume_state = VolumeState(target, state.maximum, state.muted)
            self._volume_level = target
            self._show_level(target, state.maximum)

        self._run_worker(lambda: session.set_device_volume(target), corrected)

    def _set_volume_unknown(self, message: str) -> None:
        """No usable reading: say so rather than showing a made-up level.

        A blank is the only honest display here -- showing 0 would read as
        "the tablet has been turned down", which is a claim we cannot support.
        """
        self._volume_state = None
        self._volume_level = None
        self._volume_target = None
        self._vol_slider.setEnabled(False)
        self._vol_slider.setToolTip(message)
        self._vol_readout.setText("-")

    # ------------------------------------------------------------------ #
    # Keyboard
    # ------------------------------------------------------------------ #

    def _keyboard_is_open(self) -> bool:
        """Whether the tablet's on-screen keyboard is currently up.

        Reads the session's polled value; no session (or an unknown reading)
        means False, so the camera lock behaves normally when we cannot tell.
        """
        session = self._session
        return bool(getattr(session, "keyboard_shown", False))

    def _on_key_pressed(self, name: str) -> None:
        self._key_fired = False
        if self._view.edit_mode:
            self._arm_pending(name)
            return

        # Typing on the tablet outranks every binding. While its on-screen
        # keyboard is up the user is in a text field, so a key bound to
        # something -- "x" to the mouse-lock, "w" to the stick -- must still
        # type its character. Otherwise "xylophone" lands as "ylophone" and
        # the binding silently eats the letter.
        #
        # Only printable keys get this pass; arrows and function keys still
        # fire their bindings, because those are never part of a word.
        if self._keyboard_is_open() and _is_printable_key(name):
            return

        # Bindings and the pad fire in *both* control modes. Placing a marker
        # is explicit intent to use that key, so hotkeys work while the mouse
        # still steers the camera -- no more trading one for the other.
        if self._engine.press(name):
            self._key_fired = True
            via_pad = name in JOYSTICK_KEYS and self._profile.joystick is not None
            self._status.setText(
                f"{'Joystick' if via_pad else 'Injected'} "
                f"'{keynames.pretty_name(name)}'"
            )
            return

        # Nothing consumed it. The mouse-lock hotkey owns an unbound x -- it
        # is consumed here so it never types the letter onto the device.
        if name == keynames.LOCK_KEY_NAME:
            # Never steal the mouse while the tablet's own keyboard is up:
            # the user is typing in a field, and a camera lock there is
            # never what they meant. (An explicit x binding already won
            # above, so this only affects the unbound hotkey.)
            if self._keyboard_is_open():
                self._status.setText(
                    "Camera lock skipped - the tablet's keyboard is open"
                )
                self._key_fired = True
                return
            self._toggle_mouse_lock()
            self._key_fired = True
            return

        # Navigation keys act on the device directly.
        # Printable characters arrive via text_input instead.
        self._send_editing_key(name)

    #: Text-less editing keys the laptop keyboard can push to the device.
    _EDITING_KEYS = {
        "up": AndroidKey.DPAD_UP,
        "down": AndroidKey.DPAD_DOWN,
        "left": AndroidKey.DPAD_LEFT,
        "right": AndroidKey.DPAD_RIGHT,
        "delete": AndroidKey.FORWARD_DEL,
        "home": AndroidKey.MOVE_HOME,
        "end": AndroidKey.MOVE_END,
        "pageup": AndroidKey.PAGE_UP,
        "pagedown": AndroidKey.PAGE_DOWN,
    }

    def _send_editing_key(self, name: str) -> None:
        keycode = self._EDITING_KEYS.get(name)
        if keycode is not None and self._session is not None:
            self._send_key(keycode)

    def _on_text_input(self, text: str) -> None:
        """A composed keystroke: type it into the device's focused field.

        Works in both control modes -- click a field with the mouse, then
        just type. No dependency on the device's on-screen keyboard.
        """
        if self._view.edit_mode or self._key_fired:
            return  # arming a bind / a binding already consumed this key
        if self._session is None or not text:
            return
        if text in ("\n", "\r"):
            self._send_key(AndroidKey.ENTER)
        elif text == "\b":
            self._send_key(AndroidKey.DEL)
        elif text == "\t":
            self._send_key(AndroidKey.TAB)
        elif text.isprintable():
            # The pad's keys are movement, never text -- "w" must not type
            # while it drives the stick. But typing outranks the stick the same
            # way it outranks a binding: with the tablet's keyboard up the
            # user is in a text field and the letter is what they asked for.
            if (
                not self._keyboard_is_open()
                and text.lower() in JOYSTICK_KEYS
                and self._profile.joystick is not None
            ):
                return
            self._session.inject_text(text)

    def _on_key_released(self, name: str) -> None:
        if self._engine.release(name):
            self._status.setText(f"Released '{keynames.pretty_name(name)}'")

    # -- clipboard (Ctrl+V / Ctrl+Shift+V) -------------------------------- #

    def _paste_computer_clipboard(self) -> None:
        """Ctrl+V: send this computer's clipboard to the device and paste it.

        The text becomes the device's clipboard and the device presses its own
        PASTE key, so it lands in whatever field has focus.
        """
        if self._session is None:
            self._status.setText("Not connected - nothing to paste into")
            return
        text = QGuiApplication.clipboard().text()
        if not text:
            self._status.setText("This computer's clipboard is empty")
            return
        if self._session.set_clipboard(text, paste=True):
            preview = text if len(text) <= 24 else text[:24] + "..."
            self._status.setText(f"Pasted '{preview}' onto the device")
        else:
            self._status.setText("Could not paste - the control channel is closed")

    def _paste_device_clipboard(self) -> None:
        """Ctrl+Shift+V: press the device's PASTE key (its own clipboard)."""
        if self._session is None:
            self._status.setText("Not connected - nothing to paste into")
            return
        if self._session.paste_device_clipboard():
            self._status.setText("Pasted the device's own clipboard")
        else:
            self._status.setText("Could not paste - the control channel is closed")

    # -- mouse lock (x) --------------------------------------------------- #

    def _toggle_mouse_lock(self) -> None:
        self._set_mouse_lock(not self._mouse_locked)

    def _set_mouse_lock(self, locked: bool) -> None:
        """Classic PC-game mouse-look.

        Press x: the cursor vanishes and stays exactly where it was, and the
        app takes over the camera drag -- a drag is planted for you and mouse
        movement alone steers it. Hold the left button too, if you like: it is
        the same drag either way. Press x again: the drag lifts and the cursor
        reappears.

        The keybind dock, the toolbar and the status bar are never touched
        here: hiding the mouse must not cost you your controls. Only F11
        (fullscreen) removes the chrome, and it puts it all back on Esc.
        """
        if locked == self._mouse_locked:
            if not locked:
                # An unlock request always gives the pointer back, even if we
                # did not think we were holding it.
                self._release_stale_clip()
            return
        # Hiding/showing the chrome below can surface a transient
        # ActivationChange. Without this guard, changeEvent() reads that as
        # "the user alt-tabbed" and instantly undoes the lock -- which made x
        # look like a one-shot toggle.
        self._lock_changing = True
        self._mouse_locked = locked
        self._view.pinned_cursor = locked
        try:
            self._apply_mouse_lock(locked)
        finally:
            self._lock_changing = False

    def _apply_mouse_lock(self, locked: bool) -> None:
        """The lock/unlock side effects: cursor and look polling.

        Deliberately does NOT touch the toolbar, the keybind dock or the
        status bar -- pressing x in a window used to hide the keybind panel
        and leave the user without their controls mid-game.
        """
        if locked:
            self._view._release_mouse()  # lift any half-finished LMB drag first
            self._view._look_last_pos = None  # deltas start from scratch
            self._view.setCursor(Qt.CursorShape.BlankCursor)

            # The drag plants on the profile's Camera Movement spot (its
            # default is the middle) and the pointer parks on the mirror's
            # middle: the drag of the device screen (where a camera drag
            # belongs) and the pointer of the mirror. Nothing is guessed from
            # wherever the pointer happened to be, so the lock behaves the
            # same every time.
            self._look_anchor = self._camera_anchor_px()
            self._look_park = self._centre_point()
            self._look_press = False
            self._raw_seen = False
            self._pointer_driven = False
            # The one local point the pointer is held on, so the move event
            # Windows echoes back for it can be recognised and ignored.
            self._view.note_warp(QPointF(self._view.rect().center()))
            # A clip left behind by an older build (or another app) would be
            # holding the pointer in a box it cannot leave -- clear it before
            # asking the pointer to do anything at all.
            self._release_stale_clip()
            self._park_cursor()
            # Raw input when the OS delivers it: relative deltas with no
            # cursor involved whatsoever.
            self._raw_active = self._raw_look.start(self._view_handle())
            # ...and the OS cursor when it does not, which is the source the
            # real mouse drag already travels through. Qt move events are
            # never the source: our own park would echo back as movement.
            self._view.look_move = None if self._can_read_cursor() else self._on_look_move
            self._look_poll.start()
            # Keep keyboard focus on the mirror: it is what receives the x
            # that unlocks again (and nothing else can be clicked while
            # locked if the user has no drag down of their own).
            self._view.setFocus(Qt.FocusReason.OtherFocusReason)
            self._status.setText("Mouse locked - press x to unlock")
        else:
            self._look_poll.stop()
            self._lift_look_finger()  # hand the device its release
            self._raw_look.stop()
            self._raw_active = False
            self._view.unsetCursor()
            self._look_park = None
            self._look_anchor = None
            self._look_press = False
            self._raw_seen = False
            self._pointer_driven = False
            self._view.look_move = self._on_look_move
            self._view._look_last_pos = None
            self._view.note_warp(None)
            self._warp_to_center()
            self._view.setFocus(Qt.FocusReason.OtherFocusReason)
            self._status.setText("Mouse unlocked")

    def _can_read_cursor(self) -> bool:
        """Can the OS cursor be read *and* put back? Windows, basically."""
        return self._get_cursor_pos() is not None

    def _frame_centre(self) -> tuple[int, int] | None:
        """Middle of the mirrored screen, in its own pixels.

        Clamped clear of the edge band so the drag does not start out already
        due to be re-planted.
        """
        fw, fh = self._view.frame_size
        if fw <= 0 or fh <= 0:
            return None
        band_x, band_y = _edge_band(fw, fh)
        return (
            min(max(fw // 2, band_x + 1), max(band_x + 1, fw - band_x - 1)),
            min(max(fh // 2, band_y + 1), max(band_y + 1, fh - band_y - 1)),
        )

    def _camera_anchor_px(self) -> tuple[int, int] | None:
        """The Camera Movement spot in frame pixels, clear of the edge band.

        The x-key look drag plants its finger here; falls back to the
        frame's middle when there is no frame yet.
        """
        fw, fh = self._view.frame_size
        if fw <= 0 or fh <= 0:
            return None
        x, y = self._profile.camera.to_frame_point(fw, fh)
        band_x, band_y = _edge_band(fw, fh)
        return (
            min(max(x, band_x + 1), max(band_x + 1, fw - band_x - 1)),
            min(max(y, band_y + 1), max(band_y + 1, fh - band_y - 1)),
        )

    def _view_handle(self) -> int:
        """The mirror's native window handle, or 0 when there isn't one."""
        if not _cursor_api_available():
            return 0
        try:
            return int(self._view.winId())
        except Exception:
            return 0

    def _virtual_screen(self) -> tuple[int, int, int, int] | None:
        """Left/top/width/height of the whole desktop, in physical pixels.

        The backstop for every cursor move: a point outside this rectangle is
        not on a screen at all, so moving the pointer there is a bug, not a
        preference (all monitors, so a second display is included).
        """
        if not _cursor_api_available():
            return None
        try:

            user32 = ctypes.windll.user32
            # SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, ...
            left = user32.GetSystemMetrics(76)
            top = user32.GetSystemMetrics(77)
            width = user32.GetSystemMetrics(78)
            height = user32.GetSystemMetrics(79)
            if width <= 0 or height <= 0:
                return None
            return (left, top, width, height)
        except Exception:
            return None

    def _on_screen(self, x: int, y: int) -> bool:
        """Is this point somewhere a cursor could actually be?"""
        screen = self._virtual_screen()
        if screen is None:
            return True  # can't tell: trust the caller over blocking it
        left, top, width, height = screen
        return left <= x < left + width and top <= y < top + height

    def _look_geometry(self) -> tuple[int, int, int, int] | None:
        """Physical screen rect of the mirror (Win32), for cursor maths.

        Qt geometry is device-independent while Win32 cursor calls are in
        physical pixels; at anything other than 100% scaling the two agree
        only by accident. Reading the client rect straight from the window
        handle keeps the center, the clip and the deltas in one space.

        An empty rect counts as *unknown*, not as a mirror at 0,0: a
        minimized window reports a 0x0 client area, and taking its centre
        flung the pointer into the top-left corner of the desktop.
        """
        if not _cursor_api_available():
            return None
        if self.isMinimized():
            return None  # a minimized window has no client area to measure
        try:
            from ctypes import wintypes

            hwnd = int(self._view.winId())
            user32 = ctypes.windll.user32
            rect = wintypes.RECT()
            if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
                return None
            origin = wintypes.POINT(0, 0)
            if not user32.ClientToScreen(hwnd, ctypes.byref(origin)):
                return None
            width = rect.right - rect.left
            height = rect.bottom - rect.top
            if width <= 0 or height <= 0:
                return None
            return (origin.x, origin.y, width, height)
        except Exception:
            return None

    def _mirror_rect(self) -> tuple[int, int, int, int] | None:
        """The mirror's rectangle on screen, in physical pixels.

        The Win32 client rect is the exact answer; if that is unavailable the
        mirror's own geometry is mapped to the screen instead. Everything
        that moves or contains the pointer is checked against this.
        """
        geo = self._look_geometry()
        if geo is not None:
            return geo
        if not _cursor_api_available():
            return None
        if self.isMinimized():
            return None
        try:
            corner = self._view.mapToGlobal(self._view.rect().topLeft())
            scale = self._view.devicePixelRatioF() or 1.0
            box = (
                int(corner.x() * scale),
                int(corner.y() * scale),
                int(self._view.width() * scale),
                int(self._view.height() * scale),
            )
        except Exception:
            return None
        if box[2] <= 0 or box[3] <= 0:
            return None  # a zero-area clip parks the pointer on a corner
        return box

    def _inside_mirror(self, point: tuple[int, int]) -> bool:
        """Is this screen point actually on the mirror?"""
        rect = self._mirror_rect()
        if rect is None:
            return True  # can't tell; refusing here would park it needlessly
        left, top, width, height = rect
        x, y = point
        return left <= x < left + width and top <= y < top + height

    def _release_stale_clip(self) -> None:
        """Undo any cursor confinement before touching the pointer.

        This app used to call ClipCursor, which is a *system-wide* setting:
        one left behind (by an older build, or by a crash) holds the pointer
        inside a box it cannot leave -- never leaving its middle box, never
        reporting the movement that would turn the camera. A clip is the only
        thing here that can affect the whole desktop, so the app no longer
        sets one; this clears any that is still in force.
        """
        if not _cursor_api_available():
            return
        try:

            ctypes.windll.user32.ClipCursor(None)
        except Exception:
            pass

    def _warp_to_center(self) -> None:
        """Put the OS cursor back on the middle of the mirror.

        The landing spot is remembered so the Qt fallback can recognise the
        move event Windows echoes back for our own warp and ignore it -- that
        echo is what used to cancel the look out one step at a time.
        """
        target = self._centre_point()
        if target is None:
            return
        self._set_cursor_pos(*target)
        self._view.note_warp(QPointF(self._view.rect().center()))

    def _set_cursor_pos(self, x: int, y: int) -> None:
        """Move the pointer, but only to somewhere known to be the mirror.

        Every mistake in this area has shown up as the pointer appearing at
        some odd spot of its own accord, so the app now refuses to put the
        pointer anywhere it cannot prove is inside the mirror: off the
        desktop, or off the mirror, and it does not move it at all.
        """
        if not _cursor_api_available():
            return
        x, y = int(x), int(y)
        if not self._on_screen(x, y) or not self._inside_mirror((x, y)):
            return
        try:

            ctypes.windll.user32.SetCursorPos(x, y)
        except Exception:
            pass  # best-effort; the poll just skips a beat

    def _get_cursor_pos(self) -> tuple[int, int] | None:
        if not _cursor_api_available():
            return None
        try:
            from ctypes import wintypes

            pt = wintypes.POINT()
            if not ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
                return None
            return (pt.x, pt.y)
        except Exception:
            return None

    def _centre_point(self) -> tuple[int, int] | None:
        """Middle of the mirror in physical screen pixels, if measurable."""
        if self.isMinimized():
            return None
        geo = self._look_geometry()
        if geo is not None:
            left, top, width, height = geo
            return (left + width // 2, top + height // 2)
        if not _cursor_api_available():
            return None
        try:
            corner = self._view.mapToGlobal(self._view.rect().center())
            scale = self._view.devicePixelRatioF() or 1.0
            return (int(corner.x() * scale), int(corner.y() * scale))
        except Exception:
            return None

    def _park_cursor(self) -> None:
        """Hold the invisible pointer exactly where it was parked.

        While locked the pointer is not an input any more, so it is not
        allowed to travel: it would walk itself into a screen edge (where the
        drag would stop) and out from under the blank cursor shape. It stays
        put until the lock ends, and the mouse movement goes to the game.
        """
        if self._look_park is None:
            self._look_park = self._centre_point()
        if self._look_park is not None:
            self._set_cursor_pos(*self._look_park)

    def _look_poll_tick(self) -> None:
        """Hand the device one batch of movement.

        The mouse device's own raw stream is the source to use: relative,
        unaffected by anything the app does to the pointer, and it needs the
        pointer not at all -- so while it is delivering, the pointer is not
        touched. If it stays silent while the pointer is plainly moving, this
        system is not delivering raw input and the pointer's own travel takes
        over for the rest of the lock. Both at once would count the same
        movement twice, so the choice is made once and then stuck to.
        """
        if not self._mouse_locked:
            return

        if self._raw_active and not self._pointer_driven:
            rdx, rdy = self._raw_look.take()
            if rdx or rdy:
                self._raw_seen = True
                self._on_raw_look(rdx * LOOK_RAW_GAIN, rdy * LOOK_RAW_GAIN)
                self._keep_pointer_on_the_mirror()
                return
            if self._raw_seen or not self._pointer_moved():
                self._keep_pointer_on_the_mirror()
                return
            self._pointer_driven = True
            self._status.setText("Mouse locked (reading the pointer)")

        self._pointer_tick()

    def _pointer_moved(self) -> bool:
        """Has the pointer travelled since it was parked?"""
        pos = self._get_cursor_pos()
        if pos is None or self._look_park is None:
            return False
        return pos != self._look_park

    def _pointer_tick(self) -> None:
        """Delta from the pointer's travel, then put it back on its park."""
        pos = self._get_cursor_pos()
        if pos is None or self._look_park is None:
            return
        # Physical pixels -> the view-local units _on_raw_look scales against.
        sx, sy = self._pixel_scale()
        dx = (pos[0] - self._look_park[0]) / sx
        dy = (pos[1] - self._look_park[1]) / sy
        # Re-baselining here is what stops our own park reading as a phantom
        # delta on the next tick.
        self._park_cursor()
        if dx or dy:
            self._on_raw_look(dx, dy)

    def _keep_pointer_on_the_mirror(self) -> None:
        """Put the hidden pointer back only if it has left the mirror.

        Not every tick: moving the pointer is the one thing this app can get
        visibly, globally wrong, so it is done as little as possible.
        """
        pos = self._get_cursor_pos()
        if pos is None or self._inside_mirror(pos):
            return
        self._park_cursor()

    def _pixel_scale(self) -> tuple[float, float]:
        """Physical screen pixels per view-local pixel (DPI scaling)."""
        rect = self._mirror_rect()
        if rect is None:
            return (1.0, 1.0)
        _, _, width, height = rect
        return (
            width / max(1, self._view.width()),
            height / max(1, self._view.height()),
        )

    def _on_look_move(self, dx: float, dy: float) -> None:
        """Qt move events, for platforms with no cursor we can read or park."""
        self._on_raw_look(dx, dy)
        self._park_cursor()

    def _on_look_button(self, down: bool) -> None:
        """The user pressed or released the left button while locked.

        Their own press is the most faithful drag there is, so while it is
        held the camera drag is theirs: it stays down (never re-planted on
        idle) and the app only adds the movement to it.
        """
        if not self._mouse_locked:
            return
        self._look_press = down
        if down:
            fw, fh = self._view.frame_size
            if fw > 0 and fh > 0 and not self._look_down:
                self._plant_look_finger(fw, fh)
            self._status.setText("Camera drag held (release to stop)")
        else:
            self._lift_look_finger()
            self._status.setText("Mouse locked - press x to unlock")

    def _on_raw_look(self, dx: float, dy: float) -> None:
        """Turn mouse movement into the camera drag the game already accepts.

        This is not a finger *resembling* a drag -- it is the very same
        gesture a left-button drag on the mirror produces (see DRAG_* in
        glview): same pointer, same pressed button, held for every step. The
        game answers that drag by rotating, so movement alone now steers.

        The drag is planted once, where the pointer was when x was pressed,
        and then travels with the movement. When it reaches the edge of the
        device's touchable area it is lifted and re-planted at that same
        spot, so a drag can rotate forever instead of dying at a screen edge.
        While locked the drag stays down: a held camera drag is what the game
        is looking for, and re-pressing it would read as taps.
        """
        if not self._mouse_locked:
            return
        fw, fh = self._view.frame_size
        if fw <= 0 or fh <= 0:
            return
        vw, vh = max(1, self._view.width()), max(1, self._view.height())
        if not self._look_down:
            self._plant_look_finger(fw, fh)
        nx = self._look_x + dx * (fw / vw)
        ny = self._look_y + dy * (fh / vh)
        margin_x, margin_y = _edge_band(fw, fh)
        if (
            nx < margin_x
            or nx > fw - margin_x
            or ny < margin_y
            or ny > fh - margin_y
        ):
            # The finger has reached the edge of the device's touchable area.
            # Lift it and plant it on the anchor again, then take this step's
            # travel from there: the drag continues instead of stopping dead.
            self._lift_look_finger()
            self._plant_look_finger(fw, fh)
            nx = self._look_x + dx * (fw / vw)
            ny = self._look_y + dy * (fh / vh)
        self._look_x = min(max(nx, 0.0), float(fw - 1))
        self._look_y = min(max(ny, 0.0), float(fh - 1))
        self._inject_mouse(
            MotionAction.MOVE,
            DRAG_POINTER_ID,
            int(self._look_x),
            int(self._look_y),
            fw,
            fh,
            0,
            DRAG_HELD_BUTTONS,
        )

    def _look_anchor_frame(self, fw: int, fh: int) -> tuple[int, int]:
        """The drag's home position in frame pixels."""
        anchor = self._look_anchor
        if anchor is None:
            return (fw // 2, fh // 2)
        return (
            min(max(anchor[0], 0), fw - 1),
            min(max(anchor[1], 0), fh - 1),
        )

    def _plant_look_finger(self, fw: int, fh: int) -> None:
        """Touch down on the anchor: a fresh drag, not a jump from the old spot."""
        self._look_x, self._look_y = (float(v) for v in self._look_anchor_frame(fw, fh))
        self._look_down = True
        self._inject_mouse(
            MotionAction.DOWN,
            DRAG_POINTER_ID,
            int(self._look_x),
            int(self._look_y),
            fw,
            fh,
            DRAG_PRESS_BUTTON,
            DRAG_HELD_BUTTONS,
        )

    def _lift_look_finger(self) -> None:
        """Lift the camera finger where it currently sits."""
        if not self._look_down:
            return
        fw, fh = self._view.frame_size
        if fw <= 0 or fh <= 0:
            self._look_down = False
            return
        self._look_down = False
        self._inject_mouse(
            MotionAction.UP,  # a release, so nothing is held any more
            DRAG_POINTER_ID,
            int(self._look_x),
            int(self._look_y),
            fw,
            fh,
            0,
            0,
        )

    def changeEvent(self, event) -> None:  # noqa: N802
        super().changeEvent(event)
        if (
            event.type() == QEvent.Type.ActivationChange
            and not self._lock_changing
            and not self.isActiveWindow()
            and self._mouse_locked
        ):
            # Alt-tab must never leave the cursor pinned to a dead window.
            self._set_mouse_lock(False)

    def _rebuild_overflow(self) -> None:
        """Slide the tail of the toolbar into the » menu when it does not fit.

        This is the permanent answer to a toolbar that keeps growing: the row
        cannot wrap, so the buttons that will not fit move into a menu and
        move back out as soon as there is room again.

        Widths are summed by hand rather than read from the live layout: the
        layout's sizeHint does not shrink as widgets are hidden within the
        same pass, so measuring it per iteration hid everything at once.
        """
        if self._overflow is None:
            self._overflow = QToolButton(self._bar)
            self._overflow.setText("\u00bb")  # »
            self._overflow.setToolTip("More actions")
            self._overflow.setPopupMode(
                QToolButton.ToolButtonPopupMode.InstantPopup
            )
            self._overflow.setMenu(QMenu(self._overflow))
            self._overflow.setSizePolicy(
                QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
            )
            self._bar_layout.addWidget(self._overflow)

        for button, _action in self._bar_items:
            button.setVisible(True)
        for frame, _left in self._bar_seps:
            frame.setVisible(True)
        self._overflow.setVisible(False)

        available = self._bar.width()
        if available <= 0:
            return

        # Measured against the buttons' *natural* width, not their current
        # one. When the row is squeezed the layout shrinks every button below
        # its sizeHint and Qt clips the text ("Remove pad" -> "Remove"), so
        # measuring what it currently looks like would conclude "it fits" and
        # hide the overflow entirely.
        spacing = self._bar_layout.spacing()
        tracked = {id(button) for button, _ in self._bar_items}
        fixed = spacing * self._bar_layout.count()
        for index in range(self._bar_layout.count()):
            item = self._bar_layout.itemAt(index)
            widget = item.widget()
            if widget is None:
                # addSpacing() contributes a QSpacerItem, which itemAt().widget()
                # reports as None -- so its width used to vanish from the budget
                # entirely.
                spacer = item.spacerItem()
                if spacer is not None:
                    # QSpacerItem exposes no width(); a fixed spacer from
                    # addSpacing() reports its size in the size hint.
                    fixed += max(
                        spacer.sizeHint().width(), spacer.minimumSize().width()
                    )
                continue
            if id(widget) in tracked:
                continue
            if widget is self._overflow:
                # The » button is hidden right now and reserve_for_overflow()
                # adds it back only when something is actually hidden. Counting
                # it here as well over-budgeted the row by its width.
                continue
            # Budget what the widget will *actually* occupy once the row is
            # tight, which is its minimum -- not its sizeHint. The device
            # combo pins setMinimumWidth(180) while its sizeHint for the empty
            # list is only 101, so counting the sizeHint under-budgeted the
            # row by 79px, too few buttons were hidden, and Qt laid the combo
            # out over the Refresh button beside it.
            fixed += max(
                widget.sizeHint().width(),
                widget.minimumSizeHint().width(),
                widget.minimumWidth(),
            )

        def natural(visible) -> int:
            # Width, not sizeHint: every button has an explicit fixed width
            # (text + a hair), and sizeHint still reports the stock style's
            # much wider value -- summing it hid half the row for no reason.
            return fixed + sum(button.width() for button in visible)

        visible = [button for button, _a in self._bar_items]
        actions = dict((id(b), a) for b, a in self._bar_items)
        hidden: list[QAction] = []

        def reserve_for_overflow() -> int:
            return (
                self._overflow.sizeHint().width() + spacing
                if hidden
                else 0
            )

        while visible and natural(visible) + reserve_for_overflow() > available:
            button = visible.pop()
            button.setVisible(False)
            hidden.append(actions[id(button)])
        hidden.reverse()

        # A separator only means something between two visible groups. They are
        # fixed-width and are not part of _bar_items, so nothing used to hide
        # them: once the buttons on one side slid into the » menu the
        # separators stayed, and Qt -- which lays out only what is visible --
        # pulled them left until they sat on top of the widget before them
        # (the device combo, by 3px). Hide a separator whenever the button on
        # either side of it is not on screen.
        shown = {id(button) for button in visible}
        for frame, left in self._bar_seps:
            if not 0 <= left < len(self._bar_items):
                frame.setVisible(False)
                continue
            before = id(self._bar_items[left][0]) in shown
            after = (
                left + 1 < len(self._bar_items)
                and id(self._bar_items[left + 1][0]) in shown
            )
            frame.setVisible(before and after)

        if hidden:
            menu = self._overflow.menu()
            menu.clear()
            for action in hidden:
                menu.addAction(action)
            self._overflow.setVisible(True)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        # Qt delivers resizeEvent *before* the layout has re-laid out, so
        # measuring the bar here would use the previous width and overflow the
        # wrong number of buttons. A zero-timer runs after the layout settles.
        self._overflow_timer.start(0)

    def _schedule_overflow(self) -> None:
        self._overflow_timer.start(0)
        if self._mouse_locked:
            self._look_park = self._centre_point()  # the middle moved
            self._park_cursor()

    def moveEvent(self, event) -> None:  # noqa: N802
        super().moveEvent(event)
        if self._mouse_locked:
            self._look_park = self._centre_point()
            self._park_cursor()

    # ------------------------------------------------------------------ #
    # Keybind editing
    # ------------------------------------------------------------------ #

    #: Shown whenever an edit is refused because the shipped preset is active.
    _READ_ONLY_MSG = (
        "The default keybinds are read-only - save this profile under a new "
        "name to edit it"
    )

    def _editing_locked(self) -> bool:
        """Whether the active profile refuses edits (the shipped preset).

        The default profile is the fixed fallback every user can come back
        to; it only ever changes through a copy saved under another name.
        """
        if not self._profile.read_only:
            return False
        self._status.setText(self._READ_ONLY_MSG)
        return True

    def _on_mode_toggled(self, checked: bool) -> None:
        if checked and self._profile.read_only:
            # Bounce back to off; the uncheck pass restores the view state,
            # then the reason stays up as the last word.
            self._mode_action.setChecked(False)
            self._status.setText(self._READ_ONLY_MSG)
            return
        self._engine.release_all()
        self._view.set_edit_mode(checked)
        if checked:
            self._status.setText(
                "Edit mode: press a key, then click the screen to place it."
            )
        else:
            self._clear_pending()
            self._status.setText(self._control_target_message())

    # -- control target (mouse <-> keybinds) ------------------------------ #

    def _control_target_message(self) -> str:
        if self._view.control_target == MirrorView.TARGET_KEYMAP:
            return "Keybind control active"
        return "Mouse control active"

    def _on_toggle_hotkey(self) -> None:
        self._view.toggle_control_target()

    def _on_keybind_toggled(self, checked: bool) -> None:
        self._view.set_control_target(
            MirrorView.TARGET_KEYMAP if checked else MirrorView.TARGET_MOUSE
        )

    def _on_control_target_changed(self, target: str) -> None:
        # Lifting held fingers matters: switching away while a keybind is held
        # would otherwise leave it pressed down on the device forever.
        self._engine.release_all()
        is_keymap = target == MirrorView.TARGET_KEYMAP

        # Keep the toolbar action in sync without re-entering its handler.
        if self._keybind_action.isChecked() != is_keymap:
            self._keybind_action.blockSignals(True)
            self._keybind_action.setChecked(is_keymap)
            self._keybind_action.blockSignals(False)
        self._input_label.setText(f"input: {'keybinds' if is_keymap else 'mouse'}")
        self._status.setText(self._control_target_message())

    def _begin_add_binding(self) -> None:
        """Show or hide the key recorder.

        One arming at a time: the pad and the camera spot are stood down so
        the click that follows the recording places the binding, not them. The
        recorder is a floating panel that survives clicks elsewhere, so this
        is a toggle -- click *Add key* again to put it away. It records mouse
        buttons as well as physical keys, and the read-only default profile
        refuses to open it at all.
        """
        if self._key_dialog is not None and self._key_dialog.isVisible():
            self._cancel_key_recording()
            return
        if self._editing_locked():
            return  # the preset never changes; save a copy to edit

        self._add_prev_edit = self._mode_action.isChecked()
        self._mode_action.setChecked(True)
        self._view.set_pending_joystick(False)  # one arming at a time
        self._view.set_pending_camera(False)
        self._pending_label.setText("Recording a key to bind...")
        self._status.setText("Press the key you want to bind.")

        if self._key_dialog is None:
            self._key_dialog = KeyCaptureDialog(self)
            self._key_dialog.finished.connect(self._on_key_recorder_closed)
        # Centred on the window: the captured key is the whole point of the
        # panel, and it must be impossible to miss. (It used to tuck under
        # the toolbar, where the recording happened out of sight.)
        self._key_dialog.adjustSize()
        geo = self._key_dialog.geometry()
        geo.moveCenter(self.mapToGlobal(self.rect().center()))
        self._key_dialog.setGeometry(geo)
        # Clicks anywhere record -- except on *Add key* itself, whose second
        # click is how the recorder is put away.
        cancel = next(
            (btn for btn, action in self._bar_items if action is self._add_action),
            None,
        )
        if cancel is not None:
            self._key_dialog.set_ignore_rect(
                QRect(cancel.mapToGlobal(QPoint(0, 0)), cancel.size())
            )
        self._sync_add_action(True)
        self._key_dialog.begin_recording()

    def _sync_add_action(self, on: bool) -> None:
        """Keep the *Add key* button showing whether the recorder is open."""
        if self._add_action.isChecked() != on:
            self._add_action.blockSignals(True)
            self._add_action.setChecked(on)
            self._add_action.blockSignals(False)

    def _on_key_recorder_closed(self, _result: int = 0) -> None:
        """The panel closed: use what it caught, or stand down."""
        self._sync_add_action(False)
        # Both sides of this used to be self._key_dialog, so it assigned the dialog to
        # itself and kept the handle alive after close. It is deliberately left
        # in place rather than cleared to None: _cancel_key_recording and
        # closeEvent reuse the same dialog instance, and line 2779 re-arms it.
        dialog = self._key_dialog
        name = dialog.key_name() if dialog is not None else None
        if name:
            # Armed: the next click on the phone screen places it.
            self._arm_pending(name)
        else:
            # Restore the edit state the recording found -- first, because
            # the mode toggle resets the pending labels as it stands down.
            self._mode_action.setChecked(self._add_prev_edit)
            self._pending_key = None
            self._view.set_pending_key(None)
            self._pending_label.setText("Binding cancelled.")
            self._status.setText("Binding cancelled")

    def _cancel_key_recording(self) -> None:
        """Put the recorder away without binding anything."""
        if self._key_dialog is not None:
            self._key_dialog.reset()
            self._key_dialog.hide()
        self._sync_add_action(False)
        # Backing out restores the edit state *Add binding* found -- first,
        # because the mode toggle resets the pending labels as it stands down.
        self._mode_action.setChecked(self._add_prev_edit)
        self._pending_key = None
        self._view.set_pending_key(None)
        self._pending_label.setText("Binding cancelled.")
        self._status.setText("Binding cancelled")

    def _arm_pending(self, name: str) -> None:
        self._view.set_pending_joystick(False)  # key arming wins over pad arming
        self._view.set_pending_camera(False)
        if self._profile.key_in_use(name):
            self._status.setText(
                f"'{keynames.pretty_name(name)}' is already bound - "
                "placing again will move it."
            )
        self._pending_key = name
        self._view.set_pending_key(name)
        self._pending_label.setText(
            f"'{keynames.pretty_name(name)}' armed - click the phone screen to place it."
        )

    def _clear_pending(self) -> None:
        self._pending_key = None
        self._view.set_pending_key(None)
        self._view.set_pending_joystick(False)
        self._view.set_pending_camera(False)
        self._pending_label.setText(
            "Press 'Add binding', record a key, then click the phone screen."
        )

    def _begin_add_joystick(self) -> None:
        if self._editing_locked():
            return
        self._mode_action.setChecked(True)
        self._pending_key = None
        self._view.set_pending_key(None)
        self._view.set_pending_joystick(True)
        self._view.set_pending_camera(False)  # one arming at a time
        self._pending_label.setText(
            "Click over the game's stick to place the WASD pad."
        )
        self._status.setText("Click the phone screen to place the joystick pad.")

    def _on_joystick_placed(self, nx: float, ny: float) -> None:
        self._profile.joystick = Joystick(nx=nx, ny=ny)
        # The pad owns WASD: retire any stray markers that would fight it.
        for key in JOYSTICK_KEYS:
            self._profile.remove_key(key)
        self._engine.set_profile(self._profile)
        self._clear_pending()
        # Placement is a one-shot task: leaving edit mode immediately so the
        # next WASD press drives the pad instead of arming a new binding
        # (that silent trap is why the joystick "did nothing").
        self._mode_action.setChecked(False)
        self._refresh_bindings()
        self._auto_save()
        self._status.setText("Joystick placed - W/A/S/D now drive the pad.")

    def _on_joystick_moved(self, nx: float, ny: float) -> None:
        joy = self._profile.joystick
        if joy is None:
            return
        # The view holds the same object, so this moves the drawn pad too.
        joy.nx, joy.ny = nx, ny
        self._view.update()
        self._joy_save_timer.start()

    def _on_joystick_selected(self) -> None:
        self._status.setText(
            "Joystick selected - drag the middle to move it, the rim to resize."
        )

    def _on_joystick_resized(self, radius: float) -> None:
        """A rim drag changed the pad's size."""
        joy = self._profile.joystick
        if joy is None:
            return
        # The view holds the same object, so this resizes the drawn pad too.
        joy.radius = radius
        self._sync_joystick_slider()
        self._view.update()
        self._joy_save_timer.start()

    def _on_joystick_slider(self, value: int) -> None:
        """The dock's size slider: set the pad's radius from the handle."""
        if self._editing_locked():
            return
        joy = self._profile.joystick
        if joy is None:
            return
        joy.radius = value / 100.0
        self._view.update()
        self._joy_save_timer.start()

    def _sync_joystick_slider(self) -> None:
        """Show the pad's current size on the slider (without re-firing it).

        The whole row is hidden when there is no pad, so the dock only grows a
        size control once there is something to size.
        """
        joy = self._profile.joystick
        self._joy_size_slider.setEnabled(
            joy is not None and not self._profile.read_only
        )
        self._joy_size_slider.setVisible(joy is not None)
        self._joy_size_label.setVisible(joy is not None)
        if joy is None:
            return
        value = int(round(joy.radius * 100))
        if self._joy_size_slider.value() != value:
            self._joy_size_slider.blockSignals(True)
            self._joy_size_slider.setValue(value)
            self._joy_size_slider.blockSignals(False)

    def _remove_joystick(self) -> None:
        if self._editing_locked():
            return
        if self._profile.joystick is None:
            self._status.setText("No joystick to remove.")
            return
        self._engine.release_all()  # lift the pad's finger first
        self._profile.joystick = None
        self._engine.set_profile(self._profile)
        self._refresh_bindings()
        self._auto_save()
        self._status.setText("Joystick removed.")

    # ------------------------------------------------------------------ #
    # Camera Movement (the x-key look drag's plant point)
    # ------------------------------------------------------------------ #

    def _begin_set_camera(self) -> None:
        if self._editing_locked():
            return
        self._mode_action.setChecked(True)
        self._pending_key = None
        self._view.set_pending_key(None)
        self._view.set_pending_joystick(False)  # one arming at a time
        self._view.set_pending_camera(True)
        self._pending_label.setText(
            "Click over the game's camera area to plant the Camera Movement spot."
        )
        self._status.setText(
            "Click the phone screen to place the Camera Movement spot."
        )

    def _on_camera_placed(self, nx: float, ny: float) -> None:
        if self._profile.camera.locked:
            # A locked spot does not move, not even from a fresh placement.
            self._status.setText("Camera Movement is locked - unlock it first.")
            self._clear_pending()
            return
        self._profile.camera.nx = nx
        self._profile.camera.ny = ny
        self._clear_pending()
        # Placement is a one-shot task, same as the joystick pad: leave edit
        # mode so the next x press drives the look drag instead of re-arming.
        self._mode_action.setChecked(False)
        self._refresh_bindings()
        self._auto_save()
        self._status.setText("Camera Movement spot set - x-key drags plant here.")

    def _on_camera_moved(self, nx: float, ny: float) -> None:
        cam = self._profile.camera
        if cam.locked:
            return
        # The view holds the same object, so this moves the drawn ring too.
        cam.nx, cam.ny = nx, ny
        self._update_camera_label()
        self._view.update()
        self._joy_save_timer.start()

    def _on_camera_selected(self) -> None:
        self._status.setText(
            "Camera Movement selected - drag to move it. Lock to freeze it."
        )

    def _on_camera_locked_click(self) -> None:
        self._status.setText(
            "Camera Movement is locked - press 'Lock' in Edit keybinds to unlock."
        )

    def _on_camera_lock_toggled(self, on: bool) -> None:
        if self._editing_locked():
            # Bounce the checkbox back without re-entering this handler.
            self._cam_lock_btn.blockSignals(True)
            self._cam_lock_btn.setChecked(not on)
            self._cam_lock_btn.blockSignals(False)
            return
        self._profile.camera.locked = bool(on)
        self._view.set_camera(self._profile.camera)
        self._update_camera_label()
        self._auto_save()
        self._status.setText(
            "Camera Movement locked." if on else "Camera Movement unlocked."
        )

    def _update_camera_label(self) -> None:
        cam = self._profile.camera
        suffix = " (locked)" if cam.locked else ""
        self._cam_label.setText(
            f"Camera Movement: {cam.nx * 100:.1f}% / {cam.ny * 100:.1f}%{suffix}"
        )

    def _on_binding_placed(self, key: str, nx: float, ny: float) -> None:
        self._profile.remove_key(key)  # placing again moves an existing binding
        binding = Binding(key=key, nx=nx, ny=ny)
        self._profile.bindings.append(binding)
        self._engine.set_profile(self._profile)
        self._clear_pending()
        # Placement is a one-shot task, the same as the pad and the camera
        # spot: leaving edit mode drops the crosshair and -- the important
        # part -- the next press of the new key fires it like any binding
        # instead of silently arming another placement.
        self._mode_action.setChecked(False)
        self._refresh_bindings()
        self._auto_save()
        self._status.setText(f"Bound '{keynames.pretty_name(key)}'")

    def _on_binding_moved(self, key: str, nx: float, ny: float) -> None:
        if self._editing_locked():
            return
        binding = self._profile.binding_for_key(key)
        if binding is None:
            return
        binding.nx, binding.ny = nx, ny
        # Update the two coordinate cells in place rather than calling
        # _refresh_bindings(): this fires on every mouseMoveEvent while a
        # marker is being dragged, and a full refresh does setRowCount plus
        # four setItem per row -- rebuilding the whole table at mouse rate.
        # It also reset the current row's selection mid-drag. The joystick and
        # camera handlers already mutate-and-repaint without a table rebuild;
        # this one now matches them.
        row = self._row_for_key(key)
        if row is not None:
            self._table.item(row, 1).setText(f"{binding.nx * 100:.1f}")
            self._table.item(row, 2).setText(f"{binding.ny * 100:.1f}")
        self._view.update()
        self._joy_save_timer.start()

    def _row_for_key(self, key: str) -> int | None:
        """Table row showing ``key``, or None. Rows follow bindings order."""
        for row, binding in enumerate(self._profile.bindings):
            if binding.key.lower() == key.lower():
                return row
        return None

    def _on_binding_selected(self, key: str) -> None:
        self._status.setText(f"Selected '{keynames.pretty_name(key)}'")

    def _refresh_bindings(self) -> None:
        self._view.set_bindings(self._profile.bindings)
        self._view.set_joystick(self._profile.joystick)
        self._sync_joystick_slider()
        self._view.set_camera(self._profile.camera)
        self._update_camera_label()
        # Sync the Lock toggle without letting it re-enter its own handler.
        # setChecked() emits toggled() whenever the state actually changes, and
        # _on_camera_lock_toggled() ends in _auto_save() + _refresh_profiles().
        # Unblocked, loading a profile whose lock state differed from the button
        # wrote that profile straight back to disk and repopulated the combo --
        # a spurious save on every load. (The comment here used to claim signals
        # were blocked; they were not.) The flag is restored in a finally so a
        # raise cannot leave the button permanently mute.
        self._cam_lock_btn.blockSignals(True)
        try:
            self._cam_lock_btn.setChecked(self._profile.camera.locked)
        finally:
            self._cam_lock_btn.blockSignals(False)
        self._table.setRowCount(len(self._profile.bindings))
        for row, binding in enumerate(self._profile.bindings):
            self._table.setItem(
                row, 0, QTableWidgetItem(keynames.pretty_name(binding.key))
            )
            self._table.setItem(row, 1, QTableWidgetItem(f"{binding.nx * 100:.1f}"))
            self._table.setItem(row, 2, QTableWidgetItem(f"{binding.ny * 100:.1f}"))
            self._table.setItem(row, 3, QTableWidgetItem(binding.mode))

    def _selected_binding(self) -> Binding | None:
        row = self._table.currentRow()
        if row < 0 or row >= len(self._profile.bindings):
            return None
        return self._profile.bindings[row]

    def _on_table_double_clicked(self, item) -> None:
        self._toggle_selected_mode()

    def _toggle_selected_mode(self) -> None:
        if self._editing_locked():
            return
        binding = self._selected_binding()
        if binding is None:
            return
        binding.mode = MODE_HOLD if binding.mode == MODE_TAP else MODE_TAP
        self._refresh_bindings()
        self._auto_save()
        self._status.setText(f"'{binding.display_label}' is now {binding.mode}")

    def _remove_selected_binding(self) -> None:
        if self._editing_locked():
            return
        binding = self._selected_binding()
        if binding is None:
            return
        self._profile.bindings.remove(binding)
        self._engine.set_profile(self._profile)
        self._refresh_bindings()
        self._auto_save()

    def _clear_bindings(self) -> None:
        if self._editing_locked():
            return
        if not self._profile.bindings:
            return
        confirm = QMessageBox.question(
            self,
            "Clear bindings",
            f"Remove all {len(self._profile.bindings)} bindings?",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self._profile.bindings.clear()
        self._engine.set_profile(self._profile)
        self._refresh_bindings()
        self._auto_save()

    # ------------------------------------------------------------------ #
    # Profiles
    # ------------------------------------------------------------------ #

    def _profile_path(self) -> Path | None:
        name = self._profile_combo.currentText().strip()
        return profile_path(name) if name else None

    def _load_profile(self) -> None:
        path = self._profile_path()
        if path is None or not path.is_file():
            QMessageBox.warning(self, "Load profile", "No such profile.")
            return
        try:
            self._profile = Profile.load(path)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "Load profile", str(exc))
            return
        self._engine.set_profile(self._profile)
        self._refresh_bindings()
        self._status.setText(f"Loaded '{self._profile.name}'")

    def _save_profile(self) -> None:
        # The preset is read-only; saving from it means saving a copy, so the
        # name prompt starts one keystroke away from a fresh, editable name.
        read_only = self._profile.read_only
        name, ok = QInputDialog.getText(
            self,
            "Save profile",
            "Profile name:",
            text=f"{self._profile.name} copy" if read_only else self._profile.name,
        )
        if not ok or not name.strip():
            return
        name = name.strip()
        if is_default_profile_name(name):
            self._status.setText(
                "The default keybinds are read-only - pick another name."
            )
            return
        self._profile.name = name
        try:
            self._profile.save()
        except OSError as exc:
            QMessageBox.critical(self, "Save profile", str(exc))
            return
        self._refresh_profiles()
        self._status.setText(f"Saved '{self._profile.name}'")

    def _refresh_profiles(self) -> None:
        current = self._profile_combo.currentText()
        self._profile_combo.clear()
        for path in Profile.list_available():
            self._profile_combo.addItem(path.stem)
        if current:
            self._profile_combo.setCurrentText(current)

    def _auto_save(self) -> None:
        """Keep the active profile on disk so bindings are never lost."""
        if not self._profile.name or self._profile.read_only:
            # The shipped preset never changes on disk -- it is the one state
            # the user can always fall back to.
            return
        try:
            self._profile.save()
            self._refresh_profiles()
        except OSError:
            pass

    # ------------------------------------------------------------------ #

    def closeEvent(self, event) -> None:  # noqa: N802
        self._set_mouse_lock(False)
        self._engine.release_all()
        self._discord_timer.stop()
        clear = getattr(self._discord, "clear", None)
        if clear is not None:
            clear()
        if self._key_dialog is not None:
            self._key_dialog.hide()
        if self._session is not None:
            self._session.stop()
            self._session = None
        super().closeEvent(event)
