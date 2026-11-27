"""Lock exists / not_exists on derived fields: the grid coverage warning's "show the samples missing X".

`embeddings.<field>`, `hashes.<field>` and `location` must use the same predicate as
/datasets/{id}/coverage and /missing (an embedding / hash row for the field, or a location), so the
grid count after clicking matches the warning's number. The end-to-end check against a real
database is frontend/e2e/coverage-gaps.spec.ts.
"""

import os
from typing import Any

import pytest
from fastapi import HTTPException

from backend.routers import datasets
from backend.routers.datasets import SAMPLES_JOIN_FIELD_MAPPING, parse_filter_node
from backend.tests import test_field_registry_postgres
from backend.tests.test_object_parent_column import add, ids

registry_database = test_field_registry_postgres.registry_database


def _sql(field, op='not_exists', field_mapping=None, value=None):
    args: list[Any] = []
    sql, next_idx = parse_filter_node({'field': field, 'operator': op, 'value': value}, args, 1, field_mapping)
    return sql, args, next_idx


def test_missing_hash_is_an_anti_join_on_sample_hashes():
    # The outer id must be QUALIFIED: sample_hashes has its own `id`, so a bare `id` binds to the
    # subquery's row and the filter matched every sample (caught by the e2e on a real database).
    sql, args, next_idx = _sql('hashes.dhash_8')
    assert sql == 'NOT EXISTS (SELECT 1 FROM sample_hashes _d WHERE _d.sample_id = samples.id AND _d.field_name = $1)'
    assert args == ['dhash_8'] and next_idx == 2


def test_exists_is_the_complement():
    sql, args, _ = _sql('hashes.phash', 'exists')
    assert sql == 'EXISTS (SELECT 1 FROM sample_hashes _d WHERE _d.sample_id = samples.id AND _d.field_name = $1)'
    assert args == ['phash']


def test_missing_embedding_uses_the_joined_sample_id():
    sql, args, _ = _sql('embeddings.clip', field_mapping=SAMPLES_JOIN_FIELD_MAPPING)
    assert sql == (
        'NOT EXISTS (SELECT 1 FROM embeddings _d WHERE _d.dataset_id = s.dataset_id AND _d.sample_id = s.id '
        'AND _d.field_name = $1)'
    )
    assert args == ['clip']


def test_missing_patch_field_folds_tiles_like_coverage():
    # /coverage reports patch tiles ('clip_patch_4x4_r0c1') folded into 'clip_patch_4x4'; no row is
    # stored under that virtual name, so an exact match made every sample read as missing.
    sql, args, _ = _sql('embeddings.clip_patch_4x4')
    assert "regexp_replace(_d.field_name, '_r[0-9]+c[0-9]+$', '') = $1" in sql
    assert '_d.field_name = $1' not in sql
    assert args == ['clip_patch_4x4']


def test_a_hash_named_like_a_patch_field_is_matched_exactly():
    sql, _, _ = _sql('hashes.odd_patch_2x2')
    assert sql.endswith('AND _d.field_name = $1)')


@pytest.mark.parametrize(('op', 'expected'), [('not_exists', 'location IS NULL'), ('exists', 'location IS NOT NULL')])
def test_location_presence_checks_the_column(op, expected):
    # Was `metadata ? 'location'`: location is a column, not a metadata key, so it never matched.
    sql, args, next_idx = _sql('location', op)
    assert sql == expected and args == [] and next_idx == 1


def test_location_keeps_its_geo_operators():
    sql, _, _ = _sql('location', 'geo_polygon', value=[[0, 0], [1, 0], [1, 1]])
    assert 'ST_' in sql


@pytest.mark.parametrize('field', ['embeddings.clip', 'hashes.phash'])
def test_a_derived_field_answers_presence_only(field):
    with pytest.raises(HTTPException) as err:
        _sql(field, '=', value='x')
    assert err.value.status_code == 400


def test_a_metadata_key_is_unaffected():
    sql, _, _ = _sql('embeddingsx', 'exists')
    assert sql == '(metadata ? $1)'


def grid(grain: str, *base: dict) -> dict:
    """A grid's filter as the page sends it on a dataset with objects (FilterContext.withGrain)."""
    node = {'logic': 'AND', 'filters': list(base)}
    partner = 'parent_matches' if grain == 'object' else 'has_matching_object'
    return {
        'logic': 'AND',
        'filters': [
            {'field': 'media_type', 'operator': '=' if grain == 'object' else '!=', 'value': 'object'},
            {
                'logic': 'OR',
                'filters': [
                    {'field': '_cgself', 'operator': 'cg_self', 'value': node},
                    {'field': '_crossgrain', 'operator': partner, 'value': node},
                ],
            },
        ],
    }


@pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')
def test_cross_grain_a_missing_hash_is_asked_of_the_row_as_a_whole_and_never_leaks_through_the_partner(
    registry_database,
):
    """Was a check of the cross-grain guard's internals (the partner dropped the absence test, the row kept it), which
    the one-rule piece removes; asked here at the surface instead, as the page wraps the filter. An absence test on
    the other kind of row (objects carry no image hashes) must not be vacuously true and bring every image back
    through its objects: `hashes.phash not exists` shows the images with no hash, on the images' grid, and on the
    objects' grid the objects whose image has none (an object with no image has none)."""

    async def scenario(database):
        await add(1, [(None, {'n': 1}), (None, {'n': 2}), (None, {'n': 3})], 'img')
        hashed, plain, alone = await ids(database, 1, 'img')
        await add(
            1,
            [('object', {'parent_id': hashed}), ('object', {'parent_id': plain}), ('object', {})],
            'obj',
        )
        on_hashed, on_plain, loose = await ids(database, 1, 'obj')
        await database.execute(
            "INSERT INTO sample_hashes (sample_id, field_name, hash_hex) VALUES ($1, 'phash', 'ab')",
            hashed,
        )

        async def shown(node: dict) -> set:
            page = await datasets.list_samples_post(1, datasets.SamplesRequest(filters=node, limit=100))
            assert page['total'] == len(page['items'])
            return {item['id'] for item in page['items']}

        missing = {'field': 'hashes.phash', 'operator': 'not_exists'}
        present = {'field': 'hashes.phash', 'operator': 'exists'}
        assert await shown(grid('image', present)) == {hashed}
        assert await shown(grid('object', present)) == {on_hashed}
        assert await shown(grid('image', missing)) == {plain, alone}
        assert await shown(grid('object', missing)) == {on_plain, loose}

    registry_database(scenario)
