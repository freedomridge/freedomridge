"""Tiny, offline fixtures; run: pytest examples/coco/test_coco.py."""

import json
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'python'))

from examples._shared.importer import AnnotationIndex, Assets, json_sections, writable_directory  # noqa: E402
from examples.coco.ingest import (  # noqa: E402
    LicensedImages,
    build_index,
    build_parser,
    decode_rle,
    map_sample,
    select_licenses,
)

NAMES = [
    'nose',
    'left_eye',
    'right_eye',
    'left_ear',
    'right_ear',
    'left_shoulder',
    'right_shoulder',
    'left_elbow',
    'right_elbow',
    'left_wrist',
    'right_wrist',
    'left_hip',
    'right_hip',
    'left_knee',
    'right_knee',
    'left_ankle',
    'right_ankle',
]


@pytest.fixture
def coco(tmp_path):
    (tmp_path / 'annotations').mkdir()
    (tmp_path / 'val2017').mkdir()
    Image.new('RGB', (4, 3)).save(tmp_path / 'val2017' / '000000000001.jpg')
    image = {
        'id': 1,
        'file_name': '000000000001.jpg',
        'width': 4,
        'height': 3,
        'license': 2,
        'coco_url': 'http://images.cocodataset.org/val2017/000000000001.jpg',
        'flickr_url': 'https://flickr.com/photo/1',
    }
    category = {'id': 1, 'name': 'person', 'supercategory': 'person', 'keypoints': NAMES, 'skeleton': [[1, 2], [2, 3]]}
    annotation = {'id': 7, 'image_id': 1, 'category_id': 1, 'bbox': [1, 0, 2, 2], 'area': 4, 'iscrowd': 0}
    instances = {
        'images': [image],
        'categories': [category],
        'licenses': [{'id': 2, 'name': 'Attribution License', 'url': 'https://creativecommons.org/licenses/by/2.0/'}],
        'annotations': [
            {**annotation, 'segmentation': [[1, 0, 3, 0, 3, 2], [1, 1, 2, 1, 2, 2]]},
            {**annotation, 'id': 8, 'iscrowd': 1, 'segmentation': {'size': [3, 4], 'counts': [0, 3, 9]}},
        ],
    }
    poses = {
        'categories': [category],
        'annotations': [
            {**annotation, 'num_keypoints': 2, 'keypoints': [1, 1, 2, 2, 2, 1] + [0, 0, 0] * 15},
        ],
    }
    captions = {'annotations': [{'id': i, 'image_id': 1, 'caption': f'Caption {i}: café'} for i in range(5)]}
    for kind, content in [('instances', instances), ('person_keypoints', poses), ('captions', captions)]:
        (tmp_path / 'annotations' / f'{kind}_val2017.json').write_text(json.dumps(content), encoding='utf-8')
    return tmp_path, image


def test_parser(monkeypatch):
    monkeypatch.setenv('FRIDGE_API_URL', 'http://example.invalid/api')
    args = build_parser().parse_args(['--data-root', '/data/coco'])
    assert (args.dataset_name, args.field_name, args.on_duplicate) == ('coco-val2017', 'clip', 'skip')
    assert args.api_url == 'http://example.invalid/api'
    assert (args.username, args.password) == ('admin', None)
    assert args.model == 'openai/clip-vit-base-patch32'
    assert args.licenses == 'no-derivatives'
    for option in (['--limit', '-1'], ['--batch-size', '0']):
        with pytest.raises(SystemExit):
            build_parser().parse_args(['--data-root', '/data', *option])


def test_every_coco_annotation(coco, tmp_path):
    root, image = coco
    assets = Assets(tmp_path)
    with AnnotationIndex(tmp_path) as index:
        categories, licenses = build_index(root, index)
        sample, rgb = map_sample(root, image, index, categories, licenses, assets)
        rgb.close()
        m = sample.metadata
        assert len(m['detections']) == 2
        assert m['detections'][0]['bbox'] == [0.25, 0, 0.5, 2 / 3]
        assert m['detections'][1]['iscrowd'] == 1
        assert m['detections'][0]['area'] == 4
        assert m['detections'][0]['supercategory'] == 'person'
        assert len(m['polygons'][0]['paths']) == 2
        assert m['polygons'][0]['closed'] and m['polygons'][0]['filled']
        assert m['mask_count'] == 1
        with Image.open(tmp_path / 'derived/coco/000000000001/8.png') as mask:
            assert np.asarray(mask).tolist() == [[1, 0, 0, 0]] * 3
        pose = m['keypoints'][0]
        assert pose['points'] == [[0.25, 1 / 3], [0.5, 2 / 3]]
        assert pose['attributes']['all_names'] == NAMES
        assert pose['attributes']['visibility'] == [2, 1] + [0] * 15
        assert pose['attributes']['skeleton'] == [[0, 1], [1, 2]]
        assert len(m['skeletons'][0]['points']) == 2
        assert len(m['captions']) == 5 and 'café' in m['caption_text']
        assert m['license_name'] == 'Attribution License'
        assert m['license_url'].startswith('https://creativecommons.org')
        assert m['coco_url'] == image['coco_url'] and m['flickr_url'] == image['flickr_url']
        assert map_sample(root, image, index, categories, licenses, assets, True) == sample.filepath
        # Round trip through the actual SDK serialization.
        assert json.loads(json.dumps(sample.to_dict()))['metadata']['keypoints'][0]['_type'] == 'keypoints'


# The license table of COCO 2017 instances_val2017.json, names verbatim.
COCO_LICENSES = {
    1: 'Attribution-NonCommercial-ShareAlike License',
    2: 'Attribution-NonCommercial License',
    3: 'Attribution-NonCommercial-NoDerivs License',
    4: 'Attribution License',
    5: 'Attribution-ShareAlike License',
    6: 'Attribution-NoDerivs License',
    7: 'No known copyright restrictions',
    8: 'United States Government Work',
}


def test_select_licenses():
    licenses = {key: {'id': key, 'name': name, 'url': ''} for key, name in COCO_LICENSES.items()}
    assert select_licenses('no-derivatives', licenses) == {1, 2, 4, 5, 7, 8}
    assert select_licenses(' No-Derivatives ', licenses) == {1, 2, 4, 5, 7, 8}
    assert select_licenses('all', licenses) == set(COCO_LICENSES)
    assert select_licenses('4, 5', licenses) == {4, 5}
    with pytest.raises(ValueError, match='Unknown license IDs'):
        select_licenses('4,9', licenses)
    with pytest.raises(ValueError, match='comma-separated'):
        select_licenses('attribution', licenses)


def test_licensed_images_limit_counts_selected_only(tmp_path):
    with AnnotationIndex(tmp_path) as index:
        index.add(('images', f'{i:012d}', {'id': i, 'license': lic}) for i, lic in enumerate([3, 2, 6, 4, 2], 1))
        images = LicensedImages(index, {2, 4})
        assert [row['id'] for row in images.images(0)] == [2, 4, 5]
        assert [row['id'] for row in images.images(2)] == [2, 4]
        assert [row['id'] for row in LicensedImages(index, {3, 6}).images(1)] == [1]


def test_rle_compressed_and_uncompressed():
    # Last run is delta encoded: 4 - 2 = 2. Flattening is Fortran/column-major.
    expected = np.array([0, 255, 255, 0, 0, 0, 255, 255, 255, 255]).reshape((2, 5), order='F')
    for counts in ([1, 2, 3, 4], '1232'):
        with decode_rle({'size': [2, 5], 'counts': counts}) as image:
            np.testing.assert_array_equal(np.asarray(image), expected)
    # Signed delta: 1 - 3 = -2 is represented by 'N'.
    with decode_rle({'size': [3, 3], 'counts': '332N'}) as image:
        assert np.count_nonzero(np.asarray(image)) == 4
    for counts in ([1, 1], 'P', [-1, 11]):
        with pytest.raises(ValueError):
            decode_rle({'size': [2, 5], 'counts': counts})


def test_streaming_json_boundaries(tmp_path):
    path = tmp_path / 'input.json'
    path.write_text(json.dumps({'info': {'text': 'escaped " café'}, 'values': [12345, {}, [], 'x' * 2000]}))
    assert list(json_sections(path, chunk_size=1)) == [
        ('info', {'text': 'escaped " café'}),
        ('values', 12345),
        ('values', {}),
        ('values', []),
        ('values', 'x' * 2000),
    ]
    path.write_text('{"values": [{"id": 1}, {"broken":')
    stream = json_sections(path, chunk_size=5)
    assert next(stream) == ('values', {'id': 1})
    with pytest.raises(ValueError):
        list(stream)


def test_missing_source_and_read_only_guard(tmp_path):
    with AnnotationIndex(tmp_path) as index, pytest.raises(FileNotFoundError, match='Required local files'):
        build_index(tmp_path, index)
    with pytest.raises(ValueError, match='escaped --data-root'):
        writable_directory(tmp_path.parent / 'outside', tmp_path)


def test_assets_cannot_escape_their_directory(tmp_path):
    assets = Assets(tmp_path)
    with Image.new('L', (1, 1)) as mask:
        with pytest.raises(ValueError, match='escaped'):
            assets.save('../../escape.png', mask)
    assert not (tmp_path / 'escape.png').exists()


def test_json_parser_rejects_trailing_garbage(tmp_path):
    path = tmp_path / 'bad.json'
    path.write_text('{"images": []}' + ' ' * 100 + 'unexpected')
    with pytest.raises(ValueError, match='Trailing content'):
        list(json_sections(path, chunk_size=3))
