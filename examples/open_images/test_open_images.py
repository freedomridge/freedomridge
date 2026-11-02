"""Tiny local fixtures for all Open Images annotation families; no downloads or CLIP required."""

import csv
import json
import sys
from io import BytesIO
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'python'))

from examples._shared.importer import AnnotationIndex, Assets  # noqa: E402
from examples.open_images.ingest import (  # noqa: E402  # noqa: E402
    CSV_FILES,
    DESCRIPTIONS,
    HIERARCHY,
    NARRATIVES,
    build_index,
    build_parser,
    map_sample,
    rotate_bbox,
    rotate_point,
    rotation_value,
)
from examples.sdk.compute_patch_embeddings import public_https_loader  # noqa: E402


def write_csv(path, rows):
    with path.open('w', newline='') as source:
        writer = csv.DictWriter(source, fieldnames=rows[0])
        writer.writeheader()
        writer.writerows(rows)


@pytest.fixture
def oi(tmp_path):
    annotation_dir = tmp_path / 'annotations'
    annotation_dir.mkdir()
    (tmp_path / 'validation').mkdir()
    (tmp_path / 'masks').mkdir()
    # Unequal dimensions expose rotation mistakes.
    pixels = np.zeros((3, 5, 3), dtype=np.uint8)
    pixels[0, 0] = [255, 0, 0]
    Image.fromarray(pixels).save(tmp_path / 'validation/a.jpg')
    mask_pixels = np.zeros((3, 5), dtype=np.uint8)
    mask_pixels[0, 0] = 255
    Image.fromarray(mask_pixels).save(tmp_path / 'masks/a_mask.png')
    image = {
        'ImageID': 'a',
        'Rotation': '90.0',
        'OriginalURL': 'https://example.org/a.jpg',
        'Author': 'Photographer',
        'License': 'https://creativecommons.org/licenses/by/2.0/',
        'Title': 'A café',
    }
    files = {
        'images': [image],
        'detections': [
            {
                'ImageID': 'a',
                'LabelName': '/dog',
                'Source': 'xclick',
                'Confidence': '1',
                'XMin': '.1',
                'XMax': '.7',
                'YMin': '.2',
                'YMax': '.8',
                'IsOccluded': '1',
                'IsTruncated': '0',
                'IsGroupOf': '1',
                'IsDepiction': '0',
                'IsInside': '1',
            },
        ],
        'boxable_labels': [
            {'ImageID': 'a', 'LabelName': '/dog', 'Confidence': '1', 'Source': 'verification'},
            {'ImageID': 'a', 'LabelName': '/cat', 'Confidence': '0', 'Source': 'verification'},
        ],
        'image_labels': [{'ImageID': 'a', 'LabelName': '/dog', 'Confidence': '1', 'Source': 'verification'}],
        'points': [
            {
                'ImageId': 'a',
                'Label': '/dog',
                'X': '.3',
                'Y': '.4',
                'EstimatedYesNo': answer,
                'Source': 'ih',
                'YesVotes': '2',
                'NoVotes': '1',
                'UnsureVotes': '0',
                'TextLabel': '',
            }
            for answer in ('yes', 'no', 'unsure')
        ],
        'masks': [
            {
                'ImageID': 'a',
                'LabelName': '/dog',
                'MaskPath': 'a_mask.png',
                'BoxID': 'b1',
                'BoxXMin': '.1',
                'BoxXMax': '.7',
                'BoxYMin': '.2',
                'BoxYMax': '.8',
                'PredictedIoU': '.9',
                'Clicks': '',
            },
            {
                'ImageID': 'a',
                'LabelName': '/dog',
                'MaskPath': 'a_mask.png',
                'BoxID': 'b2',
                'BoxXMin': '.1',
                'BoxXMax': '.7',
                'BoxYMin': '.2',
                'BoxYMax': '.8',
                'PredictedIoU': '.8',
                'Clicks': '',
            },
        ],
        'relationships': [
            {
                'ImageID': 'a',
                'LabelName1': '/dog',
                'LabelName2': '/cat',
                'RelationshipLabel': 'near',
                'XMin1': '.1',
                'XMax1': '.4',
                'YMin1': '.2',
                'YMax1': '.5',
                'XMin2': '.3',
                'XMax2': '.8',
                'YMin2': '.4',
                'YMax2': '.9',
            },
        ],
    }
    for kind, filename in CSV_FILES.items():
        write_csv(annotation_dir / filename, files[kind])
    # Exercise both headered and headerless descriptions, including a comma in a name.
    for i, filename in enumerate(DESCRIPTIONS):
        with (annotation_dir / filename).open('w', newline='') as source:
            writer = csv.writer(source)
            if i == 0:
                writer.writerow(['LabelName', 'DisplayName'])
            writer.writerows([['/dog', 'Dog'], ['/cat', 'Cat'], ['/animal', 'Animal, living'], ['/entity', 'Entity']])
    (annotation_dir / HIERARCHY).write_text(
        json.dumps(
            {
                'LabelName': '/entity',
                'Subcategory': [
                    {'LabelName': '/animal', 'Subcategory': [{'LabelName': '/dog'}, {'LabelName': '/cat'}]},
                ],
            },
        ),
    )
    (annotation_dir / NARRATIVES).write_text(
        '\n'.join(
            json.dumps(
                {
                    'image_id': 'a',
                    'dataset_id': 'open_images',
                    'annotator_id': i,
                    'caption': f'A dog and a cat {i}.',
                    'traces': [[{'x': 0.2, 'y': 0.3, 't': 1}]],
                    'timed_caption': [{'utterance': 'dog'}],
                },
            )
            for i in range(2)
        ),
    )
    return tmp_path


def test_parser(monkeypatch):
    monkeypatch.delenv('FRIDGE_API_URL', raising=False)
    args = build_parser().parse_args(['--data-root', '/data'])
    assert args.dataset_name == 'open-images-v7-val'
    assert args.api_url == 'http://localhost:8000/api'
    assert not args.allow_missing_narratives
    assert args.on_duplicate == 'skip'
    assert args.field_name == 'clip'


def test_all_annotations_and_rotation(oi, tmp_path):
    assets = Assets(tmp_path)
    with AnnotationIndex(tmp_path) as index:
        names, ancestors = build_index(oi, index)
        row = next(index.images(1))
        sample, rgb = map_sample(oi, row, index, names, ancestors, assets)
        assert rgb.size == (3, 5)
        with Image.open(oi / 'validation/a.jpg') as original, BytesIO() as encoded:
            # Hashing and embeddings see the saved JPEG after rotation and encoding.
            with original.rotate(90, expand=True) as rotated:
                rotated.save(encoded, format='JPEG', quality=95)
            encoded.seek(0)
            with Image.open(encoded) as expected, Image.open(sample.filepath) as saved:
                np.testing.assert_array_equal(np.asarray(rgb), np.asarray(expected))
                np.testing.assert_array_equal(np.asarray(rgb), np.asarray(saved))
        rgb.close()
        assert sample.filepath.endswith('/open_images/a-r90.jpg')
        assert map_sample(oi, row, index, names, ancestors, assets, True) == sample.filepath
        m = sample.metadata
        assert m['Rotation'] == 90 and m['rotation_applied']
        assert (m['Author'], m['Title']) == ('Photographer', 'A café')
        assert m['OriginalURL'].startswith('https://') and 'creativecommons' in m['License']
        assert len(m['detections']) == 1
        assert m['detections'][0]['bbox'] == pytest.approx([0.2, 0.3, 0.6, 0.6])
        assert m['detections'][0]['IsGroupOf'] == 1
        assert len(m['positive_labels']) == 2 and len(m['verified_absent']) == 1
        assert sample.array_fields['negative_classes'] == ['Cat'] and 'negative_labels' not in m
        assert {p['EstimatedYesNo'] for p in m['points']} == {'yes', 'no', 'unsure'}
        assert m['points'][0]['YesVotes'] == 2
        assert m['points'][0]['points'] == [[0.4, 0.7]]
        assert m['mask_count'] == 2
        assert m['instance_mask_000']['maskPath'].endswith('a/mask-b1-r90.png')
        assert m['instance_mask_001']['id'] == 'b2'
        with Image.open(tmp_path / 'derived/open_images/a/mask-b1-r90.png') as rotated:
            assert rotated.size == (3, 5)
            assert np.argwhere(np.asarray(rotated) > 0).tolist() == [[4, 0]]
        relation = m['relationships'][0]
        assert relation['label'] == 'near' and relation['bbox'] == pytest.approx([0.2, 0.2, 0.7, 0.7])
        assert (relation['subject'], relation['object']) == ('Dog', 'Cat')
        assert sample.array_fields['parent_classes'] == ['Animal, living', 'Entity']
        assert len(m['captions']) == 2 and 'dog' in m['caption_text']
        assert all('traces' not in n and 'timed_caption' not in n for n in m['narratives'])
        assert json.loads(json.dumps(sample.to_dict()))['metadata']['mask_count'] == 2


def test_missing_annotations_fail_and_narratives_opt_in(oi):
    (oi / 'annotations' / NARRATIVES).unlink()
    with AnnotationIndex(oi) as index:
        with pytest.raises(FileNotFoundError, match=NARRATIVES):
            build_index(oi, index)
        build_index(oi, index, allow_missing_narratives=True)
        assert list(index.rows('narratives', 'a')) == []
    (oi / 'annotations' / CSV_FILES['points']).unlink()
    with AnnotationIndex(oi) as index, pytest.raises(FileNotFoundError, match='point-labels'):
        build_index(oi, index, allow_missing_narratives=True)


def test_missing_mask_is_not_silently_dropped(oi):
    (oi / 'masks/a_mask.png').unlink()
    with AnnotationIndex(oi) as index:
        names, ancestors = build_index(oi, index)
        with pytest.raises(FileNotFoundError, match='a_mask.png'):
            map_sample(
                oi,
                next(index.images(1)),
                index,
                names,
                ancestors,
                Assets(oi),
            )


def test_rotation_validation_and_patch_loader(oi, monkeypatch):
    def no_network(*args, **kwargs):
        pytest.fail('Local patch loader attempted a network request')

    monkeypatch.setattr('examples.sdk.compute_patch_embeddings.requests.get', no_network)
    with public_https_loader(str(oi / 'validation/a.jpg')) as image:
        assert image.size == (5, 3) and image.mode == 'RGB'
    assert rotation_value({'ImageID': 'a', 'Rotation': ''}) == 0
    for rotation in ('NaN', '-90', '45'):
        with pytest.raises(ValueError):
            rotation_value({'ImageID': 'a', 'Rotation': rotation})


def test_unannotated_images_and_optional_attribute_names(oi):
    # The importer uses image metadata as its universe, not the bbox CSV.
    write_csv(
        oi / 'annotations' / CSV_FILES['images'],
        [
            {'ImageID': 'a', 'Rotation': '90'},
            {'ImageID': 'b', 'Rotation': ''},
        ],
    )
    Image.new('RGB', (5, 3)).save(oi / 'validation/b.jpg')
    (oi / 'annotations/oidv6-attributes-description.csv').write_text('/material,Material name\n')
    with AnnotationIndex(oi) as index:
        names, ancestors = build_index(oi, index)
        assert names['/material'] == 'Material name' and names['/m/0bl9f'] == 'Entity'
        rows = list(index.images(0))
        assert [r['ImageID'] for r in rows] == ['a', 'b']
        sample, image = map_sample(
            oi,
            rows[1],
            index,
            names,
            ancestors,
            Assets(oi),
        )
        image.close()
        assert sample.filepath == str(oi / 'validation/b.jpg')
        assert sample.metadata['detections'] == [] and sample.metadata['captions'] == []
        assert sample.array_fields['parent_classes'] == []


@pytest.mark.parametrize(
    'angle,point,box',
    [
        (0, [0.1, 0.2], [0.1, 0.2, 0.3, 0.4]),
        (90, [0.2, 0.9], [0.2, 0.6, 0.4, 0.3]),
        (180, [0.9, 0.8], [0.6, 0.4, 0.3, 0.4]),
        (270, [0.8, 0.1], [0.4, 0.1, 0.4, 0.3]),
    ],
)
def test_all_rotation_frames(angle, point, box):
    assert rotate_point([0.1, 0.2], angle) == pytest.approx(point)
    assert rotate_bbox([0.1, 0.2, 0.3, 0.4], angle) == pytest.approx(box)
