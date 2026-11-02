"""Round-two regressions: warm discovery, field identity and compact calibration."""

import os

import asyncpg
import pytest
from fridge import Calibration, Cuboid, objects_from_cuboids
from PIL import Image

from backend.routers import datasets, versions
from backend.tests import test_field_registry_postgres
from backend.tests.test_field_registry_postgres import USER, ingest
from backend.tests.test_label_cuboids import CUBOID

registry_database = test_field_registry_postgres.registry_database
postgres = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable Postgres')


def watch_scans(monkeypatch):
    scans = []
    for method in ('fetch', 'fetchrow', 'fetchval', 'execute'):
        original = getattr(asyncpg.Connection, method)

        async def watch(connection, query, *args, _original=original, **kwargs):
            if 'jsonb_each(' in query or 'jsonb_array_elements' in query:
                scans.append(query)
            return await _original(connection, query, *args, **kwargs)

        monkeypatch.setattr(asyncpg.Connection, method, watch)
    return scans


@postgres
@pytest.mark.parametrize('write', ['ingest', 'upsert', 'scalar_upsert', 'patch', 'bulk'])
def test_labelled_writes_and_sidebar_never_rescan(registry_database, monkeypatch, write):
    async def scenario(database):
        metadata = {'boxes': [CUBOID], 'semantic_mask': {'maskPath': 'mask.png'}}
        sid = (await ingest([metadata]))['ids'][0]
        await datasets.get_dataset_field_stats(1)
        before = await database.fetch(
            "SELECT field_name, unique_values, xmin::text FROM dataset_field_stats WHERE field_name LIKE '__%fields__'",
        )
        scans = watch_scans(monkeypatch)
        if write in ('ingest', 'upsert', 'scalar_upsert'):
            await ingest(
                [{'score': 1} if write == 'scalar_upsert' else metadata],
                prefix='new' if write == 'ingest' else 'sample',
                on_duplicate='error' if write == 'ingest' else 'update',
            )
        elif write == 'patch':
            await datasets.update_sample(1, sid, datasets.SampleUpdate(metadata=metadata), USER)
        else:
            await datasets.bulk_set_metadata(
                1,
                datasets.BulkSetRequest(updates=[datasets.BulkSetUpdate(sample_id=sid, metadata=metadata)]),
                USER,
            )
        after = await database.fetch(
            "SELECT field_name, unique_values, xmin::text FROM dataset_field_stats WHERE field_name LIKE '__%fields__'",
        )
        assert sorted(map(dict, before), key=str) == sorted(map(dict, after), key=str)
        await datasets.get_dataset_field_stats(1)
        assert scans == []

    registry_database(scenario)


@postgres
@pytest.mark.parametrize('write', ['ingest', 'patch'])
def test_new_cuboid_field_appends_without_sidebar_scan(registry_database, monkeypatch, write):
    async def scenario(database):
        sid = (await ingest([{'score': 1}]))['ids'][0]
        await datasets.get_dataset_field_stats(1)
        scans = watch_scans(monkeypatch)
        if write == 'ingest':
            await ingest([{'predicted_boxes': [CUBOID]}], prefix='later')
        else:
            await datasets.update_sample(1, sid, datasets.SampleUpdate(metadata={'predicted_boxes': [CUBOID]}), USER)
        fields = await datasets.get_dataset_field_stats(1)
        assert [f['type'] for f in fields if f['name'] == 'predicted_boxes'] == ['cuboids']
        assert scans == []

    registry_database(scenario)


@postgres
def test_schema_does_not_discover_missing_cuboid_cache(registry_database, monkeypatch):
    async def scenario(database):
        await ingest([{'boxes': [CUBOID]}])
        await database.execute("DELETE FROM dataset_field_stats WHERE field_name LIKE '__%fields__'")
        scans = watch_scans(monkeypatch)
        await datasets.get_dataset_schema(1)
        assert scans == []
        assert (
            await database.fetchval(
                "SELECT count(*) FROM dataset_field_stats WHERE field_name='__cuboid_fields__'",
            )
            == 0
        )

    registry_database(scenario)


@postgres
def test_single_cuboid_is_not_a_classification_and_list_is_emitted_once(registry_database):
    async def scenario(database):
        await ingest([{'single_box': CUBOID, 'boxes': [CUBOID]}])
        await database.execute("DELETE FROM dataset_field_stats WHERE field_name LIKE '__%fields__'")
        fields = await datasets.get_dataset_field_stats(1)
        assert [f['type'] for f in fields if f['name'] == 'single_box'] == ['object']
        assert [f['type'] for f in fields if f['name'] == 'boxes'] == ['cuboids']
        assert (
            await database.fetchval(
                "SELECT unique_values FROM dataset_field_stats WHERE field_name='__classification_fields__'",
            )
            == []
        )

    registry_database(scenario)


@postgres
def test_restore_keeps_cuboid_discovery_warm(registry_database, monkeypatch):
    async def scenario(database):
        await ingest([{'boxes': [CUBOID]}])
        await datasets.get_dataset_field_stats(1)
        version = await versions.create_version(1, versions.CreateVersionRequest(name='warm-cuboids'))
        await versions._restore_version(1, version['id'], False, USER, False)
        assert await database.fetchval(
            "SELECT unique_values FROM dataset_field_stats WHERE field_name='__cuboid_fields__'",
        ) == ['boxes']
        scans = watch_scans(monkeypatch)
        fields = await datasets.get_dataset_field_stats(1)
        assert [f['type'] for f in fields if f['name'] == 'boxes'] == ['cuboids']
        assert scans == []

    registry_database(scenario)


def test_object_identity_includes_field_and_canonical_link_wins(tmp_path):
    calib = Calibration([100, 0, 50, 0, 0, 100, 40, 0, 0, 0, 1, 0], [100, 80])
    box = Cuboid('Car', [0, 0, 10], [4, 2, 2], 0, id='same', cuboid_field='wrong')
    args = (7, 'frame.png', Image.new('RGB', (100, 80)), [box], calib, tmp_path)
    gt = objects_from_cuboids(*args, field='ground_truth_3d')[0]
    pred = objects_from_cuboids(*args, field='predictions_3d')[0]
    assert gt.filepath != pred.filepath
    assert objects_from_cuboids(*args, field='predictions_3d')[0].filepath == pred.filepath
    assert gt.metadata['cuboid_field'] == 'ground_truth_3d'
    assert pred.metadata['cuboid_field'] == 'predictions_3d'
    assert len(list(tmp_path.glob('*.jpg'))) == 2


@pytest.mark.parametrize('preamble', ['', 'calib_time: 09-Jan-2012 13:57:47\n# comment\n'])
def test_kitti_ignores_non_numeric_lines_and_keeps_only_needed_raw_keys(preamble):
    text = preamble + (
        'P0: 1 2 3\nP2: 100 0 50 0 0 100 40 0 0 0 1 0\n'
        'R0_rect: 1 0 0 0 1 0 0 0 1\n'
        'Tr_velo_to_cam: 0 -1 0 1 0 0 -1 2 1 0 0 3\n'
        'Tr_imu_to_velo: 1 0 0 0 0 1 0 0 0 0 1 0\n'
    )
    calibration = Calibration.from_kitti(text, [100, 80])
    assert set(calibration) == {'type', 'P', 'image_size', 'transforms', 'camera', 'kitti'}
    # Lossless for a later KITTI export (design §1.2); unused projections are dropped.
    assert calibration['kitti'] == {
        'R0_rect': [1, 0, 0, 0, 1, 0, 0, 0, 1],
        'Tr_velo_to_cam': [0, -1, 0, 1, 0, 0, -1, 2, 1, 0, 0, 3],
        'Tr_imu_to_velo': [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0],
    }
    assert calibration['P'] == [100, 0, 50, 0, 0, 100, 40, 0, 0, 0, 1, 0]
    assert calibration['transforms']['lidar->camera'] == [0, -1, 0, 1, 0, 0, -1, 2, 1, 0, 0, 3, 0, 0, 0, 1]


def test_objects_from_cuboids_requires_the_source_field(tmp_path):
    """Without `field=`, rows from a differently named cuboid field would not select their box."""
    with pytest.raises(TypeError):
        objects_from_cuboids(1, 'parent.png', Image.new('RGB', (100, 80)), [], None, tmp_path)
