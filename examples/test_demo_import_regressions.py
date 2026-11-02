"""Import regressions, including parent-first and interrupted object resume contracts."""

import sys
import types
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from examples._shared import importer as common
from examples.coco import ingest as coco_mapper
from examples.coco.test_coco import coco as coco_fixture
from examples.open_images import ingest as oi_mapper
from examples.open_images.test_open_images import oi as oi_fixture


@pytest.fixture
def coco(tmp_path):
    directory = tmp_path / 'coco'
    directory.mkdir()
    return getattr(coco_fixture, '__wrapped__')(directory)


@pytest.fixture
def oi(tmp_path):
    directory = tmp_path / 'oi'
    directory.mkdir()
    return getattr(oi_fixture, '__wrapped__')(directory)


def assets_for(root):
    return common.Assets(root)


def mapped_coco(coco):
    root, row = coco
    with common.AnnotationIndex(root) as index:
        categories, licenses = coco_mapper.build_index(root, index)
        return coco_mapper.map_sample(root, row, index, categories, licenses, assets_for(root))


def mapped_oi(root):
    with common.AnnotationIndex(root) as index:
        names, ancestors = oi_mapper.build_index(root, index)
        return oi_mapper.map_sample(root, next(index.images(1)), index, names, ancestors, assets_for(root))


def test_co1_local_crowd_mask(coco):
    sample, image = mapped_coco(coco)
    image.close()
    path = Path(sample.metadata['crowd_mask_000']['maskPath'])
    assert path.is_absolute() and path.is_relative_to(coco[0]) and path.is_file()


def test_co1_rotated_jpeg(oi):
    sample, image = mapped_oi(oi)
    image.close()
    assert Path(sample.filepath).is_relative_to(oi)
    with Image.open(sample.filepath) as saved:
        assert saved.format == 'JPEG' and saved.size == (3, 5)


def test_co1_guard_permits_media_design_without_writing(monkeypatch):
    monkeypatch.setattr(Path, 'mkdir', lambda *args, **kwargs: None)
    root = Path('/media/example/data')
    assert common.writable_directory(root / "derived", root) == root / "derived"


def test_co1_symlink_escape(tmp_path):
    root = tmp_path / 'root'
    root.mkdir()
    (root / 'derived').symlink_to(tmp_path / 'outside', target_is_directory=True)
    with pytest.raises(ValueError, match='escaped'):
        assets_for(root)
    assert not (tmp_path / 'outside').exists()


def test_co3_every_visible_bone(coco):
    root, _ = coco
    import json

    path = root / 'annotations/person_keypoints_val2017.json'
    data = json.loads(path.read_text())
    data['annotations'][0]['keypoints'] = [1, 1, 2, 2, 2, 1, 3, 1, 2] + [0, 0, 0] * 14
    path.write_text(json.dumps(data))
    sample, image = mapped_coco(coco)
    image.close()
    bones = sample.metadata['skeletons']
    assert len(bones) == 2
    assert all(len(b.get('paths', [b.get('points')])) == 1 for b in bones)
    assert len({b['id'] for b in bones}) == 2


def test_co4_absent_is_plain_filterable_strings(oi):
    sample, image = mapped_oi(oi)
    image.close()
    assert sample.array_fields['negative_classes'] == ['Cat']
    assert 'negative_labels' not in sample.metadata
    assert all('label' not in row for row in sample.metadata['verified_absent'])


def test_co5_dataset_mask_colors_and_hover_across_classes(oi):
    sample, image = mapped_oi(oi)
    image.close()
    settings = common.segmentation_settings({}, [sample])
    colors = settings['color_scheme']['fields']
    values = []
    for field in ('instance_mask_000', 'instance_mask_001'):
        descriptor = sample.metadata[field]
        with Image.open(descriptor['maskPath']) as mask:
            pixel = str(max(mask.getdata()))
        assert settings['mask_targets'][field][pixel] == 'Dog'
        values.append(colors[field]['idColors'][pixel])
    assert len(set(values)) == 2
    # A different class in the same slot must not replace the first hover target.
    assets = assets_for(oi)
    with Image.new('L', (1, 1), 255) as mask:
        field, descriptor = assets.mask('other.png', mask, 'instance_mask_000', 200, 'Cat', 0)
        bank_field, bank_descriptor = assets.mask('bank.png', mask, 'instance_mask_000', 455, 'Bird', 0)
    from fridge import Sample

    updated = common.segmentation_settings(
        settings,
        [Sample('unused', metadata={field: descriptor, bank_field: bank_descriptor})],
    )
    assert set(updated['mask_targets'][field].values()) == {'Dog', 'Cat'}
    assert updated['mask_targets'][bank_field]['200'] == 'Bird'
    assert settings['mask_targets'][field] != updated['mask_targets'][field]


def test_co9_mask_links_boxes(oi):
    path = oi / 'annotations' / oi_mapper.CSV_FILES['detections']
    path.write_text(path.read_text().replace('.1,', '.1000002,'))
    sample, image = mapped_oi(oi)
    image.close()
    detection = sample.metadata['detections'][0]
    for instance in sample.metadata['mask_instances']:
        assert instance['detection_id'] == detection['id']
        assert instance['mask_field'] in detection['mask_fields']


def test_co9_captions_plain(coco):
    sample, image = mapped_coco(coco)
    image.close()
    assert all(isinstance(x, str) for x in sample.metadata['captions'])
    assert not sample.metadata.get('caption_annotations')


def test_co9_narratives_and_html(oi):
    path = oi / 'annotations' / oi_mapper.CSV_FILES['images']
    path.write_text(
        path.read_text().replace('Photographer', 'snowpea&amp;bokchoi').replace('A café', 'A &quot;café&quot;'),
    )
    sample, image = mapped_oi(oi)
    image.close()
    assert sample.metadata['Author'] == 'snowpea&bokchoi'
    assert sample.metadata['Title'] == 'A "café"'
    assert all(isinstance(x, str) for x in sample.metadata['narratives'])


def test_co9_attribute_names_and_lean_labels(oi):
    path = oi / 'annotations' / oi_mapper.CSV_FILES['relationships']
    path.write_text(path.read_text().replace('/cat', '/m/01lhf'))
    sample, image = mapped_oi(oi)
    image.close()
    assert sample.metadata['relationships'][0].get('object') == 'Talk'
    assert 'Talk' in sample.array_fields['class_names']
    for field in ('detections', 'points', 'relationships', 'positive_labels'):
        for label in sample.metadata[field]:
            assert 'attributes' not in label and '_row' not in label


def test_co7_docs_and_removed_session_files():
    root = Path(__file__).resolve().parent
    assert not (root / 'serve_demo_assets.py').exists()
    assert not (root / 'open_images/README.remote.md').exists()
    assert not (root / 'open_images/README.network-importer.md').exists()
    assert not list(root.glob('*/smoke-results.json'))


def test_co8_environment_credentials_and_safe_default(monkeypatch):
    monkeypatch.setenv('FRIDGE_USERNAME', 'fixture-user')
    monkeypatch.setenv('FRIDGE_PASSWORD', 'fixture-secret')
    args = coco_mapper.build_parser().parse_args(['--data-root', '/unused'])
    assert (args.username, args.password, args.limit) == ('fixture-user', 'fixture-secret', 100)
    monkeypatch.delenv('FRIDGE_PASSWORD')
    assert coco_mapper.build_parser().parse_args(['--data-root', '/unused']).password is None


@pytest.fixture
def backend(monkeypatch, tmp_path):
    import fridge
    import torch

    rows: dict = {}
    events: list[Any] = []
    reductions: list[str] = []
    settings: list[dict] = []
    state = types.SimpleNamespace(status=200, fail_objects=False)

    class Response:
        @property
        def status_code(self):
            return state.status

        def __enter__(self):
            events.append('media')
            return self

        def __exit__(self, *_):
            pass

        def raise_for_status(self):
            assert state.status == 200

    client = types.SimpleNamespace(
        base_url='http://fixture/api',
        login=lambda *_: None,
        register_field_model=lambda *args: events.append(('model', args[1])),
        get_dataset_by_id=lambda *_: {'segmentation_config': settings[-1] if settings else {}},
        update_dataset=lambda _, **kwargs: (
            settings.append(kwargs['segmentation_config'])
            if 'segmentation_config' in kwargs
            else setattr(state, 'description', kwargs['description'])
        ),
        session=types.SimpleNamespace(get=lambda *args, **kwargs: Response()),
    )

    class Dataset:
        _id = 17

        def __init__(self, *args, **kwargs):
            pass

        def create_array_field(self, *_):
            pass

        def lookup_samples(self, _, paths):
            return {p: rows[p]['id'] for p in paths if p in rows}

        def add_samples(self, samples, **kwargs):
            ids = []
            for sample in samples:
                if state.fail_objects and sample.media_type == 'object':
                    raise RuntimeError('interrupted after parents')
                if sample.media_type == 'object':
                    assert any(
                        r['id'] == sample.metadata['parent_id'] and r['media_type'] == 'image' for r in rows.values()
                    )
                row = sample.to_dict()
                row['id'] = rows.get(sample.filepath, {}).get('id', 1000 - len(rows) * 7)
                rows[sample.filepath] = row
                ids.append(row['id'])
                events.append(sample.media_type)
            return {'ids': ids}

        def refresh_stats(self):
            pass

        def compute_visualization(self, *, field_name):
            reductions.append(field_name)
            count = sum(field_name in row['embeddings'] for row in rows.values())
            return {'fit_sample_size': count, 'reduced': count}

    class Model:
        @classmethod
        def from_pretrained(cls, *args, **kwargs):
            events.append('load')
            return cls()

        def to(self, *_):
            return self

        def eval(self):
            return self

        def get_image_features(self, pixel_values):
            return torch.ones((len(pixel_values), 4))

    class Inputs(dict):
        def to(self, *_):
            return self

    class Processor:
        @classmethod
        def from_pretrained(cls, *args, **kwargs):
            return cls()

        def __call__(self, images, **kwargs):
            return Inputs(pixel_values=images)

    monkeypatch.setattr(fridge, 'Dataset', Dataset)
    monkeypatch.setattr(fridge, 'connect', lambda *_: None)
    monkeypatch.setattr(fridge, 'get_client', lambda: client)
    # Replacing the lazy module avoids CLIPProcessor's first import restoring
    # the real CLIPModel behind our fake, which otherwise needs a model cache.
    monkeypatch.setitem(sys.modules, 'transformers', types.SimpleNamespace(CLIPModel=Model, CLIPProcessor=Processor))
    args = coco_mapper.build_parser().parse_args(['--data-root', str(tmp_path), '--device', 'cpu'])
    args.password = 'fixture'
    args.assets_dir, args.assets_url = tmp_path / 'derived', 'http://fixture/assets'
    return types.SimpleNamespace(
        rows=rows,
        events=events,
        reductions=reductions,
        settings=settings,
        state=state,
        args=args,
    )


def run_coco(backend, coco):
    root, _ = coco
    backend.args.data_root = root
    with common.AnnotationIndex(root) as index:
        categories, licenses = coco_mapper.build_index(root, index)
        return common.ingest_objects(
            backend.args,
            index,
            lambda row, assets, **kwargs: coco_mapper.map_sample(
                root,
                row,
                index,
                categories,
                licenses,
                assets,
                **kwargs,
            ),
            'fixture',
        )


def test_co6_objects_parent_ids_crowds_and_resume(backend, coco, monkeypatch):
    run_coco(backend, coco)
    parents = [r for r in backend.rows.values() if r['media_type'] == 'image']
    objects = [r for r in backend.rows.values() if r['media_type'] == 'object']
    assert len(parents) == 1 and len(objects) == 2
    assert [r['metadata']['is_crowd'] for r in objects] == [False, True]
    for row in objects:
        assert row['metadata']['parent_id'] == parents[0]['id']
        assert row['metadata']['crop_bbox'] == [0.25, 0, 0.5, 2 / 3]
        assert row['metadata']['annotation_id'] in (7, 8)
        assert row['tags'] == ['person']
        assert list(row['embeddings']) == ['clip_obj']
        with Image.open(row['filepath']) as crop:
            assert crop.format == 'JPEG' and crop.size == (2, 2)
    assert backend.reductions == ['clip', 'clip_obj']
    assert backend.events.index('image') < backend.events.index('media') < backend.events.index('object')
    assert backend.settings[-1]['mask_targets']['crowd_mask_000']['1'] == 'person'
    backend.reductions.clear()

    def forbidden(*args, **kwargs):
        pytest.fail('No-op resume decoded an image')

    monkeypatch.setattr(common, 'rgb_image', forbidden)
    monkeypatch.setattr(coco_mapper, 'rgb_image', forbidden)
    run_coco(backend, coco)
    assert not backend.reductions and len(backend.rows) == 3


def test_co6_interrupted_objects_resume(backend, coco):
    backend.state.fail_objects = True
    with pytest.raises(RuntimeError, match='interrupted'):
        run_coco(backend, coco)
    assert len(backend.rows) == 1
    parent_id = next(iter(backend.rows.values()))['id']
    backend.state.fail_objects = False
    run_coco(backend, coco)
    assert len(backend.rows) == 3
    assert all(r['metadata']['parent_id'] == parent_id for r in backend.rows.values() if r['media_type'] == 'object')
    assert backend.reductions == ['clip_obj']


def test_co6_rotated_group_crop(backend, oi):
    backend.args.data_root = oi
    with common.AnnotationIndex(oi) as index:
        names, ancestors = oi_mapper.build_index(oi, index)
        common.ingest_objects(
            backend.args,
            index,
            lambda row, assets, **kwargs: oi_mapper.map_sample(oi, row, index, names, ancestors, assets, **kwargs),
            'fixture',
        )
    objects = [r for r in backend.rows.values() if r['media_type'] == 'object']
    assert len(objects) == 1
    row = objects[0]
    assert row['metadata']['crop_bbox'] == pytest.approx([0.2, 0.3, 0.6, 0.6])
    assert row['metadata']['is_group_of'] is True
    assert row['tags'] == ['Dog'] and 'clip_obj' in row['embeddings']
    with Image.open(row['filepath']) as crop:
        assert crop.size == (3, 4)


def test_co8_stop_on_media_403(backend, coco):
    backend.state.status = 403
    with pytest.raises(RuntimeError, match='403.*FRIDGE_DATA_ROOT'):
        run_coco(backend, coco)
    assert len(backend.rows) == 1
    assert not backend.reductions


def test_co8_noop_skips_umap(backend, coco):
    run_coco(backend, coco)
    backend.reductions.clear()
    run_coco(backend, coco)
    assert not backend.reductions


def test_co6_parent_ids_follow_input_order(backend, coco):
    import json
    import shutil

    root, first = coco
    second = {**first, 'id': 2, 'file_name': '000000000002.jpg'}
    shutil.copy2(root / 'val2017' / first['file_name'], root / 'val2017' / second['file_name'])
    path = root / 'annotations/instances_val2017.json'
    data = json.loads(path.read_text())
    data['images'].append(second)
    data['annotations'].append({**data['annotations'][0], 'id': 9, 'image_id': 2})
    path.write_text(json.dumps(data))
    run_coco(backend, coco)
    parents = {r['metadata']['image_id']: r['id'] for r in backend.rows.values() if r['media_type'] == 'image'}
    assert parents[1] > parents[2]  # Backend IDs deliberately do not follow numeric input order.
    objects = [r for r in backend.rows.values() if r['media_type'] == 'object']
    assert len(objects) == 3
    for row in objects:
        image_id = 2 if row['metadata']['annotation_id'] == 9 else 1
        assert row['metadata']['parent_id'] == parents[image_id]


@pytest.mark.parametrize('kind', ['coco', 'open_images'])
def test_shared_rerun_never_loads_clip(backend, coco, oi, monkeypatch, kind):
    """Both object importers resume parents and crops through the same lazy encoder."""
    import transformers

    root = coco[0] if kind == 'coco' else oi
    mapper = coco_mapper if kind == 'coco' else oi_mapper
    backend.args.data_root = root

    def run():
        with common.AnnotationIndex(root) as index:
            first, second = mapper.build_index(root, index)
            return common.ingest_objects(
                backend.args,
                index,
                lambda row, assets, **kwargs: mapper.map_sample(root, row, index, first, second, assets, **kwargs),
                'fixture',
            )

    initial = run()
    assert initial['images_written'] == 1 and initial['objects_written'] > 0

    def forbidden(*args, **kwargs):
        pytest.fail('Completed rerun tried to load CLIP')

    monkeypatch.setattr(transformers.CLIPModel, 'from_pretrained', forbidden)
    monkeypatch.setattr(transformers.CLIPProcessor, 'from_pretrained', forbidden)
    backend.reductions.clear()
    resumed = run()
    assert resumed['images_written'] == resumed['objects_written'] == 0
    assert resumed['images_skipped'] == 1 and resumed['objects_skipped'] == initial['objects_written']
    assert backend.reductions == []


class FakeS3:
    def __init__(self, events=None, error=None):
        self.objects = {}
        self.events = events
        self.error = error

    def put_object(self, *, Bucket, Key, Body, ContentType):
        if self.error is not None:
            raise self.error
        self.objects[Bucket, Key] = (Body.read(), ContentType)
        if self.events is not None:
            self.events.append('upload')


@pytest.mark.parametrize('value', ['bucket/prefix', 's3://', 's3:///prefix', 'https://bucket.s3.amazonaws.com/key'])
def test_media_uri_must_name_a_bucket(value):
    import argparse

    with pytest.raises(argparse.ArgumentTypeError):
        common.media_uri(value)


def test_media_uri_normalized():
    assert common.media_uri('s3://demo-media/media/') == 's3://demo-media/media'
    assert common.media_uri('s3://demo-media') == 's3://demo-media'


def test_media_paths_stay_inside_the_data_root(tmp_path):
    from fridge import Sample

    root = tmp_path / 'root'
    (root / 'a').mkdir(parents=True)
    image = root / 'a' / 'x.jpg'
    image.write_bytes(b'image')
    (tmp_path / 'outside.jpg').write_bytes(b'outside')
    (root / 'a' / 'link.jpg').symlink_to(tmp_path / 'outside.jpg')
    s3 = FakeS3()
    media = common.Media(types.SimpleNamespace(data_root=root, media_uri='s3://bucket/prefix'), client=s3)
    assert media.remote(str(image)) == 's3://bucket/prefix/a/x.jpg'
    with pytest.raises(ValueError, match='escaped'):
        media.remote(str(root / 'a' / 'link.jpg'))
    source = root / 'a' / 'source.tif'
    source.write_bytes(b'source')
    # Relative paths and URLs (BDD100K-style) are recorded as they are.
    assert media.remote('a/x.jpg') == 'a/x.jpg' and media.remote('s3://other/x.jpg') == 's3://other/x.jpg'
    metadata = {
        'mask': {'maskPath': str(image), 'slot': 0},
        'parent_filepath': str(image),
        'source_tif': str(source),
        'note': '/elsewhere/file.jpg',
        'caption': 'a/x.jpg',
    }
    sample = Sample(str(image), metadata=metadata)
    [published] = media.publish([sample])
    assert published.filepath == published.metadata['mask']['maskPath'] == 's3://bucket/prefix/a/x.jpg'
    assert published.metadata['parent_filepath'] == 's3://bucket/prefix/a/x.jpg'
    assert published.metadata['source_tif'] == 's3://bucket/prefix/a/source.tif'
    assert published.metadata['note'] == '/elsewhere/file.jpg' and published.metadata['caption'] == 'a/x.jpg'
    # Every recorded s3:// path is uploaded, each file once.
    assert s3.objects == {
        ('bucket', 'prefix/a/x.jpg'): (b'image', 'image/jpeg'),
        ('bucket', 'prefix/a/source.tif'): (b'source', 'image/tiff'),
    }
    assert sample.filepath == str(image) and sample.metadata == metadata  # the caller's sample is untouched
    restored = media.restore(published)
    assert restored.filepath == restored.metadata['parent_filepath'] == restored.metadata['mask']['maskPath']
    assert restored.filepath == str(image) and restored.metadata['source_tif'] == str(source)
    s3.objects.clear()
    (root / 'a' / 'crop.jpg').write_bytes(b'crop')
    media.publish([Sample(str(root / 'a' / 'crop.jpg'), metadata={'parent_filepath': str(image)})])
    assert set(s3.objects) == {('bucket', 'prefix/a/crop.jpg')}  # the parent was already sent this run
    local = common.Media(types.SimpleNamespace(data_root=root, media_uri=None))
    assert local.publish([sample]) == [sample] and local.remote(str(image)) == str(image)


def test_s3_media_uploaded_before_its_rows_and_resume_uploads_nothing(backend, coco, monkeypatch):
    import mimetypes

    s3 = FakeS3(backend.events)
    monkeypatch.setattr(common.Media, 'client', property(lambda self: s3))
    backend.args.media_uri = 's3://demo-media/media/coco'
    root = coco[0]
    run_coco(backend, coco)
    base = 's3://demo-media/media/coco/'
    assert len(backend.rows) == 3 and all(path.startswith(base) for path in backend.rows)
    recorded = set(backend.rows)
    for row in backend.rows.values():
        recorded |= {v['maskPath'] for v in row['metadata'].values() if isinstance(v, dict) and 'maskPath' in v}
    assert any(path.endswith('.png') for path in recorded)  # the crowd mask
    assert {f's3://{bucket}/{key}' for bucket, key in s3.objects} == recorded
    for (_, key), (body, content_type) in s3.objects.items():
        local = root / key.removeprefix('media/coco/')
        assert body == local.read_bytes() and content_type == mimetypes.guess_type(local)[0]
    # The image is in S3 before its row exists, and the row before the server is asked to serve it.
    assert backend.events.index('upload') < backend.events.index('image') < backend.events.index('media')
    s3.objects.clear()
    run_coco(backend, coco)
    assert not s3.objects and len(backend.rows) == 3


def test_s3_media_403_names_the_bucket(backend, coco, monkeypatch):
    monkeypatch.setattr(common.Media, 'client', property(lambda self: FakeS3()))
    backend.args.media_uri = 's3://demo-media/media/coco'
    backend.state.status = 403
    with pytest.raises(RuntimeError, match='may not read s3://demo-media/media/coco'):
        run_coco(backend, coco)


@pytest.mark.parametrize('code', ['ExpiredToken', 'AccessDenied'])
def test_s3_upload_failure_records_nothing(backend, coco, monkeypatch, code):
    from botocore.exceptions import ClientError

    error = ClientError({'Error': {'Code': code, 'Message': 'fixture'}}, 'PutObject')
    monkeypatch.setattr(common.Media, 'client', property(lambda self: FakeS3(error=error)))
    backend.args.media_uri = 's3://demo-media/media/coco'
    # Only a missing or expired login gets the log-in-and-rerun advice; anything else is raised as is.
    with pytest.raises(SystemExit if code == 'ExpiredToken' else ClientError, match='aws sso login|fixture'):
        run_coco(backend, coco)
    assert not backend.rows


def test_media_client_created_once_outside_the_upload_threads(tmp_path, monkeypatch):
    import boto3

    created = []

    def client(*args, **kwargs):
        created.append(1)
        return FakeS3()

    monkeypatch.setattr(boto3, 'client', client)
    paths = []
    for index in range(40):
        path = tmp_path / f'{index}.jpg'
        path.write_bytes(b'x')
        paths.append(path)
    media = common.Media(types.SimpleNamespace(data_root=tmp_path, media_uri='s3://bucket'))
    media.upload({media.remote(str(path)): str(path) for path in paths})
    assert created == [1]
