"""8-bit RGB from any Pillow image, high-bit-depth greyscale included.

Twin of ``backend/image_modes.py`` (the backend does not import the SDK).
"""

from typing import cast

from PIL import Image

# Each image's own range spans 0..255, less this fraction of its pixels at each end (hot or dead pixels).
STRETCH_CUT = 0.005


def is_high_bit_depth(mode: str) -> bool:
    """A single channel wider than 8 bits: 16-bit (``I;16*``), 32-bit integer (``I``) or float (``F``)."""
    return mode in ("I", "F") or mode.startswith("I;16")


def to_rgb(image: Image.Image) -> Image.Image:
    """Return ``image`` as 8-bit RGB.

    Pillow opens a 16-bit greyscale PNG or TIFF as mode ``I;16`` (or ``I``) and a float TIFF as ``F``, and
    ``Image.convert("RGB")`` clips their values at 255: a multispectral band or a thermal frame comes out white.
    They're stretched instead: the image's 0.5th to 99.5th percentile spans 0..255. So a 10-, 12- or 14-bit
    sensor's images are neither dark nor white, whatever part of the 16-bit range they use; each image gets its
    own contrast. 8-bit modes convert exactly as before.
    """
    if is_high_bit_depth(image.mode):
        image = _stretch(image.convert("F" if image.mode == "F" else "I"))
    return image.convert("RGB")


def _stretch(image: Image.Image) -> Image.Image:
    # One band (I or F): getextrema gives its lowest and highest value.
    low, high = cast("tuple[float, float]", image.getextrema())
    if high <= low:  # a single value: nothing to stretch
        return Image.new("L", image.size, 128)
    histogram = image.histogram(extrema=(low, high))  # 256 bins from low to high
    step = (high - low) / len(histogram)
    cut = sum(histogram) * STRETCH_CUT
    bottom, top = low, high
    seen = 0
    for index, count in enumerate(histogram):
        seen += count
        if seen > cut:
            bottom = low + index * step
            break
    seen = 0
    for index in range(len(histogram) - 1, -1, -1):
        seen += histogram[index]
        if seen > cut:
            top = low + (index + 1) * step
            break
    scale = 255 / max(top - bottom, step)
    # Pillow takes a linear function for I and F images; converting to L clips to 0..255.
    return image.point(lambda value: value * scale - bottom * scale).convert("L")
