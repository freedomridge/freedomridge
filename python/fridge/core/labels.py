"""Typed label classes for Freedom Ridge samples.

These are thin, ergonomic wrappers over the metadata conventions the Freedom Ridge
frontend already renders and the backend already discovers/filters. Crucially,
each label **is** a plain JSON type (a ``dict`` or ``list`` subclass), so it is
natively JSON-serializable, uploads with ``add_samples`` exactly like a hand-built
dict, and renders in the grid/modal with zero conversion — while giving you
attribute access, stable ids, and conversions.

Conventions (what the frontend's ``detectLabelType`` recognizes):

- Detections:    a LIST of ``{"label", "bbox", "confidence"?, "attributes"?}``
- Classification: a dict ``{"label", "confidence"?, "attributes"?}``
- Classifications: a LIST of those dicts
- Polylines:     a LIST of ``{"label", "points"|"paths", "closed", ...}``
- Keypoints:     a LIST of ``{"label"?, "points": [[x,y], ...], "confidence"?}``
- Segmentation:  a dict ``{"maskPath", "labels"?}``
- Heatmap:       a dict ``{"type": "heatmap", "map_path", "range", "colormap"?}``

Example:
    from fridge import Sample, Detection, Detections

    sample = Sample("/img.jpg")
    sample.predictions = Detections([
        Detection("car", bbox=[0.1, 0.2, 0.3, 0.4], confidence=0.92),
        Detection("person", bbox=[0.5, 0.1, 0.1, 0.3], confidence=0.71),
    ])
    dataset.add_samples([sample])   # uploads + renders, no conversion needed

Note: these are construction/serialization helpers. When you read samples back
(iter_samples, views, eval, export), labels come back as plain dicts/lists — the
typed wrappers (and ``.area`` etc.) are not reconstructed; access values by key.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional, Sequence, Tuple


def _new_id() -> str:
    """Short unique id for a label object (selection / evaluation bookkeeping).

    Generated per construction (uuid4), so re-running an ingest produces new ids;
    pass ``id=`` explicitly if you need reproducible ids.
    """
    return uuid.uuid4().hex[:24]


class _LabelDict(dict):
    """A dict that also supports attribute access (``det.label``)."""

    _is_fridge_label = True

    def __getattr__(self, name: str):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value

    def __delattr__(self, name: str) -> None:
        try:
            del self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def to_dict(self) -> dict:
        return dict(self)


class _LabelList(list):
    """A list of label dicts (e.g. a Detections field)."""

    _is_fridge_label = True

    def to_list(self) -> list:
        return list(self)


# =============================================================================
# Cuboids
# =============================================================================


class Cuboid(_LabelDict):
    """A 3D box with geometric centre and [length, width, height] in metres.

    Camera axes are x right, y down, z forward (yaw about y); LiDAR axes are
    x forward, y left, z up (yaw about z). Zero yaw heads along +x.
    """

    def __init__(
        self,
        label,
        center,
        size,
        yaw,
        frame='camera',
        confidence=None,
        truncated=None,
        occluded=None,
        alpha=None,
        id=None,
        **attributes,
    ):
        from .geometry3d import finite_vector

        center = finite_vector(center, 3, 'center')
        size = finite_vector(size, 3, 'size')
        yaw = finite_vector([yaw], 1, 'yaw')[0]
        if any(v <= 0 for v in size):
            raise ValueError('size must be positive')
        if frame not in ('camera', 'lidar'):
            raise ValueError('frame must be camera or lidar')
        super().__init__(
            _type='cuboid',
            id=id or _new_id(),
            label=label,
            center=center,
            size=size,
            yaw=yaw,
            frame=frame,
        )
        for key, value in dict(confidence=confidence, truncated=truncated, occluded=occluded, alpha=alpha).items():
            if value is not None:
                self[key] = value
        if attributes:
            self['attributes'] = attributes

    @classmethod
    def from_kitti(cls, line, image_size):
        """Convert one KITTI label line (bottom centre) to a camera-frame cuboid.

        Retains the normalized annotated 2D box and benchmark difficulty as
        attributes. DontCare regions have no valid cuboid and must stay 2D.
        """
        from .geometry3d import finite_vector

        fields = line.split()
        if len(fields) not in (15, 16) or fields[0] == 'DontCare':
            raise ValueError('Expected a non-DontCare KITTI 3D label line (15 or 16 fields)')
        truncated, occluded, alpha, x1, y1, x2, y2, h, w, length, x, y, z, yaw = finite_vector(
            fields[1:15],
            14,
            'KITTI label',
        )
        iw, ih = finite_vector(image_size, 2, 'image_size')
        if iw <= 0 or ih <= 0:
            raise ValueError('image_size must be positive')
        difficulty = 'unrated'
        for name, min_height, max_occluded, max_truncated in (
            ('easy', 40, 0, 0.15),
            ('moderate', 25, 1, 0.3),
            ('hard', 25, 2, 0.5),
        ):
            if y2 - y1 > min_height and occluded <= max_occluded and truncated <= max_truncated:
                difficulty = name
                break
        return cls(
            fields[0],
            [x, y - h / 2, z],
            [length, w, h],
            yaw,
            confidence=float(fields[15]) if len(fields) == 16 else None,
            truncated=truncated,
            occluded=int(occluded),
            alpha=alpha,
            bbox_2d=[x1 / iw, y1 / ih, (x2 - x1) / iw, (y2 - y1) / ih],
            difficulty=difficulty,
        )

    def corners(self):
        """Return the eight corners in this cuboid's frame."""
        from .geometry3d import cuboid_corners

        return cuboid_corners(self)


class Cuboids(_LabelList):
    """A list of :class:`Cuboid` labels for one sample field."""

    def __init__(self, cuboids: Optional[Sequence[dict]] = None):
        super().__init__(cuboids or [])


# =============================================================================
# Detections
# =============================================================================


class Detection(_LabelDict):
    """A single object detection.

    Args:
        label: class label.
        bbox: ``[x, y, width, height]`` (top-left + size). Normalized to [0, 1]
            or pixel coordinates — the frontend auto-detects (values > 2 ⇒ pixels).
        confidence: optional model confidence.
        index: optional integer instance index (for tracking).
        id: optional stable id (auto-generated if omitted).
        **attributes: any extra per-object attributes (stored under "attributes").
    """

    def __init__(
        self,
        label: str,
        bbox,
        confidence: Optional[float] = None,
        index: Optional[int] = None,
        id: Optional[str] = None,
        **attributes: Any,
    ):
        if isinstance(bbox, dict):  # accept {x, y, width, height}
            bbox = [bbox["x"], bbox["y"], bbox["width"], bbox["height"]]
        bbox = list(bbox)
        if len(bbox) != 4:
            raise ValueError("bbox must be [x, y, width, height] (4 values)")
        d: Dict[str, Any] = {"label": label, "bbox": bbox, "id": id or _new_id()}
        if confidence is not None:
            d["confidence"] = confidence
        if index is not None:
            d["index"] = index
        if attributes:
            d["attributes"] = attributes
        super().__init__(d)

    @property
    def area(self) -> float:
        """Bounding-box area in the bbox's own units (normalized or pixels)."""
        _, _, w, h = self["bbox"]
        return float(w) * float(h)


class Detections(_LabelList):
    """A list of :class:`Detection` for one sample field."""

    def __init__(self, detections: Optional[Sequence[dict]] = None):
        super().__init__(detections or [])


# =============================================================================
# Classifications
# =============================================================================


class Classification(_LabelDict):
    """A single classification label."""

    def __init__(self, label: str, confidence: Optional[float] = None, id: Optional[str] = None, **attributes: Any):
        d: Dict[str, Any] = {"label": label, "id": id or _new_id()}
        if confidence is not None:
            d["confidence"] = confidence
        if attributes:
            d["attributes"] = attributes
        super().__init__(d)


class Classifications(_LabelList):
    """A list of :class:`Classification` (multi-label classification)."""

    def __init__(self, classifications: Optional[Sequence[dict]] = None):
        super().__init__(classifications or [])


# =============================================================================
# Polylines / Polygons
# =============================================================================


class Polyline(_LabelDict):
    """A polyline or polygon.

    Provide EITHER ``points`` (a single ring: ``[[x, y], ...]``) OR ``paths``
    (multiple rings for holes: ``[[[x, y], ...], ...]``). Coordinates are
    normalized [0, 1] or pixel.

    Args:
        label: class label.
        points: single ring of ``[x, y]`` vertices.
        paths: multiple rings (exterior + holes).
        closed: whether the shape is closed (True = polygon, False = open line).
        filled: whether to fill the shape (polygon) vs draw a line only.
        id: optional stable id.
        **attributes: extra per-object attributes.
    """

    def __init__(
        self,
        label: str,
        points: Optional[Sequence[Sequence[float]]] = None,
        paths: Optional[Sequence[Sequence[Sequence[float]]]] = None,
        closed: bool = False,
        filled: bool = False,
        id: Optional[str] = None,
        **attributes: Any,
    ):
        if points is None and paths is None:
            raise ValueError("Polyline requires either `points` or `paths`")
        d: Dict[str, Any] = {"label": label, "closed": closed, "filled": filled, "id": id or _new_id()}
        if points is not None:
            d["points"] = [list(p) for p in points]
        if paths is not None:
            d["paths"] = [[list(p) for p in ring] for ring in paths]
        if attributes:
            d["attributes"] = attributes
        super().__init__(d)


class Polylines(_LabelList):
    """A list of :class:`Polyline`."""

    def __init__(self, polylines: Optional[Sequence[dict]] = None):
        super().__init__(polylines or [])


# Polygon is a shorthand for a closed, filled Polyline.
def Polygon(label: str, points=None, paths=None, id: Optional[str] = None, **attributes: Any) -> Polyline:
    """A closed, filled :class:`Polyline`."""
    return Polyline(label, points=points, paths=paths, closed=True, filled=True, id=id, **attributes)


# =============================================================================
# Keypoints
# =============================================================================


class Keypoint(_LabelDict):
    """A set of keypoints for one object (e.g. a pose skeleton).

    Args:
        points: ``[[x, y], ...]`` keypoint coordinates (normalized or pixel).
        label: optional object label.
        confidence: optional per-object or per-point confidence (scalar or list).
        id: optional stable id.
        **attributes: extra attributes.
    """

    def __init__(
        self,
        points: Sequence[Sequence[float]],
        label: Optional[str] = None,
        confidence: Optional[Any] = None,
        id: Optional[str] = None,
        **attributes: Any,
    ):
        # The "_type" marker lets the frontend disambiguate keypoints from
        # polygons (both carry a "points" array).
        d: Dict[str, Any] = {"_type": "keypoints", "points": [list(p) for p in points], "id": id or _new_id()}
        if label is not None:
            d["label"] = label
        if confidence is not None:
            d["confidence"] = confidence
        if attributes:
            d["attributes"] = attributes
        super().__init__(d)


class Keypoints(_LabelList):
    """A list of :class:`Keypoint`."""

    def __init__(self, keypoints: Optional[Sequence[dict]] = None):
        super().__init__(keypoints or [])


# =============================================================================
# Segmentation / Heatmap (mask & overlay conventions)
# =============================================================================


class Segmentation(_LabelDict):
    """A semantic-segmentation mask stored on disk (s3:// or local path).

    Args:
        mask_path: path to the mask PNG (stored as the ``maskPath`` key the
            frontend expects).
        labels: optional list of class names present in the mask.
        mask_targets: optional ``{pixel_or_hex: class_name}`` mapping.
    """

    def __init__(
        self,
        mask_path: str,
        labels: Optional[List[str]] = None,
        mask_targets: Optional[Dict[Any, str]] = None,
    ):
        d: Dict[str, Any] = {"maskPath": mask_path}
        if labels is not None:
            d["labels"] = list(labels)
        if mask_targets is not None:
            d["mask_targets"] = mask_targets
        super().__init__(d)


class Heatmap(_LabelDict):
    """A heatmap overlay (single-channel PNG colormapped over the image).

    Mirrors ``Sample.add_heatmap``. ``range`` order is meaningful — ``(100, 0)``
    inverts the colormap.
    """

    def __init__(self, map_path: str, range: Tuple[float, float], colormap: Optional[str] = None):
        if len(range) != 2:
            raise ValueError("range must be (start, end)")
        try:
            start, end = float(range[0]), float(range[1])
        except (TypeError, ValueError):
            raise ValueError("range must be two numbers (start, end)")
        if start == end:
            raise ValueError("range start and end must differ")
        d: Dict[str, Any] = {"type": "heatmap", "map_path": map_path, "range": [start, end]}
        if colormap:
            d["colormap"] = colormap
        super().__init__(d)


__all__ = [
    "Cuboid",
    "Cuboids",
    "Detection",
    "Detections",
    "Classification",
    "Classifications",
    "Polyline",
    "Polylines",
    "Polygon",
    "Keypoint",
    "Keypoints",
    "Segmentation",
    "Heatmap",
]
