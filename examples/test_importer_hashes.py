"""Importer/backfill parity on encoded files, including EXIF and lossy derivatives."""

from types import SimpleNamespace
from unittest.mock import Mock

import imagehash
import numpy as np
import pytest
from fridge.core.dataset import Dataset
from fridge.core.sample import Sample
from PIL import Image, ImageOps

from examples import import_demo_data as runner
from examples import test_demo_import_regressions as object_fixtures
from examples._shared import importer
from examples.eurosat import ingest as eurosat


@pytest.fixture
def backend(monkeypatch, tmp_path):
    import fridge

    client = Mock()
    dataset = Mock(_id=1)
    dataset.lookup_samples.return_value = {}
    dataset.get_schema.return_value = []
    dataset.add_samples.side_effect = lambda samples, **kwargs: {'ids': list(range(1, len(samples) + 1))}
    monkeypatch.setattr(fridge, 'connect', Mock())
    monkeypatch.setattr(fridge, 'get_client', lambda: client)
    monkeypatch.setattr(fridge, 'Dataset', lambda *args, **kwargs: dataset)
    monkeypatch.setattr(importer, 'check_media', Mock())
    args = importer.parser('fixture', 'fixture').parse_args(['--data-root', str(tmp_path), '--no-reduce'])
    args.password = 'fixture'
    return SimpleNamespace(args=args, dataset=dataset, client=client)


def run_import(backend, samples, **kwargs):
    return importer.ingest(
        backend.args,
        samples,
        {},
        'hash fixture',
        encoder_factory=lambda args: lambda images: [[1.0, 2.0] for image in images],
        **kwargs,
    )


def backfill(path):
    ds = Dataset.__new__(Dataset)
    ds._id = 1
    ds.client = Mock()
    ds.client.export_hashes.return_value = {'items': []}
    ds.iter_samples = Mock(return_value=iter([Sample.from_row({'id': 1, 'filepath': str(path)})]))
    result = ds.compute_hashes(image_loader=Image.open)
    assert result == dict(computed=1, skipped=0, objects_skipped=0, errors=0)
    return {item['field_name']: item['hash_hex'] for item in ds.client.upload_hashes.call_args.args[1]}


def patterned_image():
    pixels = np.random.default_rng(17).integers(0, 256, (47, 63, 3), dtype=np.uint8)
    return Image.fromarray(pixels)


@pytest.mark.parametrize(
    'mode,extension,orientation',
    [
        ('RGB', 'jpg', None),
        ('RGB', 'jpg', 6),
        ('L', 'png', None),
        ('RGBA', 'png', None),
        ('P', 'png', None),
        ('CMYK', 'jpg', None),
        ('RGB', 'tiff', None),
    ],
)
def test_importer_matches_compute_hashes_on_encoded_images(
    backend,
    tmp_path,
    monkeypatch,
    mode,
    extension,
    orientation,
):
    path = tmp_path / f'fixture.{extension}'
    with patterned_image() as source, source.convert(mode) as converted:
        options = {}
        if orientation:
            exif = converted.getexif()
            exif[274] = orientation
            options['exif'] = exif
        converted.save(path, **options)
    sample = Sample(str(path))
    with monkeypatch.context() as context:
        opened = Mock(wraps=Image.open)
        context.setattr(Image, 'open', opened)
        assert run_import(backend, [sample])['written'] == 1
        assert opened.call_count == 1  # Hashes reuse CLIP's decoded RGB image.
    assert sample.hashes == backfill(path)
    with Image.open(path) as source, source.convert('RGB') as rgb:
        expected = {
            'dhash_8': str(imagehash.dhash(rgb, hash_size=8)),
            'phash_8': str(imagehash.phash(rgb, hash_size=8)),
        }
        assert sample.hashes == expected
        if orientation:
            with ImageOps.exif_transpose(source) as upright:
                assert str(imagehash.dhash(upright)) != sample.hashes['dhash_8']
    assert all(len(value) == 16 and value == value.lower() for value in sample.hashes.values())
    assert sample.to_dict()['hashes'] == sample.hashes


def test_no_hashes_and_resume_do_not_compute(backend, tmp_path, monkeypatch):
    path = tmp_path / 'fixture.jpg'
    with patterned_image() as image:
        image.save(path)
    sample = Sample(str(path))
    backend.args.no_hashes = True
    monkeypatch.setattr(
        'fridge.core.hashes.resolve_hash_algorithms',
        Mock(side_effect=AssertionError('hashing disabled')),
    )
    run_import(backend, [sample])
    assert not sample.hashes
    backend.args.no_hashes = False
    backend.dataset.lookup_samples.return_value = {str(path): 1}
    monkeypatch.setattr(Image, 'open', Mock(side_effect=AssertionError('resume opened image')))
    assert run_import(backend, [sample])['duplicate_skipped'] == 1
    assert not sample.hashes


@pytest.mark.parametrize('disabled', [False, True])
def test_object_import_hashes_only_parents_and_resumes(monkeypatch, tmp_path, disabled):
    backend = getattr(object_fixtures.backend, '__wrapped__')(monkeypatch, tmp_path)
    coco = getattr(object_fixtures.coco, '__wrapped__')(tmp_path)
    backend.args.no_hashes = disabled
    object_fixtures.run_coco(backend, coco)
    for row in backend.rows.values():
        if row['media_type'] == 'image' and not disabled:
            assert row['hashes'] == backfill(row['filepath'])
        else:
            assert not row['hashes']
    monkeypatch.setattr('fridge.core.hashes.resolve_hash_algorithms', Mock(side_effect=AssertionError('resume hashed')))
    assert object_fixtures.run_coco(backend, coco)['images_written'] == 0


def test_rotated_open_images_hashes_match_saved_jpeg(monkeypatch, tmp_path):
    root = getattr(object_fixtures.oi, '__wrapped__')(tmp_path)
    with patterned_image() as image:
        image.save(root / 'validation/a.jpg')
    opened = Mock(wraps=Image.open)
    with monkeypatch.context() as context:
        context.setattr(Image, 'open', opened)
        sample, image = object_fixtures.mapped_oi(root)
        try:
            importer.attach_hashes(SimpleNamespace(), [sample], [image])
            with Image.open(sample.filepath) as saved:
                assert image.tobytes() == saved.convert('RGB').tobytes()
        finally:
            image.close()
    # Only the explicit verification above opens the stored filepath.
    assert sum(str(call.args[0]) == sample.filepath for call in opened.call_args_list) == 1
    assert sample.hashes == backfill(sample.filepath)


def test_eurosat_hashes_rendered_jpeg(backend, tmp_path):
    import rasterio
    from rasterio.transform import from_origin

    directory = tmp_path / 'tif/Forest'
    directory.mkdir(parents=True)
    source_path = directory / 'tile.tif'
    with rasterio.open(
        source_path,
        'w',
        driver='GTiff',
        width=32,
        height=32,
        count=13,
        dtype='uint16',
        crs='EPSG:4326',
        transform=from_origin(8, 49, 0.01, 0.01),
    ) as source:
        source.write(np.random.default_rng(7).integers(0, 3000, (13, 32, 32), dtype=np.uint16))
    sample = next(eurosat.iter_samples(backend.args))

    def prepare(sample):
        from pathlib import Path

        Path(sample.filepath).parent.mkdir(parents=True)
        with rasterio.open(sample.metadata['source_tif']) as source:
            eurosat.render_rgb(source, sample.filepath)

    run_import(backend, [sample], prepare=prepare)
    assert sample.hashes == backfill(sample.filepath)


@pytest.mark.parametrize('flags', [[], ['--no-hashes'], ['--hash-algorithms', 'phash_16', 'dhash_8']])
def test_runner_forwards_hash_options_to_every_importer(tmp_path, flags):
    args = runner.build_parser().parse_args(['--all', '--data-root', str(tmp_path), *flags])
    _, commands = runner.commands(args, {'FRIDGE_API_TOKEN': 'fixture'})
    assert len(commands) == 9
    for name, command in commands:
        assert ('--no-hashes' in command) == args.no_hashes
        start = command.index('--hash-algorithms') + 1
        assert command[start : start + len(args.hash_algorithms)] == args.hash_algorithms


@pytest.mark.parametrize('name', ['dhash8', 'md5_8', 'whash_6'])
def test_unknown_hash_name_is_rejected_while_parsing(name, capsys):
    import argparse

    from examples._shared.importer import hash_arguments

    parser = argparse.ArgumentParser()
    hash_arguments(parser)
    with pytest.raises(SystemExit):
        parser.parse_args(['--hash-algorithms', name])
    error = capsys.readouterr().err
    assert 'Invalid hash algorithm' in error or 'power of two' in error


def test_plain_ingest_records_and_looks_up_s3_paths(backend, tmp_path, monkeypatch):
    s3 = object_fixtures.FakeS3()
    monkeypatch.setattr(importer.Media, 'client', property(lambda self: s3))
    backend.args.media_uri = 's3://demo-media/media/eurosat'
    path = tmp_path / 'AnnualCrop' / 'one.jpg'
    path.parent.mkdir()
    patterned_image().save(path)
    run_import(backend, [Sample(str(path))])
    [sent] = backend.dataset.add_samples.call_args.args[0]
    assert sent.filepath == 's3://demo-media/media/eurosat/AnnualCrop/one.jpg'
    assert s3.objects == {('demo-media', 'media/eurosat/AnnualCrop/one.jpg'): (path.read_bytes(), 'image/jpeg')}
    # Duplicates are looked up by the recorded S3 path, so a rerun skips without decoding or uploading.
    assert backend.dataset.lookup_samples.call_args.args == ('filepath', [sent.filepath])
    backend.dataset.lookup_samples.return_value = {sent.filepath: 1}
    s3.objects.clear()
    counts = run_import(backend, [Sample(str(path))])
    assert counts['duplicate_skipped'] == 1 and not s3.objects
