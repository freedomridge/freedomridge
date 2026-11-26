"""What describe_dataset reads of the samples (docs/ASSISTANT.md), against a disposable PostgreSQL: the statements a
call runs over the samples table are pinned, so a later change can't quietly bring back the reads it dropped (the
dataset's capped count for the sidebar, the sidebar's total for null counts, the lists of values read from the samples
or an index, coverage's second count and its hashes); and the four routes it now asks for less answer the app as
before when asked as the app asks.
"""

import os
import re
from typing import Any

import asyncpg.connection
import pytest

from backend.routers import datasets
from backend.tests import test_assistant_core
from backend.tests.statistics_helpers import run_refresh
from backend.tests.test_assistant_core import ADMIN, POWER, TRIALS, World
from backend.tests.test_assistant_views import CROPS, _plant, ok
from backend.tests.test_field_registry_postgres import USER

registry_database = test_assistant_core.registry_database
world = test_assistant_core.world
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')

OVER_SAMPLES = re.compile(r'\b(FROM|JOIN)\s+samples\b', re.IGNORECASE)
LOCATED = 5  # of _plant's images, the first ones
OBJECTS = 3


class Statements:
    """The text of every statement run with arguments while it watches (every read the routes make has some)."""

    def __init__(self, monkeypatch: Any):
        self.texts: list[str] = []
        real = asyncpg.connection.Connection._execute
        texts = self.texts

        async def recorded(connection: Any, query: Any, *args: Any, **kwargs: Any) -> Any:
            texts.append(' '.join(str(query).split()))
            return await real(connection, query, *args, **kwargs)

        monkeypatch.setattr(asyncpg.connection.Connection, '_execute', recorded)

    def over_samples(self) -> list[str]:
        return [text for text in self.texts if OVER_SAMPLES.search(text)]


def kinds(texts: list[str]) -> list[str]:
    """Each statement over the samples, by what it does: `ends` reads an index's ends (smallest and largest), which is
    no scan; `count` counts rows; anything else (a list of distinct values, a join) is named in full."""
    named = []
    for text in texts:
        if re.search(r'\b(min|MIN)\(', text) and 'count(' not in text:
            named.append('ends')
        elif 'count(' in text and not re.search(r'DISTINCT|unnest|GROUP BY|JOIN|LIMIT', text, re.IGNORECASE):
            named.append('count')
        else:
            named.append(text)
    return sorted(named)


async def plant(w: World) -> None:
    """_plant's twelve images (two tags, `crop` indexed), the first LOCATED with a location, OBJECTS object rows under
    the first image, and a vector for two images."""
    await _plant(w, private=False)
    await w.database.execute(
        "UPDATE dataset_fields SET is_promoted = TRUE WHERE dataset_id = $1 AND field_name = 'crop'",
        TRIALS,
    )
    images = [
        row['id'] for row in await w.database.fetch('SELECT id FROM samples WHERE dataset_id = $1 ORDER BY id', TRIALS)
    ]
    await w.database.execute(
        "UPDATE samples SET location = ST_SetSRID(ST_MakePoint(10, 50), 4326) WHERE id = ANY($1::int[])",
        images[:LOCATED],
    )
    objects = [
        {'filepath': f'object-{n}.jpg', 'media_type': 'object', 'metadata': {'parent_id': images[0], 'area': n}}
        for n in range(OBJECTS)
    ]
    await datasets.add_samples(TRIALS, {'samples': objects}, user=USER)
    vectors = [{'sample_id': sample_id, 'field_name': 'clip', 'vector': [1.0, 0.0, 0.0]} for sample_id in images[:2]]
    stored = await w.api(ADMIN, 'POST', f'/api/datasets/{TRIALS}/embeddings', json=vectors)
    assert stored.status_code == 200, stored.text
    await run_refresh(w.database, TRIALS)


def test_describe_dataset_counts_the_samples_once_and_lists_only_kept_values(world, monkeypatch):
    async def scenario(w: World):
        await plant(w)
        statements = Statements(monkeypatch)
        result = ok(await w.tool(POWER, 'describe_dataset', dataset=TRIALS))
        # One statement counts the samples: /coverage's rows, object rows and located rows (two index-only scans, one
        # after the other). The rest of what names the samples reads an index's ends (created_at's twice, id's).
        assert kinds(statements.over_samples()) == ['count', 'ends', 'ends', 'ends'], statements.over_samples()
        assert result['dataset']['samples'] == len(CROPS) and result['dataset']['objects'] == OBJECTS, result
        assert result['samples_with_location'] == LOCATED and result['embedding_fields'] == ['clip'], result
        fields = {field['name']: field for field in result['fields']}
        assert fields['media_type']['values'] == {'distinct': 2, 'examples': ['image', 'object']}, fields
        assert fields['tags']['values'] == {'distinct': 2, 'examples': ['bright', 'dark']}, fields
        assert fields['crop']['values'] == {'distinct': 4, 'examples': sorted(set(CROPS))}, fields
        assert fields['media_type']['complete'] and fields['tags']['complete'], fields
        assert fields['id']['samples_with_value'] == len(CROPS) + OBJECTS, fields

        # A change the statistics haven't counted: the lists are still the kept ones, nothing is read for them, and
        # they say so, as every other field does then.
        await datasets.add_samples(TRIALS, {'samples': [{'filepath': 'late.jpg', 'tags': ['late']}]}, user=USER)
        statements.texts.clear()
        result = ok(await w.tool(POWER, 'describe_dataset', dataset=TRIALS))
        assert kinds(statements.over_samples()) == ['count', 'ends', 'ends', 'ends'], statements.over_samples()
        fields = {field['name']: field for field in result['fields']}
        assert fields['tags']['complete'] is False and fields['tags']['values']['distinct'] is None, fields
        assert fields['media_type']['complete'] is False, fields
        assert result['dataset']['samples'] == len(CROPS) + 1, result  # the counts are the samples', now

    world(scenario)


def test_the_routes_answer_the_app_as_before(world, monkeypatch):
    async def scenario(w: World):
        await plant(w)
        statements = Statements(monkeypatch)
        capped = 'SELECT count(*) FROM (SELECT 1 FROM samples WHERE dataset_id = $1 LIMIT $2) s'
        total = 'SELECT count(*) FROM samples WHERE dataset_id = $1'

        async def get(path: str, **query: str) -> tuple[Any, str]:
            """The answer, and every statement it ran, one per line."""
            statements.texts.clear()
            response = await w.api(ADMIN, 'GET', f'/api/datasets/{TRIALS}{path}', params=query)
            assert response.status_code == 200, response.text
            return response.json(), '\n'.join(statements.texts)

        # The dataset: the app's request counts up to 100,001 samples for sidebar_expand_all; brief, its row only.
        whole, ran = await get('')
        assert whole['sidebar_expand_all'] is True and capped in ran, (whole, ran)
        brief, ran = await get('', brief='true')
        assert brief['sidebar_expand_all'] is None and 'FROM samples' not in ran, (brief, ran)
        assert all(whole[key] == brief[key] for key in ('id', 'name', 'description', 'access_level', 'deleting'))

        # field-stats: the app's request counts the samples for a partial field's null_count and reads the indexed
        # field's values from its index; brief does neither, and gives the same counts.
        app, ran = await get('/field-stats')
        assert {entry['name']: entry for entry in app}['area']['null_count'] == len(CROPS), app
        assert total in ran.split('\n') and 'WITH RECURSIVE' in ran, ran
        brief, ran = await get('/field-stats', brief='true')
        assert 'null_count' not in {entry['name']: entry for entry in brief}['area'], brief
        assert total not in ran.split('\n') and 'WITH RECURSIVE' not in ran, ran
        counts = {entry['name']: entry.get('sample_count') for entry in app}
        assert counts == {entry['name']: entry.get('sample_count') for entry in brief}

        # coverage: the app's request reads the hashes and counts in two statements; brief, the same numbers.
        app, ran = await get('/coverage')
        assert app['hashes'] == [] and 'sample_hashes' in ran, (app, ran)
        brief, ran = await get('/coverage', brief='true')
        assert brief == app and 'sample_hashes' not in ran, (brief, ran)

        # schema: the app's request reads media_type's and tags' lists from the samples and the indexed field's from
        # its index; the statistics' are the same lists, in the same order, while nothing waits to be counted.
        app, ran = await get('/schema')
        assert 'unnest(tags)' in ran and 'DISTINCT media_type' in ran and 'WITH RECURSIVE' in ran, ran
        app = {field['name']: field for field in app}
        kept, ran = await get('/schema', kept_values='true', stale='true')
        assert 'unnest(tags)' not in ran and 'DISTINCT media_type' not in ran and 'WITH RECURSIVE' not in ran, ran
        kept = {field['name']: field for field in kept}
        for name in ('media_type', 'tags', 'crop'):
            assert kept[name]['values'] == app[name]['values'], (kept[name], app[name])
        for name in ('media_type', 'tags'):
            assert kept[name]['values_complete'] is True and kept[name]['stats_stale'] is False, kept[name]
            assert 'values_complete' not in app[name] and 'stats_stale' not in app[name], app[name]

    world(scenario)


def test_before_the_statistics_counted_anything_the_built_in_lists_are_pending(world, monkeypatch):
    async def scenario(w: World):
        await w.switch_on(power_user=True)
        await datasets.add_samples(TRIALS, {'samples': [{'filepath': 'only.jpg', 'tags': ['first']}]}, user=USER)
        statements = Statements(monkeypatch)
        result = ok(await w.tool(POWER, 'describe_dataset', dataset=TRIALS))
        fields = {field['name']: field for field in result['fields']}
        # Nothing kept yet: no list is read from the samples for them; they say their statistics aren't counted.
        assert not any('unnest(tags)' in text or 'DISTINCT media_type' in text for text in statements.texts)
        for name in ('media_type', 'tags'):
            assert fields[name]['values'] is None and fields[name]['complete'] is False, fields[name]
        assert result['dataset']['samples'] == 1, result

    world(scenario)
