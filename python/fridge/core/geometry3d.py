"""Camera/LiDAR cuboid geometry. Projection is mirrored in frontend/lib/geometry3d.ts.

Edges are clipped at homogeneous depth 0.1 before perspective division. Extents
are image-clamped pixel [x, y, width, height]; an invisible box has no extent.
"""

from __future__ import annotations

import math

import numpy as np

from .labels import _LabelDict

NEAR_PLANE = 0.1
EDGES = ((0, 1), (1, 2), (2, 3), (3, 0), (4, 5), (5, 6), (6, 7), (7, 4), (0, 4), (1, 5), (2, 6), (3, 7))


def finite_vector(values, length, name):
    result = np.asarray(values, dtype=float)
    if result.shape != (length,) or not np.isfinite(result).all():
        raise ValueError(f'{name} must contain {length} finite numbers')
    return result.tolist()


def cuboid_corners(cuboid) -> np.ndarray:
    """Eight corners in the box's frame; 0–3 are the front (+length) face."""
    length, width, height = cuboid['size']
    signs = np.array(
        [[1, -1, -1], [1, -1, 1], [1, 1, 1], [1, 1, -1], [-1, -1, -1], [-1, -1, 1], [-1, 1, 1], [-1, 1, -1]],
    )
    c, s = math.cos(cuboid['yaw']), math.sin(cuboid['yaw'])
    if cuboid['frame'] == 'camera':
        local = signs * [length / 2, height / 2, width / 2]
        rotation = np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])
    elif cuboid['frame'] == 'lidar':
        local = signs * [length / 2, width / 2, height / 2]
        rotation = np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])
    else:
        raise ValueError('frame must be camera or lidar')
    return local @ rotation.T + cuboid['center']


def transform_points(points, transform) -> np.ndarray:
    """Apply a row-major affine 4×4 transform to N three-dimensional points."""
    points = np.asarray(points, dtype=float)
    matrix = np.asarray(transform, dtype=float).reshape(4, 4)
    return points @ matrix[:3, :3].T + matrix[:3, 3]


class Calibration(_LabelDict):
    """Per-sample projection from rectified camera coordinates to full-size pixels.

    ``P`` is a row-major 3×4 matrix, ``image_size`` is [width, height], and
    ``transforms`` maps frame pairs (e.g. ``lidar->camera``) to row-major 4×4 matrices.
    ``P[2, :3]`` must be the unit optical axis (KITTI convention), so the
    near-plane depth is measured in metres. Instances are JSON dictionaries suitable for ``sample.calibration``.
    """

    def __init__(self, P, image_size, transforms=None, camera=None):
        size = finite_vector(image_size, 2, 'image_size')
        if any(v <= 0 or not v.is_integer() for v in size):
            raise ValueError('image_size must contain positive integer dimensions')
        matrices = {}
        for name, matrix in (transforms or {}).items():
            matrices[name] = finite_vector(matrix, 16, name)
            if matrices[name][12:] != [0, 0, 0, 1]:
                raise ValueError(f'{name} must be an affine transform')
        super().__init__(
            type='calibration',
            P=finite_vector(P, 12, 'P'),
            image_size=[int(v) for v in size],
            transforms=matrices,
        )
        if camera is not None:
            self['camera'] = camera

    @classmethod
    def from_kitti(cls, text, image_size):
        """Parse KITTI object calibration, ignoring comments and non-numeric lines.

        Stores P2, the composed LiDAR-to-camera transform, and the raw rectification and
        sensor transforms under ``kitti`` (R0_rect, Tr_velo_to_cam and, when present,
        Tr_imu_to_velo) so a later KITTI export is lossless. The unused camera
        projections (P0, P1, P3) are not stored.
        """
        raw = {}
        for line in text.splitlines():
            key, separator, values = line.partition(':')
            if separator and key.strip() in ('P2', 'R0_rect', 'Tr_velo_to_cam', 'Tr_imu_to_velo'):
                try:
                    raw[key.strip()] = [float(v) for v in values.split()]
                except ValueError:
                    continue
        rect = np.eye(4)
        rect[:3, :3] = np.array(finite_vector(raw['R0_rect'], 9, 'R0_rect')).reshape(3, 3)
        velo = np.eye(4)
        velo[:3] = np.array(finite_vector(raw['Tr_velo_to_cam'], 12, 'Tr_velo_to_cam')).reshape(3, 4)
        calibration = cls(
            raw['P2'],
            image_size,
            transforms={'lidar->camera': (rect @ velo).ravel().tolist()},
            camera='image_2',
        )
        kitti = {'R0_rect': finite_vector(raw['R0_rect'], 9, 'R0_rect')}
        kitti['Tr_velo_to_cam'] = finite_vector(raw['Tr_velo_to_cam'], 12, 'Tr_velo_to_cam')
        if 'Tr_imu_to_velo' in raw:
            kitti['Tr_imu_to_velo'] = finite_vector(raw['Tr_imu_to_velo'], 12, 'Tr_imu_to_velo')
        calibration['kitti'] = kitti
        return calibration

    def to_camera(self, points, frame):
        if frame == 'camera':
            return np.asarray(points, dtype=float)
        if frame != 'lidar':
            raise ValueError('frame must be camera or lidar')
        transform = self['transforms'].get('lidar->camera')
        if transform is None:
            raise ValueError('Calibration requires a lidar->camera transform for lidar cuboids')
        return transform_points(points, transform)

    def distance(self, cuboid) -> float:
        """Euclidean distance to the geometric centre in the camera frame."""
        return float(np.linalg.norm(self.to_camera([cuboid['center']], cuboid['frame'])[0]))

    def project(self, cuboid) -> tuple[list, list | None]:
        """Return visible edge segments and their image-clamped extent in pixels."""
        points = self.to_camera(cuboid_corners(cuboid), cuboid['frame'])
        projection = np.array(self['P']).reshape(3, 4)
        homogeneous = points @ projection[:, :3].T + projection[:, 3]
        segments = []
        for start, end in EDGES:
            a, b = homogeneous[start].copy(), homogeneous[end].copy()
            if a[2] < NEAR_PLANE and b[2] < NEAR_PLANE:
                continue
            if a[2] < NEAR_PLANE:
                a += (NEAR_PLANE - a[2]) / (b[2] - a[2]) * (b - a)
                a[2] = NEAR_PLANE
            elif b[2] < NEAR_PLANE:
                b += (NEAR_PLANE - b[2]) / (a[2] - b[2]) * (a - b)
                b[2] = NEAR_PLANE
            segments.append([(a[:2] / a[2]).tolist(), (b[:2] / b[2]).tolist()])
        if not segments:
            return [], None
        pixels = np.array(segments).reshape(-1, 2)
        low = np.maximum(pixels.min(axis=0), [0, 0])
        high = np.minimum(pixels.max(axis=0), self['image_size'])
        extent = [*low.tolist(), *(high - low).tolist()] if (high > low).all() else None
        return segments, extent
