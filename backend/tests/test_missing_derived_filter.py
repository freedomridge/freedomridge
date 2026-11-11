"""Lock exists / not_exists on derived fields: the grid coverage warning's "show the samples missing X".

`embeddings.<field>`, `hashes.<field>` and `location` must use the same predicate as
/datasets/{id}/coverage and /missing (an embedding / hash row for the field, or a location), so the
grid count after clicking matches the warning's number. The end-to-end check against a real
database is frontend/e2e/coverage-gaps.spec.ts.
"""

from typing import Any

import pytest
from fastapi import HTTPException

from backend.routers.datasets import SAMPLES_JOIN_FIELD_MAPPING, _strictify_negations, parse_filter_node


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


def test_cross_grain_partner_drops_the_absence_and_self_keeps_it():
    # An absence test on the OTHER grain (objects carry no image hashes) would be vacuously
    # true and leak every row across the parent link, exactly like is_null / not_exists on metadata.
    node = {'field': 'hashes.phash', 'operator': 'not_exists'}
    assert _strictify_negations(node, partner=True) is None
    assert _strictify_negations(node, partner=False) == node
