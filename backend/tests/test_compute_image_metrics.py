"""fridge.compute_image_metrics must return exactly what Dataset.compute_metrics used to compute per sample.

The reference functions below are verbatim copies of the closures that lived inside
Dataset.compute_metrics before the computation was made public, and of _saturation before it
stopped converting the image to HSV. The faster code must return identical values, so stored
metrics don't change. Needs opencv (the "metrics" extra), so the module is skipped where it is
not installed.
"""

import builtins
import math
from collections.abc import Callable

import pytest

cv2 = pytest.importorskip("cv2")

import fridge  # noqa: E402
import numpy as np  # noqa: E402
from fridge.core.dataset import _exposure_clipping, compute_image_metrics  # noqa: E402
from PIL import Image, ImageStat  # noqa: E402

KEYS = {
    "brightness",
    "blurriness",
    "contrast",
    "entropy",
    "salt_and_pepper",
    "saturation",
    "pct_underexposed",
    "pct_overexposed",
}


def _brightness(img):
    stat = ImageStat.Stat(img)
    try:
        r, g, b = stat.mean
    except ValueError:
        r = g = b = stat.mean[0]
    return float(np.sqrt(0.241 * r * r + 0.691 * g * g + 0.068 * b * b))


def _blurriness(gray):
    lap = cv2.Laplacian(np.array(gray), cv2.CV_64F)
    return float(lap.var())


def _contrast(img):
    hist, _ = np.histogram(cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR), bins=256, range=(0, 256))
    return float(np.max(np.where(hist > 0)) - np.min(np.where(hist > 0)))


def _entropy(img):
    return float(img.entropy())


def _salt_and_pepper(gray):
    g = np.array(gray)
    salt = g >= 244
    pepper = g <= 10
    k = np.ones((2, 2), np.uint8)
    s_open = cv2.morphologyEx(salt.astype(np.uint8), cv2.MORPH_OPEN, k)
    p_open = cv2.morphologyEx(pepper.astype(np.uint8), cv2.MORPH_OPEN, k)
    isolated = np.sum(salt & ~s_open.astype(np.uint8)) + np.sum(pepper & ~p_open.astype(np.uint8))
    return float(isolated / g.size * 100)


def _saturation(img):
    return float(np.asarray(img.convert("HSV"))[:, :, 1].mean())


def reference_metrics(img):
    """The per-sample dict the old compute_metrics loop built (img is RGB)."""
    gray = img.convert("L")
    under, over = _exposure_clipping(gray)
    return {
        "brightness": _brightness(img),
        "blurriness": _blurriness(gray),
        "contrast": _contrast(img),
        "entropy": _entropy(img),
        "salt_and_pepper": _salt_and_pepper(gray),
        "saturation": _saturation(img),
        "pct_underexposed": under,
        "pct_overexposed": over,
    }


def _checkerboard():
    # 2-pixel squares so the pattern is not removed by the 2x2 salt-and-pepper opening.
    arr = np.indices((64, 64)).sum(axis=0) // 2 % 2 * 255
    return Image.fromarray(arr.astype(np.uint8), mode="L").convert("RGB")


def _gradient():
    x = np.tile(np.linspace(0, 255, 64, dtype=np.uint8), (64, 1))
    y = x.T
    return Image.fromarray(np.dstack([x, y, 255 - x]), mode="RGB")


def _speckled():
    arr = np.full((64, 64), 128, dtype=np.uint8)
    arr[5, 5] = 255  # isolated salt
    arr[20, 30] = 0  # isolated pepper
    return Image.fromarray(arr, mode="L").convert("RGB")


IMAGES = {
    "solid_gray": lambda: Image.new("RGB", (64, 64), (128, 128, 128)),
    "checkerboard": _checkerboard,
    "gradient": _gradient,
    "speckled": _speckled,
}


def _random(shape, seed, values=None):
    rng = np.random.default_rng(seed)
    if values is None:
        return rng.integers(0, 256, shape, dtype=np.uint8)
    return rng.choice(np.array(values, dtype=np.uint8), shape)


def _random_rgb(height: int, width: int, seed: int) -> Callable[[], Image.Image]:
    return lambda: Image.fromarray(_random((height, width, 3), seed), "RGB")


# Inputs for the identical-output check: random images of several sizes, the degenerate ones, and every
# mode compute_image_metrics converts to RGB first.
EDGE_IMAGES: dict[str, Callable[[], Image.Image]] = {
    **{
        f"random_{h}x{w}_{seed}": _random_rgb(h, w, seed)
        for h, w, seed in [(64, 48, 0), (333, 217, 1), (480, 640, 2), (1, 1, 3), (1, 97, 4), (97, 1, 5)]
    },
    "only_0_and_255": lambda: Image.fromarray(_random((40, 30, 3), 6, values=[0, 255]), "RGB"),
    "near_extremes": lambda: Image.fromarray(_random((40, 30, 3), 7, values=[0, 1, 5, 10, 244, 250, 254, 255]), "RGB"),
    "black": lambda: Image.new("RGB", (16, 16), (0, 0, 0)),
    "white": lambda: Image.new("RGB", (16, 16), (255, 255, 255)),
    "constant_color": lambda: Image.new("RGB", (16, 16), (12, 200, 77)),
    "black_1x1": lambda: Image.new("RGB", (1, 1), (0, 0, 0)),
    "white_1x1": lambda: Image.new("RGB", (1, 1), (255, 255, 255)),
    "mode_L": lambda: Image.fromarray(_random((50, 40), 8), "L"),
    "mode_1": lambda: Image.fromarray(_random((50, 40), 9, values=[0, 1]).astype(bool)),
    "mode_P": lambda: Image.fromarray(_random((50, 40, 3), 10), "RGB").quantize(colors=37),
    "mode_RGBA": lambda: Image.fromarray(_random((50, 40, 4), 11), "RGBA"),
    "mode_LA": lambda: Image.fromarray(_random((50, 40, 2), 12), "LA"),
    "mode_CMYK": lambda: Image.fromarray(_random((50, 40, 4), 13), "CMYK"),
}


def _assert_identical(actual, expected):
    """Same keys, every value a float and exactly equal (NaN only where the reference has NaN)."""
    assert actual.keys() == expected.keys()
    for key, value in expected.items():
        assert type(actual[key]) is float and type(value) is float, key
        same = actual[key] == value or (math.isnan(actual[key]) and math.isnan(value))
        assert same, f"{key}: {actual[key]!r} != reference {value!r}"


def test_public_export():
    assert fridge.compute_image_metrics is compute_image_metrics
    assert "compute_image_metrics" in fridge.__all__


@pytest.mark.parametrize("name", IMAGES)
def test_exact_key_set_and_float_values(name):
    result = compute_image_metrics(IMAGES[name]())
    assert set(result) == KEYS
    assert all(type(v) is float for v in result.values())


@pytest.mark.parametrize("name", IMAGES)
def test_values_equal_original_closures(name):
    img = IMAGES[name]()
    assert compute_image_metrics(img) == reference_metrics(img)


def test_known_values_on_solid_gray_and_checkerboard():
    gray = compute_image_metrics(IMAGES["solid_gray"]())
    assert gray["blurriness"] == 0.0
    assert gray["contrast"] == 0.0
    assert gray["saturation"] == 0.0
    assert gray["pct_underexposed"] == 0.0
    assert gray["pct_overexposed"] == 0.0
    board = compute_image_metrics(IMAGES["checkerboard"]())
    assert board["contrast"] == 255.0
    assert board["pct_underexposed"] == 50.0
    assert board["pct_overexposed"] == 50.0
    speckled = compute_image_metrics(IMAGES["speckled"]())
    assert speckled["salt_and_pepper"] == 2 / (64 * 64) * 100


@pytest.mark.parametrize("name", EDGE_IMAGES)
def test_values_identical_to_the_reference_on_edge_cases(name):
    img = EDGE_IMAGES[name]()
    assert img.mode == (name.removeprefix("mode_") if name.startswith("mode_") else "RGB")
    _assert_identical(compute_image_metrics(img), reference_metrics(img.convert("RGB")))


@pytest.mark.parametrize("mode", ["L", "RGBA"])
def test_non_rgb_input_is_converted_to_rgb(mode):
    source = IMAGES["gradient"]().convert(mode)
    assert compute_image_metrics(source) == reference_metrics(source.convert("RGB"))
    assert set(compute_image_metrics(source)) == KEYS


def test_input_image_is_not_modified():
    img = IMAGES["gradient"]().convert("RGBA")
    compute_image_metrics(img)
    assert img.mode == "RGBA"


def test_missing_opencv_raises_install_hint(monkeypatch):
    real_import = builtins.__import__

    def no_cv2(name, *args, **kwargs):
        if name == "cv2":
            raise ImportError("no cv2")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_cv2)
    with pytest.raises(ImportError, match="opencv-python-headless"):
        compute_image_metrics(IMAGES["solid_gray"]())


def test_dataset_compute_metrics_writes_the_same_dict_in_batches(monkeypatch):
    from types import SimpleNamespace

    from fridge.core.dataset import Dataset

    images = {"a": IMAGES["gradient"](), "b": IMAGES["checkerboard"](), "c": IMAGES["speckled"]()}
    samples = [
        SimpleNamespace(id=1, filepath="a", metadata={}),
        SimpleNamespace(id=2, filepath="b", metadata={"brightness": 1.0}),  # skipped
        SimpleNamespace(id=3, filepath="c", metadata={}),
    ]
    writes = []
    ds = object.__new__(Dataset)
    monkeypatch.setattr(ds, "iter_samples", lambda **kwargs: iter(samples), raising=False)
    monkeypatch.setattr(ds, "set_values_per_sample_multi", lambda pending: writes.append(dict(pending)), raising=False)
    monkeypatch.setattr(ds, "refresh_stats", lambda: None, raising=False)

    ds.compute_metrics(image_loader=images.__getitem__, batch_size=1)

    assert writes == [{1: reference_metrics(images["a"])}, {3: reference_metrics(images["c"])}]
