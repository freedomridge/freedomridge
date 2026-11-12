"""An embeddings map's layout id (embedding_layouts, 0027). Its writers give a map a new one, once, in their own
transaction: POST /embeddings/reduce, a map re-fit's publish, and a coordinates upload without vector versions that
moved a point already on the map or added points to a whole-image map whose point count was 0; so does a delete of
every vector of the field. Placing new samples and other deletes never do (a mirror's copy: in
test_follower_layouts.py). The scatter and the dataset answer the id; a map without one is null."""

import json
import os
import struct
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pytest

from backend import bulk_jobs, dataset_deletion, map_placement
from backend.map_artifacts import LocalArtifactStore
from backend.routers import datasets, embeddings
from backend.tests import test_field_registry_postgres
from backend.tests.test_bulk_jobs import finish
from backend.tests.test_dataset_deletion import claim, step
from backend.tests.test_layout_coordinates import seed, upload
from backend.tests.test_map_placement import add_samples, placer, refitted_map, run_to_end
from backend.tests.test_map_refit import fake_fit, map_points  # noqa: F401  (fake_fit is a fixture)
from backend.tests.test_upgrade_paths import ROOT, database  # noqa: F401  (database is a fixture)

registry_database = test_field_registry_postgres.registry_database
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN') or not os.getenv('FRIDGE_TEST_POSTGRES_CONTAINER'),
    reason='Disposable PostgreSQL required',
)
USER = {'id': 1, 'role': 'admin'}


async def layout(db, field='clip', dataset_id=1):
    return await db.fetchval(
        'SELECT layout_id::text FROM embedding_layouts WHERE dataset_id = $1 AND field_name = $2',
        dataset_id,
        field,
    )


async def count_writes(db):
    """Record every write of a layout row, so a test can tell one new id from two."""
    await db.execute(
        """CREATE TABLE layout_writes (field_name text, layout_id uuid);
           CREATE FUNCTION record_layout_write() RETURNS trigger AS $$
           BEGIN
               INSERT INTO layout_writes VALUES (NEW.field_name, NEW.layout_id);
               RETURN NULL;
           END;
           $$ LANGUAGE plpgsql;
           CREATE TRIGGER record_layout_write AFTER INSERT OR UPDATE ON embedding_layouts
               FOR EACH ROW EXECUTE FUNCTION record_layout_write();""",
    )


async def writes(db):
    return await db.fetchval('SELECT count(*) FROM layout_writes')


@pytest.fixture
def fake_umap(monkeypatch):
    """A reducer whose layout moves every point on each fit."""
    fits = []

    def fit_transform(vectors):
        fits.append(len(vectors))
        return np.full((len(vectors), 2), float(len(fits)))

    reducer = SimpleNamespace(fit_transform=fit_transform)
    monkeypatch.setitem(sys.modules, 'umap', SimpleNamespace(UMAP=lambda **kwargs: reducer))
    monkeypatch.setattr(embeddings, 'require_free_space', AsyncMock())
    return fits


def test_each_reduce_is_one_new_layout_of_its_map_and_a_patch_scale_s_is_its_scale_s(registry_database, fake_umap):
    async def scenario(db):
        await seed(db)
        await seed(db, field='clip_patch_2x2_r0c0')
        await seed(db, field='clip_patch_2x2_r0c1')
        await count_writes(db)
        assert await layout(db) is None  # never laid out since the upgrade
        await embeddings.reduce_embeddings(1, embeddings.ReduceRequest(), USER)
        first = await layout(db)
        assert first is not None and await writes(db) == 1
        await embeddings.reduce_embeddings(1, embeddings.ReduceRequest(), USER)
        second = await layout(db)
        assert second not in (None, first) and await writes(db) == 2

        await embeddings.reduce_embeddings(1, embeddings.ReduceRequest(field_name='clip_patch_2x2'), USER)
        assert await layout(db, 'clip_patch_2x2') is not None and await writes(db) == 3
        assert await layout(db) == second
        assert sorted(r['field_name'] for r in await db.fetch('SELECT field_name FROM embedding_layouts')) == [
            'clip',
            'clip_patch_2x2',
        ]

    registry_database(scenario)


def test_an_upload_is_a_new_layout_when_it_moves_a_point(registry_database, monkeypatch):
    monkeypatch.setattr(embeddings, 'require_free_space', AsyncMock())

    async def scenario(db):
        await seed(db)
        await count_writes(db)
        # A first layout, uploaded in two requests: new points only, on a map with no point count yet.
        await upload(range(1, 5))
        await upload(range(5, 9))
        # Every point again, where it is.
        await upload(range(1, 9))
        assert await layout(db) is None and await writes(db) == 0

        # One point of eight moves: one new layout.
        await upload([3], shift=1)
        first = await layout(db)
        assert first is not None and await writes(db) == 1
        await upload([3], shift=1)  # where it already is
        assert await layout(db) == first and await writes(db) == 1
        await upload(range(1, 9), shift=2)  # a whole layout in one request: once
        second = await layout(db)
        assert second not in (None, first) and await writes(db) == 2

        # A projection (with the vector's version) is placement's kind of write, even when it moves a point.
        version = await db.fetchval("SELECT xmin::text FROM embeddings WHERE sample_id = 4 AND field_name = 'clip'")
        await embeddings.upload_coordinates(
            1,
            'clip',
            [embeddings.CoordinateUpdate(sample_id=4, x=99, y=99, vector_version=version)],
            USER,
        )
        assert (
            await db.fetchval("SELECT x FROM embedding_coordinates WHERE sample_id = 4 AND field_name = 'clip'") == 99
        )
        assert await layout(db) == second and await writes(db) == 2

    registry_database(scenario)


def test_an_upload_to_a_patch_tile_is_a_new_layout_of_its_scale(registry_database, monkeypatch):
    monkeypatch.setattr(embeddings, 'require_free_space', AsyncMock())

    async def scenario(db):
        await seed(db, field='clip_patch_2x2_r1c0')
        await seed(db, field='clip_patch_2x2_r1c1')
        await upload(range(1, 9), field='clip_patch_2x2_r1c0')
        await upload(range(1, 9), field='clip_patch_2x2_r1c1')
        assert await db.fetchval('SELECT count(*) FROM embedding_layouts') == 0
        await upload([1], shift=5, field='clip_patch_2x2_r1c1')
        assert await layout(db, 'clip_patch_2x2') is not None
        assert await db.fetchval('SELECT count(*) FROM embedding_layouts') == 1

    registry_database(scenario)


async def placed_before_the_upgrade(db, ids, field='clip'):
    """Points written as before layout ids: the map has none."""
    await db.execute(
        'INSERT INTO embedding_coordinates (sample_id, field_name, x, y) SELECT i, $2, i, -i FROM unnest($1::int[]) i',
        list(ids),
        field,
    )


def test_a_whole_field_delete_gives_its_map_a_new_layout_with_each_unit(registry_database, monkeypatch):
    """Dataset.delete_embedding_field: every unit that deletes vectors gives the map a new id in its own transaction,
    so a lasso drawn before (with the old id, or with none) compares stale whatever lays the map out again after, a
    projection or new points alone included. A delete by filter doesn't."""
    monkeypatch.setattr(embeddings, 'require_free_space', AsyncMock())
    monkeypatch.setattr(bulk_jobs, 'BATCH_SIZE', 3)

    async def scenario(db):
        await seed(db)
        await upload(range(1, 9))
        await upload([1], shift=1)
        old = await layout(db)
        await count_writes(db)
        await datasets.bulk_delete_embeddings(
            1,
            'clip',
            datasets.BulkTargetRequest(filters={'field': 'id', 'operator': '<', 'value': 3}),
            USER,
        )
        assert (await finish(db))['result'] == {'deleted': 2}
        assert await layout(db) == old and await writes(db) == 0  # a delete by filter
        await datasets.bulk_delete_embeddings(1, 'clip', datasets.BulkTargetRequest(filters={}), USER)
        assert (await finish(db))['result'] == {'deleted': 6}
        assert await writes(db) == 3  # each unit (3 samples) that deleted some of the 6 vectors
        emptied = await layout(db)
        assert emptied not in (None, old)
        # Embedded and laid out again by new points alone, or by projections: the id the delete gave stays new.
        await seed(db)
        await upload(range(1, 5))
        version = await db.fetchval("SELECT xmin::text FROM embeddings WHERE sample_id = 5 AND field_name = 'clip'")
        await embeddings.upload_coordinates(
            1,
            'clip',
            [embeddings.CoordinateUpdate(sample_id=5, x=5, y=5, vector_version=version)],
            USER,
        )
        assert await layout(db) == emptied

    registry_database(scenario)


def test_new_points_on_a_ranked_map_emptied_by_deleting_its_samples_are_a_new_layout(registry_database, monkeypatch):
    """A whole-image map's point count (embedding_viz_meta) answers whether it had any, in one key lookup. A map with
    no count (a patch scale, a map never ranked) isn't asked: the known gap (docs/architecture/JOBS.md)."""
    monkeypatch.setattr(embeddings, 'require_free_space', AsyncMock())

    async def scenario(db):
        await seed(db)
        await placed_before_the_upgrade(db, range(1, 9))
        await db.execute("SELECT recompute_viz_rank(1, 'clip', TRUE)")
        assert await layout(db) is None
        await db.execute('DELETE FROM samples WHERE dataset_id = 1')
        assert await db.fetchval("SELECT total_points FROM embedding_viz_meta WHERE field_name = 'clip'") == 0
        await db.execute(
            "INSERT INTO samples (id, dataset_id, filepath, media_type) SELECT i, 1, 'new-' || i, 'image' "
            'FROM generate_series(11, 18) i',
        )
        await db.execute(
            "INSERT INTO embeddings (dataset_id, sample_id, field_name, vector) SELECT 1, i, 'clip', '[1,2,3]' "
            'FROM generate_series(11, 18) i',
        )
        await upload(range(11, 15))
        refilled = await layout(db)
        assert refilled is not None  # stale for a lasso with no id, as for one with the old map's
        await upload(range(15, 19))  # more new points on the map it has now
        assert await layout(db) == refilled

    registry_database(scenario)


def test_a_refit_publish_is_one_new_layout_and_placing_new_samples_is_none(
    registry_database,
    tmp_path,
    monkeypatch,
    fake_fit,  # noqa: F811
):
    monkeypatch.setattr(map_placement, 'require_free_space', AsyncMock())
    store = LocalArtifactStore(tmp_path)

    async def scenario(db):
        await count_writes(db)
        await refitted_map(db, store, n=60)  # an old map written without a layout id, then re-fitted
        published = await layout(db)
        assert published is not None and await writes(db) == 1

        new = list(range(1001, 1041))
        await add_samples(db, new, ['A' if i % 2 else 'B' for i in new], seed=1)
        [job] = await map_placement.schedule_placements(db.pool)
        finished = await run_to_end(db, await placer(db), job)
        assert finished['state'] == 'succeeded', finished['error']
        assert set(new) <= set(await map_points(db))
        assert await layout(db) == published and await writes(db) == 1

    registry_database(scenario)


def test_deletes_and_changed_vectors_never_change_the_layout_id(registry_database, monkeypatch):
    monkeypatch.setattr(embeddings, 'require_free_space', AsyncMock())

    async def scenario(db):
        await seed(db)
        await upload(range(1, 9))
        await upload([1], shift=1)
        current = await layout(db)
        await count_writes(db)
        await db.execute("UPDATE embeddings SET vector = '[3,2,1]' WHERE sample_id = 2 AND field_name = 'clip'")
        await db.execute("DELETE FROM embeddings WHERE sample_id = 3 AND field_name = 'clip'")
        await db.execute('DELETE FROM samples WHERE id = 4')
        await db.execute("DELETE FROM embedding_coordinates WHERE sample_id = 5 AND field_name = 'clip'")
        assert await db.fetchval('SELECT count(*) FROM embedding_coordinates') == 4
        assert await layout(db) == current and await writes(db) == 0

    registry_database(scenario)


def binary_header(response):
    body = bytes(response.body)
    (length,) = struct.unpack('<I', body[:4])
    return json.loads(body[4 : 4 + length])


def test_the_scatter_and_the_dataset_answer_each_map_s_layout_id(registry_database, monkeypatch):
    monkeypatch.setattr(embeddings, 'require_free_space', AsyncMock())
    monkeypatch.setattr(datasets, 'get_optional_user', AsyncMock(return_value=None))
    monkeypatch.setattr(datasets, 'check_dataset_access', AsyncMock())

    async def scenario(db):
        await seed(db)
        await seed(db, field='clip_patch_2x2_r0c0')
        await placed_before_the_upgrade(db, range(1, 9))
        await placed_before_the_upgrade(db, range(1, 9), field='clip_patch_2x2_r0c0')
        request = embeddings.EmbeddingsRequest(field='clip', sample_pct=100)
        # Not laid out since the upgrade: null, the layout a lasso saved without an id was drawn on.
        assert (await embeddings.get_embeddings_post(1, request))['layout_id'] is None
        assert binary_header(await embeddings.get_embeddings_post(1, request, format='bin'))['layout_id'] is None
        assert (await datasets.get_dataset(1, request=None))['map_layouts'] == {}  # type: ignore[arg-type]

        await upload([1], shift=1)
        await upload([1], shift=1, field='clip_patch_2x2_r0c0')
        clip, patch = await layout(db), await layout(db, 'clip_patch_2x2')
        assert (await embeddings.get_embeddings_post(1, request))['layout_id'] == clip
        assert binary_header(await embeddings.get_embeddings_post(1, request, format='bin'))['layout_id'] == clip
        patch_request = embeddings.EmbeddingsRequest(field='clip_patch_2x2', sample_pct=100)
        assert (await embeddings.get_embeddings_post(1, patch_request))['layout_id'] == patch
        answer = await datasets.get_dataset(1, request=None)  # type: ignore[arg-type]
        assert answer['map_layouts'] == {'clip': clip, 'clip_patch_2x2': patch}
        assert (await datasets.get_dataset(2, request=None))['map_layouts'] == {}  # type: ignore[arg-type]

    registry_database(scenario)


def test_the_scatter_reads_the_points_again_when_a_new_layout_lands_meanwhile(registry_database, monkeypatch):
    monkeypatch.setattr(embeddings, 'require_free_space', AsyncMock())

    async def scenario(db):
        await seed(db)
        await upload(range(1, 9))
        read = embeddings._query_embeddings_no_filter
        reads = []

        async def query(*args):
            result = await read(*args)
            reads.append(result)
            if len(reads) == 1:  # a new layout commits after the points were read
                async with db.pool.acquire() as connection, connection.transaction():
                    await connection.execute("UPDATE embedding_coordinates SET x = x + 100 WHERE field_name = 'clip'")
                    await connection.execute("SELECT bump_map_layout(1, 'clip')")
            return result

        monkeypatch.setattr(embeddings, '_query_embeddings_no_filter', query)
        answer = await embeddings.get_embeddings_post(1, embeddings.EmbeddingsRequest(field='clip', sample_pct=100))
        assert len(reads) == 2
        assert answer['layout_id'] == await layout(db)
        assert min(point['x'] for point in answer['points']) >= 100  # the points of that layout

    registry_database(scenario)


def test_deleting_a_dataset_removes_its_layout_ids_in_bounded_units(registry_database, monkeypatch):
    async def held(db, dataset_id):
        return await db.fetchval('SELECT count(*) FROM embedding_layouts WHERE dataset_id = $1', dataset_id)

    async def scenario(db):
        monkeypatch.setattr(dataset_deletion, 'BATCH_SIZE', 7)
        for dataset_id in (1, 2):
            await db.execute("SELECT bump_map_layout($1, 'map_' || n) FROM generate_series(1, 20) n", dataset_id)
        await datasets.delete_dataset(1, USER)
        current = await claim(db)
        before = await held(db, 1)
        assert before == 20
        while current['state'] != 'succeeded':
            current = await step(db, current)
            after = await held(db, 1)
            assert 0 <= before - after <= 7  # never all of them in one unit
            before = after
        assert before == 0 and await held(db, 2) == 20
        assert not await db.fetchval('SELECT 1 FROM datasets WHERE id = 1')

    registry_database(scenario)


# A 2.9.0 database with a map: the upgrade writes no layout row and logs nothing for followers.
UPGRADE_RELEASE = 'v2.9.0'
UPGRADE_SEED = """
INSERT INTO datasets (name, followed) VALUES ('mapped', TRUE);
INSERT INTO samples (dataset_id, filepath, media_type) SELECT 1, 'mapped-' || g, 'image' FROM generate_series(1, 5) g;
INSERT INTO embeddings (dataset_id, sample_id, field_name, vector)
    SELECT 1, id, 'clip', ARRAY[id, 1]::real[]::vector FROM samples;
INSERT INTO embedding_coordinates (sample_id, field_name, x, y) SELECT id, 'clip', id, -id FROM samples;
"""


def test_the_upgrade_leaves_every_map_on_the_layout_it_has(database):  # noqa: F811
    make, psql, _, migrate = database
    old_schema_sql = subprocess.run(
        ['git', '-C', str(ROOT), 'show', f'{UPGRADE_RELEASE}:backend/db/schema.sql'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    name = make(old_schema_sql + UPGRADE_SEED)
    logged = psql(name, 'SELECT count(*) FROM dataset_changes')
    result = migrate(name)
    assert result.returncode == 0, f'apply_migrations.sh failed\n{result.stdout}\n{result.stderr}'
    assert psql(name, 'SELECT count(*) FROM embedding_layouts') == '0'
    assert psql(name, 'SELECT count(*) FROM dataset_changes') == logged
    # The first new layout after it: a row, and the map's followers are told.
    psql(name, "SELECT bump_map_layout(1, 'clip')")
    assert (
        psql(name, "SELECT count(*) FROM embedding_layouts WHERE field_name = 'clip' AND layout_id IS NOT NULL") == '1'
    )
    assert int(psql(name, 'SELECT count(*) FROM dataset_changes')) == int(logged) + 1
