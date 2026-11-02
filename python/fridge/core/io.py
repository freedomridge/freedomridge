"""Import/export of standard computer-vision dataset formats.

Importers parse COCO / YOLO / VOC / classification-directory annotations into
Freedom Ridge's typed labels (``Detections`` / ``Classification`` in sample
metadata) and ``add_samples`` them. Exporters walk any dataset *or view* and write
the chosen format out — so you can export a filtered subset, consistent with
Freedom Ridge's view-everything model.

Bounding-box convention: COCO/VOC produce **pixel** ``[x, y, w, h]`` boxes; YOLO
produces **normalized** ``[x, y, w, h]`` (top-left).

Export round-trip fidelity: exporters detect normalized-vs-pixel per sample
(all coords <= 1 ⇒ normalized) or honor an explicit ``normalized=`` arg. Pixel↔
normalized conversion needs image width/height (from ``metadata['width']/['height']``,
set by import, or by opening the image); export **raises** if a conversion is
needed but dimensions are unavailable, rather than emitting wrong coordinates.

Used via the thin Dataset methods (``dataset.import_coco(...)``, ``dataset.export(...)``).
"""

from __future__ import annotations

import glob
import json
import os
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Set, cast

_IMG_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff", ".gif")


def _iter_samples(view_or_dataset):
    """Yield Sample objects from a Dataset or DatasetView."""
    if hasattr(view_or_dataset, "iter_samples"):
        return view_or_dataset.iter_samples()
    return iter(view_or_dataset)


def _image_size(sample):
    """(width, height) from metadata, else by opening the local image, else (None, None)."""
    md = sample.metadata or {}
    w, h = md.get("width"), md.get("height")
    if w and h:
        return int(w), int(h)
    try:
        from PIL import Image

        with Image.open(sample.filepath) as im:
            return im.size
    except Exception:
        return None, None


def _sample_is_normalized(dets) -> bool:
    """True if every box in ``dets`` looks normalized (all coords <= 1).

    Decided once per sample (not per-box) so a small object can't be mistaken for
    a normalized box; uses <= 1.0 (unambiguous) rather than a lenient threshold.
    """
    for d in dets:
        bb = d.get("bbox")
        if isinstance(bb, (list, tuple)) and len(bb) == 4 and max(abs(float(v)) for v in bb) > 1.0:
            return False
    return True


def _to_pixel_xywh(bbox, w, h, is_norm):
    """Convert an [x,y,w,h] box to pixel coords. Raises if normalized but dims unknown."""
    x, y, bw, bh = bbox
    if is_norm:
        if not w or not h:
            raise ValueError(
                "COCO export needs image width/height to convert normalized boxes to "
                "pixels — store metadata['width']/['height'] (e.g. via import) or keep "
                "the images openable.",
            )
        return [x * w, y * h, bw * w, bh * h]
    return [x, y, bw, bh]


def _to_norm_cxcywh(bbox, w, h, is_norm):
    """Convert an [x,y,w,h] box to normalized center [cx,cy,w,h] (YOLO). Raises if pixel but dims unknown."""
    x, y, bw, bh = bbox
    if not is_norm:
        if not w or not h:
            raise ValueError(
                "YOLO export needs image width/height to normalize pixel boxes — "
                "store metadata['width']/['height'] or keep the images openable.",
            )
        x, y, bw, bh = x / w, y / h, bw / w, bh / h
    return [x + bw / 2, y + bh / 2, bw, bh]


# --------------------------------------------------------------------------- #
# importers
# --------------------------------------------------------------------------- #


# One import used to be ONE POST. A real COCO train2017 (118k images, ~860k annotations)
# serialises to several hundred MB, so nginx returns 413 before the backend sees a byte and
# nothing is imported; below that, a single row-by-row transaction outlives the proxy timeout.
# Chunking also makes a failed import resumable with on_duplicate='skip'.
_IMPORT_BATCH = 1000


def _add_batched(dataset, samples, on_duplicate, batch_size=_IMPORT_BATCH):
    """add_samples in chunks, accumulating the per-chunk results into one dict."""
    ids: list = []
    skipped = 0
    existing: dict = {}
    for start in range(0, len(samples), batch_size):
        result = dataset.add_samples(samples[start : start + batch_size], on_duplicate=on_duplicate)
        ids.extend(result.get("ids", []) or [])
        skipped += result.get("skipped", 0) or 0
        # `existing` is how on_duplicate='error' reports WHICH filepaths collided. Dropping it
        # made the batched importers silently lose the duplicate report the single-request
        # version returned.
        existing.update(result.get("existing", {}) or {})
    out = {"ids": ids, "skipped": skipped, "count": len(ids)}
    if existing:
        out["existing"] = existing
    return out


def import_coco(dataset, images_dir, annotations_json, label_field="ground_truth", on_duplicate="skip"):
    """Import COCO-format detections (bbox is pixel ``[x, y, w, h]``)."""
    from .labels import Detection, Detections
    from .sample import Sample

    with open(annotations_json) as f:
        coco = json.load(f)
    cats = {c["id"]: c["name"] for c in coco.get("categories", [])}
    anns_by_img: Dict[Any, List[Any]] = {}
    for a in coco.get("annotations", []):
        anns_by_img.setdefault(a["image_id"], []).append(a)

    samples = []
    for img in coco.get("images", []):
        dets = []
        for a in anns_by_img.get(img["id"], []):
            x, y, w, h = a["bbox"]
            d = Detection(cats.get(a["category_id"], str(a["category_id"])), bbox=[x, y, w, h])
            if a.get("score") is not None:
                d["confidence"] = a["score"]
            if a.get("iscrowd"):
                d["attributes"] = {"iscrowd": 1}
            dets.append(d)
        md = {label_field: Detections(dets)}
        if img.get("width"):
            md["width"] = img["width"]
        if img.get("height"):
            md["height"] = img["height"]
        samples.append(Sample(os.path.join(images_dir, img["file_name"]), metadata=md))

    result = _add_batched(dataset, samples, on_duplicate)
    print(f"Imported {len(samples)} COCO images → '{dataset.name}'")
    return result


def import_yolo(dataset, images_dir, labels_dir, classes, label_field="ground_truth", on_duplicate="skip"):
    """Import YOLO detections. Each ``<image>.txt`` has lines ``cls cx cy w h [conf]`` (normalized)."""
    from .labels import Detection, Detections
    from .sample import Sample

    samples = []
    for img_path in sorted(glob.glob(os.path.join(images_dir, "*"))):
        if not img_path.lower().endswith(_IMG_EXTS):
            continue
        stem = os.path.splitext(os.path.basename(img_path))[0]
        label_path = os.path.join(labels_dir, stem + ".txt")
        dets = []
        if os.path.exists(label_path):
            with open(label_path) as f:
                for line in f:
                    parts = line.split()
                    if len(parts) < 5:
                        continue
                    cls = int(float(parts[0]))
                    cx, cy, bw, bh = map(float, parts[1:5])
                    x, y = cx - bw / 2, cy - bh / 2  # center → top-left (normalized)
                    if not (0 <= cls < len(classes)):
                        print(
                            f"  Warning: YOLO class index {cls} out of range for "
                            f"{len(classes)} classes (check the `classes` list); using raw index.",
                        )
                    label = classes[cls] if 0 <= cls < len(classes) else str(cls)
                    conf = float(parts[5]) if len(parts) > 5 else None
                    dets.append(Detection(label, bbox=[x, y, bw, bh], confidence=conf))
        samples.append(Sample(os.path.abspath(img_path), metadata={label_field: Detections(dets)}))

    result = _add_batched(dataset, samples, on_duplicate)
    print(f"Imported {len(samples)} YOLO images → '{dataset.name}'")
    return result


def import_voc(dataset, images_dir, annotations_dir, label_field="ground_truth", on_duplicate="skip"):
    """Import Pascal VOC detections (per-image XML; bndbox is pixel xmin/ymin/xmax/ymax)."""
    from .labels import Detection, Detections
    from .sample import Sample

    samples = []
    for xml_path in sorted(glob.glob(os.path.join(annotations_dir, "*.xml"))):
        root = ET.parse(xml_path).getroot()
        filename = root.findtext("filename")
        size = root.find("size")
        width_text = size.findtext("width") if size is not None else None
        height_text = size.findtext("height") if size is not None else None
        width = int(float(width_text)) if width_text else None
        height = int(float(height_text)) if height_text else None
        dets = []
        for obj in root.findall("object"):
            bb = obj.find("bndbox")
            if bb is None:
                continue
            xmin = float(cast("str", bb.findtext("xmin")))
            ymin = float(cast("str", bb.findtext("ymin")))
            xmax = float(cast("str", bb.findtext("xmax")))
            ymax = float(cast("str", bb.findtext("ymax")))
            dets.append(Detection(cast("str", obj.findtext("name")), bbox=[xmin, ymin, xmax - xmin, ymax - ymin]))

        img_path = os.path.join(images_dir, filename) if filename else None
        if not img_path or not os.path.exists(img_path):
            stem = os.path.splitext(os.path.basename(xml_path))[0]
            for ext in _IMG_EXTS:
                cand = os.path.join(images_dir, stem + ext)
                if os.path.exists(cand):
                    img_path = cand
                    break
        md: Dict[str, Any] = {label_field: Detections(dets)}
        if width:
            md["width"] = width
        if height:
            md["height"] = height
        samples.append(
            Sample(
                os.path.abspath(img_path) if img_path and os.path.exists(img_path) else (filename or xml_path),
                metadata=md,
            ),
        )

    result = _add_batched(dataset, samples, on_duplicate)
    print(f"Imported {len(samples)} VOC images → '{dataset.name}'")
    return result


def import_classification_dir(dataset, root, label_field="ground_truth", on_duplicate="skip"):
    """Import an ImageNet-style directory tree (``root/<class>/*.jpg`` → ``Classification(<class>)``)."""
    from .labels import Classification
    from .sample import Sample

    samples = []
    for cls in sorted(os.listdir(root)):
        cdir = os.path.join(root, cls)
        if not os.path.isdir(cdir):
            continue
        for fn in sorted(os.listdir(cdir)):
            if not fn.lower().endswith(_IMG_EXTS):
                continue
            samples.append(
                Sample(
                    os.path.abspath(os.path.join(cdir, fn)),
                    metadata={label_field: Classification(cls)},
                    tags=[cls],
                ),
            )

    result = _add_batched(dataset, samples, on_duplicate)
    print(f"Imported {len(samples)} images from classification tree → '{dataset.name}'")
    return result


# --------------------------------------------------------------------------- #
# exporters
# --------------------------------------------------------------------------- #


def export(view_or_dataset, export_dir, format="coco", label_field="ground_truth", normalized=None):
    """Export a dataset/view's detections to ``format`` (``"coco"`` or ``"yolo"``).

    ``normalized``: whether the stored bboxes are normalized (0-1). ``None`` (default)
    auto-detects per sample (all coords <= 1 ⇒ normalized).
    """
    fmt = format.lower()
    if fmt == "coco":
        return _export_coco(view_or_dataset, export_dir, label_field, normalized)
    if fmt == "yolo":
        return _export_yolo(view_or_dataset, export_dir, label_field, normalized)
    raise ValueError(f"Unsupported export format: {format!r} (use 'coco' or 'yolo')")


def _det_list(sample, label_field):
    v = sample.metadata.get(label_field)
    if isinstance(v, dict):
        return [v]
    return [d for d in v if isinstance(d, dict)] if isinstance(v, list) else []


def _unique_name(name, seen):
    """Disambiguate a basename/stem so duplicates don't overwrite each other."""
    if name not in seen:
        seen.add(name)
        return name
    i = 1
    root, ext = os.path.splitext(name)
    while f"{root}_{i}{ext}" in seen:
        i += 1
    out = f"{root}_{i}{ext}"
    seen.add(out)
    return out


def _export_coco(view_or_dataset, export_dir, label_field, normalized=None):
    os.makedirs(export_dir, exist_ok=True)
    images, annotations = [], []
    cat_ids: Dict[str, int] = {}  # label -> id
    ann_id = 1
    seen_names: Set[str] = set()

    for img_id, sample in enumerate(_iter_samples(view_or_dataset), start=1):
        w, h = _image_size(sample)
        dets = _det_list(sample, label_field)
        is_norm = _sample_is_normalized(dets) if normalized is None else normalized
        images.append(
            {
                "id": img_id,
                "file_name": _unique_name(os.path.basename(sample.filepath), seen_names),
                "width": w or 0,
                "height": h or 0,
            },
        )
        for det in dets:
            label = det.get("label")
            if label not in cat_ids:
                cat_ids[label] = len(cat_ids) + 1
            x, y, bw, bh = _to_pixel_xywh(det["bbox"], w, h, is_norm)
            ann = {
                "id": ann_id,
                "image_id": img_id,
                "category_id": cat_ids[label],
                "bbox": [x, y, bw, bh],
                "area": bw * bh,
                "iscrowd": int((det.get("attributes") or {}).get("iscrowd", 0)),
            }
            if det.get("confidence") is not None:
                ann["score"] = det["confidence"]
            annotations.append(ann)
            ann_id += 1

    coco = {
        "images": images,
        "annotations": annotations,
        "categories": [{"id": i, "name": name} for name, i in cat_ids.items()],
    }
    out = os.path.join(export_dir, "labels.json")
    with open(out, "w") as f:
        json.dump(coco, f)
    print(f"Exported {len(images)} images, {len(annotations)} detections → {out}")
    return out


def _export_yolo(view_or_dataset, export_dir, label_field, normalized=None):
    labels_dir = os.path.join(export_dir, "labels")
    os.makedirs(labels_dir, exist_ok=True)
    classes: List[str] = []
    seen_stems: Set[str] = set()

    n_imgs = n_dets = 0
    for sample in _iter_samples(view_or_dataset):
        w, h = _image_size(sample)
        dets = _det_list(sample, label_field)
        is_norm = _sample_is_normalized(dets) if normalized is None else normalized
        # Disambiguate so two images sharing a basename stem don't overwrite each other.
        stem = _unique_name(os.path.splitext(os.path.basename(sample.filepath))[0], seen_stems)
        lines = []
        for det in dets:
            label = det.get("label")
            if label not in classes:
                classes.append(label)
            cx, cy, bw, bh = _to_norm_cxcywh(det["bbox"], w, h, is_norm)
            conf = det.get("confidence")
            row = f"{classes.index(label)} {cx:.6f} {cy:.6f} {bw:.6f} {bh:.6f}"
            if conf is not None:
                row += f" {conf:.6f}"
            lines.append(row)
            n_dets += 1
        with open(os.path.join(labels_dir, stem + ".txt"), "w") as f:
            f.write("\n".join(lines))
        n_imgs += 1

    with open(os.path.join(export_dir, "classes.txt"), "w") as f:
        f.write("\n".join(classes))
    print(f"Exported {n_imgs} images, {n_dets} detections → {labels_dir} (+ classes.txt)")
    return labels_dir
