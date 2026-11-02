"""Arbitrary box/polyline fields share discovery, filtering and statistics contracts."""

# ruff: noqa: F811
# flake8: noqa: F811
import os

import asyncpg
import pytest

from backend.routers import datasets
from backend.tests.statistics_helpers import run_refresh
from backend.tests.test_field_registry_postgres import USER, ingest, registry_database  # noqa: F401

pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')
BOX = [{'label': 'car', 'bbox': [0.1, 0.1, 0.2, 0.2], 'confidence': 0.9}]
LANES = [{'label': 'lane', 'paths': [[[0, 0], [0.2, 0.2]], [[0.6, 0.6], [1, 1]]], 'closed': False}]


def test_discovery_and_statistics_use_shape_not_name(registry_database):
    async def scenario(database):
        await ingest([{'vehicles': [], 'lanes': [], 'pedestrians': None}])
        await datasets._get_dataset_field_stats_impl(1, None)  # cache the empty discovery
        await ingest(
            [
                {
                    'vehicles': BOX,
                    'pedestrians': BOX,
                    'lanes': LANES,
                    'poses': [{'_type': 'keypoints', 'points': [[0, 0]]}],
                    'scalar': 1,
                },
            ],
            prefix='later',
        )
        for refresh in [False, True]:
            if refresh:
                await database.execute('UPDATE datasets SET stats_need_refresh = TRUE, stats_full_refreshed_at = NULL')
                await run_refresh(database, 1)
            fields = {f['name']: f for f in await datasets._get_dataset_field_stats_impl(1, None)}
            for name, kind, label in [
                ('vehicles', 'detections', 'car'),
                ('pedestrians', 'detections', 'car'),
                ('lanes', 'polygons', 'lane'),
            ]:
                assert fields[name]['type'] == kind
                assert fields[name + '.label']['values'] == [label]
            assert fields['poses']['type'] == 'keypoints'
            for cache, expected in [
                ('__detection_fields__', ['pedestrians', 'vehicles']),
                ('__polyline_fields__', ['lanes']),
            ]:
                assert (
                    await database.fetchval(
                        'SELECT unique_values FROM dataset_field_stats WHERE dataset_id=1 AND field_name=$1',
                        cache,
                    )
                    == expected
                )

    registry_database(scenario)


def test_custom_labels_filter_autocomplete_and_aggregate(registry_database):
    async def scenario(database):
        await ingest([{'vehicles': BOX, 'lanes': LANES}, {'vehicles': [], 'lanes': []}, {}])
        await datasets._get_dataset_field_stats_impl(1, None)
        for field in ['vehicles.label', 'lanes.label', 'vehicles.confidence']:
            val = 'lane' if field.startswith('lanes') else (0.5 if field.endswith('confidence') else 'car')
            result = await datasets.list_samples_post(
                1,
                datasets.SamplesRequest(
                    filters={'field': field, 'operator': '>' if field.endswith('confidence') else '=', 'value': val},
                ),
            )
            assert result['total'] == 1
        negative = await datasets.list_samples_post(
            1,
            datasets.SamplesRequest(filters={'field': 'vehicles.label', 'operator': 'not in', 'value': ['car']}),
        )
        assert negative['total'] == 2
        assert 'car' in str(await datasets._autocomplete_impl(1, 'vehicles.label', 'ca', 100))
        result = await datasets.aggregate(
            1,
            datasets.AggregateRequest(
                aggregations=[datasets.AggregationSpec(field='vehicles.label', op='count_values')],
            ),
        )
        assert result['results'][0]['value'] == [{'value': 'car', 'count': 1}]

    registry_database(scenario)


def test_discovery_survives_patch_rename_and_clear(registry_database):
    async def scenario(database):
        sample_id = (await ingest([{'vehicles': [], 'lanes': []}]))['ids'][0]
        await datasets._get_dataset_field_stats_impl(1, None)
        await datasets.update_sample(
            1,
            sample_id,
            datasets.SampleUpdate(metadata={'vehicles': BOX, 'lanes': LANES}),
            USER,
        )
        fields = {f['name']: f for f in await datasets._get_dataset_field_stats_impl(1, None)}
        assert fields['vehicles']['type'] == 'detections'
        assert fields['lanes']['type'] == 'polygons'
        await datasets.rename_field(1, 'vehicles', datasets.FieldRenameRequest(new_name='traffic'), USER)
        fields = {f['name']: f for f in await datasets._get_dataset_field_stats_impl(1, None)}
        assert 'vehicles.label' not in fields
        assert fields['traffic']['type'] == 'detections'
        assert fields['traffic.label']['values'] == ['car']
        await datasets.clear_field(1, 'traffic', USER)
        assert not any(f['name'].startswith('traffic') for f in await datasets._get_dataset_field_stats_impl(1, None))

    registry_database(scenario)


def _watch_scans(monkeypatch, database):
    scans = []
    for method in ('fetch', 'fetchrow', 'fetchval', 'execute'):
        original = getattr(asyncpg.Connection, method)

        async def watch(connection, query, *args, _original=original, **kwargs):
            if 'jsonb_each(' in query or 'jsonb_array_elements' in query:
                scans.append(query)
            return await _original(connection, query, *args, **kwargs)

        monkeypatch.setattr(asyncpg.Connection, method, watch)
    return scans


@pytest.mark.parametrize('write', ['ingest', 'upsert', 'patch', 'bulk'])
def test_writes_keep_discovery_caches_without_scanning(registry_database, monkeypatch, write):
    async def scenario(database):
        sid = (
            await ingest(
                [
                    {
                        'detections': BOX,
                        'lanes': LANES,
                        'heat': {'type': 'heatmap'},
                        'mask': {'maskPath': 'm.png'},
                        'class': {'label': 'ok'},
                    },
                ],
            )
        )['ids'][0]
        await datasets._get_dataset_field_stats_impl(1, None)
        before = await database.fetch(
            "SELECT field_name, xmin::text AS token FROM dataset_field_stats WHERE field_name LIKE '\\_\\_%'",
        )
        assert len(before) == 7
        scans = _watch_scans(monkeypatch, database)
        if write == 'ingest':
            await ingest([{'detections': BOX}], prefix='later')
        elif write == 'upsert':
            await ingest([{'detections': BOX}], on_duplicate='update')
        elif write == 'patch':
            await datasets.update_sample(1, sid, datasets.SampleUpdate(metadata={'detections': BOX}), USER)
        else:
            await datasets.bulk_set_metadata(
                1,
                datasets.BulkSetRequest(updates=[datasets.BulkSetUpdate(sample_id=sid, metadata={'detections': BOX})]),
                USER,
            )
        after = await database.fetch(
            "SELECT field_name, xmin::text AS token FROM dataset_field_stats WHERE field_name LIKE '\\_\\_%'",
        )
        assert {tuple(row) for row in after} == {tuple(row) for row in before}
        await datasets._get_dataset_field_stats_impl(1, None)
        assert scans == []

    registry_database(scenario)


def test_new_field_appends_only_its_cache_without_scanning(registry_database, monkeypatch):
    async def scenario(database):
        await ingest([{'detections': BOX}])
        await datasets._get_dataset_field_stats_impl(1, None)
        before = {
            r['field_name']: r['token']
            for r in await database.fetch(
                "SELECT field_name, xmin::text AS token FROM dataset_field_stats WHERE field_name LIKE '\\_\\_%'",
            )
        }
        scans = _watch_scans(monkeypatch, database)
        await ingest([{'traffic': BOX}], prefix='new')
        fields = {f['name']: f for f in await datasets._get_dataset_field_stats_impl(1, None)}
        assert fields['traffic']['type'] == 'detections'
        assert fields['traffic.label']['values'] == ['car']
        after = {
            r['field_name']: r['token']
            for r in await database.fetch(
                "SELECT field_name, xmin::text AS token FROM dataset_field_stats WHERE field_name LIKE '\\_\\_%'",
            )
        }
        assert before.keys() == after.keys()
        assert {name for name in before if before[name] != after[name]} == {'__detection_fields__'}
        assert scans == []

    registry_database(scenario)


def test_plain_lists_never_get_label_stats_or_rescans(registry_database, monkeypatch):
    async def scenario(database):
        sid = (
            await ingest([{'keywords': ['a'], 'track': [[1, 2]], 'objects': [{'label': 'class'}], 'vehicles': BOX}])
        )['ids'][0]
        await run_refresh(database, 1)
        labels = await database.fetch("SELECT field_name FROM dataset_field_stats WHERE field_name LIKE '%.label'")
        assert {row['field_name'] for row in labels} == {'objects.label', 'vehicles.label'}
        await datasets._get_dataset_field_stats_impl(1, None)
        # Old deployments may have these rows: writes must not dirty or scan them.
        await database.execute("""INSERT INTO dataset_field_stats
            (dataset_id,field_name,field_type,unique_values,values_complete) VALUES
            (1,'keywords.label','enum','{}',TRUE),(1,'track.label','enum','{}',TRUE)""")
        scans = _watch_scans(monkeypatch, database)
        await datasets.update_sample(
            1,
            sid,
            datasets.SampleUpdate(metadata={'keywords': ['b'], 'track': [[3, 4]]}),
            USER,
        )
        assert await database.fetchval(
            "SELECT bool_and(values_complete) FROM dataset_field_stats "
            "WHERE field_name IN ('keywords.label','track.label')",
        )
        await datasets._get_dataset_field_stats_impl(1, None)
        assert scans == []

    registry_database(scenario)


def test_dotted_metadata_keys_keep_scalar_semantics_and_statistics(registry_database):
    async def scenario(database):
        await ingest(
            [
                {'gps.lat': 41.5, 'cam.label': 'front', 'vehicles.label': 'scalar', 'vehicles': BOX},
                {'gps.lat': 10.0, 'cam.label': 'rear', 'vehicles.label': 'other'},
            ],
        )
        await run_refresh(database, 1)
        for field, value, op in [('gps.lat', 20, '>'), ('cam.label', 'front', '='), ('vehicles.label', 'scalar', '=')]:
            result = await datasets.list_samples_post(
                1,
                datasets.SamplesRequest(filters={'field': field, 'operator': op, 'value': value}),
            )
            assert result['total'] == 1
            result = await datasets.aggregate(
                1,
                datasets.AggregateRequest(aggregations=[datasets.AggregationSpec(field=field, op='count_values')]),
            )
            assert len(result['results'][0]['value']) == 2
        assert 'front' in str(await datasets._autocomplete_impl(1, 'cam.label', 'fr', 100))
        await datasets.clear_field(1, 'vehicles', USER)
        for response in (await datasets.get_dataset_schema(1), await datasets._get_dataset_field_stats_impl(1, None)):
            fields = {f['name']: f for f in response}
            assert fields['cam.label']['values'] == ['front', 'rear']
            assert fields['vehicles.label']['values'] == ['other', 'scalar']
            assert fields['vehicles.label']['origin'] == 'metadata'

    registry_database(scenario)


@pytest.mark.parametrize('refresh', [False, True])
def test_discovery_precedence_matches_frontend(registry_database, refresh):
    async def scenario(database):
        await ingest(
            [
                {
                    'keywords': ['bbox', 'label'],
                    'nullbox': [{'bbox': None, 'label': 'class'}],
                    'numberbox': [{'bbox': 3, 'label': 'class'}],
                    'posebox': [{'_type': 'keypoints', 'bbox': {}, 'points': []}],
                    'poses': [{'_type': 'keypoints', 'points': []}],
                    'cuboids': [{'_type': 'cuboid', 'bbox': [], 'points': [], 'label': '3d'}],
                    'heat': {'type': 'heatmap', 'maskPath': 'm', 'label': 'hot'},
                    'lanes': LANES,
                },
            ],
        )
        if refresh:
            await run_refresh(database, 1)
        fields = {f['name']: f for f in await datasets._get_dataset_field_stats_impl(1, None)}
        for name, kind in [
            ('keywords', 'list'),
            ('nullbox', 'classification'),
            ('numberbox', 'classification'),
            ('posebox', 'detections'),
            ('poses', 'keypoints'),
            ('cuboids', 'cuboids'),
            ('heat', 'heatmap'),
            ('lanes', 'polygons'),
        ]:
            assert fields[name]['type'] == kind, name
        caches = {
            r['field_name']: r['unique_values']
            for r in await database.fetch(
                "SELECT field_name,unique_values FROM dataset_field_stats WHERE field_name LIKE '\\_\\_%'",
            )
        }
        assert caches['__detection_fields__'] == ['posebox']
        assert caches['__keypoint_fields__'] == ['poses']
        assert caches['__polyline_fields__'] == ['lanes']

    registry_database(scenario)


def test_failed_vector_scan_does_not_fail_field_stats(registry_database, monkeypatch):
    async def scenario(database):
        await ingest([{'vehicles': BOX}])
        original = asyncpg.Connection.fetch

        async def fail_discovery(connection, query, *args, **kwargs):
            if 'jsonb_each(' in query and 'bbox' in query:
                raise RuntimeError('discovery unavailable')
            return await original(connection, query, *args, **kwargs)

        monkeypatch.setattr(asyncpg.Connection, 'fetch', fail_discovery)
        assert await datasets._get_dataset_field_stats_impl(1, None)

    registry_database(scenario)


def test_vector_schema_keeps_registry_metadata(registry_database):
    async def scenario(database):
        await ingest([{'detections': BOX, 'polygons': LANES, 'vehicles': BOX}])
        await run_refresh(database, 1)
        fields = {f['name']: f for f in await datasets._get_dataset_field_stats_impl(1, None)}
        schema = {f['name']: f for f in await datasets.get_dataset_schema(1)}
        for name in ['detections', 'polygons', 'vehicles']:
            assert fields[name]['origin'] == 'metadata'
            assert fields[name]['declared_type'] == 'list'
            assert fields[name]['sample_count'] == 1
            assert fields[name]['type'] == schema[name]['type']
            assert fields[name]['is_promoted'] is False
        await datasets.set_field_type(1, 'vehicles', datasets.SetFieldTypeRequest(field_type='datetime'), USER)
        for response in (await datasets.get_dataset_schema(1), await datasets._get_dataset_field_stats_impl(1, None)):
            vector = next(field for field in response if field['name'] == 'vehicles')
            assert vector['type'] == 'detections'
            assert vector['declared_type'] == 'datetime'

    registry_database(scenario)


def test_custom_label_equality_uses_containment(registry_database):
    async def scenario(database):
        await ingest([{'vehicles': BOX, 'lanes': LANES}])
        await datasets._get_dataset_field_stats_impl(1, None)
        context = await datasets.get_dataset_array_columns(1)
        for field in ['vehicles.label', 'lanes.label']:
            for op in ['=', '!=', 'in', 'not in']:
                args: list = []
                sql, _ = datasets.parse_filter_node(
                    {'field': field, 'operator': op, 'value': ['car'] if 'in' in op else 'car'},
                    args,
                    1,
                    array_columns=context,
                )
                assert '@>' in sql and 'jsonb_array_elements' not in sql

    registry_database(scenario)


@pytest.mark.parametrize('name', ['scene-type', '123scene'])
def test_autocomplete_preserves_legacy_names(registry_database, name):
    async def scenario(database):
        await ingest([{name: 'car'}, {name: 'bus'}])
        assert 'car' in str(await datasets._autocomplete_impl(1, name, 'ca', 100))

    registry_database(scenario)


def test_discovery_counts_samples_once_for_multiple_fields(registry_database):
    async def scenario(database):
        await ingest([{'vehicles': BOX, 'detections': BOX, 'lanes': LANES}, {'vehicles': BOX}, {'vehicles': None}])
        await run_refresh(database, 1)
        counts = {
            r['field_name']: r['sample_count']
            for r in await database.fetch('SELECT field_name,sample_count FROM dataset_field_stats WHERE dataset_id=1')
        }
        assert counts['__detection_fields__'] == 2
        assert counts['__polyline_fields__'] == 1
        assert counts['vehicles'] == 2
        assert counts['vehicles.label'] == 2

    registry_database(scenario)


def test_statistics_removes_obsolete_plain_list_label_rows(registry_database):
    async def scenario(database):
        await ingest([{'keywords': ['bbox'], 'ground_truth': ['x'], 'predictions': ['y']}])
        await database.execute("""INSERT INTO dataset_field_stats
            (dataset_id,field_name,field_type,unique_values,values_complete) VALUES
            (1,'keywords.label','enum','{}',TRUE), (1,'ground_truth.label','enum','{}',TRUE),
            (1,'predictions.label','enum','{}',TRUE)""")
        fields = await datasets._get_dataset_field_stats_impl(1, None)
        assert not any(field['name'].endswith('.label') for field in fields)
        await run_refresh(database, 1)
        assert not await database.fetchval(
            "SELECT EXISTS(SELECT 1 FROM dataset_field_stats WHERE field_name LIKE '%.label')",
        )

    registry_database(scenario)


def test_delete_and_restore_invalidation_keep_discovery(registry_database):
    from backend.routers import versions

    async def scenario(database):
        ids = (await ingest([{'vehicles': BOX, 'lanes': LANES}, {'vehicles': BOX}]))['ids']
        await datasets._get_dataset_field_stats_impl(1, None)
        query = (
            "SELECT field_name,unique_values,xmin::text FROM dataset_field_stats "
            "WHERE field_name LIKE '\\_\\_%' ORDER BY 1"
        )
        before = await database.fetch(query)
        await datasets.delete_sample(1, ids[0], USER)
        assert await database.fetch(query) == before
        async with database.pool.acquire() as conn, conn.transaction():
            await versions._invalidate_restored_statistics(conn, 1)
        assert await database.fetch(query) == before

    registry_database(scenario)


@pytest.mark.parametrize('operation', ['set', 'delete', 'rename', 'clone'])
def test_background_bulk_preserves_discovery(registry_database, operation):
    from backend.tests.test_bulk_jobs import finish

    async def scenario(database):
        ids = (await ingest([{'vehicles': BOX}, {'vehicles': BOX}]))['ids']
        await datasets._get_dataset_field_stats_impl(1, None)
        if operation == 'set':
            await datasets.bulk_set_metadata(
                1,
                datasets.BulkSetRequest(sample_ids=ids, metadata={'traffic': BOX}, background=True),
                USER,
            )
        elif operation == 'delete':
            await datasets.bulk_delete_samples(1, datasets.BulkTargetRequest(sample_ids=[ids[0]]), USER)
        else:
            endpoint = datasets.rename_field if operation == 'rename' else datasets.clone_field
            await endpoint(1, 'vehicles', datasets.FieldRenameRequest(new_name='traffic', background=True), USER)
        await finish(database)
        caches = {
            r['field_name']: r['unique_values']
            for r in await database.fetch(
                "SELECT field_name,unique_values FROM dataset_field_stats WHERE field_name LIKE '\\_\\_%'",
            )
        }
        assert len(caches) == 7
        assert caches['__detection_fields__'] == (['vehicles'] if operation == 'delete' else ['traffic', 'vehicles'])

    registry_database(scenario)


def test_ingest_and_bulk_append_before_locking_value_rows(registry_database, monkeypatch):
    import asyncio

    from backend import sample_ingest
    from backend.field_registry import lock_value_rows
    from backend.tests.test_bulk_jobs import finish

    async def scenario(database):
        sid = (await ingest([{'score': 1, 'vehicles': BOX}]))['ids'][0]
        await datasets._get_dataset_field_stats_impl(1, None)
        appended, release = asyncio.Event(), asyncio.Event()
        original = lock_value_rows

        async def pause_after_append(*args):
            appended.set()
            await release.wait()
            await original(*args)

        await datasets.bulk_set_metadata(
            1,
            datasets.BulkSetRequest(sample_ids=[sid], metadata={'score': 3, 'bulk_boxes': BOX}, background=True),
            USER,
        )
        monkeypatch.setattr(sample_ingest, 'lock_value_rows', pause_after_append)
        inserting = asyncio.create_task(ingest([{'score': 2, 'incoming': BOX}], prefix='concurrent'))
        editing = None
        try:
            await asyncio.wait_for(appended.wait(), 3)
            editing = asyncio.create_task(finish(database))
            await asyncio.sleep(0.1)
            release.set()
            await asyncio.wait_for(asyncio.gather(inserting, editing), 5)
            assert await database.fetchval(
                "SELECT unique_values FROM dataset_field_stats WHERE field_name='__detection_fields__'",
            ) == ['bulk_boxes', 'incoming', 'vehicles']
        finally:
            release.set()
            await asyncio.gather(inserting, *([editing] if editing else []), return_exceptions=True)

    registry_database(scenario)


@pytest.mark.parametrize('name', ['ground_truth', 'predictions', 'classes'])
@pytest.mark.parametrize('refresh', [False, True])
def test_classification_lists_keep_label_paths(registry_database, name, refresh):
    async def scenario(database):
        await ingest([{name: [{'label': 'dog'}]}, {name: [{'label': 'dog'}, {'label': 'cat'}]}, {}])
        if refresh:
            await run_refresh(database, 1, force=True)
        fields = {f['name']: f for f in await datasets._get_dataset_field_stats_impl(1, None)}
        assert fields[name]['type'] == 'classification'
        assert fields[name + '.label']['values'] == ['cat', 'dog']
        for op, value, expected in [('=', 'dog', 2), ('not in', ['dog'], 1)]:
            result = await datasets.list_samples_post(
                1,
                datasets.SamplesRequest(filters={'field': name + '.label', 'operator': op, 'value': value}),
            )
            assert result['total'] == expected
        result = await datasets.aggregate(
            1,
            datasets.AggregateRequest(
                aggregations=[datasets.AggregationSpec(field=name + '.label', op='count_values')],
            ),
        )
        assert {row['value']: row['count'] for row in result['results'][0]['value']} == {'dog': 2, 'cat': 1}
        assert {r['value'] for r in (await datasets._autocomplete_impl(1, name + '.label', '', 100))['values']} == {
            'cat',
            'dog',
        }

    registry_database(scenario)


def test_classification_list_append_does_not_scan_or_invalidate(registry_database, monkeypatch):
    async def scenario(database):
        await ingest([{'single': {'label': 'ok'}}])
        await datasets._get_dataset_field_stats_impl(1, None)
        before = await database.fetch(
            "SELECT field_name,xmin::text FROM dataset_field_stats WHERE field_name LIKE '\\_\\_%' ORDER BY 1",
        )
        scans = _watch_scans(monkeypatch, database)
        sid = (await ingest([{'classes': [{'label': 'dog'}]}], prefix='later'))['ids'][0]
        await datasets.update_sample(1, sid, datasets.SampleUpdate(metadata={'classes': [{'label': 'dog'}]}), USER)
        fields = {f['name']: f for f in await datasets._get_dataset_field_stats_impl(1, None)}
        assert fields['classes']['type'] == 'classification'
        assert 'dog' in fields['classes.label']['values']
        assert 'single.label' not in fields
        after = await database.fetch(
            "SELECT field_name,xmin::text FROM dataset_field_stats WHERE field_name LIKE '\\_\\_%' ORDER BY 1",
        )
        assert len(before) == len(after) == 7
        assert [a['field_name'] for a, b in zip(after, before) if a != b] == ['__classification_fields__']
        assert scans == []

    registry_database(scenario)


def test_statistics_publishes_caches_before_bikes_and_lanes(registry_database):
    async def scenario(database):
        # The disposable database uses en_US.utf8, where bikes0.label sorts before its cache.
        await ingest([{'bikes0': BOX, 'lanes': LANES}])
        for _ in range(2):
            await database.execute("DELETE FROM dataset_field_stats WHERE field_name LIKE '\\_\\_%'")
            await run_refresh(database, 1, force=True)
            rows = await database.fetch(
                "SELECT field_name,unique_values FROM dataset_field_stats "
                "WHERE field_name IN ('bikes0.label','lanes.label')",
            )
            assert {r['field_name']: r['unique_values'] for r in rows} == {
                'bikes0.label': ['car'],
                'lanes.label': ['lane'],
            }
            assert (
                await database.fetchval(
                    "SELECT count(*) FROM dataset_field_stats WHERE field_name LIKE '\\_\\_%'",
                )
                == 7
            )

    registry_database(scenario)


def test_filter_compilation_never_discovers_missing_caches(registry_database, monkeypatch):
    async def scenario(database):
        await ingest([{'vehicles': BOX, 'ground_truth': [{'label': 'dog'}]}])
        scans = _watch_scans(monkeypatch, database)
        columns = await datasets.get_dataset_array_columns(1)
        assert {'detections', 'polygons', 'ground_truth', 'predictions'} <= columns.label_fields
        assert 'vehicles' not in columns.label_fields
        assert (await datasets.list_samples_post(1, datasets.SamplesRequest()))['total'] == 1
        assert await datasets._autocomplete_impl(1, 'filepath', '', 100)
        assert not any('jsonb_each(' in query for query in scans)
        assert not await database.fetchval(
            "SELECT EXISTS(SELECT 1 FROM dataset_field_stats WHERE field_name LIKE '\\_\\_%')",
        )

    registry_database(scenario)


def test_concurrent_sidebar_discovery_is_single_flight(registry_database, monkeypatch):
    import asyncio

    async def scenario(database):
        await ingest([{'vehicles': BOX, 'lanes': LANES, 'classes': [{'label': 'dog'}]}])
        scans = _watch_scans(monkeypatch, database)
        responses = await asyncio.gather(*[datasets._get_dataset_field_stats_impl(1, None) for _ in range(4)])
        for response in responses:
            assert {f['name']: f['type'] for f in response}['vehicles'] == 'detections'
        discovery = [query for query in scans if 'jsonb_each(' in query]
        assert len(discovery) == 7
        assert len(set(discovery)) == 7

    registry_database(scenario)


def test_sidebar_timeout_releases_discovery_lock(registry_database, monkeypatch):
    from backend import label_fields

    async def scenario(database):
        await ingest([{'vehicles': BOX}])
        original = asyncpg.Connection.fetch
        seen_timeouts = []

        async def slow_scan(connection, query, *args, **kwargs):
            if 'jsonb_each(' in query:
                seen_timeouts.append(await connection.fetchval('SHOW statement_timeout'))
                await connection.execute('SELECT pg_sleep(0.5)')
            return await original(connection, query, *args, **kwargs)

        monkeypatch.setattr(label_fields, 'DISCOVERY_TIMEOUT_MS', 100, raising=False)
        monkeypatch.setattr(asyncpg.Connection, 'fetch', slow_scan)
        await datasets._get_dataset_field_stats_impl(1, None)
        assert seen_timeouts == ['100ms'] * 7
        assert not await database.fetchval(
            "SELECT EXISTS(SELECT 1 FROM dataset_field_stats WHERE field_name LIKE '\\_\\_%')",
        )
        # A second request gets the locks and can publish after the error, on the same pool.
        monkeypatch.setattr(asyncpg.Connection, 'fetch', original)
        monkeypatch.setattr(label_fields, 'DISCOVERY_TIMEOUT_MS', 10_000)
        fields = {f['name']: f for f in await datasets._get_dataset_field_stats_impl(1, None)}
        assert fields['vehicles']['type'] == 'detections'
        async with database.pool.acquire() as connection:
            assert await connection.fetchval('SHOW statement_timeout') == '10s'

    registry_database(scenario)


def test_uncached_list_field_still_filters_and_autocompletes(registry_database):
    """A box field that exists but isn't in a discovery cache yet (new dataset, deploy, restore)."""

    async def scenario(database):
        await ingest([{'vehicles': BOX}, {'vehicles': BOX}, {'other': 1}])
        await database.execute("DELETE FROM dataset_field_stats WHERE field_name LIKE '\\_\\_%'")
        for spec, expected in [
            ({'field': 'vehicles.label', 'operator': '=', 'value': 'car'}, 2),
            ({'field': 'vehicles.label', 'operator': 'not in', 'value': ['car']}, 1),
            ({'field': 'vehicles.confidence', 'operator': '>', 'value': 0.5}, 2),
        ]:
            result = await datasets.list_samples_post(1, datasets.SamplesRequest(filters=spec))
            assert result['total'] == expected, spec
        values = (await datasets._autocomplete_impl(1, 'vehicles.label', '', 100))['values']
        assert {row['value'] for row in values} == {'car'}

    registry_database(scenario)
