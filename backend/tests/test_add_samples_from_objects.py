"""Dataset.add_samples_from keeps objects with their images (contract g2, section 5).

Each copied object (media_type 'object') is linked to the copy of its image: its metadata.parent_id names the
target's sample at the image's path, after the path rule. No copied object keeps a source id: one whose image isn't
in the target is copied with parent_id null, and every object added without an image is counted in
`objects_without_image`.

The SDK copies against the whole app here, on the test database: its requests are answered in this process. The
two-server case reads from a source of its own (test_add_samples_from's FakeServer) that numbers its samples as
the target's unrelated samples are numbered, so a source id that leaked would name one of them. A similarity result
is checked on that fake alone.
"""

import asyncio
import os
from typing import Any, Dict, Optional

import pytest
import requests
from fridge.core.client import Client
from fridge.core.dataset import Dataset
from fridge.core.sample import Sample
from fridge.core.view import SampleQuery, SimilarityResultView

from backend.tests import test_access_rule_spec, test_field_registry_postgres
from backend.tests.test_access_rule_spec import ADMIN
from backend.tests.test_add_samples_from import FakeServer

registry_database = test_field_registry_postgres.registry_database
world = test_access_rule_spec.world
needs_database = pytest.mark.skipif(
    not os.getenv('FRIDGE_TEST_POSTGRES_DSN'),
    reason='Requires an isolated PostgreSQL/PostGIS test database',
)

SOURCE, TARGET, OTHER = 1001, 1002, 1003
# The source: four images, and objects named by id. 5000 is an object with a lower id than its image (5003).
IMAGES = {5001: '/m/img_a.jpg', 5002: '/m/img_b.jpg', 5003: '/m/img_c.jpg', 5004: '/m/img_d.jpg'}
LINKED: Dict[str, Any] = {  # object path: its image's source id
    '/m/crops/c0.jpg': 5003,
    '/m/crops/a0.jpg': 5001,
    '/m/crops/a1.jpg': 5001,
    '/m/crops/b0.jpg': '5002',  # a sample id written as text, as the server reads it
    '/m/crops/d0.jpg': 5004,
}
OBJECT_IDS = {
    '/m/crops/c0.jpg': 5000,
    '/m/crops/a0.jpg': 5010,
    '/m/crops/a1.jpg': 5011,
    '/m/crops/b0.jpg': 5012,
    '/m/crops/d0.jpg': 5013,
}
ELSEWHERE = 6000  # an image of another dataset
WITHOUT_IMAGE = {  # object path: its source parent_id, which names no image of the source
    '/m/crops/elsewhere.jpg': ELSEWHERE,
    '/m/crops/gone.jpg': 999999,
    '/m/crops/on_object.jpg': 5010,
}
NOT_AN_ID = '/m/crops/text.jpg'  # parent_id 'abc': copied as it is
CROP = '/m/patches/crop.jpg'  # an image (a to_patches crop) whose parent_id names another dataset's image


@pytest.fixture(autouse=True)
def no_array_fields(monkeypatch):
    monkeypatch.setattr(Sample, '_known_array_fields', set())


@pytest.fixture
def sdk(monkeypatch):
    """sdk(w): a Client signed in as the admin, whose requests the app answers on the test's event loop. SDK calls
    run in a thread (asyncio.to_thread), as a script would run them against a server."""
    state: Dict[str, Any] = {}

    def transport(session, method, url, **kwargs):
        prepared = session.prepare_request(
            requests.Request(
                method,
                url,
                params=kwargs.get('params'),
                data=kwargs.get('data'),
                json=kwargs.get('json'),
                headers=kwargs.get('headers'),
            ),
        )

        async def call():
            return await state['world'].client.request(
                prepared.method,
                prepared.url,
                content=prepared.body,
                headers=dict(prepared.headers),
            )

        answer = asyncio.run_coroutine_threadsafe(call(), state['loop']).result(timeout=120)
        response = requests.Response()
        response.status_code = answer.status_code
        response.headers.update(answer.headers)
        response._content = answer.content
        response._content_consumed = True
        response.url = prepared.url
        response.request = prepared
        return response

    monkeypatch.setattr(requests.Session, 'request', transport)

    def make(w):
        state['world'], state['loop'] = w, asyncio.get_running_loop()
        return Client(base_url='http://test/api', api_token=w.tokens[ADMIN])

    return make


def handle(client, dataset_id: int, name: str) -> Dataset:
    dataset = Dataset.__new__(Dataset)
    dataset._id, dataset.name, dataset.client, dataset._dirty, dataset._array_field_names = (
        dataset_id,
        name,
        client,
        False,
        [],
    )
    return dataset


async def insert(w, dataset_id: int, sample_id: Optional[int], path: str, media_type: str, metadata: dict) -> int:
    if sample_id is None:
        return await w.database.fetchval(
            'INSERT INTO samples (dataset_id, filepath, media_type, metadata) VALUES ($1, $2, $3, $4) RETURNING id',
            dataset_id,
            path,
            media_type,
            metadata,
        )
    await w.database.execute(
        'INSERT INTO samples (id, dataset_id, filepath, media_type, metadata) VALUES ($1, $2, $3, $4, $5)',
        sample_id,
        dataset_id,
        path,
        media_type,
        metadata,
    )
    return sample_id


async def seed_source(w) -> None:
    """The source, its objects linked by the server as in any dataset, and another dataset with one image."""
    for dataset_id in (SOURCE, TARGET, OTHER):
        await w.dataset(dataset_id)
    await insert(w, OTHER, ELSEWHERE, '/x/elsewhere.jpg', 'image', {})
    await insert(w, SOURCE, OBJECT_IDS['/m/crops/c0.jpg'], '/m/crops/c0.jpg', 'object', {'parent_id': 5003})
    for sample_id, path in IMAGES.items():
        await insert(w, SOURCE, sample_id, path, 'image', {'mask': {'maskPath': path.replace('img_', 'masks/')}})
    for path, parent in LINKED.items():
        if path != '/m/crops/c0.jpg':
            metadata = {'parent_id': parent, 'label': 'cat', 'parent_filepath': IMAGES[int(parent)]}
            await insert(w, SOURCE, OBJECT_IDS[path], path, 'object', metadata)
    for path, parent in WITHOUT_IMAGE.items():
        await insert(w, SOURCE, None, path, 'object', {'parent_id': parent})
    await insert(w, SOURCE, None, NOT_AN_ID, 'object', {'parent_id': 'abc'})
    await insert(w, SOURCE, None, CROP, 'image', {'parent_id': ELSEWHERE})
    await w.database.execute("SELECT setval(pg_get_serial_sequence('samples', 'id'), 10000)")
    linked = await w.database.fetchval(
        "SELECT count(*) FROM samples WHERE dataset_id = $1 AND media_type = 'object' AND parent_id IS NOT NULL",
        SOURCE,
    )
    assert linked == len(LINKED)


async def rows(w, dataset_id: int) -> Dict[str, Any]:
    found = await w.database.fetch(
        'SELECT id, filepath, media_type, metadata, parent_id FROM samples WHERE dataset_id = $1',
        dataset_id,
    )
    return {row['filepath']: row for row in found}


async def count(w, dataset_id: int, filters: dict) -> int:
    response = await w.call(
        ADMIN,
        'POST',
        f'/api/datasets/{dataset_id}/samples/query',
        json={'limit': 0, 'filters': filters},
    )
    assert response.status_code == 200, response.text
    return response.json()['total']


async def objects_of_images(w, dataset_id: int, text: str) -> int:
    """The objects grid under an image filter (filepath contains `text`), as the app couples the two grains."""
    return await count(
        w,
        dataset_id,
        {
            'logic': 'AND',
            'filters': [
                {'field': 'media_type', 'operator': '=', 'value': 'object'},
                {
                    'field': '_',
                    'operator': 'parent_matches',
                    'value': {'field': 'filepath', 'operator': 'contains', 'value': text},
                },
            ],
        },
    )


async def unlinked(w, dataset_id: int) -> Dict[str, Any]:
    response = await w.call(ADMIN, 'POST', f'/api/datasets/{dataset_id}/objects/unlinked')
    assert response.status_code == 200, response.text
    return response.json()


def new_path(path: str, rule: Optional[dict]) -> str:
    return path.replace('/m/', 'photos/', 1) if rule else path


@needs_database
@pytest.mark.parametrize('rule', [None, {'/m': 'photos'}])
def test_each_copied_object_is_linked_to_the_copy_of_its_image(world, sdk, rule):
    async def scenario(w):
        await seed_source(w)
        client = sdk(w)
        source, target = handle(client, SOURCE, 'source'), handle(client, TARGET, 'target')

        answer = await asyncio.to_thread(target.add_samples_from, source, batch_size=3, path_prefixes=rule)

        total = len(IMAGES) + len(LINKED) + len(WITHOUT_IMAGE) + 2
        expected = {'seen': total, 'added': total, 'skipped': 0, 'objects_without_image': len(WITHOUT_IMAGE) + 1}
        if rule:
            expected['rewritten'] = total
        assert answer == expected
        copied = await rows(w, TARGET)
        source_ids = set(IMAGES) | set(OBJECT_IDS.values()) | {ELSEWHERE}
        for path, parent in LINKED.items():
            image = copied[new_path(IMAGES[int(parent)], rule)]
            obj = copied[new_path(path, rule)]
            assert obj['metadata']['parent_id'] == image['id'], path
            assert obj['parent_id'] == image['id'], path
            if 'parent_filepath' in obj['metadata']:
                assert obj['metadata']['parent_filepath'] == image['filepath']
        for path in WITHOUT_IMAGE:
            obj = copied[new_path(path, rule)]
            assert obj['metadata']['parent_id'] is None and obj['parent_id'] is None, path
        assert copied[new_path(NOT_AN_ID, rule)]['metadata']['parent_id'] == 'abc'
        assert copied[new_path(CROP, rule)]['metadata']['parent_id'] == ELSEWHERE  # not an object: as it is
        objects = [row for row in copied.values() if row['media_type'] == 'object']
        assert not {row['metadata'].get('parent_id') for row in objects} & source_ids
        assert (await unlinked(w, TARGET))['total'] == len(WITHOUT_IMAGE) + 1
        assert await objects_of_images(w, TARGET, 'img_a') == await objects_of_images(w, SOURCE, 'img_a') == 2

        # Run again with the same arguments: everything is there, nothing changes.
        again = await asyncio.to_thread(target.add_samples_from, source, batch_size=3, path_prefixes=rule)
        assert again == {
            **expected,
            'added': 0,
            'skipped': total,
            'objects_without_image': 0,
            **({'rewritten': 0} if rule else {}),
        }
        assert await rows(w, TARGET) == copied

    world(scenario)


@needs_database
def test_an_object_links_to_an_image_the_target_already_has(world, sdk):
    """Images already in the target count, whether copied earlier (outside this copy's view) or there before the
    copy at the same path; an image neither in the view nor in the target gives null."""

    async def scenario(w):
        await seed_source(w)
        client = sdk(w)
        source, target = handle(client, SOURCE, 'source'), handle(client, TARGET, 'target')
        before = await insert(w, TARGET, None, IMAGES[5004], 'image', {'mine': True})
        first = await asyncio.to_thread(target.add_samples_from, source.filter(filepath__in=[IMAGES[5001]]))
        assert first == {'seen': 1, 'added': 1, 'skipped': 0}  # a view without objects: as before

        answer = await asyncio.to_thread(target.add_samples_from, source.filter(media_type='object'))

        objects = len(LINKED) + len(WITHOUT_IMAGE) + 1
        unlinked_here = ['/m/crops/c0.jpg', '/m/crops/b0.jpg']  # img_c and img_b were not copied
        assert answer == {
            'seen': objects,
            'added': objects,
            'skipped': 0,
            'objects_without_image': len(WITHOUT_IMAGE) + 1 + len(unlinked_here),
        }
        copied = await rows(w, TARGET)
        assert copied['/m/crops/d0.jpg']['parent_id'] == before  # the image the target had before the copy
        assert copied['/m/crops/a0.jpg']['parent_id'] == copied[IMAGES[5001]]['id']  # copied earlier
        for path in unlinked_here:
            assert copied[path]['metadata']['parent_id'] is None and copied[path]['parent_id'] is None

    world(scenario)


@needs_database
def test_a_copy_interrupted_in_its_images_resumes_with_images_before_objects(world, sdk, monkeypatch):
    async def scenario(w):
        await seed_source(w)
        client = sdk(w)
        source, target = handle(client, SOURCE, 'source'), handle(client, TARGET, 'target')
        real, calls = client.add_samples, []

        def interrupted(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise requests.ConnectionError('interrupted')
            return real(*args, **kwargs)

        monkeypatch.setattr(client, 'add_samples', interrupted)
        with pytest.raises(requests.ConnectionError):
            await asyncio.to_thread(target.add_samples_from, source, batch_size=2)
        written = await rows(w, TARGET)
        assert written and all(row['media_type'] != 'object' for row in written.values())
        monkeypatch.setattr(client, 'add_samples', real)

        answer = await asyncio.to_thread(target.add_samples_from, source, batch_size=2)

        assert answer['skipped'] == len(written) and answer['objects_without_image'] == len(WITHOUT_IMAGE) + 1
        copied = await rows(w, TARGET)
        for path, parent in LINKED.items():
            assert copied[path]['parent_id'] == copied[IMAGES[int(parent)]]['id'], path

    world(scenario)


@needs_database
def test_a_cut_view_copies_what_it_holds_and_links_its_objects(world, sdk):
    async def scenario(w):
        await seed_source(w)
        client = sdk(w)
        source = handle(client, SOURCE, 'source')
        whole = handle(client, TARGET, 'whole')
        crops_only = handle(client, OTHER, 'crops')

        answer = await asyncio.to_thread(whole.add_samples_from, source.all().limit(100), batch_size=4)
        assert answer['seen'] == len(IMAGES) + len(LINKED) + len(WITHOUT_IMAGE) + 2
        assert answer['objects_without_image'] == len(WITHOUT_IMAGE) + 1
        copied = await rows(w, TARGET)
        for path, parent in LINKED.items():
            assert copied[path]['parent_id'] == copied[IMAGES[int(parent)]]['id'], path

        # Sorted by path, the first three are objects whose images the view doesn't hold.
        view = source.filter(filepath__contains='/m/crops/').order_by('filepath').limit(3)
        answer = await asyncio.to_thread(crops_only.add_samples_from, view)
        assert answer == {'seen': 3, 'added': 3, 'skipped': 0, 'objects_without_image': 3}
        assert sorted(path for path in await rows(w, OTHER) if path.startswith('/m/')) == [
            '/m/crops/a0.jpg',
            '/m/crops/a1.jpg',
            '/m/crops/b0.jpg',
        ]

    world(scenario)


@needs_database
def test_a_rule_that_would_merge_two_samples_is_refused_before_either_pass(world, sdk):
    async def scenario(w):
        await seed_source(w)
        client = sdk(w)
        source, target = handle(client, SOURCE, 'source'), handle(client, TARGET, 'target')
        with pytest.raises(ValueError, match='same path'):
            await asyncio.to_thread(target.add_samples_from, source, path_prefixes={'/m/crops/a0.jpg': IMAGES[5001]})
        assert await rows(w, TARGET) == {}

    world(scenario)


@needs_database
def test_on_another_server_the_source_ids_never_name_the_targets_samples(world, sdk):
    """The source numbers its samples as the target's unrelated samples are numbered: a source id that reached the
    target would link an object to one of them (and delete it with it)."""

    async def scenario(w):
        await w.dataset(TARGET)
        unrelated = [await insert(w, TARGET, 7000 + n, f'/b/unrelated_{n}.jpg', 'image', {}) for n in (1, 2)]
        await w.database.execute("SELECT setval(pg_get_serial_sequence('samples', 'id'), 10000)")
        server = FakeServer()
        server.add_dataset(1)
        server.seed(1, 7001, '/m/img_a.jpg')
        server.seed(1, 7002, '/m/img_b.jpg')
        for sample_id, parent in ((7003, 7001), (7004, 7002), (7005, 7001)):
            server.seed(1, sample_id, f'/m/crops/{sample_id}.jpg', media_type='object', metadata={'parent_id': parent})
        source_client = Client.__new__(Client)
        source_client.base_url, source_client.session = 'http://source.invalid/api', server
        source = handle(source_client, 1, 'source')
        target = handle(sdk(w), TARGET, 'target')

        answer = await asyncio.to_thread(target.add_samples_from, source, path_prefixes={'/m': '/n'})

        assert answer == {'seen': 5, 'added': 5, 'skipped': 0, 'rewritten': 5, 'objects_without_image': 0}
        copied = await rows(w, TARGET)
        for sample_id, image in ((7003, '/n/img_a.jpg'), (7004, '/n/img_b.jpg'), (7005, '/n/img_a.jpg')):
            obj = copied[f'/n/crops/{sample_id}.jpg']
            assert obj['parent_id'] == copied[image]['id'] and obj['metadata']['parent_id'] == copied[image]['id']
            assert obj['parent_id'] not in unrelated
        assert (await unlinked(w, TARGET))['total'] == 0
        assert await objects_of_images(w, TARGET, 'unrelated') == 0

    world(scenario)


@needs_database
@pytest.mark.parametrize('rule', [None, {'/h': '/k'}])
def test_a_heading_without_a_location_is_kept(world, sdk, rule):
    """Contract 1.3: an upload or an edit can set a heading alone; the samples rows nested it in the location only,
    so a copy dropped it. Also in a copy of a cut view."""

    async def scenario(w):
        await w.dataset(SOURCE)
        await w.dataset(TARGET)
        await w.dataset(OTHER)
        added = await w.call(
            ADMIN,
            'POST',
            f'/api/datasets/{SOURCE}/samples',
            json={
                'samples': [
                    {'filepath': '/h/alone.jpg', 'heading': 45.0},
                    {
                        'filepath': '/h/both.jpg',
                        'heading': 90.0,
                        'location': {'type': 'Point', 'coordinates': [10, 20]},
                    },
                    {'filepath': '/h/edited.jpg'},
                    {'filepath': '/h/none.jpg'},
                ],
            },
        )
        assert added.status_code == 200, added.text
        edited = added.json()['ids'][2]
        patched = await w.call(ADMIN, 'PATCH', f'/api/datasets/{SOURCE}/samples/{edited}', json={'heading': 135.0})
        assert patched.status_code == 200, patched.text
        client = sdk(w)
        source = handle(client, SOURCE, 'source')
        headings = {'alone.jpg': 45.0, 'both.jpg': 90.0, 'edited.jpg': 135.0, 'none.jpg': None}

        for target_id, view in ((TARGET, source.all()), (OTHER, source.all().limit(10))):
            answer = await asyncio.to_thread(
                handle(client, target_id, 'copy').add_samples_from,
                view,
                path_prefixes=rule,
            )
            assert answer['added'] == 4
            copied = await w.database.fetch(
                'SELECT filepath, heading, location IS NOT NULL AS located FROM samples WHERE dataset_id = $1',
                target_id,
            )
            prefix = '/k/' if rule else '/h/'
            assert {row['filepath']: row['heading'] for row in copied} == {
                prefix + name: heading for name, heading in headings.items()
            }
            assert {row['filepath'] for row in copied if row['located']} == {prefix + 'both.jpg'}

    world(scenario)


@needs_database
@pytest.mark.parametrize('rule', [None, {'/m': 'photos'}])
def test_a_versions_view_copies_the_samples_as_the_version_holds_them(world, sdk, rule):
    """Contract 1.8: a version's view is copied from the version, never from the dataset's rows of today, with its
    objects linked to the copies of their images as the version had them."""

    async def scenario(w):
        await seed_source(w)
        made = await w.call(ADMIN, 'POST', f'/api/datasets/{SOURCE}/versions', json={'name': 'v1'})
        assert made.status_code == 200, made.text
        # The dataset changes after the version: an image's metadata, an object moved to another image, an image
        # deleted (its objects with it), a sample added.
        a1, img_b = OBJECT_IDS['/m/crops/a1.jpg'], 5002
        for sample_id, body in ((5001, {'metadata': {'state': 'later'}}), (a1, {'metadata': {'parent_id': 5004}})):
            changed = await w.call(ADMIN, 'PATCH', f'/api/datasets/{SOURCE}/samples/{sample_id}', json=body)
            assert changed.status_code == 200, changed.text
        deleted = await w.call(ADMIN, 'DELETE', f'/api/datasets/{SOURCE}/samples/{img_b}')
        assert deleted.status_code == 200, deleted.text
        await insert(w, SOURCE, None, '/m/later.jpg', 'image', {})
        client = sdk(w)
        version = await asyncio.to_thread(handle(client, SOURCE, 'source').version, 'v1')  # its request needs the loop
        held = {
            row['filepath']: row
            for row in await w.database.fetch(
                'SELECT sample_id, filepath, media_type, metadata FROM version_snapshots WHERE version_id = $1',
                made.json()['id'],
            )
        }

        answer = await asyncio.to_thread(handle(client, TARGET, 'copy').add_samples_from, version, path_prefixes=rule)

        total = len(IMAGES) + len(LINKED) + len(WITHOUT_IMAGE) + 2
        expected = {'seen': total, 'added': total, 'skipped': 0, 'objects_without_image': len(WITHOUT_IMAGE) + 1}
        if rule:
            expected['rewritten'] = total
        assert answer == expected
        copied = await rows(w, TARGET)
        assert sorted(copied) == sorted(new_path(path, rule) for path in held)
        for path, row in held.items():
            if row['media_type'] != 'object':
                assert copied[new_path(path, rule)]['metadata'] == json_metadata(row['metadata'], rule), path
        for path, parent in LINKED.items():  # as the version had them: a1 under img_a, b0 under the deleted img_b
            image = copied[new_path(IMAGES[int(parent)], rule)]
            assert copied[new_path(path, rule)]['parent_id'] == image['id'], path

        # A version's view with a limit: the first eight samples by id; c0's image (img_c) is in the version but
        # not in this view, nor in the target, so c0 has none.
        limited = await asyncio.to_thread(handle(client, OTHER, 'some').add_samples_from, version.limit(8))
        assert limited == {'seen': 8, 'added': 8, 'skipped': 0, 'objects_without_image': len(WITHOUT_IMAGE) + 2}
        some = await rows(w, OTHER)
        assert some['/m/crops/c0.jpg']['parent_id'] is None
        assert '/m/later.jpg' not in some and IMAGES[5002] in some

    world(scenario)


def json_metadata(metadata: Any, rule: Optional[dict]) -> Any:
    """A version row's metadata as the copy writes it (the rule's start replaced in text values)."""
    if isinstance(metadata, dict):
        return {key: json_metadata(value, rule) for key, value in metadata.items()}
    if isinstance(metadata, str) and rule and metadata.startswith('/m/'):
        return new_path(metadata, rule)
    return metadata


def test_a_source_of_another_kind_is_refused_before_anything_is_read():
    server, source, target = fake_world()
    for other in (SampleQuery(source), object(), [Sample('/m/x.jpg')]):  # dataset.samples is a SampleQuery
        with pytest.raises(TypeError, match="a version's view"):
            target.add_samples_from(other)
    assert server.requests == []


# --- Without a database: the fake server alone ---


def fake_world():
    server = FakeServer()
    client = Client.__new__(Client)
    client.base_url, client.session = 'http://test.invalid/api', server
    server.add_dataset(1)
    server.add_dataset(2)
    server.seed(1, 10, '/m/img_a.jpg')
    server.seed(1, 11, '/m/img_b.jpg')
    server.seed(1, 5, '/m/crops/a0.jpg', media_type='object', metadata={'parent_id': 10})
    server.seed(1, 12, '/m/crops/b0.jpg', media_type='object', metadata={'parent_id': 11})
    return server, handle(client, 1, 'source'), handle(client, 2, 'target')


def test_a_similarity_result_copies_its_images_before_its_objects():
    server, source, target = fake_world()
    order = [server.rows[1][sample_id] for sample_id in (5, 10, 12)]  # an object first; img_b isn't in it
    similar = SimilarityResultView([Sample.from_row(row, source) for row in order], source)

    answer = target.add_samples_from(similar, path_prefixes={'/m': 'photos'})

    assert answer == {'seen': 3, 'added': 3, 'skipped': 0, 'rewritten': 3, 'objects_without_image': 1}
    posts = [body['samples'] for url, body in server.requests if url.endswith('/datasets/2/samples')]
    assert [[row['filepath'] for row in post] for post in posts] == [
        ['photos/img_a.jpg'],
        ['photos/crops/a0.jpg'],
        ['photos/crops/b0.jpg'],
    ]
    copied = {row['filepath']: row for row in server.rows[2].values()}
    assert copied['photos/crops/a0.jpg']['metadata']['parent_id'] == copied['photos/img_a.jpg']['id']
    assert copied['photos/crops/b0.jpg']['metadata']['parent_id'] is None


def test_a_view_without_objects_costs_one_count_and_answers_as_before():
    server, source, target = fake_world()

    answer = target.add_samples_from(source.filter(media_type='image'))

    assert answer == {'seen': 2, 'added': 2, 'skipped': 0}
    queries = [body for url, body in server.requests if url.endswith('/datasets/1/samples/query')]
    counts = [body for body in queries if body['limit'] == 0]
    assert len(counts) == 1
    posts = [url for url, _ in server.requests if url.endswith('/datasets/2/samples')]
    assert len(posts) == 1
