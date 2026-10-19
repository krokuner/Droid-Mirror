"""The mirror widget: renders frames and hosts the keybind overlay.

Two render paths:

* **GPU (preferred).** Each frame's Y/U/V planes are uploaded as three single
  channel textures and converted to RGB in a fragment shader. This keeps the
  CPU out of the colour path entirely -- the cheapest way to keep latency down.
* **CPU fallback.** If a shader program or GL context cannot be created, the
  frame is converted with :mod:`droidmirror.color` and blitted as a QImage. The
  app stays usable on machines where GL is unavailable.

The same widget doubles as the keybind editor: in *map* mode a click places the
pending key's marker, and markers can be dragged to fine-tune their position.
"""

from __future__ import annotations

import time

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetrics, QImage, QPainter, QPen
from PySide6.QtOpenGLWidgets import QOpenGLWidget

from ..color import yuv420_to_rgb
from ..decoder import DecodedFrame
from ..keymap import Binding, CameraAnchor, Joystick
from ..protocol import MotionAction, POINTER_ID_GENERIC_FINGER, POINTER_ID_MOUSE
from . import keys as keynames
from .layout import Rect, fit_rect, normalised_to_point, point_to_frame, point_to_normalised

#: Scroll wheel -> flick gesture timing. The delay between DOWN and MOVE is
#: what gives Android a *velocity* to fling with; sending MOVE+UP instantly
#: would read as a teleport and fling at the maximum clamp every notch.
WHEEL_STROKE_MS = 45
#: Per-stroke cap; leftover notches chain into follow-up strokes so a fast
#: spin still scrolls smoothly instead of teleporting the list.
WHEEL_MAX_NOTCHES = 3.0
#: How far one notch flicks, as a fraction of the mirrored screen's height.
WHEEL_REACH_FRACTION = 0.05

#: The drag gesture a game rotates its camera on: a left-button drag on the
#: mirror. It is spelled out once here and replayed by *both* the real mouse
#: and the `x` camera lock, so the two can never drift apart -- a look that
#: only *resembled* this drag (a plain finger, no button state) was silently
#: ignored by the game while the identical real drag worked.
DRAG_POINTER_ID = POINTER_ID_MOUSE
DRAG_PRESS_BUTTON = 1  # android.view.MotionEvent.BUTTON_PRIMARY, on DOWN
DRAG_HELD_BUTTONS = 1  # ...and held for every MOVE until the finger lifts

try:  # PyOpenGL is optional: without it we fall back to the CPU renderer.
    import ctypes

    from OpenGL import GL

    _HAVE_GL = True
except Exception:  # pragma: no cover - depends on the host
    GL = None  # type: ignore[assignment]
    _HAVE_GL = False

VERTEX_SHADER = """
#version 120
attribute vec2 position;
attribute vec2 texcoord;
varying vec2 v_texcoord;
void main() {
    v_texcoord = texcoord;
    gl_Position = vec4(position, 0.0, 1.0);
}
"""

# Limited-range BT.601, matching the CPU path in droidmirror.color.
FRAGMENT_SHADER = """
#version 120
varying vec2 v_texcoord;
uniform sampler2D tex_y;
uniform sampler2D tex_u;
uniform sampler2D tex_v;
void main() {
    float y = texture2D(tex_y, v_texcoord).r;
    float u = texture2D(tex_u, v_texcoord).r;
    float v = texture2D(tex_v, v_texcoord).r;
    float luma = (y - 16.0 / 255.0) * 1.164383;
    float cb = u - 128.0 / 255.0;
    float cr = v - 128.0 / 255.0;
    vec3 rgb = vec3(
        luma + 1.596027 * cr,
        luma - 0.391762 * cb - 0.812968 * cr,
        luma + 2.017232 * cb
    );
    gl_FragColor = vec4(clamp(rgb, 0.0, 1.0), 1.0);
}
"""

#: x, y, u, v -- a triangle strip covering the viewport.
_QUAD = np.array(
    [
        -1.0, -1.0, 0.0, 1.0,
        1.0, -1.0, 1.0, 1.0,
        -1.0, 1.0, 0.0, 0.0,
        1.0, 1.0, 1.0, 0.0,
    ],
    dtype=np.float32,
)

MARKER_RADIUS = 21.0
#: Ring radius for the Camera Movement marker (a touch bigger than a chip).
CAMERA_RADIUS = MARKER_RADIUS * 1.8
FALLBACK_BG = QColor(18, 18, 21)


class MirrorView(QOpenGLWidget):
    """Displays the device screen and the keybind overlay."""

    #: key, nx, ny
    binding_placed = Signal(str, float, float)
    #: key, nx, ny
    binding_moved = Signal(str, float, float)
    #: key
    binding_selected = Signal(str)
    #: nx, ny -- joystick pad dropped on the screen
    joystick_placed = Signal(float, float)
    #: nx, ny -- joystick pad dragged to a new spot
    joystick_moved = Signal(float, float)
    #: joystick pad picked up
    joystick_selected = Signal()
    #: new pad radius (fraction of the shorter frame side) -- rim dragged
    joystick_resized = Signal(float)
    #: nx, ny -- camera movement spot dropped on the screen
    camera_placed = Signal(float, float)
    #: nx, ny -- camera movement spot dragged to a new spot
    camera_moved = Signal(float, float)
    #: camera movement spot picked up
    camera_selected = Signal()
    #: a locked camera spot was clicked (move refused; explain, don't drag)
    camera_locked_click = Signal()
    #: key name
    key_pressed = Signal(str)
    #: key name
    key_released = Signal(str)
    #: composed text for one keystroke (event.text()) -- e.g. "A", "!", "\n".
    #: emitted for bound and unbound keys alike; MainWindow decides whether it
    #: becomes a binding or lands on the device as typed text.
    text_input = Signal(str)
    #: new control target (TARGET_MOUSE / TARGET_KEYMAP)
    control_target_changed = Signal(str)
    #: Ctrl+V -- paste this computer's clipboard onto the device
    paste_computer_clipboard = Signal()
    #: Ctrl+Shift+V -- paste whatever the device itself copied
    paste_device_clipboard = Signal()
    #: latency HUD shown / hidden
    hud_changed = Signal(bool)
    #: keybind/joystick/camera markers shown / hidden (F7)
    overlay_visibility_changed = Signal(bool)

    #: Which input drives the device. Only one is live at a time, so a mouse
    #: click can never fire a stray touch while you are playing with keybinds.
    TARGET_MOUSE = "mouse"
    TARGET_KEYMAP = "keymap"

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumSize(360, 260)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)

        self._frame: DecodedFrame | None = None
        self._bindings: list[Binding] = []
        self._edit_mode = False
        self._control_target = self.TARGET_MOUSE
        self._pending_key: str | None = None
        self._selected: str | None = None
        self._dragging: str | None = None
        self._mouse_active = False
        self._mouse_pos: QPointF | None = None
        #: Set by the window while the cursor is hidden (mouse-look): button
        #: drags are inert, movement deltas steer the camera instead.
        self.pinned_cursor = False
        #: Look callback (dx, dy) -> the window's camera clutch. Fed by
        #: Qt mouse moves: a hidden cursor still delivers them. Only used
        #: when the window cannot read the pointer itself.
        self.look_move = None
        #: Look callback (down: bool) -> the user's own left button while
        #: locked. Their press is the drag; the window only adds movement.
        self.look_button = None
        #: Last seen cursor position while locked (deltas are incremental,
        #: so even a failed re-center warp cannot turn into a spin).
        self._look_last_pos: QPointF | None = None
        #: Where the window last warped the hidden cursor to. Windows echoes a
        #: SetCursorPos back as a real move event; without this the app's own
        #: warp would be read as the user moving the opposite way and the two
        #: would cancel the look out.
        self._look_warp_pos: QPointF | None = None

        # -- scroll wheel -> touch flick ----------------------------------#
        #: Notches waiting to be flicked (bursts coalesce across strokes).
        self._wheel_accum = 0.0
        #: Where the cursor was when the wheel last moved.
        self._wheel_pos = QPointF()
        #: True while a stroke's finger is down (DOWN sent, MOVE/UP pending).
        self._wheel_active = False
        self._wheel_stroke = (0.0, 0.0, 0.0, 0.0)

        # -- on-screen joystick (GameLoop-style WASD pad) -----------------#
        self._joystick: Joystick | None = None
        self._joy_selected = False
        self._dragging_joy = False
        #: True while the rim is being dragged to resize the pad.
        self._resizing_joy = False
        self._pending_joy = False
        #: Where the cursor is while placing, so the ghost pad follows it.
        self._ghost_pos: QPointF | None = None

        # -- camera movement anchor (where the x-key look drag plants) ----#
        self._camera = CameraAnchor()
        self._cam_selected = False
        self._dragging_cam = False
        self._pending_cam = False

        # -- overlay markers (keybinds / joystick / camera) visibility -----#
        #: F7 flips this: when False, the gameplay markers are not drawn so a
        #: clean video feed can be recorded or watched. Editing still works --
        #: toggling markers off never blocks placing or dragging them.
        self._overlay_visible = True

        # -- latency HUD ------------------------------------------------#
        self._hud = False
        #: Decoder backend name ("amf"/"qsv"/"software"), fed by MainWindow.
        self.hud_backend = "-"
        #: End-to-end staleness vs the device clock (seconds), fed by
        #: MainWindow from MirrorSession.video_lag_s. -1 = unknown yet.
        self.hud_lag_s = -1.0
        self._prev_recv_ns = 0
        self._prev_pts_us = 0
        #: Paint timing: start of the current paint, and the last full paint.
        self._paint_start_ns = 0
        self._paint_ns = 0
        self._paint_ms = 0.0

        #: callable(action, pointer_id, x, y, w, h, action_button, buttons)
        self.injector = None

        self._gl_failed = not _HAVE_GL
        self._gl_ready = False
        self._program = 0
        self._textures: list[int] = []
        self._vbo = 0
        self._u_locs: dict[str, int] = {}
        self._attrs: dict[str, int] = {}
        self._gl_error = ""

    # -- state ------------------------------------------------------------ #

    @property
    def frame_size(self) -> tuple[int, int]:
        if self._frame is None:
            return 0, 0
        return self._frame.width, self._frame.height

    @property
    def current_frame(self) -> DecodedFrame | None:
        return self._frame

    @property
    def edit_mode(self) -> bool:
        """True while placing/dragging keybind markers."""
        return self._edit_mode

    @property
    def control_target(self) -> str:
        return self._control_target

    @property
    def render_path(self) -> str:
        """Which renderer is actually in use (shown in the status bar)."""
        if self._gl_failed:
            return "CPU"
        return "GPU" if self._gl_ready else "starting"

    @property
    def hud_enabled(self) -> bool:
        return self._hud

    def set_hud(self, on: bool) -> None:
        """Show or hide the on-screen latency HUD."""
        on = bool(on)
        if on == self._hud:
            return
        self._hud = on
        self.hud_changed.emit(on)
        self.update()

    def toggle_hud(self) -> bool:
        """Flip the HUD; returns its new state."""
        self.set_hud(not self._hud)
        return self._hud

    @property
    def overlay_visible(self) -> bool:
        """Whether the gameplay markers (keybinds, pad, camera) are drawn."""
        return self._overlay_visible

    def set_overlay_visible(self, visible: bool) -> None:
        """Show or hide every keybind/joystick/camera marker on the mirror.

        Purely visual: the engine still injects touches, the pad still steers
        and edit mode still lets you drag markers around -- they are just not
        drawn, so the screen is clean for watching or recording.
        """
        visible = bool(visible)
        if visible == self._overlay_visible:
            return
        self._overlay_visible = visible
        self.overlay_visibility_changed.emit(visible)
        self.update()

    def toggle_overlay(self) -> bool:
        """Flip marker visibility; returns the new state."""
        self.set_overlay_visible(not self._overlay_visible)
        return self._overlay_visible

    def set_edit_mode(self, on: bool) -> None:
        self._edit_mode = bool(on)
        if not on:
            self._selected = None
            self._dragging = None
            self._joy_selected = False
            self._dragging_joy = False
            self._resizing_joy = False
            self._cam_selected = False
            self._dragging_cam = False
        self.setCursor(Qt.CrossCursor if on else Qt.ArrowCursor)
        self.update()

    def set_control_target(self, target: str) -> None:
        """Switch which input drives the device."""
        if target not in (self.TARGET_MOUSE, self.TARGET_KEYMAP):
            raise ValueError(f"unknown control target {target!r}")
        if target == self._control_target:
            return
        # Handing control away mid-click must not leave a finger pressed on the
        # device, or the phone would be stuck in a permanent touch.
        if self._control_target == self.TARGET_MOUSE:
            self._release_mouse()
        self._control_target = target
        self._dragging = None
        self.control_target_changed.emit(target)
        self.update()

    def toggle_control_target(self) -> str:
        """Flip between mouse and keybind control; returns the new target."""
        self.set_control_target(
            self.TARGET_KEYMAP
            if self._control_target == self.TARGET_MOUSE
            else self.TARGET_MOUSE
        )
        return self._control_target

    def set_pending_key(self, key: str | None) -> None:
        self._pending_key = key
        self.update()

    def set_bindings(self, bindings: list[Binding]) -> None:
        self._bindings = list(bindings)
        self.update()

    # -- on-screen joystick ------------------------------------------------ #

    @property
    def joystick(self) -> Joystick | None:
        return self._joystick

    @property
    def pending_joystick(self) -> bool:
        """True while armed: the next click places the joystick pad."""
        return self._pending_joy

    @property
    def pending_key(self) -> str | None:
        """The key armed for placement, or None."""
        return self._pending_key

    def set_joystick(self, joy: Joystick | None) -> None:
        """Update the pad drawn over the game's stick (None = no pad)."""
        self._joystick = joy
        if joy is None:
            self._joy_selected = False
            self._dragging_joy = False
        self.update()

    def set_pending_joystick(self, on: bool) -> None:
        self._pending_joy = bool(on)
        self._ghost_pos = None
        if self._pending_joy:
            self.setCursor(Qt.CrossCursor)
        elif not self._edit_mode:
            self.setCursor(Qt.ArrowCursor)
        self.update()

    # -- camera movement anchor -------------------------------------------- #

    @property
    def camera(self) -> CameraAnchor:
        return self._camera

    @property
    def pending_camera(self) -> bool:
        """True while armed: the next click places the camera spot."""
        return self._pending_cam

    def set_camera(self, camera: CameraAnchor | None) -> None:
        """Update the spot the camera drag plants on (never absent)."""
        self._camera = camera if camera is not None else CameraAnchor()
        self.update()

    def set_pending_camera(self, on: bool) -> None:
        self._pending_cam = bool(on)
        self._ghost_pos = None
        if self._pending_cam:
            self.setCursor(Qt.CrossCursor)
        elif not self._edit_mode:
            self.setCursor(Qt.ArrowCursor)
        self.update()

    def set_frame(self, frame: DecodedFrame) -> None:
        if frame is not self._frame:
            # Keep the previous arrival/pts so the HUD can show frame gaps.
            prev = self._frame
            self._prev_recv_ns = prev.recv_ns if prev is not None else 0
            self._prev_pts_us = prev.pts_us if prev is not None else 0
            self._frame = frame
        self.update()

    def clear(self) -> None:
        self._frame = None
        self.update()

    # -- geometry --------------------------------------------------------- #

    def frame_rect(self) -> Rect:
        fw, fh = self.frame_size
        if fw <= 0 or fh <= 0:
            return Rect(0.0, 0.0, float(self.width()), float(self.height()))
        return fit_rect(fw, fh, self.width(), self.height())

    def _marker_pos(self, binding: Binding) -> QPointF:
        r = self.frame_rect()
        return QPointF(r.x + binding.nx * r.w, r.y + binding.ny * r.h)

    def _label_font(self) -> QFont:
        """The chip font: bold, sized off the marker so it never overflows."""
        font = QFont()
        font.setFamilies(["Segoe UI", "Noto Sans", "Arial"])
        font.setBold(True)
        font.setPixelSize(max(13, int(MARKER_RADIUS * 0.8)))
        return font

    def _marker_label(self, binding: Binding) -> str:
        label = binding.display_label
        return label if len(label) <= 8 else label[:8]

    def _marker_rect(self, binding: Binding) -> QRectF:
        """Pill behind a key label -- drawn size AND hit area, one source.

        Width follows the text (minimum: the old circle's footprint) so long
        labels like SHIFT get a chip that actually contains them, clamped
        inside the widget so edge markers never half-disappear.
        """
        pos = self._marker_pos(binding)
        metrics = QFontMetrics(self._label_font())
        text_w = metrics.horizontalAdvance(self._marker_label(binding))
        width = max(MARKER_RADIUS * 2, text_w + 20)
        height = MARKER_RADIUS * 1.7
        x = pos.x() - width / 2
        y = pos.y() - height / 2
        vw, vh = self.width(), self.height()
        if vw > width:
            x = min(max(x, 2.0), vw - width - 2.0)
        if vh > height:
            y = min(max(y, 2.0), vh - height - 2.0)
        return QRectF(x, y, width, height)

    def _binding_at(self, pos: QPointF) -> Binding | None:
        best: Binding | None = None
        best_d2 = None
        for b in self._bindings:
            if not b.enabled:
                continue
            p = self._marker_pos(b)
            d2 = (p.x() - pos.x()) ** 2 + (p.y() - pos.y()) ** 2
            # The chip rect (or the old circular halo for short labels) owns
            # the click; nearest center wins when chips overlap.
            inside = d2 <= MARKER_RADIUS * MARKER_RADIUS or self._marker_rect(b).contains(pos)
            if inside and (best_d2 is None or d2 < best_d2):
                best, best_d2 = b, d2
        return best

    def _joystick_geom(self) -> tuple[QPointF, float]:
        """Centre and on-screen radius of the pad, in widget pixels."""
        assert self._joystick is not None
        rect = self.frame_rect()
        cx, cy = normalised_to_point(self._joystick.nx, self._joystick.ny, rect)
        return QPointF(cx, cy), self._joystick.radius * min(rect.w, rect.h)

    def _joystick_at(self, pos: QPointF) -> bool:
        if self._joystick is None:
            return False
        centre, radius = self._joystick_geom()
        d = ((centre.x() - pos.x()) ** 2 + (centre.y() - pos.y()) ** 2) ** 0.5
        return d <= radius

    def _joystick_radius_at(self, pos: QPointF) -> bool:
        """Whether ``pos`` is on the pad's rim -- the resize handle.

        The outer band of the ring, wide enough to grab but kept off the inner
        disc so an ordinary drag still *moves* the pad instead of resizing it.
        """
        if self._joystick is None:
            return False
        centre, radius = self._joystick_geom()
        d = ((centre.x() - pos.x()) ** 2 + (centre.y() - pos.y()) ** 2) ** 0.5
        # Ignore a pad too small to have a usable rim (it would swallow the
        # whole disc and every drag would resize).
        if radius < 14:
            return False
        return radius * 0.7 <= d <= radius

    def _radius_fraction_at(self, pos: QPointF) -> float:
        """Widget-pixel distance from the pad centre -> a radius fraction."""
        rect = self.frame_rect()
        shortest = min(rect.w, rect.h)
        if shortest <= 0:
            return 0.12
        centre, _radius = self._joystick_geom()
        d = ((centre.x() - pos.x()) ** 2 + (centre.y() - pos.y()) ** 2) ** 0.5
        return d / shortest

    def _camera_geom(self) -> tuple[QPointF, float]:
        """Centre and on-screen radius of the Camera Movement ring."""
        rect = self.frame_rect()
        cx, cy = normalised_to_point(self._camera.nx, self._camera.ny, rect)
        return QPointF(cx, cy), CAMERA_RADIUS

    def _camera_at(self, pos: QPointF) -> bool:
        centre, radius = self._camera_geom()
        d = ((centre.x() - pos.x()) ** 2 + (centre.y() - pos.y()) ** 2) ** 0.5
        return d <= radius

    # -- GL setup --------------------------------------------------------- #

    def initializeGL(self) -> None:  # noqa: N802 (Qt naming)
        if self._gl_failed:
            return
        try:
            GL.glClearColor(
                FALLBACK_BG.redF(), FALLBACK_BG.greenF(), FALLBACK_BG.blueF(), 1.0
            )
            self._program = self._build_program()
            self._textures = list(GL.glGenTextures(3))
            for tex in self._textures:
                GL.glBindTexture(GL.GL_TEXTURE_2D, tex)
                GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_LINEAR)
                GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_LINEAR)
                GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_S, GL.GL_CLAMP_TO_EDGE)
                GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_T, GL.GL_CLAMP_TO_EDGE)

            self._vbo = GL.glGenBuffers(1)
            GL.glBindBuffer(GL.GL_ARRAY_BUFFER, self._vbo)
            GL.glBufferData(GL.GL_ARRAY_BUFFER, _QUAD.nbytes, _QUAD, GL.GL_STATIC_DRAW)

            self._attrs = {
                "position": GL.glGetAttribLocation(self._program, "position"),
                "texcoord": GL.glGetAttribLocation(self._program, "texcoord"),
            }
            self._u_locs = {
                name: GL.glGetUniformLocation(self._program, name)
                for name in ("tex_y", "tex_u", "tex_v")
            }
            self._gl_ready = True
        except Exception as exc:  # pragma: no cover - host dependent
            self._gl_error = str(exc)
            self._gl_failed = True
            self._gl_ready = False

    def _build_program(self) -> int:
        program = GL.glCreateProgram()
        shaders = []
        for kind, source in (
            (GL.GL_VERTEX_SHADER, VERTEX_SHADER),
            (GL.GL_FRAGMENT_SHADER, FRAGMENT_SHADER),
        ):
            shader = GL.glCreateShader(kind)
            GL.glShaderSource(shader, source)
            GL.glCompileShader(shader)
            if not GL.glGetShaderiv(shader, GL.GL_COMPILE_STATUS):
                log = GL.glGetShaderInfoLog(shader)
                raise RuntimeError(f"shader compile failed: {log}")
            GL.glAttachShader(program, shader)
            shaders.append(shader)
        GL.glLinkProgram(program)
        if not GL.glGetProgramiv(program, GL.GL_LINK_STATUS):
            raise RuntimeError(f"shader link failed: {GL.glGetProgramInfoLog(program)}")
        for shader in shaders:
            GL.glDeleteShader(shader)
        return program

    # -- painting --------------------------------------------------------- #

    def resizeGL(self, w: int, h: int) -> None:  # noqa: N802
        pass

    def paintGL(self) -> None:  # noqa: N802
        self._paint_start_ns = time.perf_counter_ns()
        painter = QPainter(self)
        try:
            if self._gl_ready and not self._gl_failed:
                painter.beginNativePainting()
                try:
                    self._paint_video_gl()
                finally:
                    painter.endNativePainting()
            else:
                self._paint_video_cpu(painter)
            self._paint_overlay(painter)
        finally:
            painter.end()
        now = time.perf_counter_ns()
        self._paint_ns = now
        self._paint_ms = (now - self._paint_start_ns) / 1e6

    def _paint_video_gl(self) -> None:
        GL.glClearColor(
            FALLBACK_BG.redF(), FALLBACK_BG.greenF(), FALLBACK_BG.blueF(), 1.0
        )
        GL.glClear(GL.GL_COLOR_BUFFER_BIT)
        frame = self._frame
        if frame is None:
            return

        # UNPACK_ALIGNMENT is *global* GL state and our planes are tightly
        # packed (widths need not be a multiple of 4), so we set it to 1 for
        # the uploads. It MUST be put back: Qt's QPainter uploads glyph
        # textures for the overlay below and assumes the default alignment of
        # 4, and endNativePainting() does not restore it. Left at 1, every
        # glyph Qt rasterises afterwards is uploaded with the wrong row pitch
        # and comes out smeared/overlapping -- which is exactly what wrecked
        # the latency HUD and the keybind chip labels.
        previous_align = int(GL.glGetIntegerv(GL.GL_UNPACK_ALIGNMENT))
        try:
            GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, 1)
            for index, plane in enumerate(frame.planes):
                GL.glActiveTexture(GL.GL_TEXTURE0 + index)
                GL.glBindTexture(GL.GL_TEXTURE_2D, self._textures[index])
                height, width = plane.shape[:2]
                GL.glTexImage2D(
                    GL.GL_TEXTURE_2D,
                    0,
                    GL.GL_RED,
                    width,
                    height,
                    0,
                    GL.GL_RED,
                    GL.GL_UNSIGNED_BYTE,
                    plane,
                )
        finally:
            GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, previous_align)

        rect = self.frame_rect()
        dpr = self.devicePixelRatioF()
        # GL's origin is bottom-left; Qt's is top-left.
        gl_y = (self.height() - rect.bottom) * dpr
        GL.glViewport(
            int(round(rect.x * dpr)),
            int(round(gl_y)),
            int(round(rect.w * dpr)),
            int(round(rect.h * dpr)),
        )

        GL.glUseProgram(self._program)
        for index, name in enumerate(("tex_y", "tex_u", "tex_v")):
            GL.glActiveTexture(GL.GL_TEXTURE0 + index)
            GL.glBindTexture(GL.GL_TEXTURE_2D, self._textures[index])
            GL.glUniform1i(self._u_locs[name], index)

        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, self._vbo)
        stride = 4 * 4
        GL.glEnableVertexAttribArray(self._attrs["position"])
        GL.glVertexAttribPointer(
            self._attrs["position"], 2, GL.GL_FLOAT, GL.GL_FALSE, stride,
            ctypes.c_void_p(0),
        )
        GL.glEnableVertexAttribArray(self._attrs["texcoord"])
        GL.glVertexAttribPointer(
            self._attrs["texcoord"], 2, GL.GL_FLOAT, GL.GL_FALSE, stride,
            ctypes.c_void_p(2 * 4),
        )
        GL.glDrawArrays(GL.GL_TRIANGLE_STRIP, 0, 4)
        GL.glDisableVertexAttribArray(self._attrs["position"])
        GL.glDisableVertexAttribArray(self._attrs["texcoord"])

    def _paint_video_cpu(self, painter: QPainter) -> None:
        painter.fillRect(self.rect(), FALLBACK_BG)
        frame = self._frame
        if frame is None:
            return
        rgb = yuv420_to_rgb(frame.y, frame.u, frame.v)
        image = QImage(
            rgb.data, frame.width, frame.height, frame.width * 3,
            QImage.Format.Format_RGB888,
        )
        rect = self.frame_rect()
        # drawImage scales on the fly; QImage.scaled() would copy every frame.
        painter.drawImage(QRectF(rect.x, rect.y, rect.w, rect.h), image)

    def _paint_overlay(self, painter: QPainter) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        font = QFont()
        font.setPointSize(9)
        font.setBold(True)
        painter.setFont(font)

        if self._edit_mode:
            rect = self.frame_rect()
            painter.setPen(QPen(QColor(255, 190, 60, 180), 1, Qt.PenStyle.DashLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(
                int(rect.x), int(rect.y), int(rect.w), int(rect.h)
            )

        # The pad sits under the key markers so bound points stay clickable.
        # F7 hides every gameplay marker for a clean feed; the edit frame and
        # the HUD stay (diagnostics, not markers). Hiding is cosmetic only --
        # touch injection and marker editing are untouched.
        if self._overlay_visible:
            if self._joystick is not None:
                self._paint_joystick(painter, self._joystick)
            if self._pending_joy and self._ghost_pos is not None and self._joystick is None:
                # Placing the first pad: show it under the cursor before the click.
                ghost = Joystick(nx=0.5, ny=0.5)
                self._paint_joystick(painter, ghost, at=self._ghost_pos, ghost=True)

            # The camera spot sits under the key markers for the same reason.
            #
            # Only drawn when it is actually in play. A profile with no
            # bindings and no pad has nothing to map, so a CAM ring parked in
            # the middle of the screen was pure clutter over the thing you
            # are mirroring. Edit mode always draws it -- that is where you
            # drag it into place.
            if self._edit_mode or self._bindings or self._joystick is not None:
                self._paint_camera(painter, self._camera)
            if self._pending_cam and self._ghost_pos is not None:
                self._paint_camera(painter, CameraAnchor(), at=self._ghost_pos, ghost=True)

            for binding in self._bindings:
                if not binding.enabled:
                    continue
                self._paint_marker(painter, binding)

        if self._hud:
            self._paint_hud(painter)

    def _paint_joystick(
        self,
        painter: QPainter,
        joy: Joystick,
        *,
        at: QPointF | None = None,
        ghost: bool = False,
    ) -> None:
        """The translucent WASD pad drawn over the game's own stick."""
        if at is None:
            centre, radius = self._joystick_geom()
        else:
            rect = self.frame_rect()
            centre = at
            radius = joy.radius * min(rect.w, rect.h)
        if radius <= 1:
            return

        selected = self._joy_selected and not ghost
        if selected:
            edge = QColor(255, 190, 60, 230)
        elif ghost:
            edge = QColor(255, 190, 60, 150)
        else:
            edge = QColor(255, 255, 255, 110)
        painter.setBrush(QBrush(QColor(255, 255, 255, 14 if ghost else 26)))
        painter.setPen(
            QPen(edge, 2.5, Qt.PenStyle.DashLine if ghost else Qt.PenStyle.SolidLine)
        )
        painter.drawEllipse(centre, radius, radius)

        # Centre hub -- where the finger rests at neutral.
        painter.setBrush(QBrush(QColor(255, 255, 255, 150)))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(centre, 7, 7)

        # WASD labels at the four stops, GameLoop-style.
        if radius >= 40:
            pad_font = QFont()
            pad_font.setFamilies(["Segoe UI", "Noto Sans", "Arial"])
            pad_font.setBold(True)
            pad_font.setPixelSize(max(12, int(radius * 0.26)))
            painter.setFont(pad_font)
            metrics = painter.fontMetrics()
            offset = radius * 0.62
            for label, dx, dy in (
                ("W", 0.0, -1.0),
                ("A", -1.0, 0.0),
                ("S", 0.0, 1.0),
                ("D", 1.0, 0.0),
            ):
                lx = centre.x() + dx * offset
                ly = centre.y() + dy * offset
                w = metrics.horizontalAdvance(label)
                base = QPointF(lx - w / 2, ly + metrics.ascent() / 2 - 2)
                painter.setPen(QPen(QColor(0, 0, 0, 200)))
                for ox, oy in ((-1.0, 0.0), (1.0, 0.0), (0.0, -1.0), (0.0, 1.0)):
                    painter.drawText(
                        QPointF(base.x() + ox, base.y() + oy), label
                    )
                painter.setPen(QPen(QColor(255, 255, 255, 235)))
                painter.drawText(base, label)

    def _paint_camera(
        self,
        painter: QPainter,
        camera: CameraAnchor,
        *,
        at: QPointF | None = None,
        ghost: bool = False,
    ) -> None:
        """Ring + pill marking where the x-key camera drag plants its finger."""
        if at is None:
            centre, radius = self._camera_geom()
        else:
            centre, radius = at, CAMERA_RADIUS
        locked = camera.locked and not ghost
        selected = self._cam_selected and not ghost

        if selected:
            edge = QColor(255, 190, 60, 230)
        elif locked:
            edge = QColor(150, 160, 180, 190)
        elif ghost:
            edge = QColor(140, 200, 255, 150)
        else:
            edge = QColor(140, 200, 255, 220)
        painter.setBrush(QBrush(QColor(140, 200, 255, 18 if ghost else 34)))
        painter.setPen(
            QPen(
                edge,
                2.5,
                Qt.PenStyle.DashLine if (ghost or locked) else Qt.PenStyle.SolidLine,
            )
        )
        painter.drawEllipse(centre, radius, radius)

        # Centre dot: the exact plant point.
        painter.setBrush(QBrush(QColor(255, 255, 255, 180)))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(centre, 4, 4)

        # CAM pill under the ring, same chip metrics as the key markers.
        label = "CAM"
        font = self._label_font()
        painter.setFont(font)
        metrics = painter.fontMetrics()
        width = max(MARKER_RADIUS * 2, metrics.horizontalAdvance(label) + 20)
        height = MARKER_RADIUS * 1.7
        pill = QRectF(
            centre.x() - width / 2, centre.y() + radius + 4, width, height
        )
        if selected:
            fill = QColor(90, 70, 20, 230)
        elif locked:
            fill = QColor(60, 64, 74, 220)
        else:
            fill = QColor(30, 60, 90, 220)
        painter.setBrush(QBrush(fill))
        painter.setPen(QPen(QColor(0, 0, 0, 175), 1.5))
        painter.drawRoundedRect(pill, pill.height() / 2, pill.height() / 2)
        tx = pill.center().x() - metrics.horizontalAdvance(label) / 2
        ty = pill.center().y() + (metrics.ascent() - metrics.descent()) / 2
        painter.setPen(QPen(QColor(0, 0, 0, 200)))
        for ox, oy in ((-1.0, 0.0), (1.0, 0.0), (0.0, -1.0), (0.0, 1.0)):
            painter.drawText(QPointF(tx + ox, ty + oy), label)
        painter.setPen(QPen(QColor(255, 255, 255)))
        painter.drawText(QPointF(tx, ty), label)

        if locked:
            self._paint_lock_glyph(
                painter, centre + QPointF(radius * 0.72, -radius * 0.72)
            )

    def _paint_lock_glyph(self, painter: QPainter, at: QPointF) -> None:
        """A tiny padlock so a frozen camera spot reads at a glance."""
        body = QRectF(at.x() - 7, at.y() - 2, 14, 11)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(230, 235, 245, 235))
        painter.drawRoundedRect(body, 2.5, 2.5)
        painter.setPen(QPen(QColor(230, 235, 245, 235), 2.2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawArc(QRectF(at.x() - 4.5, at.y() - 7, 9, 9), 0, 180 * 16)
        painter.setBrush(QColor(40, 44, 56, 255))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QPointF(at.x(), at.y() + 3.5), 1.6, 1.6)

    def hud_lines(self) -> list[str]:
        """Latency figures for the HUD, newest frame against the last one.

        Pure computation over state the view already holds, so tests can assert
        on the numbers without a GL context.
        """
        now = time.perf_counter_ns()
        paint_ms = (now - self._paint_start_ns) / 1e6 if self._paint_start_ns else 0.0
        lines = [f"render  {self.render_path}   paint {paint_ms:5.1f} ms"]
        frame = self._frame
        if frame is None:
            lines.append("waiting for frames...")
            return lines

        lines.append(f"backend {self.hud_backend}")
        if self.hud_lag_s >= 0:
            lines.append(f"lag    {self.hud_lag_s:6.2f} s")
        if frame.decode_ms > 0:
            lines.append(f"decode  {frame.decode_ms:5.2f} ms")
        if frame.recv_ns and self._prev_recv_ns and frame.recv_ns > self._prev_recv_ns:
            lines.append(f"arrive  {(frame.recv_ns - self._prev_recv_ns) / 1e6:5.1f} ms")
        if frame.pts_us and self._prev_pts_us:
            delta = frame.pts_us - self._prev_pts_us
            # The device clock restarts on session changes; ignore nonsense.
            if 0 < delta < 10_000_000:
                lines.append(f"device  {delta / 1000:5.1f} ms")
        if frame.decoded_ns and now > frame.decoded_ns:
            lines.append(f"queue   {(now - frame.decoded_ns) / 1e6:5.1f} ms")
        if frame.recv_ns and now > frame.recv_ns:
            lines.append(f"age     {(now - frame.recv_ns) / 1e6:5.1f} ms")
        return lines

    def _paint_hud(self, painter: QPainter) -> None:
        """Semi-transparent readout in the top-left corner."""
        hud_font = QFont()
        hud_font.setPointSize(9)
        hud_font.setBold(True)
        painter.setFont(hud_font)  # markers/pad set bigger fonts mid-overlay
        lines = self.hud_lines()
        metrics = painter.fontMetrics()
        pad = 8
        text_w = max(metrics.horizontalAdvance(line) for line in lines)
        line_h = metrics.height()
        box = QRectF(8, 8, text_w + pad * 2, line_h * len(lines) + pad * 2)
        painter.setPen(Qt.PenStyle.NoPen)
        # Near-opaque: a bright, noisy game frame bleeding through the box
        # (alpha 170) turned the readout into an unreadable smear.
        painter.setBrush(QColor(0, 0, 0, 244))
        painter.drawRoundedRect(box, 5, 5)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        painter.setPen(QPen(QColor(170, 255, 200)))
        y = box.y() + pad + metrics.ascent()
        for line in lines:
            painter.drawText(QPointF(box.x() + pad, y), line)
            y += line_h

    def _paint_marker(self, painter: QPainter, binding: Binding) -> None:
        rect = self._marker_rect(binding)
        selected = binding.key == self._selected
        pending = binding.key == self._pending_key

        if pending:
            fill = QColor(255, 190, 60, 240)
        elif binding.mode == "hold":
            fill = QColor(70, 150, 255, 235)
        else:
            fill = QColor(60, 200, 130, 235)
        edge = QColor(255, 255, 255, 245) if selected else QColor(0, 0, 0, 175)

        painter.setBrush(QBrush(fill))
        painter.setPen(QPen(edge, 3 if selected else 1.5))
        painter.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)

        label = self._marker_label(binding)
        painter.setFont(self._label_font())
        metrics = painter.fontMetrics()
        tx = rect.center().x() - metrics.horizontalAdvance(label) / 2
        ty = rect.center().y() + (metrics.ascent() - metrics.descent()) / 2
        # Dark outline first, then white: readable over any game art, with
        # no dependency on the chip's fill surviving under it.
        painter.setPen(QPen(QColor(0, 0, 0, 200)))
        for ox, oy in ((-1.0, 0.0), (1.0, 0.0), (0.0, -1.0), (0.0, 1.0)):
            painter.drawText(QPointF(tx + ox, ty + oy), label)
        painter.setPen(QPen(QColor(255, 255, 255)))
        painter.drawText(QPointF(tx, ty), label)

    # -- interaction ------------------------------------------------------ #

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self.setFocus()
        pos = QPointF(event.position())

        if self._edit_mode:
            # Arming the pad wins over everything: the click IS the placement.
            if self._pending_joy:
                rect = self.frame_rect()
                nx, ny = point_to_normalised(pos.x(), pos.y(), rect)
                self._pending_joy = False
                self._ghost_pos = None
                self.joystick_placed.emit(nx, ny)
                self.update()
                return
            # Arming the camera spot: same one-shot placement.
            if self._pending_cam:
                rect = self.frame_rect()
                nx, ny = point_to_normalised(pos.x(), pos.y(), rect)
                self._pending_cam = False
                self._ghost_pos = None
                self.camera_placed.emit(nx, ny)
                self.update()
                return

            hit = self._binding_at(pos)
            if hit is not None:
                self._selected = hit.key
                self._joy_selected = False
                self._cam_selected = False
                self._dragging = hit.key
                self.binding_selected.emit(hit.key)
                self.update()
            elif self._pending_key:
                rect = self.frame_rect()
                nx, ny = point_to_normalised(pos.x(), pos.y(), rect)
                self.binding_placed.emit(self._pending_key, nx, ny)
            elif self._joystick_at(pos):
                # Pad picked up. The rim resizes, the inner disc moves.
                self._selected = None
                self._joy_selected = True
                self._cam_selected = False
                self._resizing_joy = self._joystick_radius_at(pos)
                self._dragging_joy = not self._resizing_joy
                self.joystick_selected.emit()
                self.update()
            elif self._camera_at(pos):
                self._selected = None
                self._joy_selected = False
                if self._camera.locked:
                    # Frozen on purpose: explain instead of dragging.
                    self._cam_selected = False
                    self.camera_locked_click.emit()
                else:
                    self._cam_selected = True
                    self._dragging_cam = True
                    self.camera_selected.emit()
                self.update()
            else:
                self._selected = None
                self._joy_selected = False
                self._cam_selected = False
                self.update()
            return

        # In keybind mode the mouse is deliberately inert, so a stray click
        # cannot inject a touch while you are playing. A bound mouse button
        # is the exception: the click *is* its keybind and fires the tap.
        if self._control_target != self.TARGET_MOUSE:
            name = keynames.mouse_button_name(event.button())
            if name is not None:
                self.key_pressed.emit(name)
            return

        # Locked = mouse-look. The pointer is parked, so the click lands
        # wherever it was parked; the *button* is still real and the window
        # hangs the camera drag off it, so holding the left button works
        # exactly like holding it for a real drag.
        if self.pinned_cursor:
            if event.button() == Qt.MouseButton.LeftButton and self.look_button:
                self.look_button(True)
            return

        self._mouse_active = True
        self._mouse_pos = pos
        self._inject_mouse_at(
            pos,
            MotionAction.DOWN,
            action_button=DRAG_PRESS_BUTTON,
            buttons=DRAG_HELD_BUTTONS,
        )

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        pos = QPointF(event.position())
        if self._edit_mode:
            if self._dragging:
                rect = self.frame_rect()
                nx, ny = point_to_normalised(pos.x(), pos.y(), rect)
                self.binding_moved.emit(self._dragging, nx, ny)
                self.update()
            elif self._resizing_joy and self._joystick is not None:
                # Rim drag: the radius follows the pointer's distance from
                # the pad's centre, so the pad grows and shrinks under it.
                self.joystick_resized.emit(self._radius_fraction_at(pos))
            elif self._dragging_joy and self._joystick is not None:
                rect = self.frame_rect()
                nx, ny = point_to_normalised(pos.x(), pos.y(), rect)
                self.joystick_moved.emit(nx, ny)
            elif self._dragging_cam and not self._camera.locked:
                rect = self.frame_rect()
                nx, ny = point_to_normalised(pos.x(), pos.y(), rect)
                self.camera_moved.emit(nx, ny)
            elif self._pending_joy and self._joystick is None and self._ghost_pos != pos:
                # Hover preview: the ghost pad trails the cursor before the click.
                self._ghost_pos = pos
                self.update()
            elif self._pending_cam and self._ghost_pos != pos:
                # Hover preview for the camera spot as well.
                self._ghost_pos = pos
                self.update()
            return

        if self.pinned_cursor:
            # Hidden cursor, free to move: every step away from the last
            # position is a look delta. The window warps the cursor back to
            # center right after, so the next delta starts fresh.
            last, self._look_last_pos = self._look_last_pos, pos
            if self._look_warp_pos is not None and pos == self._look_warp_pos:
                # Our own warp coming back as a move event: a baseline, not a
                # delta. Counting it would undo the movement that caused it.
                return
            if self.look_move is not None and last is not None:
                dx = pos.x() - last.x()
                dy = pos.y() - last.y()
                if dx or dy:
                    self.look_move(dx, dy)
            return
        if self._control_target == self.TARGET_MOUSE and self._mouse_active:
            self._mouse_pos = pos
            self._inject_mouse_at(
                pos,
                MotionAction.MOVE,
                action_button=0,
                buttons=DRAG_HELD_BUTTONS,
            )

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._edit_mode:
            self._dragging = None
            self._dragging_joy = False
            self._resizing_joy = False
            self._dragging_cam = False
            return
        if self._control_target == self.TARGET_KEYMAP:
            # The release half of a bound mouse button, so holds track it.
            name = keynames.mouse_button_name(event.button())
            if name is not None:
                self.key_released.emit(name)
            return
        if self.pinned_cursor:
            if event.button() == Qt.MouseButton.LeftButton and self.look_button:
                self.look_button(False)
            return
        if self._control_target == self.TARGET_MOUSE and self._mouse_active:
            self._mouse_pos = QPointF(event.position())
            self._release_mouse()

    def _inject_mouse_at(
        self, pos: QPointF, action: MotionAction, action_button: int, buttons: int
    ) -> None:
        if self.injector is None:
            return
        fw, fh = self.frame_size
        if fw <= 0 or fh <= 0:
            return
        rect = self.frame_rect()
        x, y = point_to_frame(pos.x(), pos.y(), rect, fw, fh)
        self.injector(action, DRAG_POINTER_ID, x, y, fw, fh, action_button, buttons)

    def note_warp(self, pos: QPointF | None) -> None:
        """Record where the window just parked the hidden cursor.

        The matching move event that follows is the warp echoing back, not a
        gesture, and ``mouseMoveEvent`` skips it. ``None`` forgets it again
        (unlocking moves the pointer for real).
        """
        self._look_warp_pos = None if pos is None else QPointF(pos)

    def _release_mouse(self) -> None:
        """Lift the mouse finger, if one is currently down."""
        if not self._mouse_active:
            return
        pos, self._mouse_pos = self._mouse_pos, None
        self._mouse_active = False
        if pos is not None:
            self._inject_mouse_at(pos, MotionAction.UP, action_button=0, buttons=0)

    def wheelEvent(self, event) -> None:  # noqa: N802
        """Scroll wheel -> a flick gesture on the device (scrcpy-style).

        Works in both control modes: a wheel notch involves no held button,
        so there is nothing for the mouse/keybind split to guard against.
        Edit mode ignores it -- arranging markers must not scroll lists.
        """
        delta = event.angleDelta().y()
        if delta and not self._edit_mode:
            if not self.hasFocus():
                self.setFocus()  # typing follows the wheel from here on
            self._note_wheel(delta / 120.0, QPointF(event.position()))
            event.accept()
            return
        event.ignore()

    def _note_wheel(self, notches: float, pos: QPointF) -> None:
        """Accumulate wheel motion; start a flick stroke when idle.

        Each stroke is a short finger drag at the cursor: DOWN, a beat, then
        MOVE + UP. Bursts coalesce -- notches that arrive mid-stroke pile up
        and chain into the next stroke, capped so a fast spin scrolls far
        rather than teleporting.
        """
        if self.injector is None or self._edit_mode:
            return
        self._wheel_accum += notches
        self._wheel_pos = pos
        if not self._wheel_active:
            self._start_stroke()

    def _start_stroke(self) -> None:
        if self._wheel_accum == 0.0 or self.injector is None:
            return
        fw, fh = self.frame_size
        if fw <= 0 or fh <= 0:
            self._wheel_accum = 0.0
            return

        notches = max(
            -WHEEL_MAX_NOTCHES, min(WHEEL_MAX_NOTCHES, self._wheel_accum)
        )
        self._wheel_accum -= notches

        rect = self.frame_rect()
        x, y = point_to_frame(self._wheel_pos.x(), self._wheel_pos.y(), rect, fw, fh)
        # Qt: positive delta = wheel up = show content above = content moves
        # down = +y in screen coordinates. Same sign, no flip.
        dy = notches * WHEEL_REACH_FRACTION * fh
        x_end = max(0.0, min(float(fw), float(x)))
        y_end = max(0.0, min(float(fh), y + dy))
        if abs(y_end - y) < 1.0:
            self._start_stroke()  # degenerate (clamped flat): try the next chunk
            return

        self._wheel_active = True
        self._wheel_stroke = (float(x), float(y), x_end, y_end)
        self.injector(
            MotionAction.DOWN, POINTER_ID_GENERIC_FINGER, int(x), int(y),
            fw, fh, 0, 0,
        )
        QTimer.singleShot(WHEEL_STROKE_MS, self._finish_stroke)

    def _finish_stroke(self) -> None:
        if not self._wheel_active:
            return  # already finished (double timer / edit-mode exit)
        self._wheel_active = False
        if self.injector is None:
            return
        fw, fh = self.frame_size
        if fw <= 0 or fh <= 0:
            return
        _x0, _y0, x1, y1 = self._wheel_stroke
        self.injector(
            MotionAction.MOVE, POINTER_ID_GENERIC_FINGER, int(x1), int(y1),
            fw, fh, 0, 0,
        )
        self.injector(
            MotionAction.UP, POINTER_ID_GENERIC_FINGER, int(x1), int(y1),
            fw, fh, 0, 0,
        )
        if self._wheel_accum != 0.0:
            self._start_stroke()  # chain whatever piled up during this stroke

    def keyPressEvent(self, event) -> None:  # noqa: N802
        # F7/F9 are handled here rather than left to the toolbar actions:
        # in fullscreen the toolbar is hidden, and a hidden toolbar's action
        # shortcut never fires -- so F7 (and the latency HUD) silently did
        # nothing. The view is always the focused widget, so catching them
        # here makes them work windowed, fullscreen, and with the chrome gone.
        if not event.isAutoRepeat():
            if event.key() == keynames.OVERLAY_KEY_CODE:
                self.toggle_overlay()
                event.accept()
                return
            if event.key() == keynames.HUD_KEY_CODE:
                self.toggle_hud()
                event.accept()
                return

        # Clipboard hotkeys are checked before the binding/typing path so they
        # can never arm a binding, fire a keybind, or type a control character
        # (Ctrl+V's event.text() is a raw \x16). Plain V still types/binds.
        if event.key() == Qt.Key.Key_V and event.modifiers() & Qt.ControlModifier:
            if not event.isAutoRepeat():
                if event.modifiers() & Qt.ShiftModifier:
                    self.paste_device_clipboard.emit()
                else:
                    self.paste_computer_clipboard.emit()
            event.accept()
            return

        name = keynames.key_name(event.key(), event.text())
        if name and keynames.is_bindable(name):
            if not event.isAutoRepeat():
                self.key_pressed.emit(name)
            # The composed character rides along even for auto-repeat, so
            # holding Backspace keeps deleting like any normal text field.
            text = event.text()
            if text:
                self.text_input.emit(text)
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event) -> None:  # noqa: N802
        name = keynames.key_name(event.key(), event.text())
        if name and keynames.is_bindable(name):
            if not event.isAutoRepeat():
                self.key_released.emit(name)
            event.accept()
            return
        super().keyReleaseEvent(event)
