"""One facet request for every expanded sidebar section.

After a filter change the sidebar asks for all its expanded fields' filtered facets in one request:
one selectivity probe, and each field computed under its own budget, a few at a time, so no field
runs out of budget waiting for a connection behind the request's other fields.
"""

import asyncio

from backend.routers import datasets
from backend.tests.statistics_helpers import run_refresh
from backend.tests.test_field_registry_postgres import USER, registry_database  # noqa: F401

FIELDS = [f'n{k:02d}' for k in range(12)]
SELECTIVE = {
    'logic': 'AND',
    'filters': [
        {'field': 'media_type', 'operator': '!=', 'value': 'object'},
        {'field': 'batch', 'operator': '=', 'value': 'late'},
    ],
}


async def seed(database, monkeypatch):
    monkeypatch.setattr(datasets, '_LIGHTNING_FACET_MAX_ROWS', 5)  # 'late' (3 rows) is selective
    samples = [
        {
            'filepath': f'sample-{n}.jpg',
            'metadata': {'batch': 'late' if n > 17 else 'early', **{name: n + k for k, name in enumerate(FIELDS)}},
        }
        for n in range(1, 21)
    ]
    await datasets.add_samples(1, {'samples': samples}, user=USER)
    await run_refresh(database, 1)


def slow_fields(database, monkeypatch, seconds):
    """Each field's filtered MIN/MAX holds a connection for `seconds`; returns live counters."""
    state = {'started': 0, 'running': 0, 'peak': 0}

    async def minmax(dataset_id, field_name, filters):
        state['started'] += 1
        state['running'] += 1
        state['peak'] = max(state['peak'], state['running'])
        try:
            await database.fetchval(f'SELECT pg_sleep({seconds})')
        finally:
            state['running'] -= 1
        return {'min': 1, 'max': 2}

    monkeypatch.setattr(datasets, 'compute_filtered_minmax', minmax)
    return state


def test_many_fields_share_one_probe_and_each_gets_its_own_budget(registry_database, monkeypatch):  # noqa: F811
    async def scenario(database):
        await seed(database, monkeypatch)
        probes = []
        original_probe = datasets._filtered_set_is_broad

        async def counting_probe(*args, **kwargs):
            probes.append(args)
            return await original_probe(*args, **kwargs)

        monkeypatch.setattr(datasets, '_filtered_set_is_broad', counting_probe)
        # 12 fields of 0.4 s each against a 1 s budget and a 5-connection pool: run all at once, the
        # last two would wait 0.8 s for a connection and run out of budget before their query ran.
        monkeypatch.setattr(datasets, '_LIGHTNING_FIELD_STATS_BUDGET_S', 1.0)
        state = slow_fields(database, monkeypatch, 0.4)
        fields = await datasets._get_dataset_field_stats_impl(1, SELECTIVE, fields=FIELDS)
        assert {field['name']: field['provenance'] for field in fields} == {name: 'exact' for name in FIELDS}
        assert {(field['min'], field['max']) for field in fields} == {(1, 2)}
        assert len(probes) == 1
        assert state['peak'] == max(1, database.pool.get_max_size() // 4)

        # Lightning-off is unchanged: every field at once, no budget.
        state.update(started=0, peak=0)
        eager = await datasets._get_dataset_field_stats_impl(1, SELECTIVE, lightning_mode=False)
        assert all(field['provenance'] == 'exact' for field in eager if field['name'] in FIELDS)
        assert state['peak'] > max(1, database.pool.get_max_size() // 4)

    registry_database(scenario)


def test_cancelling_a_facet_request_never_starts_its_queued_fields(registry_database, monkeypatch):  # noqa: F811
    async def scenario(database):
        await seed(database, monkeypatch)
        state = slow_fields(database, monkeypatch, 0.4)
        request = asyncio.ensure_future(datasets._get_dataset_field_stats_impl(1, SELECTIVE, fields=FIELDS))
        while state['started'] == 0:
            await asyncio.sleep(0.01)
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)
        started = state['started']
        await asyncio.sleep(0.6)
        assert started < len(FIELDS)
        assert state['started'] == started and state['running'] == 0
        assert database.pool.get_idle_size() == database.pool.get_size()
        active = await database.fetchval(
            "SELECT count(*) FROM pg_stat_activity WHERE query LIKE 'SELECT pg_sleep(0.4)%' AND state = 'active'",
        )
        assert active == 0

    registry_database(scenario)
