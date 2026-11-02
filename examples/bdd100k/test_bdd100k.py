"""Preserve BDD metadata, pixel boxes, normalized polygons, telemetry and path identity."""

import json
from weakref import WeakKeyDictionary

from PIL import Image

from examples.bdd100k.ingest import build_parser, iter_samples, map_sample


def test_mapping_and_telemetry(tmp_path):
    video = tmp_path / 'video.json'
    video.write_text(
        json.dumps(
            {
                'startTime': 100,
                'locations': [
                    {'timestamp': 10100, 'latitude': 40, 'longitude': -80, 'course': 45, 'speed': 20},
                ],
            },
        ),
    )
    label = {
        'attributes': {'weather': 'clear', 'scene': 'highway'},
        'frames': [
            {
                'timestamp': 10000,
                'objects': [
                    {
                        'id': 7,
                        'category': 'car',
                        'box2d': {'x1': 10, 'y1': 20, 'x2': 30, 'y2': 60},
                        'poly2d': [[128, 72, 'L'], [256, 144, 'L']],
                    },
                ],
            },
        ],
    }
    sample = map_sample(label, 'train', 'bdd100k/100k_images/train/a.jpg', video)
    assert sample.metadata['detections'][0]['bbox'] == [10, 20, 20, 40]
    assert sample.metadata['polygons'][0]['points'] == [[0.1, 0.1], [0.2, 0.2]]
    assert sample.location == (-80, 40) and sample.heading == 45
    assert sample.metadata['timestamp'] == 10.1 and sample.metadata['speed'] == 20
    assert sample.tags == ['train', 'clear', 'highway']


def test_relative_and_s3_paths_preserved(tmp_path):
    images = tmp_path / 'bdd100k/100k_images/train'
    labels = tmp_path / 'bdd100k/100k_annotations/train'
    images.mkdir(parents=True)
    labels.mkdir(parents=True)
    Image.new('RGB', (8, 8)).save(images / 'a.jpg')
    (labels / 'a.json').write_text(json.dumps({'name': 'a.jpg'}))
    args = build_parser().parse_args(['--data-root', str(tmp_path)])
    assert args.limit == 100 and args.dataset_name == 'BDD100K' and args.password is None
    paths: WeakKeyDictionary = WeakKeyDictionary()
    sample = next(iter_samples(args, paths))
    assert sample.filepath == 'bdd100k/100k_images/train/a.jpg'
    json.dumps(sample.to_dict())
    assert not any(key.startswith('_') for key in sample.metadata)
    assert paths[sample] == images / 'a.jpg'
    args.s3_bucket, args.s3_prefix = 'example-bucket', 'demo'
    sample = next(iter_samples(args, paths))
    assert sample.filepath == 's3://example-bucket/demo/bdd100k/100k_images/train/a.jpg'
    json.dumps([sample.to_dict()])  # the add_samples request body
    assert paths[sample] == images / 'a.jpg'
