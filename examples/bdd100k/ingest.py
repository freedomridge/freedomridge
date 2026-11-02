#!/usr/bin/env python3
"""Import BDD100K keyframes, labels and optional telemetry, with resumable offline CLIP."""

import json
import sys
from pathlib import Path
from typing import Dict, List, Optional
from weakref import WeakKeyDictionary

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'python'))

from fridge import Sample  # noqa: E402

from examples._shared.importer import contained_path, ingest, parser  # noqa: E402


def parse_bdd100k_label(label_path: str) -> Optional[Dict]:
    """Parse a BDD100K JSON label file."""
    try:
        with open(label_path, 'r') as f:
            return json.load(f)
    except Exception as e:
        print(f"Error parsing {label_path}: {e}")
        return None


def get_video_metadata_at_10s(video_data_path: str) -> Optional[Dict]:
    """
    Extract metadata at the 10-second mark from video data JSON.

    BDD100K 100k_data JSON structure:
    {
        "locations": [{"timestamp": ms, "latitude": lat, "longitude": lon, "speed": mph,
                       "course": degrees, "accuracy": m}, ...],
        "gps": [{"timestamp": ms, "latitude": lat, "longitude": lon,  "speed": mph, "altitude": m, ...}, ...],
        "accelerometer": [{"timestamp": ms, "x": val, "y": val, "z": val}, ...],
        "gyro": [{"timestamp": ms, "x": val, "y": val, "z": val}, ...],
        "startTime": ms,
        ...
    }

    We extract the data closest to 10 seconds from video start.
    """
    try:
        with open(video_data_path, 'r') as f:
            video_data = json.load(f)

        metadata = {}

        # Get start time to calculate 10-second mark
        start_time = video_data.get('startTime', 0)
        target_time = start_time + 10000  # 10 seconds in milliseconds

        # Extract location/GPS data at 10-second mark
        locations = video_data.get('locations', [])
        if locations:
            # Find closest location to 10-second mark
            closest_loc = min(locations, key=lambda x: abs(x.get('timestamp', 0) - target_time))

            if 'latitude' in closest_loc and 'longitude' in closest_loc:
                metadata['location'] = {
                    'type': 'Point',
                    'coordinates': [closest_loc['longitude'], closest_loc['latitude']],
                }
                metadata['gps_accuracy'] = closest_loc.get('accuracy', None)

            if 'speed' in closest_loc:
                metadata['speed'] = closest_loc['speed']  # in mph

            if 'course' in closest_loc:
                metadata['heading'] = closest_loc['course']  # degrees

            # Use timestamp from location
            if 'timestamp' in closest_loc:
                metadata['timestamp'] = closest_loc['timestamp'] / 1000.0  # Convert to seconds

        # Extract GPS data (has additional fields like altitude)
        gps_data = video_data.get('gps', [])
        if gps_data:
            closest_gps = min(gps_data, key=lambda x: abs(x.get('timestamp', 0) - target_time))

            if 'altitude' in closest_gps:
                metadata['altitude'] = closest_gps['altitude']

            if 'vertical accuracy' in closest_gps:
                metadata['vertical_accuracy'] = closest_gps['vertical accuracy']

            if 'horizontal accuracy' in closest_gps:
                metadata['horizontal_accuracy'] = closest_gps['horizontal accuracy']

        # Extract accelerometer data
        accelerometer = video_data.get('accelerometer', [])
        if accelerometer:
            closest_accel = min(accelerometer, key=lambda x: abs(x.get('timestamp', 0) - target_time))
            metadata['accelerometer'] = {
                'x': closest_accel.get('x'),
                'y': closest_accel.get('y'),
                'z': closest_accel.get('z'),
            }

        # Extract gyroscope data
        gyro = video_data.get('gyro', [])
        if gyro:
            closest_gyro = min(gyro, key=lambda x: abs(x.get('timestamp', 0) - target_time))
            metadata['gyroscope'] = {
                'x': closest_gyro.get('x'),
                'y': closest_gyro.get('y'),
                'z': closest_gyro.get('z'),
            }

        return metadata if metadata else None

    except Exception as e:
        # Not all videos may have telemetry data
        print(f"Warning: Could not extract video metadata from {video_data_path}: {e}")
        return None


def convert_bbox_to_detections(labels: List[Dict]) -> List[Dict]:
    """
    Convert BDD100K bounding boxes to Freedom Ridge detection format.

    BDD100K bbox format: {x1, y1, x2, y2}
    Freedom Ridge wants: {label, bbox: [x, y, width, height]}
    """
    detections = []
    for label in labels:
        if 'box2d' in label:
            bbox = label['box2d']
            x1, y1 = bbox['x1'], bbox['y1']
            x2, y2 = bbox['x2'], bbox['y2']

            detections.append(
                {
                    'label': label['category'],
                    'bbox': [x1, y1, x2 - x1, y2 - y1],  # Convert to x, y, width, height
                    'score': 1.0,  # Ground truth annotations
                },
            )

    return detections


def convert_poly2d_to_polygons(objects: List[Dict]) -> List[Dict]:
    """
    Convert BDD100K poly2d annotations to Freedom Ridge polygon format.

    BDD100K poly2d format: [[x, y, "L"], [x, y, "C"], ...]
    where L = line segment, C = curve segment

    Freedom Ridge format: {label, points: [[x, y], [x, y], ...], closed}

    Polygon types in BDD100K:
    - area/drivable: Driveable road surface
    - area/alternative: Sidewalks, shoulders
    - lane/road curb: Road edges
    - lane/single white, lane/double white: Lane markings
    - lane/single yellow, lane/double yellow: Yellow lane markings
    """
    polygons = []
    # BDD100K image dimensions
    IMG_W = 1280
    IMG_H = 720

    for obj in objects:
        if 'poly2d' in obj:
            # Extract just x,y coordinates from [x, y, type] format
            # AND Normalize to 0-1 range
            points = [[pt[0] / IMG_W, pt[1] / IMG_H] for pt in obj['poly2d']]

            polygons.append(
                {
                    'label': obj['category'],
                    'points': points,
                    'closed': True,  # BDD100K polygons are closed shapes
                    'id': obj.get('id'),
                },
            )

    return polygons


def build_parser():
    # BDD100K records its own S3 paths (--s3-bucket) instead of --media-uri.
    result = parser(
        __doc__,
        'BDD100K',
        batch_size=32,
        limit_help='First N images per split; 0 = all',
        media_uri_option=False,
    )
    result.add_argument('--s3-bucket')
    result.add_argument('--s3-prefix', default='')
    result.add_argument('--storage-config-id', type=int)
    return result


def map_sample(label_data, split, image_path, video_path):
    attributes = label_data.get('attributes', {})
    metadata = {
        'split': split,
        'weather': attributes.get('weather', 'unknown'),
        'scene': attributes.get('scene', 'unknown'),
        'timeofday': attributes.get('timeofday', 'unknown'),
    }
    telemetry = get_video_metadata_at_10s(str(video_path)) if video_path.is_file() else {}
    metadata.update(telemetry or {})
    heading = metadata.pop('heading', None)
    location = metadata.pop('location', None)
    objects: list[dict] = next(
        (f.get('objects', []) for f in label_data.get('frames', []) if f.get('timestamp') == 10000),
        [],
    )
    # Also accept the provider's image-level labels export.
    objects = objects or label_data.get('labels', [])
    detections = convert_bbox_to_detections(objects)
    polygons = convert_poly2d_to_polygons(objects)
    if detections:
        metadata['detections'] = detections
    if polygons:
        metadata['polygons'] = polygons
    return Sample(
        image_path,
        tags=[split, attributes.get('weather', ''), attributes.get('scene', '')],
        metadata=metadata,
        location=tuple(location['coordinates']) if location else None,
        heading=heading,
    )


def iter_samples(args, local_paths=None):
    root = args.data_root.resolve()
    bdd = contained_path(root / 'bdd100k', root)
    if not (bdd / '100k_annotations').is_dir():
        raise FileNotFoundError('Expected <data-root>/bdd100k/100k_annotations; see examples/bdd100k/README.md')
    for split in ('train', 'val', 'test'):
        labels = sorted((bdd / '100k_annotations' / split).glob('*.json'))
        for path in labels[: args.limit or None]:
            label = parse_bdd100k_label(str(contained_path(path, root)))
            if not label:
                continue
            filename = label['name'] if label['name'].endswith('.jpg') else label['name'] + '.jpg'
            image = contained_path(bdd / '100k_images' / split / filename, root)
            if not image.is_file():
                print(f'Skipping missing image: {image}')
                continue
            relative = image.relative_to(root).as_posix()
            key = f"{args.s3_prefix.strip('/')}/{relative}" if args.s3_prefix else relative
            filepath = f's3://{args.s3_bucket}/{key}' if args.s3_bucket else relative
            video = contained_path(bdd / '100k_data' / split / (Path(filename).stem + '.json'), root)
            sample = map_sample(label, split, filepath, video)
            if local_paths is not None:
                local_paths[sample] = image
            yield sample


def main():
    args = build_parser().parse_args()

    def setup(client, dataset):
        if args.storage_config_id is not None:
            client.update_dataset(dataset._id, storage_config_id=args.storage_config_id)

    local_paths: WeakKeyDictionary[Sample, Path] = WeakKeyDictionary()
    ingest(
        args,
        iter_samples(args, local_paths),
        {},
        'BDD100K autonomous driving dataset (complete).',
        dataset_setup=setup,
        local_path=local_paths.__getitem__,
    )


if __name__ == '__main__':
    main()
