#!/usr/bin/env python3
"""Import local COCO val2017 images, all annotations, attribution, and inline CLIP vectors."""

import itertools
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'python'))

from fridge import Detection, Keypoint, Polygon, Polyline, Sample  # noqa: E402

from examples._shared.credits import DATASET_CREDITS  # noqa: E402
from examples._shared.importer import (  # noqa: E402
    AnnotationIndex,
    contained_path,
    ingest_objects,
    json_sections,
    parser,
    require_files,
    rgb_image,
)

ROOT = Path(__file__).resolve().parents[2]


ARRAY_FIELDS = ('class_names', 'supercategory_names')

FILES = {
    'instances': 'instances_val2017.json',
    'poses': 'person_keypoints_val2017.json',
    'captions': 'captions_val2017.json',
}


def build_parser():
    result = parser(
        __doc__,
        'coco-val2017',
        work_dir=True,
        limit_help='First N images with a selected license, in ID order; 0 = all',
    )
    result.add_argument(
        '--licenses',
        default='no-derivatives',
        help="'no-derivatives' (default: every license except NoDerivs), 'all', "
        "or comma-separated license IDs from instances_val2017.json",
    )
    return result


def select_licenses(value, licenses):
    preset = value.strip().lower()
    if preset == 'all':
        return set(licenses)
    if preset == 'no-derivatives':
        # The app serves resized thumbnails, object crops and patches, so NoDerivs photos are left out.
        return {key for key, item in licenses.items() if 'NoDeriv' not in item['name']}
    try:
        selected = {int(token.strip()) for token in value.split(',')}
    except ValueError as exc:
        raise ValueError("--licenses must be 'all', 'no-derivatives' or comma-separated integer license IDs") from exc
    if not selected <= licenses.keys():
        raise ValueError(f'Unknown license IDs: {sorted(selected - licenses.keys())}')
    return selected


class LicensedImages:
    """The index's images whose license is selected; --limit counts only those."""

    def __init__(self, index, allowed):
        self.index, self.allowed = index, allowed

    def images(self, limit):
        selected = (row for row in self.index.images(0) if row['license'] in self.allowed)
        return itertools.islice(selected, limit or None)


def build_index(root, index):
    require_files([root / 'annotations' / name for name in FILES.values()])
    categories: dict = {}
    licenses: dict = {}

    def records():
        for kind, filename in FILES.items():
            print(f'Indexing {filename}', flush=True)
            for section, row in json_sections(root / 'annotations' / filename):
                if section == 'categories':
                    categories.setdefault(row['id'], {}).update(row)
                elif section == 'licenses':
                    licenses[row['id']] = row
                elif section == 'images' and kind == 'instances':
                    # COCO filenames use zero-padded IDs; ordering must match numeric ID order.
                    yield 'images', f"{row['id']:012d}", row
                elif section == 'annotations':
                    yield kind, row['image_id'], row

    index.add(records())
    return categories, licenses


def decode_rle(rle):
    """Decode COCO uncompressed or signed/delta-compressed, column-major RLE."""
    height, width = map(int, rle['size'])
    counts = rle['counts']
    if isinstance(counts, str):
        decoded: list[int] = []
        position = 0
        while position < len(counts):
            value = shift = 0
            while True:
                if position >= len(counts):
                    raise ValueError('Truncated compressed COCO RLE')
                char = ord(counts[position]) - 48
                position += 1
                if not 0 <= char <= 63:
                    raise ValueError('Invalid compressed COCO RLE character')
                value |= (char & 31) << shift
                shift += 5
                if not char & 32:
                    if char & 16:
                        value |= -1 << shift
                    break
            if len(decoded) > 2:
                value += decoded[-2]
            decoded.append(value)
        counts = decoded
    if height <= 0 or width <= 0 or any(c < 0 for c in counts) or sum(counts) != height * width:
        raise ValueError('Invalid COCO RLE size/counts')
    pixels = np.zeros(height * width, dtype=np.uint8)
    offset = 0
    for i, count in enumerate(counts):
        if i % 2:
            pixels[offset : offset + count] = 255
        offset += count
    return Image.fromarray(pixels.reshape((height, width), order='F'))


def map_sample(root, image, index, categories, licenses, assets, metadata_only=False, annotations_only=False):
    path = contained_path(root / 'val2017' / image['file_name'], root)
    if metadata_only:
        return str(path)
    require_files([path])
    width, height = image['width'], image['height']
    license_info = licenses[image['license']]
    metadata = {
        **{key: value for key, value in image.items() if key != 'id'},
        'image_id': image['id'],
        'split': 'val2017',
        'license_id': image['license'],
        'license_name': license_info['name'],
        'license_url': license_info['url'],
        'detections': [],
        'polygons': [],
        'keypoints': [],
        'skeletons': [],
        'captions': [],
    }
    classes, supercategories = set(), set()
    mask_count = 0
    for annotation in index.rows('instances', image['id']):
        category = categories[annotation['category_id']]
        label = category['name']
        classes.add(label)
        supercategories.add(category['supercategory'])
        attrs = {
            key: value for key, value in annotation.items() if key not in ('id', 'bbox', 'segmentation', 'image_id')
        }
        attrs['annotation_id'] = annotation['id']
        attrs['supercategory'] = category['supercategory']
        bbox = [value / scale for value, scale in zip(annotation['bbox'], (width, height, width, height))]
        stable_id = f"coco-{annotation['id']}"
        detection = Detection(label, bbox, id=stable_id)
        detection.update(attrs)
        detection['is_crowd'] = bool(annotation['iscrowd'])
        metadata['detections'].append(detection)
        segmentation = annotation.get('segmentation', [])
        if isinstance(segmentation, list):
            paths = [
                [[ring[i] / width, ring[i + 1] / height] for i in range(0, len(ring), 2)]
                for ring in segmentation
                if ring
            ]
            if paths:
                metadata['polygons'].append(Polygon(label, paths=paths, id=f'{stable_id}-polygon', **attrs))
        else:
            field = f'crowd_mask_{mask_count:03d}' if annotation['iscrowd'] else f'instance_mask_{mask_count:03d}'
            mask = None if annotations_only else decode_rle(segmentation)
            try:
                field, descriptor = assets.mask(
                    f"coco/{image['id']:012d}/{annotation['id']}.png",
                    mask,
                    field,
                    annotation['category_id'],
                    label,
                    mask_count,
                )
            finally:
                if mask is not None:
                    mask.close()
            descriptor.update(id=f'{stable_id}-mask', detection_id=stable_id)
            metadata[field] = descriptor
            mask_count += 1
    for annotation in index.rows('poses', image['id']):
        category = categories[annotation['category_id']]
        names = category['keypoints']
        triples = list(zip(*[iter(annotation['keypoints'])] * 3))
        visible = [i for i, (_, _, visibility) in enumerate(triples) if visibility > 0]
        points = [[triples[i][0] / width, triples[i][1] / height] for i in visible]
        edges = [[a - 1, b - 1] for a, b in category['skeleton']]
        attrs = {key: value for key, value in annotation.items() if key != 'id'}
        attrs.update(
            annotation_id=annotation['id'],
            names=[names[i] for i in visible],
            all_names=names,
            point_indices=visible,
            visibility=[v for _, _, v in triples],
            skeleton=edges,
        )
        metadata['keypoints'].append(Keypoint(points, label=category['name'], id=f"pose-{annotation['id']}", **attrs))
        paths = [
            [[triples[i][0] / width, triples[i][1] / height] for i in edge]
            for edge in edges
            if all(triples[i][2] > 0 for i in edge)
        ]
        for bone, points in enumerate(paths):
            metadata['skeletons'].append(
                Polyline(
                    category['name'],
                    points=points,
                    id=f"skeleton-{annotation['id']}-{bone}",
                    annotation_id=annotation['id'],
                ),
            )
    for annotation in index.rows('captions', image['id']):
        metadata['captions'].append(annotation['caption'])
    metadata.update(
        caption_text='\n'.join(metadata['captions']),
        classes=sorted(classes),
        supercategories=sorted(supercategories),
        mask_count=mask_count,
    )
    return Sample(
        str(path),
        tags=['val2017'],
        metadata=metadata,
        array_fields={'class_names': sorted(classes), 'supercategory_names': sorted(supercategories)},
    ), None if annotations_only else rgb_image(path)


def main():
    args = build_parser().parse_args()
    root = args.data_root.resolve()
    with AnnotationIndex(args.work_dir or root / '.work', root) as index:
        categories, licenses = build_index(root, index)
        allowed = select_licenses(args.licenses, licenses)
        excluded = sorted(set(licenses) - allowed)
        print(f'Licenses: importing IDs {sorted(allowed)}; excluding {excluded or "none"}', flush=True)
        ingest_objects(
            args,
            LicensedImages(index, allowed),
            lambda row, assets, **kwargs: map_sample(root, row, index, categories, licenses, assets, **kwargs),
            DATASET_CREDITS['coco-val2017'],
            array_fields=ARRAY_FIELDS,
            refresh_description=True,
        )


if __name__ == '__main__':
    main()
