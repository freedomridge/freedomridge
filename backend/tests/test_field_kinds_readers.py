"""Every reader of the page's wrapped filter reads with the record of which kind of row has which field
(field_kinds.reading), and finds a first value at once: the first `label` an image ever gets, written just before the
reader is asked about it. Each reader is asked twice: with the record as it is read now ('fresh'), and with every read
of the record giving the one from before the write ('stale'), so each statement that skips the images for `label` is
refused (require_field_kinds) and read again, the third time without the record. The answer is the right one both
ways, never a fallback to cached or unfiltered values."""

import json
import os
from typing import Any

import asyncpg
import pytest

from backend import field_kinds
from backend.routers import datasets, sampling
from backend.tests import test_field_registry_postgres as registry_helpers
from backend.tests import test_regions_spec, test_upgrade_paths
from backend.tests.test_access_rule_spec import ADMIN
from backend.tests.test_assistant_conversation_spec import count_statistics as count_dataset_statistics
from backend.tests.test_assistant_core_spec import open_door
from backend.tests.test_assistant_views_spec import result
from backend.tests.test_bulk_jobs import finish
from backend.tests.test_field_kinds import count_statistics, eq, farm, images, objects
from backend.tests.test_object_parent_column import ids
from backend.tests.test_regions_spec import ADMIN_USER

registry_database = registry_helpers.registry_database
database = test_upgrade_paths.database
regions_world = test_regions_spec.regions_world
USER = registry_helpers.USER
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)
MODES = ('fresh', 'stale')
DS = 81  # the assistant's dataset, in a database of its own (regions_world)
KITE = images(eq('label', 'kite'))


async def kite(database, monkeypatch, mode: str) -> tuple[dict[str, int], list[int]]:
    """The farm (a red and a blue image, a car and a truck), located, its statistics counted; then the first `label`
    an image ever has, `kite` on the blue one. Returns the rows and the list of the record's reads from then on."""
    rows = await farm(database)
    for name, n in (('red', 1), ('blue', 2)):
        point = {'type': 'Point', 'coordinates': [n, 1]}
        await datasets.update_sample(1, rows[name], datasets.SampleUpdate(metadata={'n': n}, location=point), user=USER)
    await count_statistics(database)
    before = await field_kinds.load(database, 1)
    await datasets.update_sample(1, rows['blue'], datasets.SampleUpdate(metadata={'label': 'kite'}), user=USER)
    assert before is not None and not before.seen('label', 'image')
    loads: list[int] = []
    real = field_kinds.load

    async def load(source: Any, dataset_id: int) -> Any:
        loads.append(dataset_id)
        return before if mode == 'stale' else await real(source, dataset_id)

    monkeypatch.setattr(field_kinds, 'load', load)
    return rows, loads


def refused_and_read_again(mode: str, loads: list[int], readings: int | None = 1) -> None:
    """In 'stale' mode each of the reader's readings read the record twice (both refused) before reading without it;
    `readings` None: several, some of which compile nothing (a unit with no filtered read reads the record once)."""
    assert loads, 'the record was never read'
    if mode == 'stale' and readings is not None:
        assert len(loads) == 2 * readings, loads
    elif mode == 'stale':
        assert len(loads) >= 2, loads
    else:
        assert len(loads) == (readings or len(loads)), loads


@pytest.mark.parametrize('mode', MODES)
def test_the_map_points_find_a_first_value_at_once(mode, monkeypatch, registry_database):
    async def scenario(database):
        rows, loads = await kite(database, monkeypatch, mode)
        answer = await datasets.get_dataset_map_points_post(1, datasets.MapPointsRequest(filters=KITE, points=True))
        assert {item['id'] for item in answer['items']} == {rows['blue']}
        refused_and_read_again(mode, loads)

    registry_database(scenario)


@pytest.mark.parametrize('mode', MODES)
def test_the_map_extent_finds_a_first_value_at_once(mode, monkeypatch, registry_database):
    async def scenario(database):
        _, loads = await kite(database, monkeypatch, mode)
        answer = await datasets.get_dataset_map_extent(1, datasets.MapExtentRequest(filters=KITE))
        assert (answer['count'], answer['w'], answer['e']) == (1, 2, 2)
        refused_and_read_again(mode, loads)

    registry_database(scenario)


@pytest.mark.parametrize('mode', MODES)
def test_the_histogram_and_its_preview_find_a_first_value_at_once(mode, monkeypatch, registry_database):
    async def scenario(database):
        _, loads = await kite(database, monkeypatch, mode)
        request = datasets.HistogramRequest(field='colour', filters=KITE)
        answer = await datasets.get_histogram_post(1, request)
        assert [(bucket['label'], bucket['count']) for bucket in answer['buckets']] == [('blue', 1)], answer
        preview = await datasets.get_histogram_preview_post(1, request)
        # The preview reads the filter only for a broad set (more than 100,000 samples): on four, it says so.
        assert preview['preview']['status'] == 'not_broad', preview
        refused_and_read_again(mode, loads, readings=None)

    registry_database(scenario)


@pytest.mark.parametrize('mode', MODES)
def test_the_histogram_preview_retries_a_refusal_of_its_guarded_statement(mode, monkeypatch, registry_database):
    """Review S5: with the preview's thresholds lowered so that four samples count as a broad set, the preview runs
    its guarded statement over the drawn rows; a refusal is read again (not reported as "failed"), and the preview
    finds the blue image only."""

    async def scenario(database):
        rows, loads = await kite(database, monkeypatch, mode)
        monkeypatch.setattr(datasets, '_LIGHTNING_FACET_MAX_ROWS', 0)
        monkeypatch.setattr(datasets, '_HISTOGRAM_PREVIEW_MIN_MATCHES', 1)
        request = datasets.HistogramRequest(field='colour', filters=KITE)
        preview = await datasets.get_histogram_preview_post(1, request)
        assert preview['preview']['status'] == 'ok', preview
        assert preview['preview']['matched'] == 1, preview
        assert [(bucket['label'], bucket['count']) for bucket in preview['buckets']] == [('blue', 1)], preview
        refused_and_read_again(mode, loads, readings=1)

    registry_database(scenario)


@pytest.mark.parametrize('mode', MODES)
def test_the_aggregate_finds_a_first_value_at_once(mode, monkeypatch, registry_database):
    async def scenario(database):
        rows, loads = await kite(database, monkeypatch, mode)
        spec = datasets.AggregationSpec(field='colour', op='count_values')
        answer = await datasets.aggregate(1, datasets.AggregateRequest(aggregations=[spec], filters=KITE))
        assert answer['results'][0]['value'] == [{'value': 'blue', 'count': 1}], answer
        count = datasets.AggregationSpec(field='label', op='count')
        answer = await datasets.aggregate(
            1,
            datasets.AggregateRequest(aggregations=[count], filters=objects(eq('label', 'kite'))),
        )
        assert answer['results'][0]['value'] == 1, answer  # the truck, whose image is the kite
        refused_and_read_again(mode, loads, readings=2)

    registry_database(scenario)


@pytest.mark.parametrize('mode', MODES)
def test_the_autocomplete_finds_a_first_value_at_once(mode, monkeypatch, registry_database):
    async def scenario(database):
        _, loads = await kite(database, monkeypatch, mode)
        answer = await datasets.autocomplete_field_post(1, 'colour', datasets.AutocompleteRequest(filters=KITE))
        assert [entry['value'] for entry in answer['values']] == ['blue'], answer
        assert answer['exhaustive'] is True
        refused_and_read_again(mode, loads)

    registry_database(scenario)


def colours(entries: Any) -> dict[str, Any]:
    (colour,) = [entry for entry in entries if entry.get('name') == 'colour']
    return colour


@pytest.mark.parametrize('mode', MODES)
def test_the_sidebar_retries_a_refusal_before_any_fallback_and_finds_a_first_value(
    mode,
    monkeypatch,
    registry_database,
):
    """The sidebar's request (lightning off: every facet computed), its facet request and its facet stream: a refused
    statement is read again before the facet's fallback to cached values could see it, so the colour facet is the
    filtered one (only blue), never the dataset's values (blue and red) marked cached."""

    async def scenario(database):
        _, loads = await kite(database, monkeypatch, mode)
        sidebar = colours(await datasets._get_dataset_field_stats_impl(1, KITE, lightning_mode=False))
        assert (sidebar['values'], sidebar.get('provenance')) == (['blue'], 'exact'), sidebar
        facet = colours(await datasets._get_dataset_field_stats_impl(1, KITE, True, ['colour']))
        assert (facet['values'], facet.get('provenance')) == (['blue'], 'exact'), facet
        stream = await datasets._stream_field_facets(1, KITE, True, ['colour'], None)
        lines = [
            json.loads(bytes(chunk) if isinstance(chunk, memoryview) else chunk) async for chunk in stream.body_iterator
        ]
        assert lines[-1] == {'done': True}
        streamed = colours(lines[:-1])
        assert (streamed['values'], streamed.get('provenance')) == (['blue'], 'exact'), streamed
        refused_and_read_again(mode, loads, readings=None)

    registry_database(scenario)


@pytest.mark.parametrize('mode', MODES)
def test_select_alls_token_finds_a_first_value_at_once(mode, monkeypatch, registry_database):
    async def scenario(database):
        rows, loads = await kite(database, monkeypatch, mode)
        made = await datasets.create_selection_token(1, datasets.SelectionTokenRequest(filters=KITE), user=USER)
        assert made['count'] == 1
        kept = await database.fetch('SELECT sample_id FROM region_selections WHERE token = $1', made['token'])
        assert [row['sample_id'] for row in kept] == [rows['blue']]  # a refused insert left no row behind
        refused_and_read_again(mode, loads)

    registry_database(scenario)


@pytest.mark.parametrize('mode', MODES)
def test_sparse_sample_finds_a_first_value_at_once(mode, monkeypatch, registry_database):
    async def scenario(database):
        monkeypatch.setattr(sampling, 'db', database)
        rows, loads = await kite(database, monkeypatch, mode)
        request = sampling.SparseSampleRequest(count=1, fields=['n'], filters=KITE)
        answer = await sampling.sparse_sample(1, request)
        assert answer['sample_ids'] == [rows['blue']], answer
        refused_and_read_again(mode, loads)

    registry_database(scenario)


@pytest.mark.parametrize('mode', MODES)
def test_a_bulk_edit_by_filter_finds_a_first_value_and_a_refused_unit_writes_nothing(
    mode,
    monkeypatch,
    registry_database,
):
    """The job's filter is compiled at submit, with the record read then; in 'stale' mode that record is the one from
    before the kite, so the job's first predicate skips the images for `label` and its unit is refused. The unit runs
    in a savepoint, so the refused attempt kept nothing, and the next attempt, with the predicate that skips nothing,
    edits exactly the kite's image."""

    async def scenario(database):
        rows, loads = await kite(database, monkeypatch, mode)
        request = datasets.BulkSetRequest(filters=KITE, metadata={'checked': True}, background=True)
        await datasets.bulk_set_metadata(1, request, user=USER)
        args = await database.fetchval("SELECT args FROM jobs WHERE kind = 'bulk_mutation'")
        # Fresh, `label` is seen on images and nothing is skipped; stale, the images are skipped for it.
        assert ('kinds_predicate' in args) == (mode == 'stale')
        done = await finish(database)
        assert done['result']['updated_count'] == 1, done['result']
        checked = await database.fetch("SELECT id FROM samples WHERE metadata ? 'checked'")
        assert [row['id'] for row in checked] == [rows['blue']]
        assert loads == [1]  # read once, when the filter was compiled (at submit); the unit's retry needs no record

    registry_database(scenario)


def test_the_retry_runs_each_attempt_in_a_savepoint_so_a_refused_write_keeps_nothing(registry_database):
    async def scenario(database):
        await farm(database)
        attempts: list[str] = []
        async with database.pool.acquire() as connection, connection.transaction():

            async def attempt(name: str) -> str:
                attempts.append(name)
                await connection.execute(
                    'UPDATE samples SET metadata = metadata || jsonb_build_object($1::text, 1)',
                    name,
                )
                if name == 'refused':
                    await connection.execute("DO $$ BEGIN RAISE EXCEPTION 'changed' USING ERRCODE = '40001'; END $$")
                return name

            assert await field_kinds.retrying(['refused', 'kept'], attempt, savepoint=connection) == 'kept'
        assert attempts == ['refused', 'kept']
        assert await database.fetchval("SELECT count(*) FROM samples WHERE metadata ? 'refused'") == 0
        assert await database.fetchval("SELECT count(*) FROM samples WHERE metadata ? 'kept'") == 4

        async def refuse(name: str) -> None:
            await database.execute("DO $$ BEGIN RAISE EXCEPTION 'changed' USING ERRCODE = '40001'; END $$")

        with pytest.raises(asyncpg.SerializationError):  # the last attempt's refusal is the answer
            await field_kinds.retrying(['first', 'last'], refuse)

    registry_database(scenario)


@pytest.mark.parametrize('mode', MODES)
def test_the_assistants_tools_find_a_first_value_at_once(mode, monkeypatch, regions_world):
    """The assistant's tools read through the API's routes, in process (backend/assistant/inner.py): make_view counts
    through the samples query and field_values through the aggregate, both with the record."""
    if not os.getenv('FRIDGE_TEST_POSTGRES_CONTAINER'):
        pytest.skip('Requires disposable PostgreSQL')

    async def scenario(r):
        await r.dataset(DS)
        await add_as(DS, [(None, {'colour': 'red'}), (None, {'colour': 'blue'})], 'tool/img')
        red, blue = await ids(r.database, DS, 'tool/img')
        await add_as(
            DS,
            [('object', {'parent_id': red, 'label': 'car'}), ('object', {'parent_id': blue, 'label': 'truck'})],
            'tool/obj',
        )
        await count_dataset_statistics(r, DS)
        before = await field_kinds.load(r.database, DS)
        await datasets.update_sample(DS, blue, datasets.SampleUpdate(metadata={'label': 'kite'}), user=ADMIN_USER)
        loads: list[int] = []
        real = field_kinds.load

        async def load(source: Any, dataset_id: int) -> Any:
            loads.append(dataset_id)
            return before if mode == 'stale' else await real(source, dataset_id)

        monkeypatch.setattr(field_kinds, 'load', load)
        await open_door(r)
        view = await result(ADMIN, 'make_view', {'dataset': DS, 'filter': 'label = "kite"'})
        assert view['count'] == 1, view
        values = await result(ADMIN, 'field_values', {'view': view['view'], 'field': 'colour'})
        assert values['values'] == [{'value': 'blue', 'count': 1}], values
        # A value list with more values than it keeps is counted again over its filter when downloaded.
        await r.database.execute(
            """INSERT INTO assistant_lists (handle, user_id, dataset_id, field_name, value_kind, distinct_count, source)
               VALUES ('vl_kinds', $1, $2, 'colour', 'text', 5000, $3)""",
            ADMIN,
            DS,
            {'filters': images(eq('label', 'kite'))},
        )
        csv = await r.call(ADMIN, 'GET', '/api/assistant/lists/vl_kinds/csv')
        assert csv.status_code == 200, csv.text
        assert csv.text.split('\r\n')[1:] == ['"blue",1', ''], csv.text
        refused_and_read_again(mode, loads, readings=None)

    regions_world(scenario)


async def add_as(dataset_id: int, samples: list, prefix: str) -> None:
    """add_samples as the admin of regions_world: an object's filepath and media_type, and its metadata."""
    await datasets.add_samples(
        dataset_id,
        {
            'samples': [
                {'filepath': f'{prefix}-{index}.jpg', **({'media_type': kind} if kind else {}), 'metadata': metadata}
                for index, (kind, metadata) in enumerate(samples)
            ],
        },
        user=ADMIN_USER,
    )
