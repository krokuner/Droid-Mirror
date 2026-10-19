"""Pure geometry for fitting the mirror into its widget.

Kept free of Qt so the coordinate maths -- the part that decides where a
keybind actually lands on the phone -- can be unit tested directly.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    w: float
    h: float

    @property
    def right(self) -> float:
        return self.x + self.w

    @property
    def bottom(self) -> float:
        return self.y + self.h

    @property
    def center(self) -> tuple[float, float]:
        return self.x + self.w / 2, self.y + self.h / 2


def fit_rect(
    frame_width: int,
    frame_height: int,
    view_width: float,
    view_height: float,
) -> Rect:
    """Largest letterboxed rect with the frame's aspect ratio, centred."""
    if frame_width <= 0 or frame_height <= 0 or view_width <= 0 or view_height <= 0:
        return Rect(0.0, 0.0, max(view_width, 0.0), max(view_height, 0.0))

    scale = min(view_width / frame_width, view_height / frame_height)
    w = frame_width * scale
    h = frame_height * scale
    return Rect((view_width - w) / 2.0, (view_height - h) / 2.0, w, h)


def point_to_normalised(px: float, py: float, rect: Rect) -> tuple[float, float]:
    """Widget point -> normalised (0..1) position inside the video rect."""
    if rect.w <= 0 or rect.h <= 0:
        return 0.0, 0.0
    nx = (px - rect.x) / rect.w
    ny = (py - rect.y) / rect.h
    return min(1.0, max(0.0, nx)), min(1.0, max(0.0, ny))


def normalised_to_point(nx: float, ny: float, rect: Rect) -> tuple[float, float]:
    """Normalised position -> widget point."""
    return rect.x + nx * rect.w, rect.y + ny * rect.h


def point_to_frame(
    px: float, py: float, rect: Rect, frame_width: int, frame_height: int
) -> tuple[int, int]:
    """Widget point -> integer frame pixel, clamped inside the frame."""
    nx, ny = point_to_normalised(px, py, rect)
    x = int(round(nx * max(frame_width - 1, 0)))
    y = int(round(ny * max(frame_height - 1, 0)))
    return min(max(x, 0), max(frame_width - 1, 0)), min(
        max(y, 0), max(frame_height - 1, 0)
    )


def contains(rect: Rect, px: float, py: float) -> bool:
    return rect.x <= px <= rect.right and rect.y <= py <= rect.bottom
