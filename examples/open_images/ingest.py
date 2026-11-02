#!/usr/bin/env python3
"""Import every annotation family from a local Open Images V7 validation download."""

import csv
import html
import json
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'python'))

from fridge import Classification, Detection, Keypoint, Sample  # noqa: E402

from examples._shared.credits import DATASET_CREDITS  # noqa: E402
from examples._shared.importer import (  # noqa: E402
    AnnotationIndex,
    contained_path,
    ingest_objects,
    parser,
    require_files,
    rgb_image,
)

ROOT = Path(__file__).resolve().parents[2]


ARRAY_FIELDS = (
    'class_names',
    'parent_classes',
    'negative_parent_classes',
    'positive_classes',
    'negative_classes',
    'point_classes',
    'point_states',
    'relationship_predicates',
    'mask_classes',
)

CSV_FILES = {
    'images': 'validation-images-with-rotation.csv',
    'detections': 'validation-annotations-bbox.csv',
    'boxable_labels': 'validation-annotations-human-imagelabels-boxable.csv',
    'image_labels': 'oidv7-val-annotations-human-imagelabels.csv',
    'points': 'oidv7-val-annotations-point-labels.csv',
    'masks': 'validation-annotations-object-segmentation.csv',
    'relationships': 'oidv6-validation-annotations-vrd.csv',
}
DESCRIPTIONS = ('oidv7-class-descriptions.csv', 'oidv7-class-descriptions-boxable.csv')
HIERARCHY = 'bbox_labels_600_hierarchy.json'
ATTRIBUTE_DESCRIPTIONS = 'oidv6-attributes-description.csv'
NARRATIVES = 'open_images_validation_localized_narratives.jsonl'
# Official v6 attribute descriptions; used even when the optional CSV was not downloaded.
# https://storage.googleapis.com/openimages/v6/oidv6-attributes-description.csv
ATTRIBUTE_NAMES = {
    '/m/02gy9n': 'Transparent',
    '/m/05z87': 'Plastic',
    '/m/0dnr7': '(made of)Textile',
    '/m/04lbp': '(made of)Leather',
    '/m/083vt': 'Wooden',
    '/m/02wzbmj': 'Stand',
    '/m/083mg': 'Walk',
    '/m/06h7j': 'Run',
    '/m/0by3w': 'Jump',
    '/m/015c4z': 'Sit',
    '/m/051_dmw': 'Lay',
    '/m/019nj4': 'Smile',
    '/m/0463cq4': 'Cry',
    '/m/015lz1': 'Sing',
    '/m/01lhf': 'Talk',
}
FLAGS = ('IsOccluded', 'IsTruncated', 'IsGroupOf', 'IsDepiction', 'IsInside')


def build_parser():
    ap = parser(__doc__, 'open-images-v7-val', work_dir=True)
    ap.add_argument(
        '--allow-missing-narratives',
        action='store_true',
        help='Explicit partial import while narratives arrive; later rerun with --on-duplicate update',
    )
    return ap


def read_csv(path):
    with path.open(newline='', encoding='utf-8-sig') as source:
        yield from csv.DictReader(source)


def taxonomy(directory):
    # The synthetic taxonomy root is not listed in the class-description CSV.
    names = {'/m/0bl9f': 'Entity', **ATTRIBUTE_NAMES}
    description_files = list(DESCRIPTIONS)
    if (directory / ATTRIBUTE_DESCRIPTIONS).is_file():
        description_files.append(ATTRIBUTE_DESCRIPTIONS)
    for filename in description_files:
        with (directory / filename).open(newline='', encoding='utf-8-sig') as source:
            for mid, name in csv.reader(source):
                if mid != 'LabelName':
                    names[mid] = name
    with (directory / HIERARCHY).open() as source:
        hierarchy = json.load(source)
    ancestors: dict[str, set[str]] = {}

    def visit(node, parents):
        mid = node['LabelName']
        ancestors.setdefault(mid, set()).update(parents)
        for child in node.get('Subcategory', []):
            visit(child, parents | {mid})
        # "Part" is a part-of relation, not an is-a class ancestor.
        for child in node.get('Part', []):
            visit(child, set())

    visit(hierarchy, set())
    return names, ancestors


def build_index(root, index, allow_missing_narratives=False):
    directory = root / 'annotations'
    required = [directory / f for f in [*CSV_FILES.values(), *DESCRIPTIONS, HIERARCHY]]
    if not allow_missing_narratives:
        required.append(directory / NARRATIVES)
    require_files(required)
    names, ancestors = taxonomy(directory)
    with (directory / DESCRIPTIONS[1]).open(newline='', encoding='utf-8-sig') as source:
        mids = sorted({row[0] for row in csv.reader(source) if row[0] != 'LabelName'})
    index.mask_ids = {mid: number for number, mid in enumerate(mids, 1)}

    def records():
        for kind, filename in CSV_FILES.items():
            print(f'Indexing {filename}', flush=True)
            for number, row in enumerate(read_csv(directory / filename), 1):
                row['_row'] = number
                yield kind, row.get('ImageID', row.get('ImageId')), row
        narratives = directory / NARRATIVES
        if narratives.is_file():
            print(f'Indexing {NARRATIVES} (discarding traces)', flush=True)
            with narratives.open(encoding='utf-8') as source:
                for line in source:
                    row = json.loads(line)
                    # Keep caption/provenance, never retain mouse traces or timed utterances.
                    yield (
                        'narratives',
                        row['image_id'],
                        {k: v for k, v in row.items() if k not in ('traces', 'timed_caption', 'voice_recording')},
                    )
        else:
            print('WARNING: partial import without narratives; rerun with --on-duplicate update', flush=True)

    index.add(records())
    return names, ancestors


def rotation_value(row):
    rotation = float(row.get('Rotation') or 0)
    if rotation not in (0, 90, 180, 270):
        raise ValueError(f"Unsupported Rotation {rotation} for {row['ImageID']}")
    return int(rotation)


def bbox(row, prefix='', suffix=''):
    x, y = float(row[f'{prefix}XMin{suffix}']), float(row[f'{prefix}YMin{suffix}'])
    return [x, y, float(row[f'{prefix}XMax{suffix}']) - x, float(row[f'{prefix}YMax{suffix}']) - y]


def box_corners(row, prefix=''):
    return tuple(float(row[f'{prefix}{key}']) for key in ('XMin', 'XMax', 'YMin', 'YMax'))


def rotate_point(point, rotation):
    """Normalized coordinates in the same counterclockwise frame as Pillow.rotate."""
    x, y = point
    return {
        0: [x, y],
        90: [y, 1 - x],
        180: [1 - x, 1 - y],
        270: [1 - y, x],
    }[rotation]


def rotate_bbox(box, rotation):
    x, y, width, height = box
    corners = [rotate_point(p, rotation) for p in ([x, y], [x + width, y + height])]
    left, top = min(p[0] for p in corners), min(p[1] for p in corners)
    return [left, top, max(p[0] for p in corners) - left, max(p[1] for p in corners) - top]


def map_sample(root, row, index, names, ancestors, assets, metadata_only=False, annotations_only=False):
    image_id = row['ImageID']
    path = contained_path(root / 'validation' / f'{image_id}.jpg', root)
    rotation = rotation_value(row)
    relative = f'open_images/{image_id}-r{rotation}.jpg'
    filepath = str(assets.path(relative)) if rotation else str(path)
    if metadata_only:
        return filepath
    require_files([path])
    image = None if annotations_only else rgb_image(path, rotation)
    try:
        if rotation and image is not None:
            saved_image = assets.save(relative, image, decoded=True)
            image.close()
            image = saved_image
        metadata: dict = {k: html.unescape(v) if k in ('Author', 'Title') else v for k, v in row.items() if k != '_row'}
        metadata.update(
            image_id=image_id,
            split='validation',
            source_filepath=str(path),
            Rotation=rotation,
            rotation_applied=bool(rotation),
            detections=[],
            positive_labels=[],
            verified_absent=[],
            points=[],
            mask_instances=[],
            relationships=[],
            captions=[],
            narratives=[],
        )
        if image is not None:
            metadata.update(width=image.width, height=image.height)
        positive_mids: set[str] = set()
        negative_mids: set[str] = set()
        boxes_by_bounds: dict = {}
        for box in index.rows('detections', image_id):
            mid = box['LabelName']
            positive_mids.add(mid)
            attrs = {'Source': box['Source'], 'LabelName': mid, 'box_id': f"box-{box['_row']}"}
            attrs.update({flag: int(box[flag]) for flag in FLAGS})
            detection = Detection(
                names.get(mid, mid),
                rotate_bbox(bbox(box), rotation),
                confidence=float(box['Confidence']),
                id=f"box-{box['_row']}",
            )
            detection.update(attrs, is_group_of=bool(attrs['IsGroupOf']))
            metadata['detections'].append(detection)
            boxes_by_bounds.setdefault(mid, []).append((box_corners(box), detection))
        for kind in ('boxable_labels', 'image_labels'):
            for label in index.rows(kind, image_id):
                mid, confidence = label['LabelName'], float(label['Confidence'])
                (positive_mids if confidence > 0 else negative_mids).add(mid)
                attrs = {'Source': label['Source'], 'LabelName': mid, 'annotation_file': CSV_FILES[kind]}
                if confidence > 0:
                    value = Classification(names.get(mid, mid), confidence=confidence, id=f"{kind}-{label['_row']}")
                    value.update(attrs)
                    metadata['positive_labels'].append(value)
                else:
                    metadata['verified_absent'].append({'class': names.get(mid, mid), **attrs})
        for point in index.rows('points', image_id):
            mid = point['Label']
            if point['EstimatedYesNo'] == 'yes':
                positive_mids.add(mid)
            attrs = {k: v for k, v in point.items() if k not in ('_row', 'ImageId', 'X', 'Y')}
            attrs.update({key: int(point[key]) for key in ('YesVotes', 'NoVotes', 'UnsureVotes')})
            metadata['points'].append(
                Keypoint(
                    [rotate_point([float(point['X']), float(point['Y'])], rotation)],
                    label=names.get(mid, point.get('TextLabel') or mid),
                    id=f"point-{point['_row']}",
                ),
            )
            metadata['points'][-1].update(attrs)
        for slot, mask in enumerate(index.rows('masks', image_id)):
            mid = mask['LabelName']
            positive_mids.add(mid)
            mask_path = root / 'masks' / mask['MaskPath']
            if not mask_path.is_file():
                mask_path = root / 'masks' / image_id[0] / mask['MaskPath']
            require_files([mask_path])
            label = names.get(mid, mid)
            mask_path = contained_path(mask_path, root)
            relative = f"open_images/{image_id}/mask-{mask['BoxID']}-r{rotation}.png"
            rotated_mask = None
            if not annotations_only:
                with Image.open(mask_path) as source_mask:
                    rotated_mask = source_mask.rotate(rotation, expand=True)
            try:
                field, descriptor = assets.mask(
                    relative,
                    rotated_mask,
                    f'instance_mask_{slot:03d}',
                    index.mask_ids[mid],
                    label,
                    slot,
                )
            finally:
                if rotated_mask is not None:
                    rotated_mask.close()
            # Mask corners have six decimals; allow their rounding plus float32 source precision.
            corners = box_corners(mask, prefix='Box')
            matches = [
                detection
                for bounds, detection in boxes_by_bounds.get(mid, [])
                if all(abs(a - b) <= 1e-6 for a, b in zip(bounds, corners))
            ]
            if not matches:
                raise ValueError(f"Mask {mask['BoxID']} has no matching detection in {image_id}")
            detection_ids = [d['id'] for d in matches]
            descriptor.update(id=mask['BoxID'], detection_id=detection_ids[0], detection_ids=detection_ids)
            metadata[field] = descriptor
            metadata['mask_instances'].append(
                {
                    'box_id': mask['BoxID'],
                    'class': label,
                    'detection_id': detection_ids[0],
                    'detection_ids': detection_ids,
                    'mask_field': field,
                    'PredictedIoU': float(mask['PredictedIoU']),
                    'Clicks': mask['Clicks'],
                },
            )
            for detection in matches:
                detection.setdefault('mask_fields', []).append(field)
                detection.setdefault('mask_box_ids', []).append(mask['BoxID'])
        for relation in index.rows('relationships', image_id):
            first, second = relation['LabelName1'], relation['LabelName2']
            positive_mids.update((first, second))
            subject = rotate_bbox(bbox(relation, suffix='1'), rotation)
            object_box = rotate_bbox(bbox(relation, suffix='2'), rotation)
            x, y = min(subject[0], object_box[0]), min(subject[1], object_box[1])
            right = max(subject[0] + subject[2], object_box[0] + object_box[2])
            bottom = max(subject[1] + subject[3], object_box[1] + object_box[3])
            metadata['relationships'].append(
                Detection(
                    names.get(relation['RelationshipLabel'], relation['RelationshipLabel']),
                    [x, y, right - x, bottom - y],
                    id=f"relation-{relation['_row']}",
                ),
            )
            metadata['relationships'][-1].update(
                subject=names.get(first, first),
                object=names.get(second, second),
                subject_bbox=subject,
                object_bbox=object_box,
                LabelName1=first,
                LabelName2=second,
            )
        for narrative in index.rows('narratives', image_id):
            metadata['captions'].append(narrative['caption'])
            metadata['narratives'].append(narrative['caption'])
        metadata.update(
            caption_text='\n'.join(metadata['captions']),
            classes=sorted({names.get(mid, mid) for mid in positive_mids}),
            parent_classes=sorted({names.get(p, p) for mid in positive_mids for p in ancestors.get(mid, [])}),
            negative_parent_classes=sorted({names.get(p, p) for mid in negative_mids for p in ancestors.get(mid, [])}),
            mask_count=len(metadata['mask_instances']),
        )
        # Native TEXT[] fields give exact membership filters and sidebar facets.
        arrays = {
            'class_names': metadata['classes'],
            'parent_classes': metadata.pop('parent_classes'),
            'negative_parent_classes': metadata.pop('negative_parent_classes'),
            'positive_classes': sorted({v['label'] for v in metadata['positive_labels']}),
            'negative_classes': sorted({names.get(mid, mid) for mid in negative_mids}),
            'point_classes': sorted({v['label'] for v in metadata['points']}),
            'point_states': sorted({v['EstimatedYesNo'] for v in metadata['points']}),
            'relationship_predicates': sorted({v['label'] for v in metadata['relationships']}),
            'mask_classes': sorted({v['class'] for v in metadata['mask_instances']}),
        }
        for flag in FLAGS:
            metadata[f'{flag}_count'] = sum(d[flag] for d in metadata['detections'])
        return Sample(filepath, tags=['validation'], metadata=metadata, array_fields=arrays), image
    except Exception:
        if image is not None:
            image.close()
        raise


def main():
    args = build_parser().parse_args()
    root = args.data_root.resolve()
    with AnnotationIndex(args.work_dir or root / '.work', root) as index:
        names, ancestors = build_index(root, index, args.allow_missing_narratives)
        ingest_objects(
            args,
            index,
            lambda row, assets, **kwargs: map_sample(root, row, index, names, ancestors, assets, **kwargs),
            DATASET_CREDITS['open-images-v7-val'],
            array_fields=ARRAY_FIELDS,
            refresh_description=True,
        )


if __name__ == '__main__':
    main()
