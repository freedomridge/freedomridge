"""Evaluation helpers for Freedom Ridge: compare a predictions label field against
a ground-truth label field and report metrics.

All evaluations follow Freedom Ridge's compute-in-SDK / store-results pattern: they
read the label metadata fields over the API, compute metrics in Python, write a
per-sample result into ``metadata[eval_key...]`` (so you can sort/filter on it),
refresh field stats, and return a summary report.

Used via the thin Dataset methods: ``dataset.evaluate_detections(...)``,
``dataset.evaluate_classifications(...)``, ``dataset.evaluate_segmentations(...)``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _iou_xywh(a, b) -> float:
    """IoU of two ``[x, y, w, h]`` boxes (same coordinate units)."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ax2, ay2, bx2, by2 = ax + aw, ay + ah, bx + bw, by + bh
    ix1, iy1 = max(ax, bx), max(ay, by)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def _as_list(value) -> List[dict]:
    """Normalize a label field to a list of label dicts."""
    if value is None:
        return []
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    return []


def _top_label(value) -> Optional[str]:
    """Extract the single (top-confidence) label from a classification field."""
    items = _as_list(value)
    if not items:
        return None
    best = max(items, key=lambda d: d.get("confidence", 0.0))
    return best.get("label")


def _ap_from_scores(scores: List[tuple], num_gt: int) -> float:
    """All-point (COCO-style) average precision.

    scores: list of (confidence, is_tp) for every prediction of a class, across
    all samples. num_gt: total ground-truth objects of that class.
    """
    if num_gt == 0:
        return 0.0
    scores = sorted(scores, key=lambda s: s[0], reverse=True)
    tp = fp = 0
    precisions, recalls = [], []
    for _, is_tp in scores:
        if is_tp:
            tp += 1
        else:
            fp += 1
        precisions.append(tp / (tp + fp))
        recalls.append(tp / num_gt)
    if not precisions:
        return 0.0
    # Monotonic (all-point) interpolation of the PR curve.
    ap = 0.0
    prev_recall = 0.0
    # Make precision monotonically decreasing from the right.
    for i in range(len(precisions) - 2, -1, -1):
        precisions[i] = max(precisions[i], precisions[i + 1])
    for i in range(len(recalls)):
        if recalls[i] != prev_recall:
            ap += (recalls[i] - prev_recall) * precisions[i]
            prev_recall = recalls[i]
    return ap


# --------------------------------------------------------------------------- #
# classification
# --------------------------------------------------------------------------- #


def evaluate_classifications(
    dataset,
    pred_field: str,
    gt_field: str,
    eval_key: str = "eval",
    classes: Optional[List[str]] = None,
) -> dict:
    """Evaluate single-label classifications.

    Writes ``metadata[eval_key]`` = "correct"/"incorrect" per sample, plus
    ``metadata[eval_key + "_pred"]`` / ``[eval_key + "_gt"]``. Returns a report
    with accuracy, support, and a confusion matrix.
    """
    results, preds, gts = {}, [], []
    seen = set()
    correct = total = 0

    for sample in dataset.iter_samples(progress=True):
        p = _top_label(sample.metadata.get(pred_field))
        g = _top_label(sample.metadata.get(gt_field))
        if g is None:
            continue  # only evaluate samples that have a ground-truth label
        total += 1
        ok = p == g
        correct += int(ok)
        results[sample.id] = {eval_key: "correct" if ok else "incorrect", f"{eval_key}_pred": p, f"{eval_key}_gt": g}
        preds.append(p)
        gts.append(g)
        if p is not None:
            seen.add(p)
        if g is not None:
            seen.add(g)

    if results:
        dataset.set_values_per_sample_multi(results)
        dataset.refresh_stats()

    labels = classes or sorted(x for x in seen if x is not None)
    index = {c: i for i, c in enumerate(labels)}
    matrix = [[0] * len(labels) for _ in labels]
    for g, p in zip(gts, preds):
        if g in index and p in index:
            matrix[index[g]][index[p]] += 1

    print(
        f"Classifications: {correct}/{total} correct ({correct / total:.3f} accuracy)"
        if total
        else "No labeled samples",
    )
    return {
        "eval_key": eval_key,
        "num_samples": total,
        "accuracy": correct / total if total else 0.0,
        "classes": labels,
        "confusion_matrix": matrix,  # rows = ground truth, cols = predictions
    }


# --------------------------------------------------------------------------- #
# detection
# --------------------------------------------------------------------------- #


def evaluate_detections(
    dataset,
    pred_field: str,
    gt_field: str,
    eval_key: str = "eval",
    iou: float = 0.5,
    classes: Optional[List[str]] = None,
) -> dict:
    """Evaluate object detections (greedy per-image matching at one IoU threshold).

    Writes per-sample ``metadata[eval_key + "_tp"/"_fp"/"_fn"]`` counts (so you
    can filter e.g. for samples with false negatives). Returns a report with
    overall precision/recall, per-class AP, and mAP at the given IoU threshold.
    """
    per_sample = {}
    # per-class accumulators for AP
    class_scores: Dict[str, List[tuple]] = {}
    class_num_gt: Dict[str, int] = {}
    tot_tp = tot_fp = tot_fn = 0
    pred_max = gt_max = 0.0  # to detect normalized-vs-pixel unit mismatch

    def _valid_box(b):
        return isinstance(b, (list, tuple)) and len(b) == 4

    for sample in dataset.iter_samples(progress=True):
        preds = sorted(_as_list(sample.metadata.get(pred_field)), key=lambda d: d.get("confidence", 0.0), reverse=True)
        gts = _as_list(sample.metadata.get(gt_field))
        matched = [False] * len(gts)
        tp = fp = 0

        for g in gts:
            c: Any = g.get("label")
            class_num_gt[c] = class_num_gt.get(c, 0) + 1
            if _valid_box(g.get("bbox")):
                gt_max = max(gt_max, max(g["bbox"]))

        for pred in preds:
            plabel: Any = pred.get("label")
            pbox: Any = pred.get("bbox")
            conf = pred.get("confidence", 0.0)
            if not _valid_box(pbox):
                fp += 1  # a prediction without a usable bbox is a false positive
                class_scores.setdefault(plabel, []).append((conf, False))
                continue
            pred_max = max(pred_max, max(pbox))
            best_iou, best_j = 0.0, -1
            for j, gt in enumerate(gts):
                if matched[j] or gt.get("label") != plabel or not _valid_box(gt.get("bbox")):
                    continue
                iou_val = _iou_xywh(pbox, gt["bbox"])
                if iou_val >= iou and iou_val > best_iou:
                    best_iou, best_j = iou_val, j
            is_tp = best_j >= 0
            if is_tp:
                matched[best_j] = True
                tp += 1
            else:
                fp += 1
            class_scores.setdefault(plabel, []).append((conf, is_tp))

        fn = matched.count(False)
        tot_tp += tp
        tot_fp += fp
        tot_fn += fn
        if preds or gts:
            per_sample[sample.id] = {
                f"{eval_key}_tp": tp,
                f"{eval_key}_fp": fp,
                f"{eval_key}_fn": fn,
            }

    if per_sample:
        dataset.set_values_per_sample_multi(per_sample)
        dataset.refresh_stats()

    # Heuristic unit-mismatch warning: one field looks normalized, the other pixel.
    if pred_max and gt_max and ((pred_max <= 1.5) != (gt_max <= 1.5)):
        print(
            f"  Warning: '{pred_field}' and '{gt_field}' bboxes look like different "
            f"coordinate units (pred max={pred_max:.2f}, gt max={gt_max:.2f}); "
            f"IoU will be wrong. Use the same normalized/pixel space for both.",
        )

    labels = classes or sorted(class_num_gt.keys())
    per_class_ap = {c: _ap_from_scores(class_scores.get(c, []), class_num_gt.get(c, 0)) for c in labels}
    mAP = sum(per_class_ap.values()) / len(per_class_ap) if per_class_ap else 0.0
    precision = tot_tp / (tot_tp + tot_fp) if (tot_tp + tot_fp) else 0.0
    recall = tot_tp / (tot_tp + tot_fn) if (tot_tp + tot_fn) else 0.0

    print(
        f"Detections @IoU={iou}: P={precision:.3f} R={recall:.3f} mAP={mAP:.3f} (TP={tot_tp} FP={tot_fp} FN={tot_fn})",
    )
    return {
        "eval_key": eval_key,
        "iou": iou,
        "tp": tot_tp,
        "fp": tot_fp,
        "fn": tot_fn,
        "precision": precision,
        "recall": recall,
        "mAP": mAP,
        "per_class_ap": per_class_ap,
        "classes": labels,
    }


# --------------------------------------------------------------------------- #
# segmentation
# --------------------------------------------------------------------------- #


def evaluate_segmentations(
    dataset,
    pred_field: str,
    gt_field: str,
    eval_key: str = "eval",
    image_loader=None,
    num_classes: Optional[int] = None,
) -> dict:
    """Evaluate semantic-segmentation masks (index masks: pixel value = class id).

    Loads the pred and gt mask PNGs (grayscale class-id masks), computes per-
    sample pixel accuracy + mean IoU (stored in metadata), and returns a report
    with dataset-wide mean pixel accuracy and mean IoU.

    Note: assumes index (grayscale) masks where the pixel value is the class id.
    RGB masks must be converted to index masks first.
    """
    import numpy as np

    from .dataset import _default_s3_image_loader

    if image_loader is None:
        image_loader = _default_s3_image_loader()

    def _mask_path(value):
        if isinstance(value, dict):
            return value.get("maskPath") or value.get("mask_path")
        return value if isinstance(value, str) else None

    per_sample = {}
    pa_sum = n = 0.0
    # Track WHY samples were skipped so a tiny mIoU over an unrepresentative subset (or a totally
    # broken loader) isn't silently reported as a clean result.
    skipped_missing = load_failed = shape_mismatch = 0
    # Standard mIoU accumulates intersection/union per class across the WHOLE
    # dataset (not a mean of per-image mIoUs).
    inter_by_class: Dict[int, float] = {}
    union_by_class: Dict[int, float] = {}

    for sample in dataset.iter_samples(progress=True):
        pp = _mask_path(sample.metadata.get(pred_field))
        gp = _mask_path(sample.metadata.get(gt_field))
        if not pp or not gp:
            skipped_missing += 1
            continue
        try:
            pred = np.asarray(image_loader(pp).convert("L"))
            gt = np.asarray(image_loader(gp).convert("L"))
        except Exception:
            load_failed += 1
            continue
        if pred.shape != gt.shape:
            shape_mismatch += 1
            continue

        pixel_acc = float((pred == gt).mean())
        present = np.unique(np.concatenate([pred.ravel(), gt.ravel()]))
        ious = []  # per-image, stored per-sample for filtering
        for c in present:
            inter = float(np.logical_and(pred == c, gt == c).sum())
            union = float(np.logical_or(pred == c, gt == c).sum())
            inter_by_class[int(c)] = inter_by_class.get(int(c), 0.0) + inter
            union_by_class[int(c)] = union_by_class.get(int(c), 0.0) + union
            if union > 0:
                ious.append(inter / union)
        img_miou = float(sum(ious) / len(ious)) if ious else 0.0

        per_sample[sample.id] = {f"{eval_key}_pixel_acc": pixel_acc, f"{eval_key}_miou": img_miou}
        pa_sum += pixel_acc
        n += 1

    if per_sample:
        dataset.set_values_per_sample_multi(per_sample)
        dataset.refresh_stats()

    # Masks WERE referenced but none could be evaluated → a broken loader / wrong paths / mismatched
    # resolutions, NOT a clean "no masks" run. Fail loudly instead of returning mIoU=0 as success.
    if not n and (load_failed or shape_mismatch):
        raise RuntimeError(
            f"Segmentation eval found 0 evaluable mask pairs but {load_failed} failed to load and "
            f"{shape_mismatch} had shape mismatches — check the mask paths / image loader.",
        )

    class_ious = {c: inter_by_class[c] / union_by_class[c] for c in union_by_class if union_by_class[c] > 0}
    mean_iou = sum(class_ious.values()) / len(class_ious) if class_ious else 0.0
    mean_pa = pa_sum / n if n else 0.0

    if load_failed or shape_mismatch:
        print(f"  Note: skipped {load_failed} unloadable and {shape_mismatch} shape-mismatched mask pairs.")
    print(
        f"Segmentations: pixel_acc={mean_pa:.3f} mIoU={mean_iou:.3f} ({int(n)} samples)"
        if n
        else "No mask pairs found",
    )
    return {
        "eval_key": eval_key,
        "num_samples": int(n),
        "mean_pixel_accuracy": mean_pa,
        "mean_iou": mean_iou,  # dataset-wide (global) class-mean IoU
        "per_class_iou": class_ious,
        # Surfaced so a caller can see how representative num_samples is.
        "skipped_missing": skipped_missing,
        "load_failed": load_failed,
        "shape_mismatch": shape_mismatch,
    }
