"""Class-probability overlays (class_rules.py): the rule's shape, the field found as its own overlay type, and its
default rule set through the API and kept in segmentation_config."""

import os

import pytest
from fastapi import HTTPException

from backend import class_rules, label_fields
from backend.routers import datasets
from backend.tests import test_field_registry_postgres as registry_helpers

registry_database = registry_helpers.registry_database
ingest, USER = registry_helpers.ingest, registry_helpers.USER
needs_postgres = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)
THRESHOLDS = {
    'type': 'rules',
    'rules': [{'classes': [3], 'min': 0.3, 'class': 3}, {'classes': [1, 2], 'min': 1, 'class': 1}],
    'fallback': 0,
}


def test_a_rule_is_argmax_or_ordered_thresholds_with_a_fallback():
    assert class_rules.validate_rule({'type': 'argmax'}) == {'type': 'argmax'}
    stored = class_rules.validate_rule(THRESHOLDS)
    assert stored['rules'][1] == {'classes': [1, 2], 'min': 1.0, 'class': 1} and stored['fallback'] == 0
    assert class_rules.validate_rule({**THRESHOLDS, 'fallback': None})['fallback'] is None
    for wrong, message in (
        ({'type': 'max'}, '"argmax" or "rules"'),
        ({'type': 'argmax', 'x': 1}, 'only "type"'),
        ({'type': 'rules', 'rules': []}, '1 to 64'),
        ({'type': 'rules', 'rules': [{'classes': [1], 'min': 1.5, 'class': 1}]}, 'from 0 to 1'),
        ({'type': 'rules', 'rules': [{'classes': [1, 1], 'min': 0.5, 'class': 1}]}, 'twice'),
        ({'type': 'rules', 'rules': [{'classes': [1], 'min': 0.5, 'class': True}]}, 'class index'),
        ({'type': 'rules', 'rules': [{'classes': [1], 'min': 0.5}]}, '"classes", "min" and "class"'),
        ({**THRESHOLDS, 'fallback': -1}, 'class index'),
    ):
        with pytest.raises(ValueError, match=message):
            class_rules.validate_rule(wrong)
    with pytest.raises(ValueError, match='class_rules.probs'):
        class_rules.validate_config({'class_rules': {'probs': {'type': 'max'}}})
    assert class_rules.validate_config({'mask_targets': {}}) == {'mask_targets': {}}


def test_the_field_is_its_own_overlay_type():
    assert label_fields.overlay_type({'type': 'class_probabilities', 'probPath': 'p.png', 'classes': 6}) == (
        'class_probabilities'
    )


@needs_postgres
def test_the_schema_lists_the_field_and_its_default_rule_is_set_through_the_api(registry_database):
    async def scenario(database):
        label = {'type': 'class_probabilities', 'probPath': 's3://b/p.png', 'classes': 6}
        await ingest([{'probs': label}], dataset_id=1, prefix='s3://images/a')
        discovered = await label_fields.discover_fields(database.pool, 1, '__probability_fields__')
        assert discovered == ['probs']
        # The sidebar's fields list it as an overlay of its own.
        fields = await datasets._get_dataset_field_stats_impl(1, None)
        assert {'name': 'probs', 'type': 'class_probabilities', 'config': None} in fields
        assert [field['name'] for field in fields].count('probs') == 1

        # A writer sets the default; it's checked, stored in segmentation_config, and null removes it.
        writer = {**USER, 'role': 'power_user'}
        saved = await datasets.set_class_rule(1, 'probs', datasets.ClassRuleUpdate(rule=THRESHOLDS), user=writer)
        assert saved['rule']['rules'][0] == {'classes': [3], 'min': 0.3, 'class': 3}
        config = await database.fetchval('SELECT segmentation_config FROM datasets WHERE id = 1')
        assert config['class_rules']['probs'] == saved['rule']
        with pytest.raises(HTTPException) as wrong:
            await datasets.set_class_rule(1, 'probs', datasets.ClassRuleUpdate(rule={'type': 'max'}), user=writer)
        assert wrong.value.status_code == 400
        await datasets.set_class_rule(1, 'probs', datasets.ClassRuleUpdate(rule=None), user=writer)
        assert (await database.fetchval('SELECT segmentation_config FROM datasets WHERE id = 1'))['class_rules'] == {}

        # The whole segmentation_config, set by an admin, has its rules checked too.
        with pytest.raises(HTTPException) as refused:
            await datasets.update_dataset(
                1,
                datasets.DatasetUpdate(segmentation_config={'class_rules': {'probs': {'type': 'rules'}}}),
                USER,
            )
        assert refused.value.status_code == 400 and 'class_rules.probs' in refused.value.detail

    registry_database(scenario)
