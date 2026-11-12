"""The readers of an object's image read samples.parent_id (0029): the cross-grain filters that couple the image and
object grids, and the object grid ranked by its images. A linked object answers as before. An object with no image
(naming another object, a sample of another dataset, junk, a number beyond INTEGER, an id no sample has, nothing)
joins nothing, as before, and a number beyond INTEGER no longer fails the request. metadata.parent_id is what
clients filter by and receive, as before."""

import os

import pytest

from backend import index_advisor
from backend.routers import datasets
from backend.tests import test_field_registry_postgres as registry_helpers
from backend.tests.test_object_parent_column import add, ids

registry_database = registry_helpers.registry_database
pytestmark = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)
UNLINKED = ('on_object', 'on_other_dataset', 'junk', 'beyond_integer', 'gone', 'none')


async def every_kind(database) -> dict:
    """Dataset 1: a corn and a soy image, each with a linked weed object, and a weed object of every unlinked kind.
    Returns their ids by name."""
    await add(1, [(None, {'crop': 'corn'}), (None, {'crop': 'soy'})], 's3://r/img')
    corn, soy = await ids(database, 1, 's3://r/img')
    await add(2, [(None, {'crop': 'corn'})], 's3://r/other')
    (other,) = await ids(database, 2, 's3://r/other')
    await add(1, [('object', {'parent_id': image, 'label': 'weed'}) for image in (corn, soy)], 's3://r/a')
    on_corn, on_soy = await ids(database, 1, 's3://r/a')
    values = {
        'on_object': {'parent_id': on_corn},
        'on_other_dataset': {'parent_id': other},
        'junk': {'parent_id': 'abc'},
        'beyond_integer': {'parent_id': 99_999_999_999},
        'gone': {'parent_id': 99_000_000},
        'none': {},
    }
    await add(1, [('object', {**values[kind], 'label': 'weed'}) for kind in UNLINKED], 's3://r/b')
    unlinked = await ids(database, 1, 's3://r/b')  # in UNLINKED's order: the paths end -0 .. -5
    assert await database.fetchval('SELECT count(parent_id) FROM samples WHERE id = ANY($1::int[])', unlinked) == 0
    return {'corn': corn, 'soy': soy, 'on_corn': on_corn, 'on_soy': on_soy, **dict(zip(UNLINKED, unlinked))}


def grid(grain: str, *base: dict) -> dict:
    """A grid's filter as the web app sends it (FilterContext.withGrain): its grain, and the base filter matched by a
    row itself or by its partner in the other grain."""
    node = {'logic': 'AND', 'filters': list(base)}
    operator = 'parent_matches' if grain == 'object' else 'has_matching_object'
    return {
        'logic': 'AND',
        'filters': [
            {'field': 'media_type', 'operator': '=' if grain == 'object' else '!=', 'value': 'object'},
            {
                'logic': 'OR',
                'filters': [
                    {'field': '_cgself', 'operator': 'cg_self', 'value': node},
                    {'field': '_crossgrain', 'operator': operator, 'value': node},
                ],
            },
        ],
    }


async def rows(filters: dict, **request) -> tuple[list, int]:
    result = await datasets.list_samples_post(1, datasets.SamplesRequest(filters=filters, limit=100, **request))
    return [item['id'] for item in result['items']], result['total']


WEED = {'field': 'label', 'operator': '=', 'value': 'weed'}
CORN = {'field': 'crop', 'operator': '=', 'value': 'corn'}


def test_the_image_grid_shows_the_images_of_matching_objects_and_no_other_row(registry_database):
    """has_matching_object: only an object's image. The object another object names, and an image of another dataset
    named from this one, aren't anyone's image; a number beyond INTEGER failed the whole request (a cast)."""

    async def scenario(database):
        made = await every_kind(database)
        assert await rows(grid('image', WEED)) == ([made['corn'], made['soy']], 2)
        # With no grain: the object named by another object isn't that object's image.
        alone = {'field': '_x', 'operator': 'has_matching_object', 'value': {'logic': 'AND', 'filters': [WEED]}}
        assert await rows(alone) == ([made['corn'], made['soy']], 2)
        # Every unlinked kind on its own matches no image.
        for kind in UNLINKED:
            only = {'field': 'id', 'operator': 'in', 'value': [made[kind]]}
            assert await rows(grid('image', only)) == ([], 0), kind

    registry_database(scenario)


def test_the_object_grid_shows_the_objects_of_matching_images_and_keeps_unlinked_ones_out(registry_database):
    """parent_matches: an object whose image matches. Every unlinked kind matches no image, and an excluded region
    (NOT around the coupled filter) keeps every one of them, as before."""

    async def scenario(database):
        made = await every_kind(database)
        assert await rows(grid('object', CORN)) == ([made['on_corn']], 1)
        excluded = {
            'logic': 'AND',
            'filters': [
                {'field': 'media_type', 'operator': '=', 'value': 'object'},
                {'logic': 'NOT', 'filters': [grid('object', CORN)['filters'][1]]},
            ],
        }
        kept = [made['on_soy'], *(made[kind] for kind in UNLINKED)]
        assert await rows(excluded) == (sorted(kept), len(kept))

    registry_database(scenario)


def test_the_object_grid_ranked_by_its_images_puts_unlinked_objects_last(registry_database):
    async def scenario(database):
        made = await every_kind(database)
        objects = {'logic': 'AND', 'filters': [{'field': 'media_type', 'operator': '=', 'value': 'object'}]}
        ranked, total = await rows(objects, order_by_ids=[made['soy'], made['corn']], order_by_parent=True)
        assert ranked == [made['on_soy'], made['on_corn'], *sorted(made[kind] for kind in UNLINKED)]
        assert total == 8

    registry_database(scenario)


def test_a_filter_on_parent_id_still_reads_metadata(registry_database):
    """What clients filter by is metadata.parent_id, linked or not: the pivot from images to their objects, and a
    value no image has."""

    async def scenario(database):
        made = await every_kind(database)
        pivot = {'field': 'parent_id', 'operator': 'in', 'value': [made['corn'], made['on_corn']]}
        assert await rows({'logic': 'AND', 'filters': [pivot]}) == ([made['on_corn'], made['on_object']], 2)
        gone = {'field': 'parent_id', 'operator': '=', 'value': 99_000_000}
        assert await rows({'logic': 'AND', 'filters': [gone]}) == ([made['gone']], 1)

    registry_database(scenario)


def test_the_index_advisor_never_offers_the_index_the_parent_trigger_reads(registry_database):
    async def scenario(database):
        snapshot = await index_advisor.collect(database.pool, None)
        review, _ = index_advisor.build_review(snapshot, None)
        needed = {item['name']: item['needed'] for item in review['needed']}
        assert needed['idx_samples_parent_id'] == [
            "finds the objects waiting for a new image (samples.parent_id's trigger)",
        ]

    registry_database(scenario)
