#!/usr/bin/env python3
"""Selectively import KITTI object ZIPs, cuboids, LiDAR references and cached GPS."""

from __future__ import annotations

import math
import os
import re
import sys
from collections import Counter
from contextlib import ExitStack
from http.client import HTTPException
from pathlib import Path
from statistics import median
from zipfile import BadZipFile, ZipFile

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'python'))

from fridge import Calibration, Cuboid, Detection, Sample  # noqa: E402

from examples._shared.credits import DATASET_CREDITS, KITTI_ATTRIBUTION  # noqa: E402
from examples._shared.importer import (  # noqa: E402
    contained_path,
    cuboid_object_samples,
    ingest_objects,
    parser,
    rgb_image,
)
from examples.kitti.kitti_raw_oxts import (  # noqa: E402
    extract_member,
    fetch_drive,
    frame_gps,
    load_drive,
    read_mapping,
    safe_path,
    validate_member,
)

LICENSE = 'CC BY-NC-SA 3.0'
ATTRIBUTION = KITTI_ATTRIBUTION


def build_parser():
    result = parser(__doc__, 'kitti', batch_size=32)
    result.add_argument('--source-root', type=Path, help='Read-only ZIP directory (default: --data-root)')
    result.add_argument('--split', choices=('training', 'testing'), default='training')
    result.add_argument('--skip-embeddings', action='store_true')
    result.add_argument('--no-lidar', action='store_true', help='Omit LiDAR even if its optional ZIP exists')
    result.add_argument('--fetch-oxts-only', action='store_true', help='Populate GPS cache without API login or import')
    return result


def selected_frames(archive, split, limit):
    frames = []
    for info in archive.infolist():
        # Validate all paths, including unselected members; never extractall().
        validate_member(info.filename)
        match = re.fullmatch(re.escape(split) + r'/image_2/(\d{6})\.png', info.filename)
        if match:
            frames.append(match[1])
    if len(set(frames)) != len(frames) or not frames:
        raise ValueError('Missing or duplicate KITTI image frames')
    return sorted(frames)[: limit or None]


def parse_labels(text, image_size, frame_id):
    width, height = image_size
    detections, cuboids = [], []
    for index, line in enumerate(text.splitlines()):
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) not in (15, 16):
            raise ValueError(f'Invalid KITTI label in {frame_id}, line {index}')
        numbers = [float(value) for value in parts[1:]]
        if not all(math.isfinite(value) for value in numbers):
            raise ValueError('Non-finite KITTI label')
        truncated, occluded, alpha, x1, y1, x2, y2 = numbers[:7]
        if x2 <= x1 or y2 <= y1:
            raise ValueError('KITTI 2D box must have positive area')
        if parts[0] != 'DontCare' and (not 0 <= truncated <= 1 or occluded not in (0, 1, 2, 3)):
            raise ValueError('Invalid KITTI truncation/occlusion')
        identity = f'{frame_id}-{index}'
        bbox = [x1 / width, y1 / height, (x2 - x1) / width, (y2 - y1) / height]
        detection = Detection(parts[0], bbox, id=identity)
        detection.update(truncated=truncated, occluded=int(occluded), alpha=alpha)
        detections.append(detection)
        if parts[0] == 'DontCare':
            continue
        cuboid = Cuboid.from_kitti(line, image_size)
        cuboid['id'] = identity
        cuboids.append(cuboid)
    return detections, cuboids


def label_summary(detections, cuboids):
    summary = {'num_cuboids': len(cuboids), 'num_dontcare': sum(d['label'] == 'DontCare' for d in detections)}
    difficulties = Counter(box['attributes']['difficulty'] for box in cuboids)
    summary.update({f'num_{name}': difficulties[name] for name in ('easy', 'moderate', 'hard', 'unrated')})
    occlusions = Counter(box['occluded'] for box in cuboids)
    summary.update({f'num_occluded_{level}': occlusions[level] for level in range(4)})
    if cuboids:
        summary.update(
            occlusion_max=max(box['occluded'] for box in cuboids),
            truncation_mean=sum(box['truncated'] for box in cuboids) / len(cuboids),
            truncation_max=max(box['truncated'] for box in cuboids),
        )
    return summary


def bbox_iou(a, b):
    intersection = max(0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])) * max(
        0,
        min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]),
    )
    union = a[2] * a[3] + b[2] * b[3] - intersection
    return intersection / union if union > 0 else 0


def projection_ious(calibration, cuboids):
    width, height = calibration['image_size']
    for box in cuboids:
        if box['label'] == 'Pedestrian' or box['truncated'] != 0 or box['occluded'] != 0:
            continue
        _, extent = calibration.project(box)
        if extent is None:
            yield 0.0
        else:
            projected = [value / scale for value, scale in zip(extent, (width, height, width, height), strict=True)]
            yield bbox_iou(projected, box['attributes']['bbox_2d'])


def check_projection(archives, frames, split):
    scores = []
    if split == 'training':
        for frame in frames:
            with archives['image_2'].open(f'{split}/image_2/{frame}.png') as stream, Image.open(stream) as image:
                calibration, _, cuboids = read_annotations(archives, split, frame, image.size)
            scores.extend(projection_ious(calibration, cuboids))
    if len(scores) >= 20:
        score = median(scores)
        print(
            f'Projection self-check: median IoU {score:.4f} across {len(scores)} visible, untruncated boxes',
            flush=True,
        )
        if score < 0.9:
            raise ValueError('KITTI projection median IoU is below 0.9; refusing import')
    else:
        print(
            f'Projection self-check: not measurable ({len(scores)} eligible boxes; need 20, excluding Pedestrians)',
            flush=True,
        )


def read_annotations(archives, split, frame, image_size):
    calibration = Calibration.from_kitti(archives['calib'].read(f'{split}/calib/{frame}.txt').decode(), image_size)
    detections, cuboids = [], []
    if split == 'training':
        detections, cuboids = parse_labels(
            archives['label_2'].read(f'{split}/label_2/{frame}.txt').decode(),
            image_size,
            frame,
        )
    return calibration, detections, cuboids


def count_lidar_points(path, calibration, cuboids):
    if path.stat().st_size % 16:
        raise ValueError('KITTI Velodyne files must contain float32 x/y/z/intensity records')
    points = np.fromfile(path, dtype='<f4').reshape(-1, 4)
    camera = calibration.to_camera(points[:, :3], 'lidar')
    for box in cuboids:
        c, s = math.cos(box['yaw']), math.sin(box['yaw'])
        rotation = np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])
        local = (camera - box['center']) @ rotation
        length, width, height = box['size']
        inside = np.all(np.abs(local) <= np.array([length, height, width]) / 2, axis=1)
        box['attributes']['num_lidar_points'] = int(inside.sum())
    return len(points)


def make_sample(archives, root, split, frame, mapping, caches):
    path = extract_member(archives['image_2'], f'{split}/image_2/{frame}.png', root)
    with Image.open(path) as image:
        calibration, detections, cuboids = read_annotations(archives, split, frame, image.size)
        width, height = image.size
    gps, raw_metadata = {}, {}
    if split == 'training':
        raw = mapping[frame]
        gps = frame_gps(caches.get(raw.drive), raw)
        raw_metadata = {'drive': raw.drive, 'date': raw.date, 'raw_frame': raw.frame, 'gps_available': bool(gps)}
    location, heading = gps.pop('location', None), gps.pop('heading', None)
    metadata = {
        'frame_id': frame,
        'split': split,
        'width': width,
        'height': height,
        'license': LICENSE,
        'attribution': ATTRIBUTION,
        'calibration': calibration,
        **raw_metadata,
        **gps,
    }
    if 'velodyne' in archives:
        lidar_path = extract_member(archives['velodyne'], f'{split}/velodyne/{frame}.bin', root)
        point_count = count_lidar_points(lidar_path, calibration, cuboids)
        metadata['lidar'] = {
            'type': 'pointcloud',
            'path': str(lidar_path),
            'format': 'bin',
            'dtype': 'float32',
            'fields': ['x', 'y', 'z', 'intensity'],
            'frame': 'lidar',
            'num_points': point_count,
            'attribution': ATTRIBUTION,
        }
    if split == 'training':
        metadata.update(ground_truth=detections, ground_truth_3d=cuboids, **label_summary(detections, cuboids))
    return Sample(str(path), tags=[split], metadata=metadata, location=location, heading=heading)


def make_objects(parent, parent_id, assets):
    rows = cuboid_object_samples(parent, parent_id, assets, 'ground_truth_3d')
    for row in rows:
        row.tags = parent.tags
        row.metadata.update(
            {
                key: value
                for key, value in parent.metadata.items()
                if key
                in (
                    'license',
                    'attribution',
                    'frame_id',
                    'split',
                    'drive',
                    'date',
                    'raw_frame',
                    'captured_at',
                    'oxts_timestamp_raw',
                    'timestamp_timezone',
                    'speed_kmh',
                    'altitude_m',
                    'gps_available',
                )
            },
        )
    return rows


def finish_dataset(client, dataset):
    if any(field['name'] == 'occluded' for field in dataset.get_schema()):
        client.set_field_display_type(dataset._id, 'occluded', 'enum')


def run(args, encoder_factory=None):
    root = args.data_root.resolve()
    backend_root = os.getenv('FRIDGE_DATA_ROOT')
    if backend_root and not args.media_uri and not root.is_relative_to(Path(backend_root).resolve()):
        raise ValueError('--data-root must be under FRIDGE_DATA_ROOT')
    if args.fetch_oxts_only and args.split != 'training':
        raise ValueError('Testing frames have no raw mapping or GPS')
    # Keep library-generated caches with this import, including in read-only environments.
    for name, directory in (('NUMBA_CACHE_DIR', 'numba'), ('MPLCONFIGDIR', 'matplotlib')):
        os.environ.setdefault(name, str(safe_path(root, f'.cache/{directory}')))
    args.source_root = args.source_root or root
    cache_root = safe_path(root, 'oxts-cache')
    with ExitStack() as stack:
        image_zip = stack.enter_context(ZipFile(args.source_root / 'data_object_image_2.zip'))
        frames = selected_frames(image_zip, args.split, args.limit)
        mapping = read_mapping(args.source_root) if args.split == 'training' else {}
        drives = {mapping[frame].drive: mapping[frame] for frame in frames} if mapping else {}
        if args.fetch_oxts_only:
            failed = 0
            for raw in drives.values():
                try:
                    fetch_drive(cache_root, raw)
                    print(f'OXTS cached: {raw.drive}', flush=True)
                except (OSError, ValueError, BadZipFile, HTTPException) as error:
                    failed += 1
                    print(f'OXTS unavailable for {raw.drive}: {error}', file=sys.stderr, flush=True)
            if failed:
                raise RuntimeError(f'{failed} drives failed; completed caches are retained, rerun to retry')
            return {'cached_drives': len(drives)}
        caches = {}
        for drive in drives:
            try:
                caches[drive] = load_drive(cache_root, drive)
            except (OSError, ValueError) as error:
                print(f'Ignoring invalid OXTS cache for {drive}: {error}', file=sys.stderr)
                caches[drive] = None
        missing = sum(not frame_gps(caches[mapping[f].drive], mapping[f]) for f in frames) if mapping else 0
        if missing:
            print(
                f'{missing} frames lack cached GPS; retained without location. Run --fetch-oxts-only first.',
                flush=True,
            )
        archives = {'image_2': image_zip}
        names = ['calib', 'label_2'] if mapping else ['calib']
        if not args.no_lidar and (args.source_root / 'data_object_velodyne.zip').is_file():
            names.append('velodyne')
        else:
            print('LiDAR omitted (--no-lidar or optional ZIP absent)', flush=True)
        for name in names:
            archives[name] = stack.enter_context(ZipFile(args.source_root / f'data_object_{name}.zip'))
        check_projection(archives, frames, args.split)

        class Frames:
            def images(self, limit):
                return iter(frames)

        def map_sample(frame, assets, metadata_only=False, annotations_only=False):
            path = safe_path(root, f'{args.split}/image_2/{frame}.png')
            if metadata_only:
                return str(path)
            extract_member(image_zip, f'{args.split}/image_2/{frame}.png', root)
            if 'velodyne' in archives:
                extract_member(archives['velodyne'], f'{args.split}/velodyne/{frame}.bin', root)
            if annotations_only:
                return Sample(str(path)), None  # shared resume reads the persisted parent
            sample = make_sample(archives, root, args.split, frame, mapping, caches)
            return sample, rgb_image(contained_path(path, root))

        return ingest_objects(
            args,
            Frames(),
            map_sample,
            DATASET_CREDITS['kitti'],
            refresh_description=True,
            object_factory=make_objects,
            persisted_parents=True,
            encoder_factory=encoder_factory,
            dataset_finish=finish_dataset,
            recover_reduction=True,
        )


def main():
    run(build_parser().parse_args())


if __name__ == '__main__':
    main()
