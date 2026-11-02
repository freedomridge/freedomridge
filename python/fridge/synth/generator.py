"""Synthetic dataset generator for Freedom Ridge.

Draws REAL images (Pillow) of simple shapes whose pixels exactly match every
Freedom Ridge typed-label field, so you can exercise annotation filtering,
rendering, patching and evaluation end-to-end against a live backend — with zero
heavy dependencies (Pillow + numpy are already Freedom Ridge base deps; no
torch/cv2/etc.).

    from fridge import Dataset
    from fridge.synth import generate, SyntheticSpec

    gen = generate(SyntheticSpec(num_images=24))
    ds = Dataset("synth-demo")
    ds.add_samples(gen.fridge_samples())
    # gen.manifest() holds the expected labels per filepath for assertions;
    # gen.class_counts() is the expected count_values('detections.label').

Images (plus per-image segmentation-mask and heatmap PNGs) are written under
``FRIDGE_DATA_ROOT`` (the backend's data root) so the media route can serve them
for render/patch tests. Generation is deterministic for a given ``seed``.
"""

from __future__ import annotations

import math
import os
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw

from ..core.labels import (
    Classification,
    Classifications,
    Detection,
    Detections,
    Heatmap,
    Keypoint,
    Keypoints,
    Polygon,
    Polyline,
    Polylines,
    Segmentation,
)
from ..core.sample import Sample

# Segmentation pixel value = class id (0 = background). Colors are distinct per
# class so the rendered image, the index mask, and the heatmap all agree.
_CLASS_IDS = {"rectangle": 1, "circle": 2, "triangle": 3}
_CLASS_COLORS = {"rectangle": (220, 60, 60), "circle": (60, 200, 90), "triangle": (70, 120, 230)}
_MASK_TARGETS = {0: "background", 1: "rectangle", 2: "circle", 3: "triangle"}

# Fake geolocation: cluster samples around a few recognizable sites so the map +
# geo-filtering have something interesting to show.
_DEMO_SITES = [(-122.4194, 37.7749), (-73.9857, 40.7484), (2.2945, 48.8584), (139.7006, 35.6590)]


@dataclass
class SyntheticSpec:
    """What to generate. Defaults give a small, deterministic, fully-annotated set."""

    num_images: int = 24
    image_size: Tuple[int, int] = (256, 256)  # >=256 so 4x4 patches don't alias
    shapes_per_image: Tuple[int, int] = (2, 5)  # (min, max) inclusive, placed low-overlap
    shape_classes: Sequence[str] = ("rectangle", "circle", "triangle")
    include_empty_sample: bool = True  # one sample with NO annotations (null-field coverage)
    with_location: bool = True  # assign fake lat/long clustered around a few sites
    seed: int = 0


@dataclass
class GeneratedSample:
    filepath: str
    sample: Sample
    expected: dict  # ground-truth manifest for assertions (labels, counts, etc.)


@dataclass
class GeneratedDataset:
    samples: List[GeneratedSample]
    spec: SyntheticSpec
    data_root: str
    out_dir: str

    def fridge_samples(self) -> List[Sample]:
        """The Sample objects, ready for ``dataset.add_samples(...)``."""
        return [g.sample for g in self.samples]

    def manifest(self) -> dict:
        """``{filepath: expected}`` — what each sample's labels should be."""
        return {g.filepath: g.expected for g in self.samples}

    def class_counts(self) -> Dict[str, int]:
        """Expected ``count_values('detections.label')`` across the dataset."""
        counts: Dict[str, int] = {}
        for g in self.samples:
            for lbl in g.expected.get("detection_labels", []):
                counts[lbl] = counts.get(lbl, 0) + 1
        return counts

    def samples_with_label(self, label: str) -> List[str]:
        """Filepaths of samples that contain at least one detection of ``label``."""
        return [g.filepath for g in self.samples if label in g.expected.get("detection_labels", [])]


def _resolve_data_root(data_root: Optional[str]) -> str:
    """Where images must be written so the backend can serve them.

    Order: explicit arg → ``$FRIDGE_DATA_ROOT`` → the repo's ``data/`` dir (the
    backend's default DATA_ROOT, found by walking up to the dir holding
    ``backend/main.py``) → ``<cwd>/data``.
    """
    if data_root:
        return data_root
    env = os.environ.get("FRIDGE_DATA_ROOT")
    if env:
        return env
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "backend" / "main.py").exists():
            return str(parent / "data")
    return os.path.join(os.getcwd(), "data")


def _bg_color(rng: random.Random) -> Tuple[int, int, int]:
    # Muted, varied backgrounds (kept clearly distinct from the bright shape colors).
    base = rng.randint(20, 70)
    return (base + rng.randint(0, 25), base + rng.randint(0, 25), base + rng.randint(0, 25))


def _place_shapes(rng: random.Random, w: int, h: int, classes: Sequence[str], n: int) -> List[dict]:
    """Place ``n`` shapes on a coarse grid (one per cell) so they barely overlap."""
    cols = max(1, int(math.ceil(math.sqrt(n))))
    rows = max(1, int(math.ceil(n / cols)))
    cells = [(c, r) for r in range(rows) for c in range(cols)]
    rng.shuffle(cells)
    cw, ch = w / cols, h / rows
    shapes = []
    for c, r in cells[:n]:
        cls = rng.choice(list(classes))
        margin_x, margin_y = cw * 0.18, ch * 0.18
        sw = rng.uniform(cw * 0.45, cw * 0.70)
        sh = rng.uniform(ch * 0.45, ch * 0.70)
        x = c * cw + margin_x + rng.uniform(0, max(0.0, cw - sw - 2 * margin_x))
        y = r * ch + margin_y + rng.uniform(0, max(0.0, ch - sh - 2 * margin_y))
        shapes.append({"cls": cls, "x": x, "y": y, "w": sw, "h": sh})
    return shapes


def _shape_ring(shape: dict) -> List[Tuple[float, float]]:
    """Polygon outline (pixel coords) tracing the shape."""
    x, y, w, h = shape["x"], shape["y"], shape["w"], shape["h"]
    cls = shape["cls"]
    if cls == "triangle":
        return [(x + w / 2, y), (x + w, y + h), (x, y + h)]
    if cls == "circle":
        cx, cy, rx, ry = x + w / 2, y + h / 2, w / 2, h / 2
        return [(cx + rx * math.cos(2 * math.pi * i / 24), cy + ry * math.sin(2 * math.pi * i / 24)) for i in range(24)]
    return [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]  # rectangle


def _draw_shape(
    img_d: ImageDraw.ImageDraw,
    mask_d: ImageDraw.ImageDraw,
    heat_d: ImageDraw.ImageDraw,
    shape: dict,
    intensity: int,
) -> None:
    """Paint the shape identically into the RGB image, the index mask, and the heatmap."""
    cls = shape["cls"]
    color, cid = _CLASS_COLORS[cls], _CLASS_IDS[cls]
    x, y, w, h = shape["x"], shape["y"], shape["w"], shape["h"]
    box: Any = [x, y, x + w, y + h]
    if cls == "rectangle":
        img_d.rectangle(box, fill=color)
        mask_d.rectangle(box, fill=cid)
        heat_d.rectangle(box, fill=intensity)
    elif cls == "circle":
        img_d.ellipse(box, fill=color)
        mask_d.ellipse(box, fill=cid)
        heat_d.ellipse(box, fill=intensity)
    else:  # triangle
        pts = [(x + w / 2, y), (x + w, y + h), (x, y + h)]
        img_d.polygon(pts, fill=color)
        mask_d.polygon(pts, fill=cid)
        heat_d.polygon(pts, fill=intensity)


def _noisy_predictions(
    dets: List[Detection],
    rng: random.Random,
    w: int,
    h: int,
    classes: Sequence[str],
) -> List[Detection]:
    """A jittered copy of the GT detections for evaluate_detections (drops/adds/perturbs)."""
    preds: List[Detection] = []
    for d in dets:
        if rng.random() < 0.15:
            continue  # missed detection (false negative)
        x, y, bw, bh = d["bbox"]
        j = 0.02
        nb = [
            min(max(x + rng.uniform(-j, j), 0.0), 1.0),
            min(max(y + rng.uniform(-j, j), 0.0), 1.0),
            max(bw + rng.uniform(-j, j), 0.01),
            max(bh + rng.uniform(-j, j), 0.01),
        ]
        conf = round(min(max(float(d.get("confidence", 0.8)) + rng.uniform(-0.1, 0.05), 0.05), 1.0), 3)
        preds.append(Detection(d["label"], bbox=[round(v, 5) for v in nb], confidence=conf))
    if rng.random() < 0.25:  # spurious detection (false positive)
        preds.append(
            Detection(
                rng.choice(list(classes)),
                bbox=[round(rng.uniform(0, 0.8), 5), round(rng.uniform(0, 0.8), 5), 0.1, 0.1],
                confidence=round(rng.uniform(0.3, 0.6), 3),
            ),
        )
    return preds


def _dominant(dets: List[Detection]) -> str:
    counts: Dict[str, int] = {}
    for d in dets:
        counts[d["label"]] = counts.get(d["label"], 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def generate(
    spec: SyntheticSpec = SyntheticSpec(),
    out_dir: Optional[str] = None,
    data_root: Optional[str] = None,
    run_label: Optional[str] = None,
) -> GeneratedDataset:
    """Render real PNGs + matching Freedom Ridge typed labels. Deterministic per ``seed``.

    Args:
        spec: what to generate.
        out_dir: directory to write images into. Defaults to
            ``<data_root>/synth/<run_label>``.
        data_root: backend data root (see ``_resolve_data_root``); images must
            live under it to be servable.
        run_label: sub-dir name (default ``seed<seed>_n<num_images>``); re-running
            the same spec overwrites, keeping it reproducible.
    """
    classes = list(spec.shape_classes)
    root = _resolve_data_root(data_root)
    label = run_label or f"seed{spec.seed}_n{spec.num_images}"
    out = out_dir or os.path.join(root, "synth", label)
    os.makedirs(out, exist_ok=True)
    w, h = spec.image_size
    samples: List[GeneratedSample] = []

    for i in range(spec.num_images):
        rng = random.Random(spec.seed * 100003 + i)  # deterministic per image
        is_empty = spec.include_empty_sample and i == spec.num_images - 1

        img = Image.new("RGB", (w, h), _bg_color(rng))
        mask = Image.new("L", (w, h), 0)
        heat = Image.new("L", (w, h), 0)
        img_d, mask_d, heat_d = ImageDraw.Draw(img), ImageDraw.Draw(mask), ImageDraw.Draw(heat)

        shapes = [] if is_empty else _place_shapes(rng, w, h, classes, rng.randint(*spec.shapes_per_image))
        dets: List[Detection] = []
        polys: List[Polyline] = []
        kps: List[Keypoint] = []
        present: set = set()
        for j, sh in enumerate(shapes):
            conf = round(rng.uniform(0.55, 0.99), 3)
            _draw_shape(img_d, mask_d, heat_d, sh, intensity=int(60 + conf * 195))
            cls = sh["cls"]
            present.add(cls)
            nb = [sh["x"] / w, sh["y"] / h, sh["w"] / w, sh["h"] / h]
            dets.append(Detection(cls, bbox=[round(v, 5) for v in nb], confidence=conf, occluded=(j % 3 == 0)))
            polys.append(Polygon(cls, points=[[round(px / w, 5), round(py / h, 5)] for px, py in _shape_ring(sh)]))
            cx, cy = sh["x"] + sh["w"] / 2, sh["y"] + sh["h"] / 2
            kps.append(Keypoint([[round(cx / w, 5), round(cy / h, 5)]], label=cls, confidence=conf))

        img_path = os.path.join(out, f"img_{i:04d}.png")
        img.save(img_path)

        metadata: Dict = {"synth": True, "synth_index": i}
        expected = {
            "bbox_count": len(dets),
            "detection_labels": [d["label"] for d in dets],
            "classes": sorted(present),
            "has_annotations": len(dets) > 0,
            "occluded_count": sum(1 for d in dets if d.get("attributes", {}).get("occluded")),
        }
        if dets:
            dominant = _dominant(dets)
            # ground_truth = the true boxes; predictions = a deliberately-noisy copy
            # (for evaluate_detections). Both are filterable detection fields.
            metadata["ground_truth"] = Detections(dets)
            metadata["predictions"] = Detections(_noisy_predictions(dets, rng, w, h, classes))
            metadata["polygons"] = Polylines(polys)
            metadata["keypoints"] = Keypoints(kps)
            metadata["classification"] = Classification(dominant, confidence=round(rng.uniform(0.6, 0.99), 3))
            metadata["pred_classification"] = Classification(rng.choice(classes) if rng.random() < 0.2 else dominant)
            metadata["tags_multilabel"] = Classifications([Classification(c) for c in sorted(present)])
            expected["dominant_class"] = dominant

            mask_path = os.path.join(out, f"img_{i:04d}_mask.png")
            mask.save(mask_path)
            metadata["segmentation"] = Segmentation(mask_path, labels=sorted(present), mask_targets=_MASK_TARGETS)

            heat_path = os.path.join(out, f"img_{i:04d}_heat.png")
            heat.save(heat_path)
            metadata["heatmap"] = Heatmap(heat_path, range=(0, 255), colormap="viridis")
            expected["mask_path"] = mask_path
            expected["heatmap_path"] = heat_path

        location = None
        if spec.with_location:
            site = _DEMO_SITES[i % len(_DEMO_SITES)]
            location = (round(site[0] + rng.uniform(-0.05, 0.05), 6), round(site[1] + rng.uniform(-0.05, 0.05), 6))
            expected["location"] = location

        sample = Sample(filepath=img_path, tags=["synthetic"], metadata=metadata, location=location)
        samples.append(GeneratedSample(filepath=img_path, sample=sample, expected=expected))

    return GeneratedDataset(samples=samples, spec=spec, data_root=root, out_dir=out)
