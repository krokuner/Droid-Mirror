"""Build the Discord idle picture from ``idle-screen.jpg``.

The card draws big presence art as a rounded square and centre-crops anything
that is not one, so the 360x696 portrait has to be padded out to a square or the
phone loses its top and bottom. It also has to be **transparent** rather than
white: the card behind it is dark, and a white tile reads as a broken image.

Run it after replacing ``idle-screen.jpg``::

    .venv\\Scripts\\python.exe make_idle_square.py

It writes ``icons/idle-screen-square.png`` (the file to upload) and
``icons/idle-screen-preview.png`` (the same thing composited on Discord's card
colour, for looking at with human eyes -- alpha is invisible in most viewers).
Then upload the square and point ``discord_rpc.IDLE_IMAGE_URL`` at the result.

Qt does the work because the project has no image library beyond PySide6.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPainter

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "idle-screen.jpg"
SQUARE = ROOT / "icons" / "idle-screen-square.png"
PREVIEW = ROOT / "icons" / "idle-screen-preview.png"

#: Side of the square Discord is given. It scales art to 512 anyway.
SIDE = 512

#: Breathing room around the phone, in the square's pixels.
MARGIN = 28

#: Discord's card background, for the preview only.
CARD = "#232428"

# Background keying. A pixel is background when it is both very bright and
# nearly colourless: that is white paper, not the blue screen or the grey frame.
# The ramp between the two thresholds keeps the phone's antialiased edge soft
# instead of leaving it jagged, and only near-neutral pixels are considered at
# all -- the lightest band of the screen is #cfe3f5, which is bright but far too
# blue to be mistaken for paper.
FULLY_TRANSPARENT_AT = 246
FULLY_OPAQUE_AT = 234
CHROMA_MAX = 14


def keyed(source: QImage) -> QImage:
    """``source`` with its paper background punched out to transparency."""
    out = QImage(source.width(), source.height(), QImage.Format_ARGB32)
    span = FULLY_TRANSPARENT_AT - FULLY_OPAQUE_AT
    for y in range(source.height()):
        for x in range(source.width()):
            c = source.pixelColor(x, y)
            r, g, b = c.red(), c.green(), c.blue()
            lightest = max(r, g, b)
            darkest = min(r, g, b)
            alpha = 255
            if lightest - darkest <= CHROMA_MAX and darkest > FULLY_OPAQUE_AT:
                alpha = round(255 * (FULLY_TRANSPARENT_AT - darkest) / span)
                alpha = max(0, min(255, alpha))
            out.setPixelColor(x, y, QColor(r, g, b, alpha))
    return out


def square(art: QImage) -> QImage:
    """``art`` centred in a transparent square, whole phone visible."""
    # Premultiplied for the scale: smoothing a non-premultiplied image blends
    # the RGB of fully transparent pixels into the edge and leaves a pale halo.
    scaled = (
        art.convertToFormat(QImage.Format_ARGB32_Premultiplied)
        .scaled(
            SIDE - 2 * MARGIN,
            SIDE - 2 * MARGIN,
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        .convertToFormat(QImage.Format_ARGB32)
    )
    canvas = QImage(SIDE, SIDE, QImage.Format_ARGB32)
    canvas.fill(Qt.transparent)
    painter = QPainter(canvas)
    painter.drawImage(
        (SIDE - scaled.width()) // 2, (SIDE - scaled.height()) // 2, scaled
    )
    painter.end()
    return canvas


def main() -> int:
    if not SOURCE.exists():
        print(f"missing {SOURCE}", file=sys.stderr)
        return 1

    art = QImage(str(SOURCE))
    if art.isNull():
        print(f"could not read {SOURCE}", file=sys.stderr)
        return 1

    cut = keyed(art.convertToFormat(QImage.Format_RGB32))
    result = square(cut)
    result.save(str(SQUARE), "PNG")

    preview = QImage(SIDE, SIDE, QImage.Format_ARGB32)
    preview.fill(QColor(CARD))
    painter = QPainter(preview)
    painter.drawImage(0, 0, result)
    painter.end()
    preview.save(str(PREVIEW), "PNG")

    clear = sum(
        1
        for y in range(SIDE)
        for x in range(SIDE)
        if result.pixelColor(x, y).alpha() == 0
    )
    print(f"source      : {art.width()}x{art.height()}")
    print(f"wrote       : {SQUARE.relative_to(ROOT)} ({SQUARE.stat().st_size:,} bytes)")
    print(f"preview     : {PREVIEW.relative_to(ROOT)} (on {CARD}, for eyeballing)")
    print(f"transparent : {clear:,} of {SIDE * SIDE:,} pixels ({100 * clear // SIDE**2}%)")
    print(f"corner      : alpha {result.pixelColor(0, 0).alpha()}")
    print(f"screen      : alpha {result.pixelColor(SIDE // 2, SIDE // 2).alpha()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
