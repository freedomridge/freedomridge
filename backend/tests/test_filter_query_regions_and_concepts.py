"""Named regions and concept scores in one filter query (docs/FILTER_QUERY.md, docs/REGIONS.md): the two text forms,
`location within "US-KS"` and `concept "equipment" > 0.5`, come from two features that each tested its own. These
test them together: in one query, under NOT and OR, beside fields named `location`, `within` and `concept`, with each
side's mistakes worded as before, and selecting exactly their samples through samples/query."""

import hashlib
import json
import os
import re
from typing import Any

import pytest

from backend import regions
from backend.filter_query import (
    MAX_COMPARISONS,
    NUMBER,
    TEXT,
    Concept,
    FilterQueryError,
    RegionMatch,
    format_filter,
    parse_query,
)
from backend.label_fields import DatasetArrayColumns
from backend.routers import datasets
from backend.routers import filter_query as endpoints
from backend.tests.test_access_rule_spec import ADMIN, world
from backend.tests.test_field_registry_postgres import registry_database

__all__ = ['registry_database', 'world']

FIELDS = {'idx': NUMBER, 'split': TEXT}
REGIONS = {
    'US-KS': RegionMatch(key='US-KS'),
    'us-ks': RegionMatch(key='US-KS'),
    'Kansas': RegionMatch(key='US-KS'),
    'Georgia': RegionMatch(candidates=[('GE', 'country', 'Georgia'), ('US-GA', 'state', 'United States of America')]),
    'US-KZ': RegionMatch(suggestions=['US-KS', 'US-KY']),
}
CONCEPTS = [Concept(5, 'equipment', 0.7), Concept(6, 'blue sky', 0.4)]

IN_KS = {'field': 'location', 'operator': 'geo_region', 'value': 'US-KS'}
OUT_KS = {'field': 'location', 'operator': 'geo_region_exclude', 'value': 'US-KS'}


def concept(operator, op='>', threshold=0.5, concept_id=5):
    return {
        'field': '_concept',
        'operator': operator,
        'value': {'concept_id': concept_id, 'op': op, 'threshold': threshold},
    }


def parse(query, fields=FIELDS):
    return parse_query(query, fields, regions=REGIONS, concepts=CONCEPTS)


def text(node):
    return format_filter(node, concepts=CONCEPTS)


def mistake(query, fields=FIELDS, **lookups) -> dict:
    with pytest.raises(FilterQueryError) as raised:
        parse_query(query, fields, **lookups)
    return raised.value.detail


def compiled(node) -> str:
    args: list[Any] = [1]
    sql, _ = datasets.parse_filter_node(
        node,
        args,
        2,
        dataset_id=1,
        array_columns=DatasetArrayColumns(set(), label_fields=set(), datetime_fields=set()),
    )
    return sql


def test_one_query_holds_both_forms_in_either_order_and_reads_back_the_same():
    written = 'location within "US-KS" AND not concept "equipment" > 0.5 AND idx >= 3'
    nodes = parse(written)
    assert nodes == [IN_KS, concept('concept_exclude'), {'field': 'idx', 'operator': '>=', 'value': 3}]
    assert [text(node) for node in nodes] == ['location within "US-KS"', 'not concept "equipment" > 0.5', 'idx >= 3']
    assert parse(' AND '.join(text(node) for node in nodes)) == nodes
    # The other order, the name and the bare word forms: the same filters, in that order.
    other = parse('idx >= 3 AND not concept equipment > 0.5 AND location within Kansas')
    assert other == [nodes[2], nodes[1], nodes[0]]
    # Compiled together, each keeps its own SQL: the concept's one guard in front, the region's lookup by key.
    sql = compiled({'logic': 'AND', 'filters': nodes})
    assert sql.count('require_concept_scores') == 1 and 'region_id(' in sql


def test_not_around_each_and_around_both():
    # A NOT of one region test stays a NOT (regions); a NOT of one concept test is the panel's exclude (concepts).
    assert parse('NOT (location within "US-KS")') == [{'logic': 'NOT', 'filters': [IN_KS]}]
    assert parse('NOT (concept "equipment" > 0.5)') == [concept('concept_exclude')]
    both = parse('NOT (location within "US-KS" AND concept "equipment" > 0.5)')
    assert both == [{'logic': 'NOT', 'filters': [{'logic': 'AND', 'filters': [IN_KS, concept('concept_select')]}]}]
    for nodes in (parse('NOT (location within "US-KS")'), parse('NOT (concept "equipment" > 0.5)'), both):
        assert parse(text(nodes[0])) == nodes
        assert compiled(nodes[0])


def test_location_not_within_beside_not_concept():
    nodes = parse('location not within "US-KS" AND not concept "blue sky" <= 0.25')
    assert nodes == [OUT_KS, concept('concept_exclude', op='<=', threshold=0.25, concept_id=6)]
    assert [text(node) for node in nodes] == ['location not within "US-KS"', 'not concept "blue sky" <= 0.25']
    either = parse('(location not within us-ks OR not concept equipment > 0.5)')
    assert either == [{'logic': 'OR', 'filters': [OUT_KS, concept('concept_exclude')]}]
    assert text(either[0]) == '(location not within "US-KS" OR not concept "equipment" > 0.5)'


def test_fields_named_location_within_and_concept_beside_both_forms():
    fields = {**FIELDS, 'location': TEXT, 'within': TEXT, 'concept': TEXT}
    written = (
        'location = "north field" AND within = a AND concept in (a b) AND location within "US-KS" '
        'AND concept "equipment" > 0.5 AND concept exists AND location not within "US-KS" AND within != "b"'
    )
    nodes = parse(written, fields)
    assert nodes == [
        {'field': 'location', 'operator': '=', 'value': 'north field'},
        {'field': 'within', 'operator': '=', 'value': 'a'},
        {'field': 'concept', 'operator': 'in', 'value': ['a', 'b']},
        IN_KS,
        concept('concept_select'),
        {'field': 'concept', 'operator': 'exists', 'value': None},
        OUT_KS,
        {'field': 'within', 'operator': '!=', 'value': 'b'},
    ]
    assert parse(' AND '.join(text(node) for node in nodes), fields) == nodes
    # A value named within, location or concept needs no quotes either.
    assert parse('split = within AND split != concept AND split = location', fields) == [
        {'field': 'split', 'operator': '=', 'value': 'within'},
        {'field': 'split', 'operator': '!=', 'value': 'concept'},
        {'field': 'split', 'operator': '=', 'value': 'location'},
    ]


def test_each_sides_mistakes_read_as_before_with_the_other_side_present():
    cases: list[tuple[str, dict[str, Any]]] = [
        # (query, lookups that side had alone)
        ('location within Georgia', {'regions': REGIONS}),
        ('location within US-KZ', {'regions': REGIONS}),
        ('location within "nowhere"', {'regions': {}}),
        ('location = 1', {'regions': REGIONS}),
        ('idx within "US-KS"', {'regions': REGIONS}),
        ('location within', {'regions': REGIONS}),
        ('concept "equpment" > 0.5', {'concepts': CONCEPTS}),
        ('concept "equipment" = 0.5', {'concepts': CONCEPTS}),
        ('concept "equipment" > lots', {'concepts': CONCEPTS}),
        ('concept "equipment" 0.5', {'concepts': CONCEPTS}),
        ('concept > 0.5', {'concepts': CONCEPTS}),
        ('split = "train" AND concept "tractor" > 0.5', {'concepts': CONCEPTS}),
    ]
    for query, alone in cases:
        assert mistake(query, regions=REGIONS, concepts=CONCEPTS) == mistake(query, **alone), query
    # The messages themselves, as each feature wrote them.
    assert mistake('location within Georgia', regions=REGIONS, concepts=CONCEPTS)['suggestions'] == ['GE', 'US-GA']
    assert mistake('concept "equpment" > 0.5', regions=REGIONS, concepts=CONCEPTS)['message'] == (
        "unknown concept 'equpment'"
    )
    assert mistake('idx within "US-KS"', regions=REGIONS, concepts=CONCEPTS)['message'] == (
        "within applies only to 'location' (location within \"US-KS\"), not to 'idx'"
    )


def test_within_after_concept_is_the_region_forms_mistake_and_a_concept_named_within_is_quoted():
    with_field = {**FIELDS, 'concept': TEXT}
    within_only = "within applies only to 'location' (location within \"US-KS\"), not to 'concept'"
    unknown = "unknown field 'concept'; a concept's score is written concept \"name\" > 0.5"
    for query in ('concept within "US-KS"', 'concept not within "US-KS"'):
        assert mistake(query, with_field, regions=REGIONS, concepts=CONCEPTS) == {
            'message': within_only,
            'line': 1,
            'column': 9,
            'suggestions': [],
        }, query
        error = mistake(query, regions=REGIONS, concepts=CONCEPTS)
        assert (error['message'], error['column']) == (unknown, 1), query
    # A concept really named within is written, and read, quoted.
    named = [*CONCEPTS, Concept(9, 'within', 0.3)]
    [node] = parse_query('concept "within" > 0.5', FIELDS, regions=REGIONS, concepts=named)
    assert node == concept('concept_select', concept_id=9)
    assert format_filter(node, concepts=named) == 'concept "within" > 0.5'
    [hidden] = parse_query('not concept "within" > 0.5 AND idx >= 1', FIELDS, regions=REGIONS, concepts=named)[:1]
    written = 'not concept "within" > 0.5'
    assert format_filter(hidden, concepts=named) == written
    assert parse_query(written, FIELDS, regions=REGIONS, concepts=named) == [hidden]


def test_a_region_test_and_a_concept_test_are_one_comparison_each():
    half = MAX_COMPARISONS // 2
    query = ' AND '.join(['location within "US-KS"'] * half + ['concept "equipment" > 0.5'] * half)
    assert len(parse(query)) == MAX_COMPARISONS
    assert 'at most' in mistake(query + ' AND idx > 1', regions=REGIONS, concepts=CONCEPTS)['message']


def test_the_region_guard_the_two_concept_guards_and_a_comparison_each_read_their_own_parameter():
    # Compiled after earlier parameters ($1 taken): the region leaf's key, the concept's id and threshold, the
    # comparison's value, then the region guard's keys, the concept guards' ids (both read them) and the dataset.
    node = {
        'logic': 'AND',
        'filters': [IN_KS, concept('concept_select'), {'field': 'idx', 'operator': '>=', 'value': 3}],
    }
    args: list[Any] = [1]
    sql, next_idx = datasets.parse_filter_node(
        node,
        args,
        2,
        dataset_id=1,
        array_columns=DatasetArrayColumns(set(), label_fields=set(), datetime_fields=set()),
    )
    assert next_idx == len(args) + 1
    assert sorted({int(n) for n in re.findall(r'\$(\d+)', sql)}) == list(range(2, next_idx))

    def arg(pattern: str) -> list[Any]:
        found = re.findall(pattern, sql)
        assert found, pattern
        return [args[int(n) - 1] for n in found]

    assert arg(r'require_regions\(\$(\d+)::text\[\]\)') == [['US-KS']]
    owned = re.findall(r'require_dataset_concepts\(\$(\d+)::int\[\], \$(\d+)::int\)', sql)
    assert [(args[int(ids) - 1], args[int(dataset) - 1]) for ids, dataset in owned] == [([5], 1)]
    assert arg(r'require_concept_scores\(\$(\d+)::int\[\]\)') == [[5]]
    assert set(arg(r'region_id\(\$(\d+)\)')) == {'US-KS'}
    assert arg(r'_cs\.concept_id = \$(\d+)') == [5] and arg(r'_cs\.score > \$(\d+)') == [0.5]
    assert arg(r'END >= \$(\d+)') == [3]
    # The guards in front, as InitPlans run left to right: ownership, then readiness, then the region's keys.
    order = [sql.index(name) for name in ('require_dataset_concepts', 'require_concept_scores', 'require_regions')]
    assert order == sorted(order)


# --- On a database: the samples one OR of a region and a concept selects ---

pytestmark_postgres = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires disposable PostgreSQL',
)


def square(west, south, east, north):
    return [[[west, south], [east, south], [east, north], [west, north], [west, south]]]


def feature(iso, adm1, name, coordinates):
    properties = {
        'iso_3166_2': iso,
        'adm1_code': adm1,
        'iso_a2': 'US',
        'adm0_a3': 'USA',
        'admin': 'United States of America',
        'name_en': name,
        'name': name,
        'region': 'Midwest',
    }
    return {'type': 'Feature', 'properties': properties, 'geometry': {'type': 'Polygon', 'coordinates': coordinates}}


BOUNDARIES = json.dumps(
    {
        'type': 'FeatureCollection',
        'features': [
            feature('US-KS', 'USA-3514', 'Kansas', square(-102.05, 37.0, -94.6, 40.0)),
            feature('US-NE', 'USA-3520', 'Nebraska', square(-104.05, 40.0, -95.3, 43.0)),
        ],
    },
).encode()
# idx: (location, the concept's score): in Kansas or Nebraska, or none; scored high, low or not at all.
SAMPLES = {
    0: ((-100.0, 38.0), 0.9),
    1: ((-99.0, 38.5), 0.1),
    2: ((-100.0, 41.0), 0.9),
    3: ((-99.0, 41.5), 0.1),
    4: (None, 0.9),
    5: (None, None),
}


async def seed(database):
    samples = []
    for idx, (location, _) in SAMPLES.items():
        sample: dict = {'filepath': f'combined-{idx}.jpg', 'metadata': {'idx': idx}}
        if location:
            sample['location'] = {'type': 'Point', 'coordinates': list(location)}
        samples.append(sample)
    await datasets.add_samples(1, {'samples': samples}, user={'id': 1, 'role': 'admin', 'username': 'admin'})
    ids = {
        row['idx']: row['id']
        for row in await database.fetch(
            "SELECT (metadata->>'idx')::int AS idx, id FROM samples WHERE dataset_id = 1",
        )
    }
    await database.execute(
        "INSERT INTO embedding_concepts (id, dataset_id, name, embedding_field, kind, weight, threshold) "
        "VALUES (5, 1, 'equipment', 'clip', 'seed', '[1,0]', 0.5)",
    )
    for idx, (_, score) in SAMPLES.items():
        if score is not None:
            await database.execute(
                'INSERT INTO embedding_concept_scores (concept_id, sample_id, score) VALUES (5, $1, $2)',
                ids[idx],
                score,
            )
    async with database.pool.acquire() as connection:
        await regions.load(
            connection,
            regions.SETS['natural-earth-admin1'],
            BOUNDARIES,
            version='combined-test',
            sha256=hashlib.sha256(BOUNDARIES).hexdigest(),
            verified=False,
            loaded_from='file',
            user_id=None,
        )
    return {sample_id: idx for idx, sample_id in ids.items()}


@pytestmark_postgres
def test_a_region_and_a_concept_under_one_or_select_exactly_their_samples(registry_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setattr(endpoints, 'db', database)
        idx_of = await seed(database)

        async def selected(filters) -> set:
            page = await datasets.list_samples_post(1, datasets.SamplesRequest(limit=100, filters=filters))
            return {idx_of[item['id']] for item in page['items']}

        either = {'logic': 'OR', 'filters': [IN_KS, concept('concept_select')]}
        assert await selected(either) == {0, 1, 2, 4}
        neither = {'logic': 'OR', 'filters': [OUT_KS, concept('concept_exclude')]}
        assert await selected(neither) == {1, 2, 3, 4, 5}

        # The same through the text, as the app and the SDK send it.
        for written, expected in (
            ('(location within "US-KS" OR concept "equipment" > 0.5) AND idx >= 1', {1, 2, 4}),
            ('location within Kansas AND not concept equipment > 0.5', {1}),
            ('NOT (location within "US-KS" AND concept "equipment" > 0.5)', {1, 2, 3, 4, 5}),
            ('location not within "US-KS" AND not concept "equipment" > 0.5', {3, 5}),
        ):
            parsed = await endpoints.parse_filter_query(1, endpoints.FilterQueryParse(query=written))
            assert await selected(parsed['filter']) == expected, written
            # Each part written back reads as the same filters, the chips kept (`reuse`).
            again = await endpoints.parse_filter_query(
                1,
                endpoints.FilterQueryParse(query=' AND '.join(parsed['parts']), current=parsed['filters']),
            )
            assert again['filters'] == parsed['filters'] and again['reuse'] == list(range(len(parsed['filters'])))

        # A chip whose region sits beside a concept in one OR is recognised in text that names the region by name: its
        # key is looked up from the chip too, read back with the concept's name.
        current = [{'logic': 'OR', 'filters': [IN_KS, concept('concept_select')]}]
        named = await endpoints.parse_filter_query(
            1,
            endpoints.FilterQueryParse(query='(location within Kansas OR concept equipment > 0.5)', current=current),
        )
        assert named['filters'] == current and named['reuse'] == [0]

    registry_database(scenario)


FARM, OTHER = 10, 11
SECRET = 'secret-orchard'  # the other dataset's concept: its name never reaches the farm's reader


def test_another_datasets_concept_beside_a_region_is_refused_with_the_400(world):
    async def scenario(w):
        for dataset_id in (FARM, OTHER):
            await w.dataset(dataset_id)
        user = {'id': ADMIN, 'role': 'admin', 'username': f'user-{ADMIN}'}
        samples = [
            {
                'filepath': f'beside-{i}.jpg',
                'metadata': {'idx': i},
                'location': {'type': 'Point', 'coordinates': [-99.0, 38.0]},
            }
            for i in range(2)
        ]
        await datasets.add_samples(FARM, {'samples': samples}, user=user)
        other_ids = (await datasets.add_samples(OTHER, {'samples': samples[:1]}, user=user))['ids']
        others = await w.database.fetchval(
            'INSERT INTO embedding_concepts (dataset_id, name, embedding_field, kind, weight) '
            "VALUES ($1, $2, 'clip', 'seed', '[1,0]') RETURNING id",
            OTHER,
            SECRET,
        )
        await w.database.execute(
            'INSERT INTO embedding_concept_scores (concept_id, sample_id, score) VALUES ($1, $2, 0.75)',
            others,
            other_ids[0],
        )
        async with w.database.pool.acquire() as connection:
            await regions.load(
                connection,
                regions.SETS['natural-earth-admin1'],
                BOUNDARIES,
                version='combined-test',
                sha256=hashlib.sha256(BOUNDARIES).hexdigest(),
                verified=False,
                loaded_from='file',
                user_id=None,
            )
        theirs = concept('concept_select', concept_id=others)
        base = f'/api/datasets/{FARM}'
        # The region alone is the farm's two samples: the region guard passes, so the refusal is the concept's.
        alone = await w.call(ADMIN, 'POST', f'{base}/samples/query', json={'filters': IN_KS, 'limit': 0})
        assert alone.status_code == 200 and alone.json()['total'] == 2, alone.text
        for logic in ('AND', 'OR'):
            for filters in ([IN_KS, theirs], [theirs, OUT_KS]):
                node = {'logic': logic, 'filters': filters}
                for path, body in (
                    ('samples/query', {'filters': node, 'limit': 0}),
                    ('samples/query', {'filters': node, 'skip_count': True}),
                    ('map_points', {'filters': node, 'points': True}),
                    ('field-stats', {'filters': node}),
                ):
                    answer = await w.call(ADMIN, 'POST', f'{base}/{path}', json=body)
                    assert answer.status_code == 400, (logic, path, answer.status_code, answer.text)
                    assert f"Concept {others} doesn't exist in this dataset" in answer.text, (logic, path, answer.text)
                    assert SECRET not in answer.text, (logic, path, answer.text)

    world(scenario)
