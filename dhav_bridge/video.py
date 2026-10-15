"""Renderer JPEG -> DHAV I420 payload.

Colour space is full-range BT.601, which is what a DHAV v1 renderer is expected
to emit, so the browser sees the same gamut as with any other conforming
renderer.  ``cv2.COLOR_BGR2YUV_I420`` is *not* usable here: OpenCV emits
limited-range (studio swing) YUV, which comes out visibly washed and darker.
"""

from __future__ import annotations

import cv2
import numpy as np

# Neutral background used when a frame cannot be decoded: Y=16, U=V=128.
_NEUTRAL_Y = 16
_NEUTRAL_UV = 128


def parse_size(text: str) -> tuple[int, int]:
    """``"256"`` -> (256, 256); ``"320x240"`` -> (320, 240).  Both sides even."""
    parts = text.lower().split("x") if "x" in text.lower() else [text, text]
    if len(parts) != 2:
        raise ValueError(f"bad video size {text!r}")
    width, height = (int(p) for p in parts)
    for value in (width, height):
        if value <= 0 or value % 2 or value > 512:
            raise ValueError(f"video size must be even and within 512x512, got {width}x{height}")
    return width, height


def neutral_i420(width: int, height: int) -> bytes:
    out = np.empty(width * height * 3 // 2, dtype=np.uint8)
    plane = width * height
    out[:plane] = _NEUTRAL_Y
    out[plane:] = _NEUTRAL_UV
    return out.tobytes()


def bgr_to_i420(bgr: np.ndarray, width: int, height: int) -> bytes:
    """Full-range BT.601 planar I420: Y(w*h) | U(w/2*h/2) | V(w/2*h/2)."""
    if (bgr.shape[1], bgr.shape[0]) != (width, height):
        bgr = cv2.resize(bgr, (width, height), interpolation=cv2.INTER_AREA)
    block = bgr.astype(np.float32)
    blue = block[:, :, 0]
    green = block[:, :, 1]
    red = block[:, :, 2]

    luma = 0.299 * red + 0.587 * green + 0.114 * blue
    chroma_u = 128.0 - 0.168736 * red - 0.331264 * green + 0.5 * blue
    chroma_v = 128.0 + 0.5 * red - 0.418688 * green - 0.081312 * blue

    plane = width * height
    out = np.empty(plane * 3 // 2, dtype=np.uint8)
    out[:plane] = np.clip(luma, 0.0, 255.0).astype(np.uint8).reshape(-1)
    out[plane : plane + plane // 4] = _subsample(chroma_u).astype(np.uint8).reshape(-1)
    out[plane + plane // 4 :] = _subsample(chroma_v).astype(np.uint8).reshape(-1)
    return out.tobytes()


def _subsample(plane: np.ndarray) -> np.ndarray:
    return np.clip(
        (plane[0::2, 0::2] + plane[1::2, 0::2] + plane[0::2, 1::2] + plane[1::2, 1::2]) * 0.25,
        0.0,
        255.0,
    )


def jpeg_to_i420(jpeg: bytes, width: int, height: int) -> bytes | None:
    """Decode one renderer JPEG frame.  Returns ``None`` if it cannot be decoded."""
    buffer = np.frombuffer(jpeg, dtype=np.uint8)
    image = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
    if image is None:
        return None
    return bgr_to_i420(image, width, height)
