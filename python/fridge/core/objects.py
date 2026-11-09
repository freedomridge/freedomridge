"""Producer-side cuboid crop materialization for the existing object-row model."""

import hashlib
import math
from pathlib import Path

from .sample import Sample


def objects_from_cuboids(
    parent_id,
    parent_filepath,
    image,
    cuboids,
    calibration,
    crops_dir,
    location=None,
    *,
    field,
    write_crops=True,
):
    """Write projected JPEG crops and return object Samples for the parent's dataset.

    ``image`` is a full-resolution PIL image matching calibration.image_size. Boxes
    labelled DontCare or with no image extent are skipped. Stable parent paths and
    field names and cuboid ids produce stable, path-safe crop names for on_duplicate='update'.
    With ``write_crops=False``, image may be None: return rows without filesystem
    writes so an importer can check duplicates before atomically rendering crops.
    Extra attributes are copied first; they cannot overwrite the canonical links
    or geometry. Regenerate both parent labels and object rows after label edits.
    """
    if write_crops and list(image.size) != calibration['image_size']:
        raise ValueError('image size must match calibration.image_size')
    directory = Path(crops_dir)
    if write_crops:
        directory.mkdir(parents=True, exist_ok=True)
    width, height = calibration['image_size']
    rows = []
    for cuboid in cuboids:
        if cuboid['label'] == 'DontCare':
            continue
        _, extent = calibration.project(cuboid)
        if extent is None:
            continue
        x, y, w, h = extent
        # Include the field so ground-truth and prediction ids cannot share a crop.
        digest = hashlib.sha256(f'{parent_filepath}\0{field}\0{cuboid["id"]}'.encode()).hexdigest()
        filepath = directory / f'{digest}.jpg'
        if write_crops:
            from .image_modes import to_rgb

            with image.crop((math.floor(x), math.floor(y), math.ceil(x + w), math.ceil(y + h))) as crop:
                with to_rgb(crop) as rgb:
                    rgb.save(filepath, format='JPEG')
        metadata = {
            **cuboid.get('attributes', {}),
            'parent_id': parent_id,
            'parent_filepath': str(parent_filepath),
            'cuboid_id': cuboid['id'],
            'cuboid_field': field,
            'crop_bbox': [x / width, y / height, w / width, h / height],
            'label': cuboid['label'],
            'distance_m': calibration.distance(cuboid),
            'length_m': cuboid['size'][0],
            'width_m': cuboid['size'][1],
            'height_m': cuboid['size'][2],
            'yaw': cuboid['yaw'],
        }
        for key in ('confidence', 'truncated', 'occluded', 'alpha'):
            if key in cuboid:
                metadata[key] = cuboid[key]
        row = Sample(str(filepath), metadata=metadata, location=location)
        row.media_type = 'object'
        rows.append(row)
    return rows
