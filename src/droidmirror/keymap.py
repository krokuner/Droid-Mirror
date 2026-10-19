"""Keybind mapping: bind a host key to a point on the phone screen.

Coordinates are stored **normalised** (0..1) relative to the video frame rather
than in raw pixels. The frame size changes with rotation, with ``max_size``
downscaling and with device resolution, so normalised points survive all of
those; they are converted to frame pixels only at injection time.

The scrcpy control protocol expects touch coordinates in *frame* space
together with the frame size -- the server then maps them onto the real
display (see ``PositionMapper`` on the device side) -- which is exactly what
:meth:`Binding.to_frame_point` produces.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from .config import profiles_dir
from .protocol import MotionAction

# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #

MODE_TAP = "tap"    # press = a quick down+up (buttons, skills)
MODE_HOLD = "hold"  # press = finger down until release (joysticks, movement)

VALID_MODES = (MODE_TAP, MODE_HOLD)

#: Finger ids handed out to simultaneously held bindings. scrcpy reserves the
#: negative space (-1 mouse, -2 generic finger, -3 virtual finger), so synthetic
#: fingers start at 0 exactly like real ones.
FIRST_POINTER_ID = 0

#: Keys the on-screen pad consumes while it exists (checked before bindings,
#: so a stray marker under the pad cannot steal them).
JOYSTICK_KEYS = ("w", "a", "s", "d")

#: Unit step per key in frame space (y grows downward).
_JOY_DIRS: dict[str, tuple[float, float]] = {
    "w": (0.0, -1.0),
    "s": (0.0, 1.0),
    "a": (-1.0, 0.0),
    "d": (1.0, 0.0),
}


def normalised_to_frame(
    nx: float, ny: float, frame_width: int, frame_height: int
) -> tuple[int, int]:
    """The one place a normalised (0..1) point becomes frame pixels.

    ``Binding``, ``Joystick`` and ``CameraAnchor`` each carried their own copy
    of this arithmetic, with a fourth in ``ui.layout.point_to_frame``. They
    have to agree exactly -- a binding saved against one formula and replayed
    under another would tap a pixel or two off -- so they all route through
    here now.
    """
    x = int(round(nx * max(frame_width - 1, 0)))
    y = int(round(ny * max(frame_height - 1, 0)))
    return x, y


@dataclass
class Binding:
    """A single key -> screen point mapping."""

    key: str
    nx: float
    ny: float
    label: str = ""
    mode: str = MODE_TAP
    enabled: bool = True

    def __post_init__(self) -> None:
        self.nx = min(1.0, max(0.0, float(self.nx)))
        self.ny = min(1.0, max(0.0, float(self.ny)))
        if self.mode not in VALID_MODES:
            raise ValueError(f"invalid mode {self.mode!r}, expected one of {VALID_MODES}")

    @property
    def display_label(self) -> str:
        return self.label or self.key.upper()

    def to_frame_point(self, frame_width: int, frame_height: int) -> tuple[int, int]:
        """Convert the normalised point to frame pixels."""
        return normalised_to_frame(self.nx, self.ny, frame_width, frame_height)

    @classmethod
    def from_frame_point(
        cls,
        key: str,
        x: float,
        y: float,
        frame_width: int,
        frame_height: int,
        **kwargs,
    ) -> "Binding":
        nx = x / max(frame_width - 1, 1)
        ny = y / max(frame_height - 1, 1)
        return cls(key=key, nx=nx, ny=ny, **kwargs)


@dataclass
class Joystick:
    """On-screen WASD pad placed over a game's virtual stick.

    One touch finger is owned by the pad. It rests at the centre (neutral)
    while nothing is pressed and slides toward the rim as W/A/S/D go down --
    the standard keyboard-to-virtual-joystick mapping. Coordinates are
    normalised like bindings, so the pad survives rotation and resolution
    changes; ``radius`` is a fraction of the shorter frame side.
    """

    nx: float = 0.5
    ny: float = 0.5
    #: Reach in half-screens; clamped to something a game can actually read.
    radius: float = 0.12

    def __post_init__(self) -> None:
        self.nx = min(1.0, max(0.0, float(self.nx)))
        self.ny = min(1.0, max(0.0, float(self.ny)))
        self.radius = min(0.5, max(0.02, float(self.radius)))

    def centre_px(self, frame_width: int, frame_height: int) -> tuple[int, int]:
        return normalised_to_frame(self.nx, self.ny, frame_width, frame_height)

    def radius_px(self, frame_width: int, frame_height: int) -> float:
        return self.radius * min(frame_width, frame_height)

    def deflect_px(
        self, keys_held: set[str], frame_width: int, frame_height: int
    ) -> tuple[int, int]:
        """Where the pad's finger sits for the given held keys, in frame px.

        The summed direction is normalised so diagonals match straight-line
        speed, then scaled to the rim and clamped inside the frame.
        """
        cx, cy = self.centre_px(frame_width, frame_height)
        dx = dy = 0.0
        for key in keys_held:
            step = _JOY_DIRS.get(key)
            if step is not None:
                dx += step[0]
                dy += step[1]
        length = math.hypot(dx, dy)
        if length > 1e-6:
            dx, dy = dx / length, dy / length
        reach = self.radius_px(frame_width, frame_height)
        x = min(max(cx + dx * reach, 0), max(frame_width - 1, 0))
        y = min(max(cy + dy * reach, 0), max(frame_height - 1, 0))
        return int(round(x)), int(round(y))


@dataclass
class CameraAnchor:
    """Where the camera drag (mouse-look on ``x``) plants its finger.

    Normalised like bindings, so it survives rotation and resolution
    changes. ``locked`` freezes the spot while editing keybinds: the marker
    on the mirror refuses to be dragged until it is unlocked again.
    """

    nx: float = 0.5
    ny: float = 0.5
    locked: bool = False

    def __post_init__(self) -> None:
        self.nx = min(1.0, max(0.0, float(self.nx)))
        self.ny = min(1.0, max(0.0, float(self.ny)))
        self.locked = bool(self.locked)

    def to_frame_point(self, frame_width: int, frame_height: int) -> tuple[int, int]:
        """Convert the normalised point to frame pixels."""
        return normalised_to_frame(self.nx, self.ny, frame_width, frame_height)


@dataclass
class Profile:
    """A named collection of bindings, optionally tied to an app package."""

    name: str = "default"
    package: str = ""
    bindings: list[Binding] = field(default_factory=list)
    #: Optional on-screen WASD pad (None = profile has no joystick).
    joystick: Joystick | None = None
    #: Where the camera drag plants (always present; default = the middle).
    camera: CameraAnchor = field(default_factory=CameraAnchor)

    # -- queries ---------------------------------------------------------- #

    def binding_for_key(self, key: str) -> Binding | None:
        key = key.lower()
        for b in self.bindings:
            if b.enabled and b.key.lower() == key:
                return b
        return None

    def key_in_use(self, key: str, exclude: Binding | None = None) -> bool:
        key = key.lower()
        return any(
            b is not exclude and b.key.lower() == key for b in self.bindings
        )

    def remove_key(self, key: str) -> None:
        self.bindings = [b for b in self.bindings if b.key.lower() != key.lower()]

    @property
    def read_only(self) -> bool:
        """Whether this is the shipped preset, which the UI never writes.

        The default profile is the fixed fallback every user can come back
        to; edits only ever go to a copy saved under the user's own name.
        """
        return is_default_profile_name(self.name)

    # -- persistence ------------------------------------------------------ #

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "package": self.package,
            "bindings": [asdict(b) for b in self.bindings],
            # Older profiles never had a pad; None keeps the key stable.
            "joystick": asdict(self.joystick) if self.joystick is not None else None,
            # Always present, so an old profile gains the default middle spot.
            "camera": asdict(self.camera),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Profile":
        bindings = [Binding(**b) for b in data.get("bindings", [])]
        joy_raw = data.get("joystick")
        joystick = Joystick(**joy_raw) if isinstance(joy_raw, dict) else None
        cam_raw = data.get("camera")
        camera = CameraAnchor(**cam_raw) if isinstance(cam_raw, dict) else CameraAnchor()
        return cls(
            name=data.get("name", "default"),
            package=data.get("package", ""),
            bindings=bindings,
            joystick=joystick,
            camera=camera,
        )

    def save(self, path: Path | None = None) -> Path:
        path = path or profile_path(self.name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path) -> "Profile":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    @classmethod
    def list_available(cls) -> list[Path]:
        return sorted(profiles_dir().glob("*.json"))


def _slug(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", name).strip("-").lower() or "profile"


def profile_path(name: str) -> Path:
    """Canonical on-disk location for a profile name."""
    return profiles_dir() / f"{_slug(name)}.json"


#: Name of the shipped preset profile. The UI treats it as read-only: it is
#: the fixed fallback every user can return to, so edits only ever go to a
#: copy saved under the user's own name.
DEFAULT_PROFILE_NAME = "default"


def is_default_profile_name(name: str) -> bool:
    """Whether a profile name maps to the shipped preset's file."""
    return _slug(name) == DEFAULT_PROFILE_NAME


# --------------------------------------------------------------------------- #
# Injection engine
# --------------------------------------------------------------------------- #

#: (action, pointer_id, x, y, frame_width, frame_height)
InjectFn = Callable[[MotionAction, int, int, int, int, int], None]


class KeymapEngine:
    """Turns host key press/release events into touch down/up on the device.

    Holds are tracked so that several bindings can be held at once, each with
    its own finger id -- that is what makes a WASD-style joystick plus an
    action button work simultaneously.

    Frame size is supplied lazily by ``frame_size_provider`` because the video
    size is not known until the stream starts and can change on rotation.
    """

    def __init__(
        self,
        inject: InjectFn,
        profile: Profile | None = None,
        frame_size_provider: Callable[[], tuple[int, int]] | None = None,
    ) -> None:
        self._inject = inject
        self.profile = profile or Profile()
        self._frame_size_provider = frame_size_provider or (lambda: (1080, 1920))
        self._held: dict[str, int] = {}          # key -> pointer id
        self._free_pointers: list[int] = []
        self._next_pointer = FIRST_POINTER_ID

        # On-screen joystick pad: one finger that slides around its centre.
        self._joy_held: set[str] = set()          # wasd keys currently down
        self._joy_pointer: int | None = None      # finger id owned by the pad
        self._joy_last: tuple[int, int] | None = None  # last px sent
        self._joy_down: bool = False              # pad finger is on screen

    # -- helpers ---------------------------------------------------------- #

    def set_profile(self, profile: Profile) -> None:
        self.release_all()
        self.profile = profile

    @property
    def held_keys(self) -> frozenset[str]:
        return frozenset(self._held) | self._joy_held

    def _acquire_pointer(self) -> int:
        if self._free_pointers:
            return self._free_pointers.pop()
        pid = self._next_pointer
        self._next_pointer += 1
        return pid

    def _release_pointer(self, pointer_id: int) -> None:
        self._free_pointers.append(pointer_id)

    def _frame_size(self) -> tuple[int, int]:
        w, h = self._frame_size_provider()
        return int(w), int(h)

    def _send(self, action: MotionAction, pointer_id: int, binding: Binding) -> None:
        w, h = self._frame_size()
        if w <= 0 or h <= 0:
            return
        x, y = binding.to_frame_point(w, h)
        self._inject(action, pointer_id, x, y, w, h)

    def _joy_target(self) -> tuple[int, int] | None:
        """Deflected pad position, or None when unusable (no pad/no frame)."""
        joy = self.profile.joystick
        if joy is None:
            return None
        w, h = self._frame_size()
        if w <= 0 or h <= 0:
            return None
        return joy.deflect_px(self._joy_held, w, h)

    def _joy_neutral(self) -> tuple[int, int] | None:
        joy = self.profile.joystick
        if joy is None:
            return None
        w, h = self._frame_size()
        if w <= 0 or h <= 0:
            return None
        return joy.centre_px(w, h)

    # -- on-screen joystick ---------------------------------------------- #

    def _press_joy(self, key: str) -> bool:
        if key in self._joy_held:
            return False  # auto-repeat
        self._joy_held.add(key)
        target = self._joy_target()
        if target is None:
            self._joy_held.discard(key)
            return False

        if not self._joy_down:
            # Two-packet start: DOWN at the pad's CENTRE, then slide out.
            # Floating sticks take their neutral base from wherever the
            # finger first lands -- putting the finger down already-deflected
            # reads as "no input" on those (that is exactly the "WASD does
            # nothing" bug). Fixed-position sticks accept both, so this is
            # the universally correct order.
            centre = self._joy_neutral() or target
            self._joy_pointer = self._acquire_pointer()
            self._joy_down = True
            w, h = self._frame_size()
            self._inject(MotionAction.DOWN, self._joy_pointer, centre[0], centre[1], w, h)
            if target != centre:
                self._inject(MotionAction.MOVE, self._joy_pointer, target[0], target[1], w, h)
            self._joy_last = target
        elif target != self._joy_last:
            w, h = self._frame_size()
            self._inject(MotionAction.MOVE, self._joy_pointer, target[0], target[1], w, h)
            self._joy_last = target
        return True

    def _release_joy(self, key: str) -> bool:
        if key not in self._joy_held:
            return False
        self._joy_held.discard(key)
        if not self._joy_down or self._joy_pointer is None:
            return True

        w, h = self._frame_size()
        if self._joy_held:
            # Still steering: slide to the remaining direction.
            target = self._joy_target()
            if target is not None and target != self._joy_last:
                self._inject(MotionAction.MOVE, self._joy_pointer, target[0], target[1], w, h)
                self._joy_last = target
        else:
            # Neutral: lift the finger from the centre so the game sees a
            # clean release instead of a snap from the rim.
            centre = self._joy_neutral() or (0, 0)
            self._inject(MotionAction.UP, self._joy_pointer, centre[0], centre[1], w, h)
            self._release_pointer(self._joy_pointer)
            self._joy_pointer = None
            self._joy_down = False
            self._joy_last = None
        return True

    # -- event handling --------------------------------------------------- #

    def press(self, key: str) -> bool:
        """Handle a key-down. Returns True if a binding matched."""
        key = key.lower()
        if key in self._held:
            return False  # ignore auto-repeat
        # The pad owns its keys whenever it exists, even if a marker overlaps.
        if key in JOYSTICK_KEYS and self.profile.joystick is not None:
            return self._press_joy(key)
        binding = self.profile.binding_for_key(key)
        if binding is None:
            return False

        pointer_id = self._acquire_pointer()
        self._held[key] = pointer_id
        self._send(MotionAction.DOWN, pointer_id, binding)

        if binding.mode == MODE_TAP:
            # A tap is a complete gesture: down immediately followed by up.
            self._send(MotionAction.UP, pointer_id, binding)
            del self._held[key]
            self._release_pointer(pointer_id)
        return True

    def release(self, key: str) -> bool:
        """Handle a key-up. Returns True if a held binding was released."""
        key = key.lower()
        if key in self._joy_held:
            return self._release_joy(key)
        pointer_id = self._held.get(key)
        if pointer_id is None:
            return False
        binding = self.profile.binding_for_key(key)
        if binding is not None:
            self._send(MotionAction.UP, pointer_id, binding)
        del self._held[key]
        self._release_pointer(pointer_id)
        return True

    def release_all(self) -> None:
        """Lift every held finger (e.g. on focus loss or disconnect)."""
        if self._joy_down and self._joy_pointer is not None:
            centre = self._joy_neutral() or (0, 0)
            w, h = self._frame_size()
            self._inject(MotionAction.UP, self._joy_pointer, centre[0], centre[1], w, h)
            self._release_pointer(self._joy_pointer)
        self._joy_pointer = None
        self._joy_down = False
        self._joy_last = None
        self._joy_held.clear()
        for key, pointer_id in list(self._held.items()):
            binding = self.profile.binding_for_key(key)
            if binding is not None:
                self._send(MotionAction.UP, pointer_id, binding)
            self._release_pointer(pointer_id)
        self._held.clear()
