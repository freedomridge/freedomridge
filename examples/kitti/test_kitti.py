"""Synthetic KITTI fixtures only; all HTTP traffic stays on a local Range server."""

import getpass
import io
import json
import math
import socket
import stat
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from email.message import Message
from http.client import HTTPException, IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from zipfile import ZIP_DEFLATED, ZIP_STORED, BadZipFile, ZipFile, ZipInfo

import fridge
import numpy as np
import pytest
from fridge import Calibration, Sample
from PIL import Image

from examples._shared import importer as common
from examples.kitti import ingest as importer
from examples.kitti import kitti_raw_oxts as oxts

CALIBRATION = (
    'P2: 100 0 50 0 0 100 40 0 0 0 1 0\n'
    'R0_rect: 1 0 0 0 1 0 0 0 1\n'
    'Tr_velo_to_cam: 0 -1 0 0 0 0 -1 0 1 0 0 0\n'
    'Tr_imu_to_velo: 1 0 0 0 0 1 0 0 0 0 1 0\n'
)
LABEL = 'Car 0 0 0 27.77777778 17.77777778 72.22222222 62.22222222 4 2 4 0 2 10 0'
DONTCARE = 'DontCare -1 -1 -10 5 5 10 10 -1 -1 -1 -1000 -1000 -1000 -10'
RAW = oxts.RawFrame('2011_09_26', '2011_09_26_drive_0005_sync', 0)
TIMESTAMP = '2011-09-26 13:02:45.123456789'
PACKET = ' '.join(map(str, [49, 8, 100, 0, 0, math.pi / 2, 3, 4] + [0] * 22))


def seed_cache(root, raw=RAW):
    members = {'data/0000000000.txt': PACKET, 'timestamps.txt': TIMESTAMP + '\n'}
    data = {'version': 1, 'drive': raw.drive, 'source_url': 'synthetic', 'members': members}
    path = oxts.cache_path(root / 'oxts-cache', raw.drive)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return data


def test_labels_calibration_projection_and_summary():
    detections, boxes = importer.parse_labels(LABEL + '\n' + DONTCARE, (100, 80), '000042')
    assert [d['label'] for d in detections] == ['Car', 'DontCare']
    assert detections[1]['bbox'] == [0.05, 0.0625, 0.05, 0.0625]
    assert len(boxes) == 1
    box = boxes[0]
    assert box['id'] == '000042-0'
    assert box['center'] == [0, 0, 10]
    assert box['size'] == [4, 2, 4]
    assert box['attributes']['bbox_2d'] == detections[0]['bbox']
    assert box['attributes']['difficulty'] == 'easy'
    calibration = Calibration.from_kitti(CALIBRATION, (100, 80))
    assert set(calibration['kitti']) == {'R0_rect', 'Tr_velo_to_cam', 'Tr_imu_to_velo'}
    assert calibration.to_camera([[10, 0, 0]], 'lidar').tolist() == [[0, 0, 10]]
    assert list(importer.projection_ious(calibration, boxes)) == pytest.approx([1])
    summary = importer.label_summary(detections, boxes)
    assert summary['num_dontcare'] == summary['num_easy'] == summary['num_occluded_0'] == 1
    assert summary['truncation_mean'] == summary['occlusion_max'] == 0


@pytest.mark.parametrize(
    ('height', 'occluded', 'truncated', 'difficulty'),
    [
        (40.01, 0, 0.15, 'easy'),
        (40, 0, 0.15, 'moderate'),
        (25.01, 1, 0.3, 'moderate'),
        (25.01, 2, 0.5, 'hard'),
        (25, 1, 0.3, 'unrated'),
        (25, 2, 0.5, 'unrated'),
    ],
)
def test_difficulty_uses_merged_sdk(height, occluded, truncated, difficulty):
    line = f'Car {truncated} {occluded} 0 10 0 50 {height} 4 2 4 0 2 10 0'
    _, boxes = importer.parse_labels(line, (100, 80), '000000')
    assert boxes[0]['attributes']['difficulty'] == difficulty


@pytest.mark.parametrize(
    'line',
    ['Car 0', LABEL.replace('Car 0 0', 'Car nan 0'), LABEL.replace('Car 0 0', 'Car 0 1.5')],
)
def test_bad_labels(line):
    with pytest.raises(ValueError):
        importer.parse_labels(line, (100, 80), '000000')


def test_mapping_is_one_based_permutation():
    text = f'{RAW.date} {RAW.drive} 0000000007\n{RAW.date} {RAW.drive} 0000000011\n'
    mapping = oxts.parse_mapping(text, '2,1\n')
    assert mapping['000000'].frame == 11
    assert mapping['000001'].frame == 7
    assert mapping['000000'].drive == RAW.drive
    for invalid in ('0,1', '1,3', '1,1', '1'):
        with pytest.raises(ValueError):
            oxts.parse_mapping(text, invalid)
    with pytest.raises(ValueError):
        oxts.parse_mapping(text.replace(RAW.drive, '../drive'), '2,1')


def test_oxts_values_and_verified_timezone():
    gps = oxts.parse_oxts(PACKET, TIMESTAMP)
    assert gps['location'] == (8, 49)
    assert gps['heading'] == pytest.approx(0)
    assert gps['speed_kmh'] == 18
    assert gps['captured_at'] == '2011-09-26T13:02:45.123456+02:00'
    assert gps['oxts_timestamp_raw'] == TIMESTAMP + ' (Europe/Berlin local)'
    assert 'corroborated by sun position' in gps['timestamp_timezone']
    with pytest.raises(ValueError):
        oxts.parse_oxts(PACKET.replace('49 ', 'nan ', 1), TIMESTAMP)
    assert oxts.frame_gps(None, RAW) == {}


@pytest.mark.parametrize('name', ['../escape', '/absolute', 'C:/drive', 'a/../../escape', 'a\\escape'])
def test_zip_slip(tmp_path, name):
    buffer = io.BytesIO()
    with ZipFile(buffer, 'w') as archive:
        archive.writestr(name, b'bad')
    with ZipFile(buffer) as archive, pytest.raises(ValueError):
        oxts.extract_member(archive, name, tmp_path)


def test_extract_resume_repair_and_symlink(tmp_path):
    buffer = io.BytesIO()
    with ZipFile(buffer, 'w') as archive:
        archive.writestr('training/image_2/000000.png', b'complete')
        link = ZipInfo('link')
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, 'target')
    with ZipFile(buffer) as archive:
        path = oxts.extract_member(archive, 'training/image_2/000000.png', tmp_path)
        assert stat.S_IMODE(path.stat().st_mode) == 0o644
        before = path.stat().st_mtime_ns
        oxts.extract_member(archive, 'training/image_2/000000.png', tmp_path)
        assert path.stat().st_mtime_ns == before
        path.write_bytes(b'partial')
        oxts.extract_member(archive, 'training/image_2/000000.png', tmp_path)
        assert path.read_bytes() == b'complete'
        with pytest.raises(ValueError):
            oxts.extract_member(archive, 'link', tmp_path)
        outside = tmp_path / 'outside'
        outside.mkdir()
        root = tmp_path / 'root'
        root.mkdir()
        (root / 'training').symlink_to(outside, target_is_directory=True)
        with pytest.raises(ValueError):
            oxts.extract_member(archive, 'training/image_2/000000.png', root)


def raw_zip(interleaved=False, comment=False):
    buffer = io.BytesIO()
    prefix = f'{RAW.date}/{RAW.drive}/oxts/'
    with ZipFile(buffer, 'w', compression=ZIP_DEFLATED) as archive:
        if comment:
            archive.comment = b'fixture archive comment'
        archive.writestr(prefix, b'')
        archive.writestr(prefix + 'data/', b'')
        archive.writestr(prefix + 'dataformat.txt', 'not a 30-value packet')
        archive.writestr(prefix + 'data/README.txt', 'ignore this too')
        archive.writestr(prefix + 'data/0000000000.txt.bak', 'ignore this too')
        archive.writestr(prefix + 'data/0000000000.txt', PACKET)
        if interleaved:
            archive.writestr('unrelated-image.png', b'not oxts')
        archive.writestr(prefix + 'data/0000000001.txt', PACKET)
        archive.writestr(prefix + 'timestamps.txt', (TIMESTAMP + '\n') * 2)
        archive.writestr('large-image.png', b'x' * 2_000_000, compress_type=ZIP_STORED)
        # Exercise a central directory larger than the initial EOCD tail.
        for index in range(1200):
            archive.writestr(f'image_02/data/{index:010d}.png', b'')
    return buffer.getvalue()


@contextmanager
def range_server(payload, mode='range'):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            header = self.headers['Range']
            assert header
            if header.startswith('bytes=-'):
                start, end = max(0, len(payload) - int(header[7:])), len(payload) - 1
            else:
                start, end = map(int, header[6:].split('-'))
            body = payload[start : end + 1]
            requests.append((start, end))
            self.send_response(200 if mode == 'ignore' else 206)
            self.send_header('Content-Range', f'bytes {start}-{end}/{len(payload)}')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            if mode == 'truncate':
                body = body[:-1]
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_range_fetch_and_offline_resume(tmp_path, monkeypatch):
    payload = raw_zip()
    with range_server(payload) as (url, requests):
        data = oxts.fetch_drive(tmp_path, RAW, url)
        assert len(requests) <= 4
        assert sum(end - start + 1 for start, end in requests) < len(payload) / 5
        assert set(data['members']) == {'data/0000000000.txt', 'data/0000000001.txt', 'timestamps.txt'}
        assert oxts.frame_gps(data, RAW)['location'] == (8, 49)
    monkeypatch.setattr(socket.socket, 'connect', lambda *args: pytest.fail('Cached fetch attempted network'))
    assert oxts.fetch_drive(tmp_path, RAW, url) == data
    assert not list(tmp_path.glob('.kitti-*'))


@pytest.mark.parametrize('mode', ['ignore', 'truncate'])
def test_bad_range_responses_do_not_commit_cache(tmp_path, mode):
    with range_server(raw_zip(), mode) as (url, _), pytest.raises((ValueError, OSError, HTTPException)):
        oxts.fetch_drive(tmp_path, RAW, url)
    assert not list(tmp_path.glob('*.json'))


def test_range_refuses_unrelated_entries(tmp_path):
    with range_server(raw_zip(interleaved=True)) as (url, _), pytest.raises(ValueError, match='not contiguous'):
        oxts.fetch_drive(tmp_path, RAW, url)
    assert not list(tmp_path.glob('*.json'))


def test_cache_version_and_missing_frame(tmp_path):
    data = seed_cache(tmp_path)
    assert oxts.frame_gps(data, oxts.RawFrame(RAW.date, RAW.drive, 10)) == {}
    path = oxts.cache_path(tmp_path / 'oxts-cache', RAW.drive)
    data['version'] = 99
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='Invalid OXTS cache'):
        oxts.load_drive(tmp_path / 'oxts-cache', RAW.drive)
    path.write_text('[]')
    with pytest.raises(ValueError, match='Invalid OXTS cache'):
        oxts.load_drive(tmp_path / 'oxts-cache', RAW.drive)


@pytest.fixture
def source(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    image = io.BytesIO()
    Image.new('RGB', (100, 80), 'blue').save(image, format='PNG')
    for name, suffix, contents in (
        ('image_2', 'png', image.getvalue()),
        ('calib', 'txt', CALIBRATION),
        ('label_2', 'txt', LABEL + '\n' + DONTCARE),
        ('velodyne', 'bin', np.array([[10, 0, 0, 0.5], [30, 0, 0, 0]], dtype='<f4').tobytes()),
    ):
        with ZipFile(source / f'data_object_{name}.zip', 'w') as archive:
            for split in ('training', 'testing'):
                if split == 'testing' and name == 'label_2':
                    continue
                for frame in ('000000', '000001'):
                    archive.writestr(f'{split}/{name}/{frame}.{suffix}', contents)
    with ZipFile(source / 'devkit_object.zip', 'w') as archive:
        archive.writestr('mapping/train_mapping.txt', f'{RAW.date} {RAW.drive} 0000000000\n' * 2)
        archive.writestr('mapping/train_rand.txt', '2,1')
    return source


class FakeEncoder:
    def encode(self, samples, images, field):
        assert len(samples) == len(images)
        for sample in samples:
            sample.embeddings = {field: [1.0, 0.0]}


class MemoryDataset:
    _id = 1

    def __init__(self):
        self.rows = {}
        self.reductions = []
        self.refreshes = 0
        self.displays = []
        self.projected = set()
        self.logins = []

    def add_samples(self, samples, on_duplicate):
        ids = []
        for sample in samples:
            sample.id = self.rows[sample.filepath].id if sample.filepath in self.rows else len(self.rows) + 1
            self.rows[sample.filepath] = sample
            ids.append(sample.id)
        return {'ids': ids}

    def lookup_samples(self, field, paths):
        return {path: self.rows[path].id for path in paths if path in self.rows}

    def query_samples(self, dataset_id, limit, filters):
        return {
            'items': [
                dict(
                    id=row.id,
                    filepath=row.filepath,
                    media_type=row.media_type,
                    tags=row.tags,
                    metadata=row.metadata,
                    location=(
                        {'type': 'Point', 'coordinates': list(row.location), 'heading': row.heading}
                        if row.location
                        else None
                    ),
                )
                for row in self.rows.values()
                if row.id in filters['value']
            ],
        }

    def refresh_stats(self):
        self.refreshes += 1

    def compute_visualization(self, *, field_name):
        self.reductions.append(field_name)
        self.projected.add(field_name)
        count = sum(field_name in sample.embeddings for sample in self.rows.values())
        return {'fit_sample_size': count, 'reduced': count}

    def get_embedding_field_inventory(self, dataset_id):
        return [
            {'field_name': field, 'count': sum(field in s.embeddings for s in self.rows.values())}
            for field in ('clip', 'clip_obj')
        ]

    def get_vectors(self, dataset_id, field_name, limit, pending_only):
        return {'items': [] if field_name in self.projected else [1]}

    def login(self, username, password):
        self.logins.append((username, password))

    def register_field_model(self, *args):
        pass

    def update_dataset(self, dataset_id, **kwargs):
        self.description = kwargs["description"]

    def get_dataset_by_id(self, dataset_id):
        return {}

    def get_schema(self):
        return [{'name': 'occluded'}] if any(r.media_type == 'object' for r in self.rows.values()) else []

    def set_field_display_type(self, dataset_id, field, display):
        self.displays.append((field, display))


@pytest.fixture
def memory_api(monkeypatch):
    dataset = MemoryDataset()
    monkeypatch.setattr(fridge, 'connect', lambda url: None)
    monkeypatch.setattr(fridge, 'get_client', lambda: dataset)
    monkeypatch.setenv('FRIDGE_API_TOKEN', 'fixture')
    monkeypatch.setattr(fridge, 'Dataset', lambda *args, **kwargs: dataset)
    monkeypatch.setattr(common, 'check_media', lambda *args: None)
    return dataset


def args_for(source, root, *extra):
    return importer.build_parser().parse_args(['--source-root', str(source), '--data-root', str(root), *extra])


def test_import_links_embeddings_and_resume(source, tmp_path, memory_api, monkeypatch):
    root = tmp_path / 'derived'
    seed_cache(root)
    args = args_for(source, root)
    calls = []

    def factory(args):
        calls.append('load')
        return FakeEncoder()

    counts = importer.run(args, factory)
    assert counts['images_written'] == counts['objects_written'] == 2
    assert importer.ATTRIBUTION in memory_api.description
    assert 'https://www.cvlibs.net/datasets/kitti/' in memory_api.description
    assert calls == ['load']
    assert memory_api.reductions == []
    images = [row for row in memory_api.rows.values() if row.media_type == 'image']
    objects = [row for row in memory_api.rows.values() if row.media_type == 'object']
    for child in objects:
        parent = memory_api.rows[child.metadata['parent_filepath']]
        assert child.metadata['parent_id'] == parent.id
        assert child.metadata['cuboid_id'] in {box['id'] for box in parent.metadata['ground_truth_3d']}
        assert child.metadata['cuboid_field'] == 'ground_truth_3d'
        assert child.metadata['num_lidar_points'] == 1
        assert child.location == parent.location == (8, 49)
        assert child.heading == parent.heading == pytest.approx(0)
        assert child.metadata['license'] == parent.metadata['license'] == importer.LICENSE
        assert Path(child.filepath).is_relative_to(root)
        assert set(child.embeddings) == {'clip_obj'}
    assert all(set(parent.embeddings) == {'clip'} for parent in images)
    assert memory_api.displays == [('occluded', 'enum')]
    assert not list(root.rglob('*.txt'))  # labels/calibration stay in their ZIPs
    monkeypatch.setattr(socket.socket, 'connect', lambda *args: pytest.fail('Offline import attempted network'))
    mtimes = {p: p.stat().st_mtime_ns for p in root.rglob('*.jpg')}
    memory_api.description = 'Old credits'
    counts = importer.run(args, lambda args: pytest.fail('Completed rerun loaded CLIP'))
    assert importer.ATTRIBUTION in memory_api.description
    assert {p: p.stat().st_mtime_ns for p in mtimes} == mtimes
    assert memory_api.displays == [('occluded', 'enum')] * 2
    assert counts['images_written'] == counts['objects_written'] == 0
    assert memory_api.refreshes == 2
    assert memory_api.reductions == []
    # An interrupted object pass resumes even when its parent already exists.
    lost = objects[0]
    del memory_api.rows[lost.filepath]
    counts = importer.run(args, factory)
    assert counts['images_written'] == 0 and counts['objects_written'] == 1
    assert len(memory_api.rows) == 4


def test_testing_split_and_limit(source, tmp_path, memory_api):
    args = args_for(source, tmp_path / 'derived', '--split', 'testing', '--limit', '1', '--skip-embeddings')
    counts = importer.run(args, lambda args: pytest.fail('skip-embeddings loaded CLIP'))
    assert counts['images_written'] == 1 and counts['objects_written'] == 0
    sample = next(iter(memory_api.rows.values()))
    assert sample.location is None
    assert not {'ground_truth', 'ground_truth_3d', 'drive'} & sample.metadata.keys()
    assert sample.tags == ['testing']
    assert Path(sample.metadata['lidar']['path']).is_file()
    assert not memory_api.reductions


def test_fetch_only_never_connects_to_api(source, tmp_path, monkeypatch):
    root = tmp_path / 'derived'
    seed_cache(root)
    monkeypatch.setattr(fridge, 'connect', lambda *args: pytest.fail('fetch-only called API'))
    monkeypatch.setattr(socket.socket, 'connect', lambda *args: pytest.fail('fetch-only ignored complete cache'))
    assert importer.run(args_for(source, root, '--fetch-oxts-only')) == {'cached_drives': 1}
    assert not (root / 'training').exists()


def test_missing_gps_retains_frames(source, tmp_path, memory_api):
    importer.run(args_for(source, tmp_path / 'derived', '--skip-embeddings'))
    assert len(memory_api.rows) == 4
    assert all(row.location is None for row in memory_api.rows.values())


def test_media_403_aborts_after_first_image_batch(source, tmp_path, memory_api, monkeypatch):
    def refused(*args):
        raise RuntimeError('Backend refused image access (403)')

    monkeypatch.setattr(common, 'check_media', refused)
    with pytest.raises(RuntimeError, match='403'):
        importer.run(args_for(source, tmp_path / 'derived', '--skip-embeddings', '--batch-size', '1'))
    assert len(memory_api.rows) == 1
    assert not memory_api.reductions


def test_bad_projection_aborts_before_api(source, tmp_path, monkeypatch):
    with ZipFile(source / 'data_object_label_2.zip', 'w') as archive:
        for frame in ('000000', '000001'):
            archive.writestr(f'training/label_2/{frame}.txt', (LABEL.replace('0 2 10 0', '0 2 100 0') + '\n') * 10)
    monkeypatch.setattr(fridge, 'connect', lambda *args: pytest.fail('Bad projection reached API'))
    with pytest.raises(ValueError, match='below 0.9'):
        importer.run(args_for(source, tmp_path / 'derived', '--skip-embeddings'))


def test_object_crop_cannot_follow_symlink(tmp_path):
    _, boxes = importer.parse_labels(LABEL, (100, 80), '000000')
    path = tmp_path / 'parent.png'
    Image.new('RGB', (100, 80)).save(path)
    sample = Sample(
        str(path),
        metadata={
            'calibration': Calibration.from_kitti(CALIBRATION, (100, 80)),
            'ground_truth_3d': boxes,
            'split': 'training',
            'frame_id': '000000',
        },
    )
    sample.id = 1
    directory = tmp_path / 'derived/objects/1'
    directory.mkdir(parents=True)
    (directory / 'malicious.jpg').symlink_to(path)
    with pytest.raises(ValueError, match='symlinks'):
        importer.make_objects(sample, 1, common.Assets(tmp_path))


def test_defaults_and_data_root(source, tmp_path, monkeypatch):
    args = args_for(source, tmp_path / 'derived')
    assert args.limit == 100 and args.split == 'training' and args.on_duplicate == 'skip'
    assert args_for(source, tmp_path, '--limit', '0').limit == 0
    monkeypatch.setenv('FRIDGE_DATA_ROOT', str(tmp_path / 'other'))
    with pytest.raises(ValueError, match='under FRIDGE_DATA_ROOT'):
        importer.run(args)


def test_small_selection_skips_umap_without_hiding_other_failures(source, tmp_path, memory_api, monkeypatch):
    def factory(args):
        return FakeEncoder()

    args = args_for(source, tmp_path / 'derived', '--limit', '1')
    assert importer.run(args, factory)['images_written'] == 1
    assert memory_api.reductions == []
    monkeypatch.setattr(memory_api, 'get_embedding_field_inventory', lambda _: [{'field_name': 'clip', 'count': 5}])

    def bad_reduction(*args, **kwargs):
        raise ValueError('Invalid reduction')

    monkeypatch.setattr(memory_api, 'compute_visualization', bad_reduction)
    with pytest.raises(ValueError, match='Invalid reduction'):
        importer.run(args, factory)


@pytest.mark.parametrize('error', [BadZipFile('broken directory'), IncompleteRead(b'partial')])
def test_fetch_failure_preserves_successful_drives(source, tmp_path, monkeypatch, error):
    second = oxts.RawFrame('2011_09_28', '2011_09_28_drive_0001_sync', 0)
    monkeypatch.setattr(importer, 'read_mapping', lambda root: {'000000': RAW, '000001': second})
    calls = []

    def fetch(root, raw):
        calls.append(raw.drive)
        if raw == RAW:
            raise error
        return seed_cache(root.parent, raw)

    monkeypatch.setattr(importer, 'fetch_drive', fetch)
    root = tmp_path / 'derived'
    with pytest.raises(RuntimeError, match='1 drives failed'):
        importer.run(args_for(source, root, '--fetch-oxts-only'))
    assert calls == [RAW.drive, second.drive]
    assert oxts.load_drive(root / 'oxts-cache', second.drive) is not None


@pytest.mark.parametrize('cached', ['{broken', '[]', '{"version": 0}', '{"version": 1, "members": {}}'])
def test_invalid_cache_is_refetched(tmp_path, cached):
    oxts.cache_path(tmp_path, RAW.drive).write_text(cached)
    with range_server(raw_zip()) as (url, requests):
        result = oxts.fetch_drive(tmp_path, RAW, url)
    assert requests
    assert result == oxts.load_drive(tmp_path, RAW.drive)


def test_timestamp_parser_does_not_use_fromisoformat(monkeypatch):
    class CompatibleDatetime(datetime):
        @classmethod
        def fromisoformat(cls, text):
            pytest.fail('Nanosecond timestamps must work on Python 3.10')

    monkeypatch.setattr(oxts, 'datetime', CompatibleDatetime)
    assert oxts.parse_oxts(PACKET, TIMESTAMP)['captured_at'].endswith('.123456+02:00')
    assert oxts.parse_oxts(PACKET, '2011-09-26 13:02:45.1')['captured_at'].endswith('.100000+02:00')
    assert oxts.parse_oxts(PACKET, '2011-09-26 13:02:45')['captured_at'].endswith('13:02:45+02:00')
    for value in (TIMESTAMP + 'Z', TIMESTAMP + '0', TIMESTAMP + '+02:00'):
        with pytest.raises(ValueError):
            oxts.parse_oxts(PACKET, value)


def test_raw_timestamp_not_auto_detected_as_utc():
    from datetime import datetime

    gps = oxts.parse_oxts(PACKET, TIMESTAMP)
    with pytest.raises(ValueError):
        datetime.fromisoformat(gps['oxts_timestamp_raw'])
    offset = datetime.fromisoformat(gps['captured_at']).utcoffset()
    assert offset is not None and offset.total_seconds() == 7200


@pytest.mark.parametrize(
    'error',
    [HTTPError('fixture', 503, 'busy', Message(), None), IncompleteRead(b''), OSError('reset')],
)
def test_http_retries_with_backoff(monkeypatch, error):
    sleeps: list[int] = []
    calls = []
    monkeypatch.setattr(time, 'sleep', sleeps.append)

    def request(self, byte_range):
        calls.append(byte_range)
        if len(calls) < 3:
            raise error
        return b'ok'

    monkeypatch.setattr(oxts.HTTPRangeFile, '_request_once', request)
    with oxts.HTTPRangeFile('http://127.0.0.1/fixture'):
        pass
    assert calls == ['bytes=-65558'] * 3
    assert sleeps == [1, 2]


def test_http_client_errors_are_not_retried(monkeypatch):
    def request(self, byte_range):
        raise HTTPError('fixture', 404, 'missing', Message(), None)

    monkeypatch.setattr(oxts.HTTPRangeFile, '_request_once', request)
    monkeypatch.setattr(time, 'sleep', lambda _: pytest.fail('Retried permanent 404'))
    with pytest.raises(HTTPError):
        oxts.HTTPRangeFile('http://127.0.0.1/fixture')


def test_zip_comment_does_not_need_extra_tail_request(tmp_path):
    with range_server(raw_zip(comment=True)) as (url, requests):
        oxts.fetch_drive(tmp_path, RAW, url)
    assert len(requests) == 3


def test_selection_is_independent_of_cwd_training_symlink(source, tmp_path, monkeypatch):
    cwd = tmp_path / 'cwd'
    cwd.mkdir()
    (cwd / 'training').symlink_to(source, target_is_directory=True)
    monkeypatch.chdir(cwd)
    with ZipFile(source / 'data_object_image_2.zip') as archive:
        assert importer.selected_frames(archive, 'training', 1) == ['000000']


def test_projection_excludes_pedestrians_and_requires_twenty(source, tmp_path, memory_api, capsys):
    _, boxes = importer.parse_labels(LABEL.replace('Car', 'Pedestrian'), (100, 80), '000000')
    assert list(importer.projection_ious(Calibration.from_kitti(CALIBRATION, (100, 80)), boxes)) == []
    with ZipFile(source / 'data_object_label_2.zip', 'w') as archive:
        for frame in ('000000', '000001'):
            archive.writestr(f'training/label_2/{frame}.txt', LABEL.replace('0 2 10 0', '0 2 100 0'))
    importer.run(args_for(source, tmp_path / 'derived', '--skip-embeddings'))
    assert 'not measurable (2 eligible boxes' in capsys.readouterr().out


@pytest.mark.parametrize('remove_zip', [False, True])
def test_optional_lidar(source, tmp_path, memory_api, remove_zip):
    if remove_zip:
        (source / 'data_object_velodyne.zip').unlink()
    extra = [] if remove_zip else ['--no-lidar']
    counts = importer.run(args_for(source, tmp_path / 'derived', '--skip-embeddings', *extra))
    assert counts['images_written'] == counts['objects_written'] == 2
    assert all('lidar' not in s.metadata and 'num_lidar_points' not in s.metadata for s in memory_api.rows.values())
    assert not list((tmp_path / 'derived').rglob('*.bin'))


def test_outside_boxes_are_counted_and_reported(source, tmp_path, memory_api, capsys):
    with ZipFile(source / 'data_object_label_2.zip', 'w') as archive:
        for frame in ('000000', '000001'):
            archive.writestr(f'training/label_2/{frame}.txt', LABEL.replace('0 2 10 0', '1000 2 10 0'))
    counts = importer.run(args_for(source, tmp_path / 'derived', '--skip-embeddings'))
    assert counts['objects_without_extent'] == 2 and counts['objects_written'] == 0
    assert "'objects_without_extent': 2" in capsys.readouterr().out
    assert all(s.metadata['num_cuboids'] == 1 for s in memory_api.rows.values())


def test_crash_after_last_upload_recovers_finalization(source, tmp_path, memory_api, monkeypatch):
    args = args_for(source, tmp_path / 'derived')
    monkeypatch.setattr(
        memory_api,
        'get_embedding_field_inventory',
        lambda _: [
            {'field_name': 'clip', 'count': 5},
            {'field_name': 'clip_obj', 'count': 5},
        ],
    )
    original = memory_api.refresh_stats

    def crash():
        raise RuntimeError('crash after uploads')

    monkeypatch.setattr(memory_api, 'refresh_stats', crash)

    def factory(args):
        return FakeEncoder()

    with pytest.raises(RuntimeError, match='crash after uploads'):
        importer.run(args, factory)
    assert len(memory_api.rows) == 4 and not memory_api.reductions and not memory_api.displays
    monkeypatch.setattr(memory_api, 'refresh_stats', original)
    counts = importer.run(args, lambda args: pytest.fail('Resume loaded CLIP'))
    assert counts['images_written'] == counts['objects_written'] == 0
    assert memory_api.refreshes == 1
    assert memory_api.displays == [('occluded', 'enum')]
    assert memory_api.reductions == ['clip', 'clip_obj']
    importer.run(args, lambda args: pytest.fail('No-op rerun loaded CLIP'))
    assert memory_api.reductions == ['clip', 'clip_obj']


def test_token_only_import_never_prompts_or_logs_in(source, tmp_path, memory_api, monkeypatch):
    monkeypatch.delenv('FRIDGE_PASSWORD', raising=False)
    monkeypatch.setenv('FRIDGE_API_TOKEN', 'fixture')
    monkeypatch.setattr(getpass, 'getpass', lambda *_: pytest.fail('Token import prompted'))
    assert importer.run(args_for(source, tmp_path / 'derived', '--skip-embeddings'))['images_written'] == 2
    assert memory_api.logins == []


def test_s3_media_keeps_parent_links_and_lidar(source, tmp_path, memory_api, monkeypatch):
    objects_in_s3 = {}

    class FakeS3:
        def put_object(self, *, Bucket, Key, Body, ContentType):
            objects_in_s3[f's3://{Bucket}/{Key}'] = Body.read()

    monkeypatch.setattr(common.Media, 'client', property(lambda self: FakeS3()))
    # The backend never reads the local files, so they need not be under its data root.
    monkeypatch.setenv('FRIDGE_DATA_ROOT', str(tmp_path / 'backend-root'))
    root = tmp_path / 'derived'
    seed_cache(root)
    args = args_for(source, root, '--media-uri', 's3://demo-media/media/kitti')
    counts = importer.run(args, lambda args: FakeEncoder())
    assert counts['images_written'] == counts['objects_written'] == 2
    base = 's3://demo-media/media/kitti/'
    assert all(path.startswith(base) for path in memory_api.rows)
    images = [row for row in memory_api.rows.values() if row.media_type == 'image']
    objects = [row for row in memory_api.rows.values() if row.media_type == 'object']
    for child in objects:
        parent = memory_api.rows[child.metadata['parent_filepath']]
        assert child.metadata['parent_id'] == parent.id and child.metadata['num_lidar_points'] == 1
    lidar = {parent.metadata['lidar']['path'] for parent in images}
    assert all(path.startswith(base) for path in lidar)
    assert set(objects_in_s3) == set(memory_api.rows) | lidar
    for path, body in objects_in_s3.items():
        assert body == (root / path.removeprefix(base)).read_bytes()
    objects_in_s3.clear()
    counts = importer.run(args, lambda args: pytest.fail('Completed rerun loaded CLIP'))
    assert counts['images_written'] == counts['objects_written'] == 0 and not objects_in_s3
