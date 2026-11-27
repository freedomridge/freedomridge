"""The SDK's filter of similarity results asks the server (contract g7, one rule everywhere, 2.2, changed 2026-11-25).
Written from the contract, not from the code.

A SimilarityResultView gets no reading of its own: it asks the server which of its samples the filter shows (the
filter AND `id in` its samples' ids, through the SDK's ordinary query) and keeps those, in their order, with their
distances. So every filter the server takes is taken there, read by the one compiler, and none is refused for what a
value is. A view asks once and remembers (the same view object; a clone asks for itself), a request a page of ids
(1,000). The similarity `distance` is no field of the dataset: a filter that names the field `distance` is refused
with NotImplementedError, saying to compare `sample.distance` in Python, also on a dataset that has a field of that
name (filtered before the search). A key named `distance` of a list's elements, inside an element match, is no such
field and is not refused.

The SDK is driven as a script drives it, its requests served by the app in-process with real API tokens (the region
tests' world: a database of its own per test). SIMILAR is a dataset without objects of twelve samples, `s0` to `s11`,
each with a vector for the similarity search, nearest first in ORDER; LIMIT keeps the nearest ten, so the last two
are no similarity result and `id in` matters. Its fields: `word`, text with upper-case and accented letters; `place`,
text with accents in either case; a location (three samples have none, two lie outside the square SQUARE); a concept
`scene` (three samples unscored); `vehicles`, a list of labels under a name the dataset declares by its shape (an
empty list, a JSON null and no key among them); `created_at`, a day of January 2026 each; `readings`, a list whose
elements have a key named `distance`; and a field of the dataset named `distance`. MANY, of 1,100 samples, is a result
of more than a page.

Each filter is asked twice: through the similarity result, and through the samples query as the same filter AND `id
in` the result's ids. The two must agree, the similarity result keeping its order and distances: what the filter shows
is a subsequence of the unfiltered result. Where the reading doesn't hang on the database's collation or case rules,
the samples it shows are also written out by hand, so a case that shows nothing, or everything, can't pass for one
that tests something. Filters chain as their AND, and each filtered view has its own answer, leaving the result it
was made from as it was. Requests are counted by wrapping the SDK client's transport.
"""

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import pytest
from fridge import Condition, Dataset, F, geo_within, match_elements
from fridge.core import client as client_module
from requests.adapters import BaseAdapter

from backend.routers import datasets
from backend.tests import test_one_rule_everywhere_spec as spec
from backend.tests.test_access_rule_spec import ADMIN
from backend.tests.test_protected_metadata_keys_spec import sdk
from backend.tests.test_regions_spec import ADMIN_USER, Regions, post, selected

database = spec.database
regions_world = spec.regions_world
pytestmark = spec.pytestmark

SIMILAR = 77
SQUARE = [[0, 0], [5, 0], [5, 5], [0, 5]]  # no planted location is on its border
CUT = 0.5
QUERY = [1.0, 0.0, 0.0, 0.0]
LIMIT = 10
BOX = [0.1, 0.1, 0.2, 0.2]


def vehicles(*labels: str) -> list[dict[str, Any]]:
    """A list of labels as a detection field of any name stores it (a `bbox` each: the shape the dataset knows)."""
    return [{'label': label, 'bbox': BOX} for label in labels]


# name: (metadata, location, the concept's score, the day of January 2026 it was created)
ITEMS: dict[str, tuple[dict[str, Any], Optional[tuple[float, float]], Optional[float], int]] = {
    's0': ({'word': 'Apple', 'place': 'Café du Parc', 'vehicles': vehicles('car')}, (1.0, 1.0), 0.9, 1),
    's1': ({'word': 'apple', 'place': 'cafe', 'vehicles': vehicles('truck')}, (10.0, 10.0), 0.2, 2),
    's2': ({'word': 'Banana', 'place': 'CAFÉ', 'vehicles': []}, None, None, 3),
    's3': ({'word': 'banana', 'place': 'Crème', 'vehicles': vehicles('car', 'truck')}, (2.0, 3.0), 0.6, 4),
    's4': ({'word': 'Cherry', 'place': None, 'vehicles': vehicles('bus')}, (4.0, 4.0), 0.1, 5),
    's5': ({}, None, None, 6),
    's6': ({'word': 'cherry', 'place': 'café', 'vehicles': vehicles('car')}, (20.0, 1.0), 0.7, 7),
    's7': ({'word': 'Éclair', 'place': 'Le Café', 'vehicles': None}, (3.0, 1.0), 0.4, 8),
    's8': ({'word': 'zebra', 'place': 'CAFE', 'vehicles': vehicles('truck')}, None, 0.8, 9),
    's9': ({'word': 'Zebra', 'place': 'cafés', 'vehicles': vehicles('car')}, (0.5, 4.5), 0.3, 10),
    's10': ({'word': 'éclair', 'place': 'Cafe', 'vehicles': vehicles('bus', 'car')}, (2.5, 2.5), 0.55, 11),
    's11': ({'word': None, 'place': 'CAFÉ DU NORD', 'vehicles': vehicles('car')}, (1.5, 3.5), None, 12),
}
ORDER = ['s7', 's2', 's11', 's9', 's0', 's4', 's8', 's1', 's10', 's6', 's3', 's5']  # nearest first
RESULTS = ORDER[:LIMIT]
# A list whose elements have a key named `distance` (s5 and s8 have no key), and the field named `distance`.
READINGS: dict[str, Any] = {
    's0': [{'distance': 1.0}, {'distance': 9.0}],
    's1': [{'distance': 7.0}],
    's2': [],
    's3': [{'distance': 1.0}],
    's4': [{'distance': 4.5}],
    's6': [{'distance': 3.0}],
    's7': [{'distance': 2.0}],
    's9': [{'distance': 6.0}],
    's10': None,
    's11': [{'distance': 0.5}],
}
MEASURED = {'s0': 2.0, 's1': 8.0, 's4': 3.0, 's7': 12.0, 's9': 1.0}


def day(number: int) -> datetime:
    return datetime(2026, 1, number, 12, tzinfo=timezone.utc)


async def plant(r: Regions) -> spec.Planted:
    """ITEMS as a dataset without objects: each sample's vector at its place in ORDER, its day, the concept `scene`
    and its scores, and the sidebar read once (it discovers `vehicles` as a list of labels)."""
    await r.dataset(SIMILAR)
    samples = []
    for name, (metadata, location, _, _) in ITEMS.items():
        more = {'readings': READINGS[name]} if name in READINGS else {}
        if name in MEASURED:
            more['distance'] = MEASURED[name]
        body: dict[str, Any] = {'filepath': f'{name}.jpg', 'metadata': {**metadata, **more}}
        if location is not None:
            body['location'] = {'type': 'Point', 'coordinates': list(location)}
        samples.append(body)
    await datasets.add_samples(SIMILAR, {'samples': samples}, user=ADMIN_USER)
    stored = await r.database.fetch('SELECT id, filepath FROM samples WHERE dataset_id = $1', SIMILAR)
    ids = {row['filepath'][: -len('.jpg')]: row['id'] for row in stored}
    assert set(ids) == set(ITEMS), sorted(ids)
    for name, (_, _, _, number) in ITEMS.items():
        await r.database.execute('UPDATE samples SET created_at = $2 WHERE id = $1', ids[name], day(number))
    vectors = [
        {'sample_id': ids[name], 'vector': [1.0, place / 10, 0.0, 0.0], 'field_name': 'clip'}
        for place, name in enumerate(ORDER)
    ]
    assert (await post(r, f'/api/datasets/{SIMILAR}/embeddings', vectors)).is_success
    concept = await r.database.fetchval(
        'INSERT INTO embedding_concepts (dataset_id, name, embedding_field, kind, weight, threshold) '
        "VALUES ($1, 'scene', 'clip', 'seed', '[1,0,0,0]', $2) RETURNING id",
        SIMILAR,
        CUT,
    )
    for name, (_, _, score, _) in ITEMS.items():
        if score is not None:
            await r.database.execute(
                'INSERT INTO embedding_concept_scores (concept_id, sample_id, score) VALUES ($1, $2, $3)',
                concept,
                ids[name],
                score,
            )
    sidebar = await r.call(ADMIN, 'GET', f'/api/datasets/{SIMILAR}/field-stats')
    assert sidebar.status_code == 200, sidebar.text
    return spec.Planted(SIMILAR, ids, concept)


class Counted(BaseAdapter):
    """The SDK client's transport, wrapped: each request it is handed is written down (method, path, JSON body) and
    then sent on by the transport it wraps."""

    def __init__(self, inner: BaseAdapter):
        super().__init__()
        self.inner = inner
        self.sent: list[tuple[str, str, Any]] = []

    def send(self, request, **kwargs):  # type: ignore[override]
        body = json.loads(request.body) if request.body else None
        self.sent.append((request.method, urlparse(request.url).path, body))
        return self.inner.send(request, **kwargs)

    def close(self) -> None:
        self.inner.close()


def counted() -> Counted:
    """Wrap the transport of the SDK client `sdk()` signed in."""
    client = client_module._client
    assert client is not None
    session = client.session
    transport = Counted(session.get_adapter('http://test/api'))
    session.mount('http://test', transport)
    return transport


def similar() -> Any:
    """The nearest LIMIT samples to QUERY, as a script asks for them (run in the SDK's thread)."""
    return Dataset(f'dataset-{SIMILAR}').all().sort_by_similarity(vector=QUERY, limit=LIMIT)


def concept_test(p: spec.Planted, operator: str) -> Condition:
    """A concept's filter as a script writes it: the server's node, as a Condition."""
    return Condition('_concept', operator, {'concept_id': p.concept, 'op': '>', 'threshold': CUT})


# name: (the filter, built for the planted dataset; the result's samples it shows, by hand, or None where that hangs
# on the database's collation or case rules)
CASES: dict[str, tuple[Callable[[spec.Planted], Condition], Optional[set[str]]]] = {
    'a place': (lambda p: geo_within(SQUARE), {'s7', 's11', 's9', 's0', 's4', 's10'}),
    # rule 1.6: an excluded place needs a location (s2 and s8 have none)
    'an excluded place': (lambda p: ~geo_within(SQUARE), {'s1', 's6'}),
    'a range on created_at': (
        lambda p: (F('created_at') >= day(6)) & (F('created_at') < day(11)),
        {'s7', 's9', 's8', 's6'},
    ),
    'a range on text with upper-case letters': (lambda p: (F('word') >= 'C') & (F('word') < 'd'), None),
    'contains on accented text': (lambda p: F('place').contains('café'), None),
    "a concept's select": (lambda p: concept_test(p, 'concept_select'), {'s0', 's8', 's10', 's6'}),
    # rule 1.3: a concept's exclude needs a score (s2 and s11 have none)
    "a concept's exclude": (lambda p: concept_test(p, 'concept_exclude'), {'s7', 's9', 's4', 's1'}),
    'a list of labels under a name the dataset declares': (
        lambda p: F('vehicles.label') == 'car',
        {'s11', 's9', 's0', 's10', 's6'},
    ),
    # rule 1.4 (changed 2026-11-26): its negation shows the lists holding no car, s2's empty one too; not s7's JSON null
    'the negation of that list of labels': (lambda p: F('vehicles.label') != 'car', {'s4', 's8', 's1', 's2'}),
    # an element's key named `distance` is no similarity distance: taken, not refused
    'an element match on a key named distance': (
        lambda p: match_elements('readings', F('distance') < 5),
        {'s7', 's11', 's0', 's4', 's6'},
    ),
    # rule 1.4 (changed 2026-11-26): its NOT needs a list, s2's empty one too; not s10's JSON null, nor s8 with none
    'the NOT of that element match': (lambda p: ~match_elements('readings', F('distance') < 5), {'s9', 's1', 's2'}),
}


@pytest.mark.parametrize('case', list(CASES))
def test_a_filter_of_similarity_results_shows_what_the_server_shows_of_them_in_their_order(
    regions_world,
    monkeypatch,
    case,
):
    build, by_hand = CASES[case]

    async def scenario(r):
        p = await plant(r)
        sdk(r, asyncio.get_running_loop(), monkeypatch)
        condition = build(p)

        def script() -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
            result = similar()
            listed = [(sample.id, sample.distance) for sample in result]
            shown = [(sample.id, sample.distance) for sample in result.filter(condition)]
            return listed, shown

        listed, shown = await asyncio.to_thread(script)
        assert [p.names([sample_id]).pop() for sample_id, _ in listed] == RESULTS, 'the nearest first'
        distances = [distance for _, distance in listed]
        assert all(isinstance(distance, float) for distance in distances) and distances == sorted(set(distances))
        among = {'field': 'id', 'operator': 'in', 'value': [sample_id for sample_id, _ in listed]}
        wanted = await selected(r, {'logic': 'AND', 'filters': [condition.to_dict(), among]}, SIMILAR)
        if by_hand is not None:
            assert p.names(wanted) == by_hand, (case, sorted(p.names(wanted)))
        assert 0 < len(wanted) < len(listed), (case, 'the filter tests nothing here', sorted(p.names(wanted)))
        assert shown == [(sample_id, distance) for sample_id, distance in listed if sample_id in wanted], (
            case,
            [p.names([sample_id]).pop() for sample_id, _ in shown],
            sorted(p.names(wanted)),
        )

    regions_world(scenario)


DISTANCE: dict[str, Callable[[], Condition]] = {
    'alone': lambda: F('distance') < 0.5,
    'inside an OR': lambda: (F('word') == 'Apple') | (F('distance') < 0.5),
    'inside a NOT inside an AND': lambda: (F('word') == 'Apple') & ~(F('distance') >= 0.1),
}


@pytest.mark.parametrize('where', list(DISTANCE))
def test_a_filter_naming_distance_is_refused_before_any_request(regions_world, monkeypatch, where):
    """Refused also here, where the dataset has a field named `distance`: that field is filtered before the search."""

    async def scenario(r):
        p = await plant(r)
        sdk(r, asyncio.get_running_loop(), monkeypatch)
        transport = counted()

        def script() -> tuple[list[int], list[str], list[tuple[str, str, Any]]]:
            dataset = Dataset(f'dataset-{SIMILAR}')
            before = [sample.id for sample in dataset.all().filter(F('distance') < 5).sort_by_similarity(vector=QUERY)]
            result = similar()
            transport.sent.clear()
            with pytest.raises(NotImplementedError) as counting:
                len(result.filter(DISTANCE[where]()))
            with pytest.raises(NotImplementedError) as listing:
                list(result.filter(DISTANCE[where]()))
            return before, [str(counting.value), str(listing.value)], list(transport.sent)

        before, messages, sent = await asyncio.to_thread(script)
        assert [p.names([sample_id]).pop() for sample_id in before] == ['s9', 's0', 's4'], (
            'the field, before the search'
        )
        for message in messages:
            assert 'sample.distance' in message, message
        assert sent == [], sent

    regions_world(scenario)


def test_a_similarity_result_asks_once_for_a_filter_and_never_without_one(regions_world, monkeypatch):
    """With a filter: one request, the samples query, whatever is read after (len, the samples, len again, ids).
    Without one: none. On an empty result: none, and nothing is shown."""

    async def scenario(r):
        p = await plant(r)
        sdk(r, asyncio.get_running_loop(), monkeypatch)
        transport = counted()

        def script() -> dict[str, Any]:
            result = similar()
            transport.sent.clear()
            plain = (len(result), [sample.id for sample in result], len(result))
            without = list(transport.sent)
            transport.sent.clear()
            view = result.filter(geo_within(SQUARE))
            reads = (len(view), [sample.id for sample in view], len(view), view.ids())
            asked = list(transport.sent)
            empty = Dataset(f'dataset-{SIMILAR}').all().filter(F('word') == 'no sample has this word')
            nothing = empty.sort_by_similarity(vector=QUERY, limit=LIMIT)
            transport.sent.clear()
            filtered = nothing.filter(geo_within(SQUARE))
            emptied = (len(nothing), len(filtered), list(filtered))
            return {
                'plain': plain,
                'without': without,
                'reads': reads,
                'asked': asked,
                'emptied': emptied,
                'on empty': list(transport.sent),
            }

        seen = await asyncio.to_thread(script)
        count, ids, again = seen['plain']
        assert (count, again, [p.names([sample_id]).pop() for sample_id in ids]) == (LIMIT, LIMIT, RESULTS)
        assert seen['without'] == [], seen['without']
        inside = [p.ids[name] for name in RESULTS if name in {'s7', 's11', 's9', 's0', 's4', 's10'}]
        assert seen['reads'] == (len(inside), inside, len(inside), inside)
        assert [(method, path) for method, path, _ in seen['asked']] == [
            ('POST', f'/api/datasets/{SIMILAR}/samples/query'),
        ], seen['asked']
        assert seen['emptied'] == (0, 0, [])
        assert seen['on empty'] == [], seen['on empty']

    regions_world(scenario)


def test_filters_on_a_similarity_result_chain_as_their_and_and_each_view_keeps_its_own_answer(
    regions_world,
    monkeypatch,
):
    """`.filter(a).filter(b)` shows what `a AND b` shows. Filtering makes a view of its own: the unfiltered result,
    and an unfiltered clone of it, still show every one of its samples, and a result filtered by `a` is not changed by
    filtering it by `b` after. Each in the result's order, with its distances."""
    inside = {'s7', 's11', 's9', 's0', 's4', 's10'}  # geo_within(SQUARE) among the results
    with_a_car = {'s11', 's9', 's0', 's10', 's6'}  # vehicles.label = car among the results

    async def scenario(r):
        p = await plant(r)
        sdk(r, asyncio.get_running_loop(), monkeypatch)

        def script() -> dict[str, list[tuple[int, float]]]:
            def held(view: Any) -> list[tuple[int, float]]:
                return [(sample.id, sample.distance) for sample in view]

            result = similar()
            listed = held(result)
            by_place = result.filter(geo_within(SQUARE))
            first = held(by_place)
            chained = by_place.filter(F('vehicles.label') == 'car')
            return {
                'listed': listed,
                'by place': first,
                'chained': held(chained),
                'chained the other way': held(result.filter(F('vehicles.label') == 'car').filter(geo_within(SQUARE))),
                'by their and': held(result.filter(geo_within(SQUARE) & (F('vehicles.label') == 'car'))),
                'by place after': held(by_place),
                'chained again': held(chained),
                'unfiltered after': held(result),
                'an unfiltered clone': held(result.skip(0)),
            }

        seen = await asyncio.to_thread(script)
        listed = seen['listed']
        assert [p.names([sample_id]).pop() for sample_id, _ in listed] == RESULTS, 'the nearest first'

        def among(names: set[str]) -> list[tuple[int, float]]:
            return [(sample_id, distance) for sample_id, distance in listed if p.names([sample_id]) <= names]

        assert seen['by place'] == seen['by place after'] == among(inside)
        both_tests = among(inside & with_a_car)
        assert len(both_tests) == 4
        for name in ('chained', 'chained the other way', 'by their and', 'chained again'):
            assert seen[name] == both_tests, (name, [p.names([sample_id]).pop() for sample_id, _ in seen[name]])
        assert seen['unfiltered after'] == seen['an unfiltered clone'] == listed

    regions_world(scenario)


MANY = 78
COUNT = 1_100  # more ids than a page (1,000)


def test_a_filtered_result_of_more_than_a_page_asks_a_request_a_page(regions_world, monkeypatch):
    """A result of 1,100 samples, every second one `a`: the filter shows samples of both pages of ids, in the result's
    order with their distances, in two requests whatever is read after (len, the samples, len again)."""

    async def scenario(r):
        await r.dataset(MANY)
        samples = [
            {'filepath': f'm{number}.jpg', 'metadata': {'half': 'b' if number % 2 else 'a'}} for number in range(COUNT)
        ]
        await datasets.add_samples(MANY, {'samples': samples}, user=ADMIN_USER)
        stored = await r.database.fetch('SELECT id, filepath FROM samples WHERE dataset_id = $1', MANY)
        number_of = {row['id']: int(row['filepath'][1 : -len('.jpg')]) for row in stored}
        id_of = {number: sample_id for sample_id, number in number_of.items()}
        vectors = [
            {'sample_id': id_of[number], 'vector': [1.0, number / COUNT, 0.0, 0.0], 'field_name': 'clip'}
            for number in range(COUNT)
        ]
        assert (await post(r, f'/api/datasets/{MANY}/embeddings', vectors)).is_success
        sdk(r, asyncio.get_running_loop(), monkeypatch)
        transport = counted()

        def script() -> tuple[list[tuple[int, float]], tuple[Any, ...], list[tuple[str, str, Any]]]:
            result = Dataset(f'dataset-{MANY}').all().sort_by_similarity(vector=QUERY, limit=COUNT)
            listed = [(sample.id, sample.distance) for sample in result]
            transport.sent.clear()
            view = result.filter(F('half') == 'a')
            reads = (len(view), [(sample.id, sample.distance) for sample in view], len(view))
            return listed, reads, list(transport.sent)

        listed, (count, shown, again), sent = await asyncio.to_thread(script)
        assert [number_of[sample_id] for sample_id, _ in listed] == list(range(COUNT)), 'the nearest first'
        wanted = [(sample_id, distance) for sample_id, distance in listed if number_of[sample_id] % 2 == 0]
        assert (count, again, len(shown)) == (COUNT // 2,) * 3
        assert shown == wanted
        assert [(method, path) for method, path, _ in sent] == [('POST', f'/api/datasets/{MANY}/samples/query')] * 2

    regions_world(scenario)
