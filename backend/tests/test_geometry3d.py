"""Synthetic SDK geometry and the committed Python/TypeScript projection contract."""

import json
import math
from pathlib import Path

import numpy as np
import pytest
from fridge import Calibration, Cuboid, Cuboids, Sample, objects_from_cuboids
from fridge.core.geometry3d import cuboid_corners, transform_points
from PIL import Image

FIXTURE = Path(__file__).parents[2] / 'frontend/e2e/fixtures/cuboid-projection.json'


def calibration():
    return Calibration([100, 0, 50, 0, 0, 100, 40, 0, 0, 0, 1, 0], [100, 80])


def test_corners_and_known_projection():
    box = Cuboid('Car', [0, 0, 10], [4, 2, 2], 0, id='car')
    assert box['_type'] == 'cuboid'
    assert np.allclose(cuboid_corners(box)[:4, 0], 2)
    edges, extent = calibration().project(box)
    assert len(edges) == 12
    assert edges[0][0] == pytest.approx([50 + 200 / 9, 40 - 100 / 9])
    assert extent == pytest.approx([50 - 200 / 9, 40 - 100 / 9, 400 / 9, 200 / 9])
    rotated = cuboid_corners(Cuboid('Car', [0, 0, 0], [4, 2, 2], math.pi / 2))
    assert rotated[:4, 2] == pytest.approx([-2] * 4)
    sample = Sample('camera.png', metadata={'boxes': Cuboids([box]), 'calibration': calibration()})
    assert json.loads(json.dumps(sample.to_dict()))['metadata']['boxes'][0]['_type'] == 'cuboid'


def test_near_plane_clips_before_dividing_and_clamps_extent():
    box = Cuboid('near', [0, 0, 0.5], [2, 2, 2], 0)
    edges, extent = calibration().project(box)
    assert len(edges) == 8  # four rear edges are behind the camera
    assert np.isfinite(edges).all()
    assert extent == pytest.approx([0, 0, 100, 80])
    assert max(abs(v) for edge in edges for point in edge for v in point) <= 1050.00001
    assert calibration().project(Cuboid('behind', [0, 0, -10], [2, 2, 2], 0)) == ([], None)
    assert calibration().project(Cuboid('outside', [100, 0, 10], [2, 2, 2], 0))[1] is None


def test_kitti_conversion_and_lidar_round_trip():
    text = (
        'P2: 100 0 50 0 0 100 40 0 0 0 1 0\n'
        'R0_rect: 1 0 0 0 1 0 0 0 1\n'
        'Tr_velo_to_cam: 0 -1 0 1 0 0 -1 2 1 0 0 3\n'
        'Tr_imu_to_velo: 1 0 0 0 0 1 0 0 0 0 1 0\n'
    )
    calib = Calibration.from_kitti(text, [100, 80])
    transform = calib['transforms']['lidar->camera']
    points = np.array([[1, 2, 3], [-4, 0, 2]])
    assert np.allclose(
        transform_points(transform_points(points, transform), np.linalg.inv(np.array(transform).reshape(4, 4)).ravel()),
        points,
    )
    assert set(calib['kitti']) == {'R0_rect', 'Tr_velo_to_cam', 'Tr_imu_to_velo'}  # lossless export (design §1.2)
    box = Cuboid.from_kitti('Car 0.1 0 -1 10 10 60 60 2 2 4 1 3 10 0.2 0.9', [100, 80])
    assert box['center'] == [1, 2, 10]
    assert box['size'] == [4, 2, 2] and box['yaw'] == 0.2
    assert box['attributes']['bbox_2d'] == [0.1, 0.125, 0.5, 0.625]
    assert box['attributes']['difficulty'] == 'easy' and box['confidence'] == 0.9
    lidar_box = Cuboid('lidar', [7, 1, 2], [4, 2, 2], 0, frame='lidar')
    assert calib.distance(lidar_box) == 10
    assert np.allclose(cuboid_corners(lidar_box)[:4, 0], 9)
    assert len(calib.project(lidar_box)[0]) == 12
    with pytest.raises(ValueError, match='lidar->camera'):
        calibration().project(lidar_box)


@pytest.mark.parametrize(
    'height,occluded,truncated,expected',
    [
        (40.01, 0, 0.15, 'easy'),
        (40, 0, 0.15, 'moderate'),
        (25.01, 1, 0.3, 'moderate'),
        (25, 1, 0.3, 'unrated'),
        (25.01, 2, 0.5, 'hard'),
        (25, 2, 0.5, 'unrated'),
        (24, 0, 0, 'unrated'),
        (40, 3, 0, 'unrated'),
        (40, 0, 0.51, 'unrated'),
    ],
)
def test_difficulty(height, occluded, truncated, expected):
    box = Cuboid.from_kitti(f'Car {truncated} {occluded} 0 0 0 50 {height} 2 2 4 0 1 10 0', [100, 80])
    assert box['attributes']['difficulty'] == expected


def test_object_rows_write_distinct_idempotent_crops_and_scalar_fields(tmp_path):
    box = Cuboid(
        'Car',
        [0, 0, 10],
        [4, 2, 2],
        0,
        id='../../unsafe',
        confidence=0.9,
        truncated=0.1,
        occluded=1,
        alpha=0.2,
        difficulty='moderate',
        num_lidar_points=7,
        bbox_2d=[0, 0, 0.5, 0.5],
        parent_id='cannot override',
    )
    boxes = [box, Cuboid('DontCare', [0, 0, 10], [2, 2, 2], 0), Cuboid('behind', [0, 0, -10], [2, 2, 2], 0)]
    args = (42, 'parent.png', Image.new('RGB', (100, 80)), boxes, calibration(), tmp_path)
    rows = objects_from_cuboids(*args, location=(-122.42, 37.77), field='ground_truth_3d')
    assert len(rows) == 1
    row = rows[0]
    assert row.media_type == 'object' and row.location == (-122.42, 37.77)
    assert Path(row.filepath).parent == tmp_path and Path(row.filepath).is_file()
    assert row.filepath != 'parent.png'
    assert objects_from_cuboids(*args, field='ground_truth_3d')[0].filepath == row.filepath
    expected = {
        'parent_id': 42,
        'parent_filepath': 'parent.png',
        'cuboid_id': '../../unsafe',
        'cuboid_field': 'ground_truth_3d',
        'label': 'Car',
        'confidence': 0.9,
        'truncated': 0.1,
        'occluded': 1,
        'alpha': 0.2,
        'distance_m': 10,
        'length_m': 4,
        'width_m': 2,
        'height_m': 2,
        'yaw': 0,
        'difficulty': 'moderate',
        'num_lidar_points': 7,
        'bbox_2d': [0, 0, 0.5, 0.5],
    }
    assert row.metadata == {**expected, 'crop_bbox': row.metadata['crop_bbox']}
    assert row.metadata['crop_bbox'] == pytest.approx([5 / 18, 13 / 36, 4 / 9, 5 / 18])
    with Image.open(row.filepath) as crop:
        assert crop.size == (46, 24)


def test_shared_projection_fixture_is_generated_by_sdk():
    fixture = json.loads(FIXTURE.read_text())
    calib = Calibration(**fixture['calibration'])
    for case in fixture['cases']:
        edges, extent = calib.project(case['cuboid'])
        assert case['edges'] == edges
        assert case['extent'] == extent
        assert case['distance'] == calib.distance(case['cuboid'])


@pytest.mark.parametrize(
    'kwargs',
    [
        {'size': [0, 2, 2]},
        {'center': [float('nan'), 0, 1]},
        {'frame': 'ego'},
        {'yaw': float('inf')},
    ],
)
def test_invalid_cuboids_are_rejected(kwargs):
    with pytest.raises(ValueError):
        Cuboid(**{'label': 'Car', 'center': [0, 0, 10], 'size': [4, 2, 2], 'yaw': 0, **kwargs})
