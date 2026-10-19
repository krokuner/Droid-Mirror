"""YUV -> RGB conversion.

MediaCodec (and therefore the scrcpy device server) emits **studio-swing**
(limited-range) BT.601 video: luma lives in 16..235 and chroma in 16..240. The
ranges must be expanded back to full range, otherwise colours come out
noticeably compressed.

This function is the CPU fallback renderer's colour path, and it is the same
implementation asserted against FFmpeg's own ``rgb24`` conversion in
``tests/test_pipeline.py``.
"""

from __future__ import annotations

import numpy as np

# BT.601 limited-range coefficients.
_Y_SCALE = 1.164383
_R_CR = 1.596027
_G_CB = 0.391762
_G_CR = 0.812968
_B_CB = 2.017232


def yuv420_to_rgb(y: np.ndarray, u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Convert YUV420p planes to an ``(h, w, 3)`` uint8 RGB array.

    Chroma is upsampled to luma resolution by nearest-neighbour repetition,
    which is cheap and, for the flat/smooth content typical of UI and video,
    indistinguishable from bilinear for our purposes.
    """
    height, width = y.shape[:2]
    yy = y.astype(np.float32)

    uu = u.astype(np.float32)
    vv = v.astype(np.float32)
    if uu.shape != (height, width):
        uu = np.repeat(np.repeat(uu, 2, axis=0), 2, axis=1)[:height, :width]
        vv = np.repeat(np.repeat(vv, 2, axis=0), 2, axis=1)[:height, :width]

    luma = (yy - 16.0) * _Y_SCALE
    cb = uu - 128.0
    cr = vv - 128.0

    r = luma + _R_CR * cr
    g = luma - _G_CB * cb - _G_CR * cr
    b = luma + _B_CB * cb

    rgb = np.empty((height, width, 3), dtype=np.uint8)
    np.clip(r, 0, 255, out=r)
    np.clip(g, 0, 255, out=g)
    np.clip(b, 0, 255, out=b)
    rgb[..., 0] = r.astype(np.uint8)
    rgb[..., 1] = g.astype(np.uint8)
    rgb[..., 2] = b.astype(np.uint8)
    return rgb
