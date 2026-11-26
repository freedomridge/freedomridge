"""A sample's path does not change; a copy can give it a new one (contract g2-contract-paths-do-not-change).

Written from the contract, not from the code, and tested where a user meets it: the SDK driven as a user drives it,
its requests served by the whole app on a database of its own per test (the access-rule tests' World with real API
tokens, as test_protected_metadata_keys_spec drives Sample.save()), and the HTTP API signed in with a real API token
for the server's edit of one sample. What the database holds is read from it directly. The docs' side is
test_paths_do_not_change_docs_spec.py; python/fridge/test_sdk.py has the same rules against a running server.

The rule, in short: `Dataset.add_samples_from(source, ..., path_prefixes={old: new})` replaces the start of each
copied sample's `filepath`, and of every text value inside its metadata, that starts with an `old` at a folder
boundary; the longest `old` wins; nothing else of a sample changes; a sample is matched in the target by its new path;
a rule that would give two source samples the same path, and a bad rule, are refused before anything is written; the
answer gains `rewritten`. `sample.save()` after a changed `filepath` raises a ValueError (`filepath can't be changed`)
and sends nothing; a PATCH with a different `filepath` is a 422.

Where the contract leaves something open, these tests read it this way, or leave it alone:
- 1.1/1.7: without the argument, and with `{}`, the answer is exactly `{seen, added, skipped}` (compared whole) and
  the target holds what today's copy gives; with a rule the answer has `rewritten`, 0 when no added sample got a new
  path (a rule that fits nothing included).
- 1.2: a path equal to `old` (no `/` after it) is replaced by `new` alone; a trailing `/` on either side changes
  nothing, also for a path equal to `old`. An empty `new` is the data root (`/media/drive/photos/a.jpg` becomes
  `photos/a.jpg`, with a trailing `/` on `old` too), and a sample whose path is exactly that `old` is refused up front
  as in 1.5 (a ValueError naming the path, nothing written); a `new` of `/` or `s3://` keeps its slashes (`/a.jpg`,
  `s3://bucket/a.jpg`). Text is compared exactly (case and leading spaces count). An `old` that is only `/` or a scheme
  root (the boundary words don't say how `old` followed by `/` reads there), a metadata text exactly equal to an `old`
  whose `new` is empty, a `new` that equals `old` and other spellings of the same file (relative against full) are not
  settled and not tested.
- 1.3: only values are replaced, never keys; values in lists and in nested objects are replaced; a text that merely
  contains an `old` after its start, or starts with it without a folder boundary, numbers, booleans, null and empty
  text stay. Tags and array field values that look like paths are "everything else" and stay. An object row's
  `metadata.parent_filepath` is a text value like any other; its `metadata.parent_id` is section 5's.
- 1.5: "the same path" is the samples' `filepath` after the rule (equal metadata paths in two samples, a shared
  calibration file, are not a collision). Refused: a replaced path equal to an unchanged sample's path (in either id
  order), two `old`s mapped to one `new` with a file of the same name under each, nested `new`s, an `old` inside
  another whose `new`s meet, a full path made relative onto an existing relative one, and a path equal to `old`. The
  error "names an example" when it contains the colliding path or one of the two source paths. Whether a rule whose
  `new`s chain or swap (`a -> b`, `b -> a`) is refused is not settled ("another sample's path" may be read before or
  after the rule), nor whether two `old`s mapped to one `new` are refused when no two files meet, nor which samples
  count when the source is a view: none is tested, nor a sample added to the source during the copy (not promised).
  "Before it writes anything" is checked as: no sample added to the
  target, the target's samples left exactly as they were, and no array field registered on the target, with the
  colliding pair placed in the first and the last batch.
- 1.6: refused before anything is read or written is checked as no request at all sent by the SDK during the call,
  and nothing in the target. "Not a dict" is a list of pairs, a tuple, a string and a set; "not text" is a number,
  None or a list as an `old` or a `new`. A dict subclass, an os.PathLike and bytes are not settled and not tested.
- 1.4 is checked by running the same copy again (everything skipped, the target's rows unchanged to the
  microsecond), by a copy of part of the source followed by the whole, and against a target that already holds a
  sample at the new path (skipped, left as it is) or at the old path (not a match: the copy is added beside it).
- 2.1: "came from the server" is a sample read by `dataset[filepath]`, by `iter_samples()` or by `view.first()`;
  "nothing is sent" is no request at all during save(); "says what to do instead" is the message naming
  `add_samples_from` and `path_prefixes`. After a refused save, putting the path back makes save() work as today.
- 2.3: "accepted as today" is a 200 with the rest of the body written, for the path alone, the path with metadata,
  and the whole sample as the API reads it, sent back with one metadata key changed. A different path sent alone and
  with other changes is a 422 whose `detail` is a text containing the phrase, and the row (to `last_modified_at`) is
  unchanged. Tags with the sample's own path sent back, by a user with the tag level only, is accepted and writes the
  tags; the path alone at that level is not settled (an empty edit needs edit today). What every level may do with a
  path is also test_access_enforcement_spec.py's test_a_sample_patch_never_changes_the_filepath_at_any_level.
- 5.1 is checked through the app's own surfaces, by names planted in metadata (`img`, `obj`), never by id: each
  image's objects (the pivot `parent_id in [image]`), the filters across the grains as the grid builds them
  (`has_matching_object`, `parent_matches`), the unlinked-objects finder (none, and none it had to link itself), and
  deleting a copied image (its copied objects go with it, as in the source). Images and objects fall in different
  batches; an object added before its image (a lower id) and pointed at it after is linked too.
- 5.2: "no `parent_id`" is `metadata.parent_id` null or absent. Counted in `objects_without_image`: an object whose
  image is outside the view and not in the target; one whose `parent_id` names nothing, an object, a sample of a
  third dataset or a sample of the target itself (this last one is how main's "linked to an unrelated sample" shows
  on one server); one that has no `parent_id`, null or absent; one whose `parent_id` is not a sample id. The key is
  checked present (0 included) whenever the copied view holds objects and absent for a view of images alone. "No
  copied object carries a source id" is checked for the objects the copy writes (ids are one serial per server, so no
  target id can equal a source id by chance).
- 5.3: an interruption is simulated by copying first a view of images and objects whose images are in it (what a run
  that writes images first leaves); an object an earlier SDK left (naming the source's image) is skipped and left as
  it was. 5.4: a non-object keeps its `parent_id`, also when it names an image of the source itself. 5.5: a view cut
  by `limit` and by `skip` is tested; a similarity result and the cost are not.
- 1.8, a version's view (`dataset.version(name)`): the dataset is changed after the version is saved (a sample's
  metadata and tags edited, a sample deleted, one added), and the copy must hold the version's samples as it holds
  them, compared by planted names, paths, tags and metadata. With objects, an image deleted since (and its objects
  with it) must still be the one its objects link to in the copy. A collision is judged on the version's samples: one
  in the version is refused, one only in the dataset's current rows is not. A version's view limited with `limit` is
  tested; vectors and hashes of a version's samples are not compared (a version doesn't store them).
- Not tested from here: the cost note of 1.5 (a report), 2.4 ("nothing else about edits changes": the rest of the
  suite), a copy between two servers (no test here runs two apps at once).
"""

import asyncio
import os
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

import httpx
import pytest
from fridge.core import client as client_module
from fridge.core.client import Client
from fridge.core.dataset import Dataset
from fridge.core.sample import Sample

from backend.tests import test_regions_spec, test_upgrade_paths
from backend.tests.test_access_rule_spec import ADMIN, POWER
from backend.tests.test_protected_metadata_keys_spec import AsgiAdapter
from backend.tests.test_regions_spec import Regions

database = test_upgrade_paths.database
regions_world = test_regions_spec.regions_world
pytestmark = pytest.mark.skipif(
    not (os.getenv('FRIDGE_TEST_POSTGRES_DSN') and os.getenv('FRIDGE_TEST_POSTGRES_CONTAINER')),
    reason='Requires disposable PostgreSQL',
)

OLD, NEW = '/media/old', '/media/new'
PHRASE = "filepath can't be changed"
# What a copy carries besides the paths (1.3): copied as today.
CARRIED = ('media_type', 'tags', 'location', 'heading', 'labels', 'vectors', 'hashes')


# --- The SDK, signed in as the admin, its requests served by the app ---


class Recorder(AsgiAdapter):
    """The app serving the SDK's requests, keeping the method and path of each one sent."""

    def __init__(self, loop: asyncio.AbstractEventLoop, client: httpx.AsyncClient):
        super().__init__(loop, client)
        self.sent: list[tuple[str, str]] = []

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):  # type: ignore[override]
        self.sent.append((str(request.method), urlsplit(str(request.url)).path))
        return super().send(request, stream=stream, timeout=timeout, verify=verify, cert=cert, proxies=proxies)


def signed_in(r: Regions, monkeypatch: Any) -> Recorder:
    """The SDK's global client, signed in with the admin's API token; returns the record of what it sends."""
    recorder = Recorder(asyncio.get_running_loop(), r.client)
    client = Client('http://test/api', api_token=r.tokens[ADMIN])
    client.session.mount('http://test', recorder)
    monkeypatch.setattr(client_module, '_client', client)
    monkeypatch.setattr(Sample, '_known_array_fields', set())
    return recorder


async def sdk(call: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """`call` in a thread of its own, as a user's script runs it beside the server."""
    return await asyncio.to_thread(call, *args, **kwargs)


def make(name: str, samples: list[Sample], array_field: bool = True) -> Dataset:
    """A dataset made through the SDK, with the `labels` array field registered (unless not wanted) and `samples`."""
    ds = Dataset(name)
    if array_field:
        ds.create_array_field('labels')
    if samples:
        ds.add_samples(samples)
    return ds


async def stored(r: Regions, dataset_id: int) -> dict[str, dict[str, Any]]:
    """Each sample of the dataset by its filepath, as the database holds it, with its vectors and hashes."""
    rows = await r.database.fetch(
        """SELECT id, filepath, media_type, tags, metadata, ST_AsText(location) AS location, heading, labels,
                  created_at, last_modified_at
           FROM samples WHERE dataset_id = $1 ORDER BY id""",
        dataset_id,
    )
    samples = {row['id']: {**dict(row), 'vectors': {}, 'hashes': {}} for row in rows}
    vectors = await r.database.fetch(
        'SELECT sample_id, field_name, vector::text AS vector FROM embeddings WHERE dataset_id = $1',
        dataset_id,
    )
    for row in vectors:
        samples[row['sample_id']]['vectors'][row['field_name']] = row['vector']
    hashes = await r.database.fetch(
        """SELECT h.sample_id, h.field_name, h.hash_hex FROM sample_hashes h JOIN samples s ON s.id = h.sample_id
           WHERE s.dataset_id = $1""",
        dataset_id,
    )
    for row in hashes:
        samples[row['sample_id']]['hashes'][row['field_name']] = row['hash_hex']
    return {sample['filepath']: sample for sample in samples.values()}


async def array_fields(r: Regions, dataset_id: int) -> list[str]:
    """The array fields registered on the dataset, besides `tags` (every dataset's)."""
    response = await r.call(ADMIN, 'GET', f'/api/datasets/{dataset_id}/array-fields')
    assert response.status_code == 200, response.text
    return sorted(field['name'] for field in response.json() if field['name'] != 'tags')


def counts(result: dict[str, Any]) -> dict[str, Any]:
    """The answer's counts that mean what they mean today (1.7)."""
    return {key: result.get(key) for key in ('seen', 'added', 'skipped')}


def media(name: str, root: str) -> dict[str, Any]:
    """A sample's other media paths under `root` at several depths, beside values that look like paths and must stay as
    they are whatever the rule."""
    return {
        'thumbnail_path': f'{root}/thumbs/{name}.jpg',  # top level
        'mask': {'maskPath': f'{root}/masks/{name}.png', 'classes': 3},  # in an object
        'heat': {'type': 'heatmap', 'map_path': f'{root}/heat/{name}.png', 'range': [0.0, 1.0]},
        'lidar': [{'path': f'{root}/lidar/{name}.bin'}, {'path': 's3://bucket/lidar/shared.bin'}],  # objects in a list
        'sources': [f'{root}/raw/{name}.raw', f'/media/older/raw/{name}.raw', 5, None],  # texts in a list
        'deep': {'a': {'b': [{'c': [f'{root}/deep/{name}.txt']}]}},  # five levels down
        'parent': {'filepath': f'{root}/parents/{name}.jpg', 'id': 12},  # a nested key named filepath
        'calibration': f'{root}/calibration.json',  # the same in every sample: not a collision
        'root': root,  # the prefix itself
        'by_path': {f'/media/old/keys/{name}.jpg': 'a key named like a path', '/media/old/k': f'{root}/v/{name}.jpg'},
        # Look-alikes:
        'note': f'copied from /media/old/{name}.jpg',  # the prefix after the start
        'older': f'/media/older/{name}.jpg',  # no folder boundary
        'oldish': '/media/oldish',
        'dotted': '/media/old.jpg',
        'spaced': f' /media/old/{name}.jpg',  # a leading space
        'shouted': f'/MEDIA/OLD/{name}.jpg',
        'count': 7,
        'ratio': 0.5,
        'flag': True,
        'nothing': None,
        'empty': '',
    }


def rich_source() -> list[Sample]:
    """Samples with full local, relative and cloud paths, media paths in metadata, vectors, hashes, tags and an array
    field."""
    return [
        Sample(
            filepath='/media/old/a.jpg',
            tags=['keep', '/media/old/tagged'],
            metadata=media('a', OLD),
            location=(-93.5, 41.2),
            heading=90.0,
            embeddings={'paths_vec': [1.0, 2.0, 3.0]},
            hashes={'phash': '00ff00ff00ff00ff'},
            array_fields={'labels': ['cat', '/media/old/label']},
        ),
        Sample(
            filepath='/media/old/sub/b.png',
            metadata=media('b', OLD),
            embeddings={'paths_vec': [4.0, 5.0, 6.0]},
            hashes={'phash': 'ff00ff00ff00ff00'},
        ),
        Sample(filepath='/media/older/c.jpg', metadata=media('c', OLD), tags=['older']),
        Sample(filepath='x/media/old/d.jpg', metadata={'n': 1, 'same': 'x/media/old/d.jpg'}),
        Sample(filepath='s3://bucket/e.jpg', metadata={'mask': 's3://bucket/e.png'}, location=(10.0, 20.0)),
        # (A heading without a location: test_a_heading_without_a_location_is_kept.)
        Sample(
            filepath='/media/old/clips/v.mp4',
            metadata={'frames': f'{OLD}/clips/v/'},
            location=(5.0, 6.0),
            heading=12.5,
        ),
        Sample(filepath='/media/old/My Photos/ü 1.JPG', metadata={'raw': '/media/old/My Photos/ü 1.CR2'}),
        Sample(filepath='/media/old/media/old/twice.jpg', metadata={'twice': '/media/old/media/old/twice.png'}),
    ]


# Where each of rich_source()'s samples lands with {OLD: NEW}, and its metadata there.
RICH_MOVED = {
    '/media/old/a.jpg': ('/media/new/a.jpg', media('a', NEW)),
    '/media/old/sub/b.png': ('/media/new/sub/b.png', media('b', NEW)),
    '/media/older/c.jpg': ('/media/older/c.jpg', media('c', NEW)),
    'x/media/old/d.jpg': ('x/media/old/d.jpg', {'n': 1, 'same': 'x/media/old/d.jpg'}),
    's3://bucket/e.jpg': ('s3://bucket/e.jpg', {'mask': 's3://bucket/e.png'}),
    '/media/old/clips/v.mp4': ('/media/new/clips/v.mp4', {'frames': '/media/new/clips/v/'}),
    '/media/old/My Photos/ü 1.JPG': ('/media/new/My Photos/ü 1.JPG', {'raw': '/media/new/My Photos/ü 1.CR2'}),
    '/media/old/media/old/twice.jpg': ('/media/new/media/old/twice.jpg', {'twice': '/media/new/media/old/twice.png'}),
}
RICH_REWRITTEN = 5  # the samples whose filepath changes: all but c (no folder boundary), d and e (no rule fits)


def assert_copied(source: dict[str, dict], target: dict[str, dict], moved: dict[str, tuple[str, Any]]) -> None:
    """Each source sample is in the target at its new path with its new metadata, and the rest carried as it was."""
    assert sorted(target) == sorted(path for path, _ in moved.values()), sorted(target)
    for old_path, (new_path, metadata) in moved.items():
        copy, original = target[new_path], source[old_path]
        assert copy['metadata'] == metadata, (old_path, copy['metadata'])
        for key in CARRIED:
            assert copy[key] == original[key], (old_path, key, copy[key], original[key])


# --- 1. The copy gives the copies new paths ---


def test_without_the_argument_the_copy_is_todays(regions_world, monkeypatch):
    """1.1, a pin (passes today): no `path_prefixes`, every path and metadata value is copied as it is, with everything
    else, and the answer counts as today."""

    async def scenario(r):
        signed_in(r, monkeypatch)
        source = await sdk(make, 'source', rich_source())
        target = await sdk(make, 'target', [], False)
        result = await sdk(target.add_samples_from, source)
        assert result == {'seen': 8, 'added': 8, 'skipped': 0}, result
        before, after = await stored(r, source.id), await stored(r, target.id)
        assert_copied(before, after, {path: (path, row['metadata']) for path, row in before.items()})
        assert await array_fields(r, target.id) == ['labels']

    regions_world(scenario)


def test_an_empty_rule_is_the_same_as_no_rule(regions_world, monkeypatch):
    """1.6 and 1.7: `path_prefixes={}` copies exactly what a copy without it copies, and answers exactly as today."""

    async def scenario(r):
        signed_in(r, monkeypatch)
        source = await sdk(make, 'source', rich_source())
        plain = await sdk(make, 'plain', [], False)
        empty = await sdk(make, 'empty', [], False)
        without = await sdk(plain.add_samples_from, source)
        with_empty = await sdk(empty.add_samples_from, source, path_prefixes={})
        assert with_empty == without == {'seen': 8, 'added': 8, 'skipped': 0}, (with_empty, without)
        before = await stored(r, source.id)
        assert_copied(
            before,
            await stored(r, empty.id),
            {path: (path, row['metadata']) for path, row in before.items()},
        )

    regions_world(scenario)


def test_a_rule_gives_the_copies_new_paths_in_filepath_and_metadata_and_carries_the_rest(regions_world, monkeypatch):
    """1.2, 1.3 and 1.7: each filepath and each text inside metadata (in objects and lists, at any depth) that starts
    with the old folder gets the new one, the rest of it kept exactly; look-alikes, keys, numbers, tags and array
    field values stay; vectors, hashes, location, heading and media type are carried as today; `rewritten` counts the
    added samples whose filepath changed (not those whose metadata alone changed)."""

    async def scenario(r):
        signed_in(r, monkeypatch)
        source = await sdk(make, 'source', rich_source())
        target = await sdk(make, 'target', [], False)
        result = await sdk(target.add_samples_from, source, path_prefixes={OLD: NEW}, batch_size=3)
        assert counts(result) == {'seen': 8, 'added': 8, 'skipped': 0}, result
        assert result.get('rewritten') == RICH_REWRITTEN, result
        assert_copied(await stored(r, source.id), await stored(r, target.id), RICH_MOVED)
        assert await array_fields(r, target.id) == ['labels']

    regions_world(scenario)


def test_the_old_folder_is_matched_at_a_folder_boundary_and_only_at_the_start(regions_world, monkeypatch):
    """1.2: a path equal to `old`, or `old` followed by `/`, is replaced; `/media/older/...`, `/media/old.jpg` and
    `/media/old_2/...` are not; a second `old` later in the path stays."""
    paths = {
        '/media/old': '/media/new',
        '/media/old/a.jpg': '/media/new/a.jpg',
        '/media/older/a.jpg': '/media/older/a.jpg',
        '/media/old.jpg': '/media/old.jpg',
        '/media/old_2/a.jpg': '/media/old_2/a.jpg',
        '/media/oldies': '/media/oldies',
        '/media/old/x/media/old/a.jpg': '/media/new/x/media/old/a.jpg',
    }
    texts = list(paths)

    async def scenario(r):
        signed_in(r, monkeypatch)
        source = await sdk(make, 'source', [Sample(filepath=path, metadata={'paths': texts}) for path in paths])
        target = await sdk(make, 'target', [], False)
        result = await sdk(target.add_samples_from, source, path_prefixes={OLD: NEW})
        assert counts(result) == {'seen': 7, 'added': 7, 'skipped': 0}, result
        assert result.get('rewritten') == 3, result
        moved = {old: (new, {'paths': list(paths.values())}) for old, new in paths.items()}
        assert_copied(await stored(r, source.id), await stored(r, target.id), moved)

    regions_world(scenario)


@pytest.mark.parametrize(
    'rule',
    [{'/media/old/': '/media/new'}, {'/media/old': '/media/new/'}, {'/media/old/': '/media/new/'}],
    ids=['old-slash', 'new-slash', 'both-slash'],
)
def test_a_trailing_slash_on_old_or_new_makes_no_difference(regions_world, monkeypatch, rule):
    """1.2: with a trailing `/` on `old`, on `new` or on both, the copy is the one `{'/media/old': '/media/new'}`
    gives: no doubled and no lost `/`, a path equal to the folder included."""
    paths = {
        '/media/old/a.jpg': '/media/new/a.jpg',
        '/media/old/sub/b.jpg': '/media/new/sub/b.jpg',
        '/media/old': '/media/new',
        '/media/older/c.jpg': '/media/older/c.jpg',
    }

    async def scenario(r):
        signed_in(r, monkeypatch)
        samples = [Sample(filepath=path, metadata={'same': path, 'list': [path]}) for path in paths]
        source = await sdk(make, 'source', samples)
        target = await sdk(make, 'target', [], False)
        result = await sdk(target.add_samples_from, source, path_prefixes=rule)
        assert result.get('rewritten') == 3, result
        moved = {old: (new, {'same': new, 'list': [new]}) for old, new in paths.items()}
        assert_copied(await stored(r, source.id), await stored(r, target.id), moved)

    regions_world(scenario)


@pytest.mark.parametrize('longest_first', [True, False], ids=['longest-first', 'shortest-first'])
def test_the_longest_old_that_fits_wins_whatever_the_order(regions_world, monkeypatch, longest_first):
    """1.2: with `/media/old` and `/media/old/sub` both in the rule, a path under `/media/old/sub` takes the longer
    one's `new`, and `/media/old/subway/...` (no boundary for the longer one) the shorter one's; the order of the
    dict doesn't matter."""
    rules = [('/media/old/sub', 's3://bucket/b'), ('/media/old', '/media/a')]
    rule = dict(rules if longest_first else reversed(rules))
    paths = {
        '/media/old/sub/x.jpg': 's3://bucket/b/x.jpg',
        '/media/old/sub': 's3://bucket/b',
        '/media/old/subway/x.jpg': '/media/a/subway/x.jpg',
        '/media/old/y.jpg': '/media/a/y.jpg',
    }
    texts = {'m': '/media/old/sub/m.png', 'n': '/media/old/n.png', 'w': '/media/old/subway/w.png'}
    moved_texts = {'m': 's3://bucket/b/m.png', 'n': '/media/a/n.png', 'w': '/media/a/subway/w.png'}

    async def scenario(r):
        signed_in(r, monkeypatch)
        source = await sdk(make, 'source', [Sample(filepath=path, metadata=texts) for path in paths])
        target = await sdk(make, 'target', [], False)
        result = await sdk(target.add_samples_from, source, path_prefixes=rule)
        assert result.get('rewritten') == 4, result
        moved = {old: (new, moved_texts) for old, new in paths.items()}
        assert_copied(await stored(r, source.id), await stored(r, target.id), moved)

    regions_world(scenario)


KINDS = {
    'local-to-local': (
        {'/media/old': '/mnt/archive/2024'},
        {'/media/old/a.jpg': '/mnt/archive/2024/a.jpg', 's3://bucket/a.jpg': 's3://bucket/a.jpg'},
    ),
    'local-to-relative': (
        {'/media/drive/photos': 'photos'},
        {
            '/media/drive/photos/a/b.jpg': 'photos/a/b.jpg',
            '/media/drive/photos2/c.jpg': '/media/drive/photos2/c.jpg',
            'photos-old/d.jpg': 'photos-old/d.jpg',
        },
    ),
    'local-to-cloud': (
        {'/media/old': 's3://bucket/prefix'},
        {'/media/old/a/b.jpg': 's3://bucket/prefix/a/b.jpg', '/media/older/c.jpg': '/media/older/c.jpg'},
    ),
    'relative-to-local': (
        {'photos': '/media/drive/photos'},
        {
            'photos/a.jpg': '/media/drive/photos/a.jpg',
            'photos2/a.jpg': 'photos2/a.jpg',
            'old/photos/a.jpg': 'old/photos/a.jpg',
            '/photos/a.jpg': '/photos/a.jpg',
        },
    ),
    'relative-to-relative': (
        {'photos/2024': 'archive/2024'},
        {'photos/2024/a.jpg': 'archive/2024/a.jpg', 'photos/2025/a.jpg': 'photos/2025/a.jpg'},
    ),
    'cloud-to-local': (
        {'s3://bucket/prefix': '/media/new'},
        {
            's3://bucket/prefix/a.jpg': '/media/new/a.jpg',
            's3://bucket/prefix2/a.jpg': 's3://bucket/prefix2/a.jpg',
            's3://bucket/a.jpg': 's3://bucket/a.jpg',
        },
    ),
    'cloud-to-cloud': (
        {'s3://old-bucket': 's3://new-bucket/archive'},
        {
            's3://old-bucket/a/b.jpg': 's3://new-bucket/archive/a/b.jpg',
            's3://old-bucket-2/a.jpg': 's3://old-bucket-2/a.jpg',
        },
    ),
    'local-to-data-root': (
        {'/media/drive': ''},
        {'/media/drive/photos/a.jpg': 'photos/a.jpg', '/media/drive2/b.jpg': '/media/drive2/b.jpg'},
    ),
    'old-slash-to-data-root': (
        {'/media/drive/': ''},
        {'/media/drive/photos/a.jpg': 'photos/a.jpg', '/media/drive/c.jpg': 'c.jpg'},
    ),
    'local-to-filesystem-root': (
        {'/media/old': '/'},
        {
            '/media/old/a.jpg': '/a.jpg',
            '/media/old/sub/b.jpg': '/sub/b.jpg',
            '/media/older/c.jpg': '/media/older/c.jpg',
        },
    ),
    'local-to-scheme-root': (
        {'/media/old': 's3://'},
        {'/media/old/bucket/a.jpg': 's3://bucket/a.jpg', '/media/older/c.jpg': '/media/older/c.jpg'},
    ),
    'cloud-to-relative': (
        {'s3://bucket/photos': 'photos'},
        {'s3://bucket/photos/a.jpg': 'photos/a.jpg', 's3://bucket/photosets/a.jpg': 's3://bucket/photosets/a.jpg'},
    ),
}


@pytest.mark.parametrize('kind', list(KINDS))
def test_each_kind_of_old_and_new(regions_world, monkeypatch, kind):
    """1.2: `old` and `new` may each be a full local folder, a path relative to the data root or a cloud location;
    a path no `old` fits is copied unchanged. Checked in filepath and in a metadata text equal to it."""
    rule, paths = KINDS[kind]

    async def scenario(r):
        signed_in(r, monkeypatch)
        source = await sdk(make, 'source', [Sample(filepath=path, metadata={'same': [path]}) for path in paths])
        target = await sdk(make, 'target', [], False)
        result = await sdk(target.add_samples_from, source, path_prefixes=rule)
        assert counts(result) == {'seen': len(paths), 'added': len(paths), 'skipped': 0}, result
        assert result.get('rewritten') == sum(old != new for old, new in paths.items()), result
        moved = {old: (new, {'same': [new]}) for old, new in paths.items()}
        assert_copied(await stored(r, source.id), await stored(r, target.id), moved)

    regions_world(scenario)


def test_an_object_rows_parent_filepath_moves_with_it(regions_world, monkeypatch):
    """1.3, 4.1 and 5.1: an object row (its own path, `metadata.parent_id` and `parent_filepath`) gets its new path and
    a new `parent_filepath`, and its `parent_id` names the copy of its image."""

    async def scenario(r):
        signed_in(r, monkeypatch)
        source = await sdk(make, 'source', [Sample(filepath='/media/old/img.jpg')])
        image = (await stored(r, source.id))['/media/old/img.jpg']['id']
        crop = Sample(
            filepath='/media/old/crops/img_0.png',
            metadata={'parent_id': image, 'parent_filepath': '/media/old/img.jpg', 'crop_bbox': [0.1, 0.1, 0.5, 0.5]},
        )
        crop.media_type = 'object'
        await sdk(source.add_samples, [crop])
        target = await sdk(make, 'target', [], False)
        result = await sdk(target.add_samples_from, source, path_prefixes={OLD: NEW})
        assert result == {'seen': 2, 'added': 2, 'skipped': 0, 'rewritten': 2, 'objects_without_image': 0}, result
        copied = await stored(r, target.id)
        assert sorted(copied) == ['/media/new/crops/img_0.png', '/media/new/img.jpg'], sorted(copied)
        assert copied['/media/new/crops/img_0.png']['metadata'] == {
            'parent_id': copied['/media/new/img.jpg']['id'],
            'parent_filepath': '/media/new/img.jpg',
            'crop_bbox': [0.1, 0.1, 0.5, 0.5],
        }
        assert copied['/media/new/crops/img_0.png']['media_type'] == 'object'

    regions_world(scenario)


def test_with_a_rule_that_fits_nothing_the_answer_has_rewritten_0(regions_world, monkeypatch):
    """1.7: with a rule, `rewritten` is in the answer, 0 when no added sample got a new path; the copy is today's."""

    async def scenario(r):
        signed_in(r, monkeypatch)
        source = await sdk(make, 'source', rich_source())
        target = await sdk(make, 'target', [], False)
        result = await sdk(target.add_samples_from, source, path_prefixes={'/somewhere/else': '/media/new'})
        assert result == {'seen': 8, 'added': 8, 'skipped': 0, 'rewritten': 0}, result
        before = await stored(r, source.id)
        assert_copied(
            before,
            await stored(r, target.id),
            {path: (path, row['metadata']) for path, row in before.items()},
        )

    regions_world(scenario)


# --- 1.4: what is already copied is recognised by its new path ---


def test_running_the_same_copy_again_skips_everything_and_changes_nothing(regions_world, monkeypatch):
    """1.4 and 1.7: a second run with the same arguments adds nothing, counts every sample skipped, rewrites none, and
    leaves the target's rows exactly as they were."""

    async def scenario(r):
        signed_in(r, monkeypatch)
        source = await sdk(make, 'source', rich_source())
        target = await sdk(make, 'target', [], False)
        await sdk(target.add_samples_from, source, path_prefixes={OLD: NEW}, batch_size=3)
        first = await stored(r, target.id)
        again = await sdk(target.add_samples_from, source, path_prefixes={OLD: NEW}, batch_size=3)
        assert counts(again) == {'seen': 8, 'added': 0, 'skipped': 8}, again
        assert again.get('rewritten') == 0, again
        assert await stored(r, target.id) == first

    regions_world(scenario)


def test_an_interrupted_copy_run_again_skips_what_it_already_copied(regions_world, monkeypatch):
    """1.4: part of the source copied with the rule (as an interrupted run leaves it), then the whole with the same
    rule: the part is skipped, the rest added, each sample once at its new path."""

    async def scenario(r):
        signed_in(r, monkeypatch)
        source = await sdk(make, 'source', rich_source())
        target = await sdk(make, 'target', [], False)
        ids = sorted(row['id'] for row in (await stored(r, source.id)).values())
        part = await sdk(target.add_samples_from, source.filter(id__in=ids[:3]), path_prefixes={OLD: NEW})
        assert counts(part) == {'seen': 3, 'added': 3, 'skipped': 0}, part
        rest = await sdk(target.add_samples_from, source, path_prefixes={OLD: NEW}, batch_size=2)
        assert counts(rest) == {'seen': 8, 'added': 5, 'skipped': 3}, rest
        assert_copied(await stored(r, source.id), await stored(r, target.id), RICH_MOVED)

    regions_world(scenario)


def test_a_target_sample_at_the_new_path_is_skipped_and_one_at_the_old_path_is_not_a_match(regions_world, monkeypatch):
    """1.4 and 1.7: the target already holds a sample at a copy's new path: it is skipped and left exactly as it is,
    and not counted in `rewritten`. A target sample at another copy's OLD path doesn't match it: the copy is added
    at its new path beside it, which stays as it is."""

    async def scenario(r):
        signed_in(r, monkeypatch)
        source = await sdk(
            make,
            'source',
            [
                Sample(filepath='/media/old/a.jpg', tags=['from-source'], metadata={'k': 1}),
                Sample(filepath='/media/old/b.jpg', tags=['from-source'], metadata={'k': 2}),
                Sample(filepath='/media/old/c.jpg', metadata={'k': 3}),
            ],
        )
        held = [
            Sample(filepath='/media/new/a.jpg', tags=['mine'], metadata={'mine': 1}),
            Sample(filepath='/media/old/b.jpg', tags=['mine'], metadata={'mine': 2}),
        ]
        target = await sdk(make, 'target', held)
        before = await stored(r, target.id)
        result = await sdk(target.add_samples_from, source, path_prefixes={OLD: NEW})
        assert counts(result) == {'seen': 3, 'added': 2, 'skipped': 1}, result
        assert result.get('rewritten') == 2, result
        after = await stored(r, target.id)
        assert sorted(after) == ['/media/new/a.jpg', '/media/new/b.jpg', '/media/new/c.jpg', '/media/old/b.jpg']
        assert after['/media/new/a.jpg'] == before['/media/new/a.jpg']
        assert after['/media/old/b.jpg'] == before['/media/old/b.jpg']
        assert (after['/media/new/b.jpg']['tags'], after['/media/new/b.jpg']['metadata']) == (['from-source'], {'k': 2})

    regions_world(scenario)


# --- 1.8: the source is only read ---


def test_the_source_is_only_read(regions_world, monkeypatch):
    """1.8: after a copy with a rule, every source row (to `last_modified_at`), vector and hash is as it was."""

    async def scenario(r):
        signed_in(r, monkeypatch)
        source = await sdk(make, 'source', rich_source())
        before = await stored(r, source.id)
        target = await sdk(make, 'target', [], False)
        await sdk(target.add_samples_from, source, path_prefixes={OLD: NEW}, batch_size=3)
        assert await stored(r, source.id) == before

    regions_world(scenario)


def test_copying_a_dataset_into_itself_with_a_rule_is_refused_as_today(regions_world, monkeypatch):
    """1.8: source and target the same dataset, whole or as a view, is refused with a rule too, and nothing changes."""

    async def scenario(r):
        signed_in(r, monkeypatch)
        source = await sdk(make, 'source', rich_source())
        before = await stored(r, source.id)
        with pytest.raises(ValueError, match='same dataset'):
            await sdk(source.add_samples_from, source, path_prefixes={OLD: NEW})
        with pytest.raises(ValueError, match='same dataset'):
            await sdk(source.add_samples_from, source.filter(n=1), path_prefixes={OLD: NEW})
        assert await stored(r, source.id) == before

    regions_world(scenario)


# --- 1.5: a rule that would merge two samples is refused before anything is written ---

FILLERS = [f'fill/{index}.jpg' for index in range(4)]  # paths no rule below fits: the colliding pair is batches apart

COLLISIONS = {
    'replaced-onto-unchanged': ({OLD: NEW}, '/media/old/a.jpg', '/media/new/a.jpg', '/media/new/a.jpg'),
    'unchanged-before-replaced': ({OLD: NEW}, '/media/new/a.jpg', '/media/old/a.jpg', '/media/new/a.jpg'),
    'two-olds-one-new': (
        {'/media/x': '/media/n', '/media/y': '/media/n'},
        '/media/x/a.jpg',
        '/media/y/a.jpg',
        '/media/n/a.jpg',
    ),
    'nested-news': (
        {'/media/a': '/media/n', '/media/b': '/media/n/sub'},
        '/media/a/sub/x.jpg',
        '/media/b/x.jpg',
        '/media/n/sub/x.jpg',
    ),
    'old-inside-old': (
        {'/media/old': '/m', '/media/old/sub': '/m/sub2'},
        '/media/old/sub/x.jpg',
        '/media/old/sub2/x.jpg',
        '/m/sub2/x.jpg',
    ),
    'made-relative-onto-relative': (
        {'/media/drive/photos': 'photos'},
        '/media/drive/photos/a.jpg',
        'photos/a.jpg',
        'photos/a.jpg',
    ),
    'cloud-onto-local': ({'s3://bucket/p': '/media/p'}, 's3://bucket/p/a.jpg', '/media/p/a.jpg', '/media/p/a.jpg'),
    'equal-to-old': ({OLD: NEW}, '/media/old', '/media/new', '/media/new'),
}


@pytest.mark.parametrize('case', list(COLLISIONS))
def test_a_rule_that_would_give_two_samples_one_path_is_refused_before_anything_is_written(
    regions_world,
    monkeypatch,
    case,
):
    """1.5 and 1.9: the rule would give two source samples the same path, one of them in the first batch and the other
    in the last: a ValueError naming an example, no sample added to the target, the target's own samples untouched,
    and the source untouched."""
    rule, first, last, shared = COLLISIONS[case]

    async def scenario(r):
        signed_in(r, monkeypatch)
        samples = [Sample(filepath=path, metadata={'p': path}) for path in [first, *FILLERS, last]]
        source = await sdk(make, 'source', samples)
        target = await sdk(make, 'target', [Sample(filepath='kept/z.jpg', tags=['mine'], metadata={'mine': 1})])
        source_before, target_before = await stored(r, source.id), await stored(r, target.id)
        with pytest.raises(ValueError) as refused:
            await sdk(target.add_samples_from, source, path_prefixes=rule, batch_size=2)
        message = str(refused.value)
        assert any(path in message for path in (shared, first, last)), message
        assert await stored(r, target.id) == target_before
        assert await stored(r, source.id) == source_before

    regions_world(scenario)


def test_a_refused_collision_registers_no_array_field_on_the_target(regions_world, monkeypatch):
    """1.5: "before it writes anything" includes the array fields the copy would register on the target."""

    async def scenario(r):
        signed_in(r, monkeypatch)
        samples = [
            Sample(filepath='/media/old/a.jpg', array_fields={'labels': ['x']}),
            Sample(filepath='/media/new/a.jpg', array_fields={'labels': ['y']}),
        ]
        source = await sdk(make, 'source', samples)
        target = await sdk(make, 'target', [], False)
        assert await array_fields(r, target.id) == []
        with pytest.raises(ValueError):
            await sdk(target.add_samples_from, source, path_prefixes={OLD: NEW})
        assert await array_fields(r, target.id) == []
        assert await stored(r, target.id) == {}

    regions_world(scenario)


def test_an_empty_new_with_a_path_equal_to_old_is_refused_before_anything_is_written(regions_world, monkeypatch):
    """1.2 and 1.9: with an empty `new` (the data root), a sample whose path is exactly `old` would have no path left:
    refused up front as in 1.5, the sample in the last batch: a ValueError naming it, nothing written, the source
    untouched."""

    async def scenario(r):
        signed_in(r, monkeypatch)
        paths = ['/media/drive/a.jpg', *FILLERS, '/media/drive']
        source = await sdk(make, 'source', [Sample(filepath=path) for path in paths])
        target = await sdk(make, 'target', [Sample(filepath='kept/z.jpg', tags=['mine'])])
        source_before, target_before = await stored(r, source.id), await stored(r, target.id)
        with pytest.raises(ValueError) as refused:
            await sdk(target.add_samples_from, source, path_prefixes={'/media/drive': ''}, batch_size=2)
        assert '/media/drive' in str(refused.value), refused.value
        assert await stored(r, target.id) == target_before
        assert await stored(r, source.id) == source_before

    regions_world(scenario)


# --- 1.6: a bad rule is refused before anything is read or written ---

BAD_RULES: dict[str, Any] = {
    'list-of-pairs': [(OLD, NEW)],
    'tuple': ((OLD, NEW),),
    'string': OLD,
    'set': {OLD},
    'empty-old': {'': NEW},
    'number-old': {1: NEW},
    'none-old': {None: NEW},
    'number-new': {OLD: 1},
    'none-new': {OLD: None},
    'list-new': {OLD: [NEW]},
    'twice-by-slash': {OLD: NEW, f'{OLD}/': '/media/other'},
    'twice-by-slash-same-new': {OLD: NEW, f'{OLD}/': NEW},
}


@pytest.mark.parametrize('case', list(BAD_RULES))
def test_a_bad_rule_is_refused_before_anything_is_read_or_written(regions_world, monkeypatch, case):
    """1.6 and 1.9: not a dict, an empty `old`, an `old` or `new` that is not text, the same `old` named twice once
    trailing slashes are ignored: a ValueError, with no request sent during the call and nothing in the target."""

    async def scenario(r):
        sent = signed_in(r, monkeypatch)
        source = await sdk(make, 'source', rich_source())
        target = await sdk(make, 'target', [], False)
        sent.sent.clear()
        with pytest.raises(ValueError):
            await sdk(target.add_samples_from, source, path_prefixes=BAD_RULES[case])
        assert sent.sent == [], sent.sent
        assert await stored(r, target.id) == {}

    regions_world(scenario)


# --- 2. A path that can't be changed says so ---

LOADERS: dict[str, Callable[[Dataset, str], Optional[Sample]]] = {
    'by-filepath': lambda ds, path: ds[path],
    'iterated': lambda ds, path: next(s for s in ds.iter_samples() if s.filepath == path),
    'view-first': lambda ds, path: ds.filter(k=1).first(),
}


async def one_sample(r: Regions) -> tuple[Dataset, dict[str, Any]]:
    ds = await sdk(make, 'edited', [Sample(filepath='/media/old/a.jpg', tags=['t'], metadata={'k': 1})])
    return ds, (await stored(r, ds.id))['/media/old/a.jpg']


@pytest.mark.parametrize('loader', list(LOADERS))
def test_saving_a_changed_filepath_raises_and_sends_nothing(regions_world, monkeypatch, loader):
    """2.1: a sample from the server, its filepath assigned another value, its metadata and tags changed too:
    save() raises a ValueError containing the phrase, sends no request, and the stored sample is unchanged."""

    async def scenario(r):
        sent = signed_in(r, monkeypatch)
        ds, before = await one_sample(r)
        sample = await sdk(LOADERS[loader], ds, '/media/old/a.jpg')
        assert sample is not None and sample.filepath == '/media/old/a.jpg'
        sample.filepath = '/media/new/a.jpg'
        sample.metadata['k'] = 2
        sample.tags.append('u')
        sent.sent.clear()
        with pytest.raises(ValueError) as refused:
            await sdk(sample.save)
        assert PHRASE in str(refused.value), refused.value
        assert sent.sent == [], sent.sent
        assert await stored(r, ds.id) == {'/media/old/a.jpg': before}

    regions_world(scenario)


def test_the_refusal_says_what_to_do_instead(regions_world, monkeypatch):
    """2.1: the message names the way out: a copy with `add_samples_from(..., path_prefixes=...)`."""

    async def scenario(r):
        signed_in(r, monkeypatch)
        ds, _ = await one_sample(r)
        sample = await sdk(LOADERS['by-filepath'], ds, '/media/old/a.jpg')
        sample.filepath = 's3://bucket/a.jpg'
        with pytest.raises(ValueError) as refused:
            await sdk(sample.save)
        message = str(refused.value)
        assert 'add_samples_from' in message and 'path_prefixes' in message, message

    regions_world(scenario)


def test_after_a_refused_save_the_path_put_back_saves_as_today(regions_world, monkeypatch):
    """2.1: the refused sample, its filepath assigned back, saves its other changes as today."""

    async def scenario(r):
        signed_in(r, monkeypatch)
        ds, _ = await one_sample(r)
        sample = await sdk(LOADERS['by-filepath'], ds, '/media/old/a.jpg')
        sample.filepath = '/media/new/a.jpg'
        sample.metadata['k'] = 2
        with pytest.raises(ValueError):
            await sdk(sample.save)
        sample.filepath = '/media/old/a.jpg'
        await sdk(sample.save)
        after = await stored(r, ds.id)
        assert list(after) == ['/media/old/a.jpg']
        assert after['/media/old/a.jpg']['metadata'] == {'k': 2}

    regions_world(scenario)


@pytest.mark.parametrize('loader', list(LOADERS))
def test_the_same_path_assigned_back_saves_as_today(regions_world, monkeypatch, loader):
    """2.1, a pin (passes today): the same path assigned again (an equal text, not the same object) and a metadata
    change: save() writes the change, the path stays."""

    async def scenario(r):
        signed_in(r, monkeypatch)
        ds, _ = await one_sample(r)
        sample = await sdk(LOADERS[loader], ds, '/media/old/a.jpg')
        sample.filepath = ''.join(['/media/old/', 'a.jpg'])
        sample.metadata['k'] = 2
        await sdk(sample.save)
        after = await stored(r, ds.id)
        assert list(after) == ['/media/old/a.jpg']
        assert (after['/media/old/a.jpg']['metadata'], after['/media/old/a.jpg']['tags']) == ({'k': 2}, ['t'])

    regions_world(scenario)


def test_a_sample_whose_path_was_not_touched_saves_as_today(regions_world, monkeypatch):
    """2.1, a pin (passes today)."""

    async def scenario(r):
        signed_in(r, monkeypatch)
        ds, _ = await one_sample(r)
        sample = await sdk(LOADERS['by-filepath'], ds, '/media/old/a.jpg')
        sample.metadata['k'] = 3
        sample.tags.append('u')
        await sdk(sample.save)
        after = (await stored(r, ds.id))['/media/old/a.jpg']
        assert (after['metadata'], after['tags']) == ({'k': 3}, ['t', 'u'])

    regions_world(scenario)


def test_a_sample_never_added_can_change_its_path_freely(regions_world, monkeypatch):
    """2.2, a pin (passes today): a new Sample's filepath set and changed twice, then added: it is stored at the last
    one."""

    async def scenario(r):
        signed_in(r, monkeypatch)
        ds = await sdk(make, 'edited', [])
        sample = Sample(filepath='/media/old/n.jpg', metadata={'k': 1})
        sample.filepath = '/media/new/n.jpg'
        sample.filepath = '/media/new/n2.jpg'
        await sdk(ds.add_samples, [sample])
        assert list(await stored(r, ds.id)) == ['/media/new/n2.jpg']

    regions_world(scenario)


async def patch(r: Regions, ds: Dataset, sample_id: int, body: dict[str, Any]) -> httpx.Response:
    return await r.call(ADMIN, 'PATCH', f'/api/datasets/{ds.id}/samples/{sample_id}', json=body)


@pytest.mark.parametrize(
    'extra',
    [{}, {'metadata': {'k': 2}, 'tags': ['x']}],
    ids=['path-alone', 'with-other-changes'],
)
def test_a_patch_with_a_different_filepath_is_a_422_and_writes_nothing(regions_world, monkeypatch, extra):
    """2.3: the server's edit of one sample, its body carrying another filepath (alone, or with metadata and tags):
    422 with the phrase in its detail, and the row unchanged to `last_modified_at`."""

    async def scenario(r):
        signed_in(r, monkeypatch)
        ds, before = await one_sample(r)
        response = await patch(r, ds, before['id'], {'filepath': '/media/new/a.jpg', **extra})
        assert response.status_code == 422, response.text
        detail = response.json().get('detail')
        assert isinstance(detail, str) and PHRASE in detail, response.text
        assert await stored(r, ds.id) == {'/media/old/a.jpg': before}

    regions_world(scenario)


def test_a_patch_carrying_the_samples_own_path_is_accepted_as_today(regions_world, monkeypatch):
    """2.3, a pin (passes today): the path alone, the path with a metadata change, and the whole sample as the API
    reads it sent back with a metadata key changed: each 200, the changes written, the path unchanged."""

    async def scenario(r):
        signed_in(r, monkeypatch)
        ds, before = await one_sample(r)
        sample_id = before['id']
        alone = await patch(r, ds, sample_id, {'filepath': '/media/old/a.jpg'})
        assert alone.status_code == 200, alone.text
        assert (await stored(r, ds.id))['/media/old/a.jpg']['metadata'] == {'k': 1}
        with_metadata = await patch(r, ds, sample_id, {'filepath': '/media/old/a.jpg', 'metadata': {'k': 2}})
        assert with_metadata.status_code == 200, with_metadata.text
        assert (await stored(r, ds.id))['/media/old/a.jpg']['metadata'] == {'k': 2}
        query = {'filters': {'field': 'id', 'operator': 'in', 'value': [sample_id]}, 'limit': 1}
        read = await r.call(ADMIN, 'POST', f'/api/datasets/{ds.id}/samples/query', json=query)
        assert read.status_code == 200, read.text
        [whole] = read.json()['items']
        assert whole['filepath'] == '/media/old/a.jpg', whole
        whole['metadata'] = {**whole['metadata'], 'k': 3}
        sent_back = await patch(r, ds, sample_id, whole)
        assert sent_back.status_code == 200, sent_back.text
        after = await stored(r, ds.id)
        assert list(after) == ['/media/old/a.jpg']
        assert (after['/media/old/a.jpg']['metadata'], after['/media/old/a.jpg']['tags']) == ({'k': 3}, ['t'])

    regions_world(scenario)


def test_tags_with_the_samples_own_path_sent_back_are_a_tag_level_edit(regions_world, monkeypatch):
    """2.3, a pin (passes today): a user with the tag level only sends tags and the sample's own path: 200, the tags
    written, the path unchanged."""

    async def scenario(r):
        signed_in(r, monkeypatch)
        ds, before = await one_sample(r)
        await r.share(ds.id, POWER, 'tag')
        response = await r.call(
            POWER,
            'PATCH',
            f'/api/datasets/{ds.id}/samples/{before["id"]}',
            json={'tags': ['kept'], 'filepath': '/media/old/a.jpg'},
        )
        assert response.status_code == 200, response.text
        after = await stored(r, ds.id)
        assert list(after) == ['/media/old/a.jpg']
        assert (after['/media/old/a.jpg']['tags'], after['/media/old/a.jpg']['metadata']) == (['kept'], {'k': 1})

    regions_world(scenario)


# --- 5. A copy keeps objects with their images ---


def an_object(filepath: str, parent: Any, **metadata: Any) -> Sample:
    """An object sample (media_type `object`) whose `metadata.parent_id` is `parent`."""
    sample = Sample(filepath=filepath, metadata={'parent_id': parent, **metadata})
    sample.media_type = 'object'
    return sample


async def object_world(r: Regions, name: str) -> tuple[Dataset, dict[str, int]]:
    """Images A, B and C under OLD, A with objects A0 and A1 and B with B0. Images carry their name in `img` and `k`,
    objects in `obj` and a `label` (cat for A's, dog for B's). Returns the dataset and the images' ids by name."""
    images = [Sample(filepath=f'{OLD}/{n}.jpg', metadata={'img': n, 'k': n}) for n in 'ABC']
    ds = await sdk(make, name, images)
    ids = {row['metadata']['img']: row['id'] for row in (await stored(r, ds.id)).values()}
    objects = [
        an_object(f'{OLD}/crops/A0.png', ids['A'], obj='A0', label='cat'),
        an_object(f'{OLD}/crops/A1.png', ids['A'], obj='A1', label='cat'),
        an_object(f'{OLD}/crops/B0.png', ids['B'], obj='B0', label='dog'),
    ]
    await sdk(ds.add_samples, objects)
    return ds, ids


def planted(row: dict[str, Any]) -> Any:
    return row['metadata'].get('img') or row['metadata'].get('obj')


async def named(r: Regions, dataset_id: int, filters: dict[str, Any]) -> set[str]:
    """The planted names of the samples the filter finds, through the samples query."""
    query = {'filters': filters, 'limit': 1000}
    response = await r.call(ADMIN, 'POST', f'/api/datasets/{dataset_id}/samples/query', json=query)
    assert response.status_code == 200, response.text
    return {planted(item) for item in response.json()['items']}


def objects_of(image_id: int) -> dict[str, Any]:
    """The objects of one image: the app's image-to-objects pivot."""
    return {
        'logic': 'AND',
        'filters': [
            {'field': 'media_type', 'operator': '=', 'value': 'object'},
            {'field': 'parent_id', 'operator': 'in', 'value': [image_id]},
        ],
    }


def across(grain: str, leaf: dict[str, Any]) -> dict[str, Any]:
    """A filter across the grains, as the grid builds it: the rows of `grain` that match, or whose partner (an image's
    objects, an object's image) matches."""
    base = {'logic': 'AND', 'filters': [leaf]}
    own = {'field': 'media_type', 'operator': '=' if grain == 'object' else '!=', 'value': 'object'}
    partner = 'parent_matches' if grain == 'object' else 'has_matching_object'
    either = [
        {'field': '_cgself', 'operator': 'cg_self', 'value': base},
        {'field': '_crossgrain', 'operator': partner, 'value': base},
    ]
    return {'logic': 'AND', 'filters': [own, {'logic': 'OR', 'filters': either}]}


async def unlinked(r: Regions, dataset_id: int) -> dict[str, Any]:
    """The unlinked-objects finder's answer, asked by the admin."""
    response = await r.call(ADMIN, 'POST', f'/api/datasets/{dataset_id}/objects/unlinked')
    assert response.status_code == 200, response.text
    return response.json()


async def links(r: Regions, dataset_id: int) -> dict[str, set[str]]:
    """Each image's objects (by planted name), read through the pivot."""
    rows = (await stored(r, dataset_id)).values()
    return {
        row['metadata']['img']: await named(r, dataset_id, objects_of(row['id']))
        for row in rows
        if 'img' in row['metadata']
    }


async def assert_no_source_id_in_objects(r: Regions, source_id: int, target_id: int) -> None:
    """5.2: no copied object names a source sample. Ids are one serial across the server, so a target id never equals
    a source id: a source id found here can only have come from the source."""
    source_ids = {row['id'] for row in (await stored(r, source_id)).values()}
    target = await stored(r, target_id)
    target_ids = {row['id'] for row in target.values()}
    for row in target.values():
        if row['media_type'] == 'object':
            parent = row['metadata'].get('parent_id')
            assert parent not in source_ids, (row['filepath'], parent)
            assert parent is None or parent in target_ids or not isinstance(parent, int), (row['filepath'], parent)


async def assert_linked_as_in_source(r: Regions, source_id: int, target_id: int) -> None:
    """5.1, through the app's own surfaces: each image's objects, the filters across the grains and the finder answer
    in the copy as in the source."""
    expected = {'A': {'A0', 'A1'}, 'B': {'B0'}, 'C': set()}
    assert await links(r, source_id) == expected
    assert await links(r, target_id) == expected
    for grain, leaf, found in (
        ('image', {'field': 'label', 'operator': '=', 'value': 'cat'}, {'A'}),
        ('image', {'field': 'label', 'operator': '=', 'value': 'dog'}, {'B'}),
        ('object', {'field': 'k', 'operator': '=', 'value': 'B'}, {'B0'}),
        ('object', {'field': 'k', 'operator': '=', 'value': 'A'}, {'A0', 'A1'}),
    ):
        filters = across(grain, leaf)
        assert await named(r, source_id, filters) == found, (grain, leaf)
        assert await named(r, target_id, filters) == found, (grain, leaf)
    answer = await unlinked(r, target_id)
    assert (answer['total'], answer['linked']) == (0, 0), answer
    await assert_no_source_id_in_objects(r, source_id, target_id)


def test_deleting_an_image_deletes_its_objects_in_the_source(regions_world, monkeypatch):
    """5.1, a pin (passes today): in the source, deleting an image deletes its objects, which the copy must keep."""

    async def scenario(r):
        signed_in(r, monkeypatch)
        source, ids = await object_world(r, 'source')
        response = await r.call(ADMIN, 'DELETE', f'/api/datasets/{source.id}/samples/{ids["A"]}')
        assert response.status_code == 200, response.text
        assert response.json()['objects_deleted'] == 2, response.text
        assert {planted(row) for row in (await stored(r, source.id)).values()} == {'B', 'C', 'B0'}

    regions_world(scenario)


@pytest.mark.parametrize('rule', [None, {OLD: NEW}], ids=['no-rule', 'rule'])
def test_a_copy_links_each_object_to_the_copy_of_its_image(regions_world, monkeypatch, rule):
    """5.1 and 5.2, with and without a rule, images and objects in different batches: each copied object names the copy
    of its image; each image's objects, the filters across the grains and the unlinked-objects finder answer in the
    copy as in the source; `objects_without_image` is 0; deleting a copied image deletes the copies of its objects.

    On main e0f754fe a copied object keeps the source's id as its `parent_id` and ends up with no image: the finder
    counts all three copies as naming a sample of another dataset, and no copied image has an object."""

    async def scenario(r):
        signed_in(r, monkeypatch)
        source, _ = await object_world(r, 'source')
        target = await sdk(make, 'target', [], False)
        given = {} if rule is None else {'path_prefixes': rule}
        result = await sdk(target.add_samples_from, source, batch_size=2, **given)
        expected = {'seen': 6, 'added': 6, 'skipped': 0, 'objects_without_image': 0}
        assert result == (expected if rule is None else {**expected, 'rewritten': 6}), result
        await assert_linked_as_in_source(r, source.id, target.id)
        copies = {planted(row): row['id'] for row in (await stored(r, target.id)).values()}
        response = await r.call(ADMIN, 'DELETE', f'/api/datasets/{target.id}/samples/{copies["A"]}')
        assert response.status_code == 200, response.text
        assert response.json()['objects_deleted'] == 2, response.text
        assert {planted(row) for row in (await stored(r, target.id)).values()} == {'B', 'C', 'B0'}

    regions_world(scenario)


@pytest.mark.parametrize('rule', [None, {OLD: NEW}], ids=['no-rule', 'rule'])
def test_an_object_whose_image_is_outside_the_view_links_to_it_in_the_target_or_to_nothing(
    regions_world,
    monkeypatch,
    rule,
):
    """5.2: a view of two objects alone. B0's image is already in the target (a sample of the target's own at B's path,
    after the rule): B0's copy is linked to it. A0's image is in neither: A0's copy has a null `parent_id`, is linked
    to nothing, and is counted in `objects_without_image`."""

    async def scenario(r):
        signed_in(r, monkeypatch)
        source, _ = await object_world(r, 'source')
        b_path = f'{OLD if rule is None else NEW}/B.jpg'
        target = await sdk(make, 'target', [Sample(filepath=b_path, metadata={'img': 'B-own'})], False)
        given = {} if rule is None else {'path_prefixes': rule}
        result = await sdk(target.add_samples_from, source.filter(obj__in=['A0', 'B0']), **given)
        expected = {'seen': 2, 'added': 2, 'skipped': 0, 'objects_without_image': 1}
        assert result == (expected if rule is None else {**expected, 'rewritten': 2}), result
        copied = {planted(row): row for row in (await stored(r, target.id)).values()}
        assert await named(r, target.id, objects_of(copied['B-own']['id'])) == {'B0'}
        assert copied['B0']['metadata']['parent_id'] == copied['B-own']['id']
        assert copied['A0']['metadata'].get('parent_id') is None, copied['A0']['metadata']
        assert (await unlinked(r, target.id))['total'] == 1
        await assert_no_source_id_in_objects(r, source.id, target.id)

    regions_world(scenario)


def test_an_object_whose_parent_id_names_no_image_of_the_source_is_copied_linked_to_nothing(regions_world, monkeypatch):
    """5.2: objects whose `parent_id` names nothing, another object, a sample of a third dataset, or a sample of the
    target itself: each copied with a null `parent_id`, linked to nothing (the target's sample gets no object, and
    deleting it deletes none), and counted in `objects_without_image`; an ordinary object beside them is linked.

    On main e0f754fe the object naming the target's own sample is linked to it in the copy (the sample's objects list
    it) and is deleted with it: on one server that is how a source id silently names an unrelated target sample."""

    async def scenario(r):
        signed_in(r, monkeypatch)
        elsewhere = await sdk(make, 'elsewhere', [Sample(filepath='/media/elsewhere/e.jpg', metadata={'img': 'E'})])
        target = await sdk(make, 'target', [Sample(filepath='/media/target/t.jpg', metadata={'img': 'T'})], False)
        e_id = (await stored(r, elsewhere.id))['/media/elsewhere/e.jpg']['id']
        t_id = (await stored(r, target.id))['/media/target/t.jpg']['id']
        source, ids = await object_world(r, 'source')
        a0 = next(row['id'] for row in (await stored(r, source.id)).values() if row['metadata'].get('obj') == 'A0')
        nothing = 1 + max(await r.database.fetchval('SELECT max(id) FROM samples'), 10**6)
        odd = [
            an_object(f'{OLD}/crops/gone.png', nothing, obj='gone'),
            an_object(f'{OLD}/crops/of-object.png', a0, obj='of-object'),
            an_object(f'{OLD}/crops/of-elsewhere.png', e_id, obj='of-elsewhere'),
            an_object(f'{OLD}/crops/of-target.png', t_id, obj='of-target'),
        ]
        await sdk(source.add_samples, odd)
        result = await sdk(target.add_samples_from, source)
        assert result == {'seen': 10, 'added': 10, 'skipped': 0, 'objects_without_image': 4}, result
        copied = {planted(row): row for row in (await stored(r, target.id)).values()}
        for name in ('gone', 'of-object', 'of-elsewhere', 'of-target'):
            assert copied[name]['metadata'].get('parent_id') is None, (name, copied[name]['metadata'])
        assert await named(r, target.id, objects_of(t_id)) == set()
        assert await named(r, target.id, objects_of(copied['A']['id'])) == {'A0', 'A1'}
        assert (await unlinked(r, target.id))['total'] == 4
        await assert_no_source_id_in_objects(r, source.id, target.id)
        response = await r.call(ADMIN, 'DELETE', f'/api/datasets/{target.id}/samples/{t_id}')
        assert response.status_code == 200, response.text
        assert response.json()['objects_deleted'] == 0, response.text

    regions_world(scenario)


@pytest.mark.parametrize('rule', [None, {OLD: NEW}], ids=['no-rule', 'rule'])
def test_a_view_without_objects_answers_exactly_as_today(regions_world, monkeypatch, rule):
    """5.2 and 5.5: the images of a dataset that has objects, copied as a view without them: no
    `objects_without_image` in the answer."""

    async def scenario(r):
        signed_in(r, monkeypatch)
        source, _ = await object_world(r, 'source')
        target = await sdk(make, 'target', [], False)
        given = {} if rule is None else {'path_prefixes': rule}
        result = await sdk(target.add_samples_from, source.filter(img__in=['A', 'B', 'C']), **given)
        expected = {'seen': 3, 'added': 3, 'skipped': 0}
        assert result == (expected if rule is None else {**expected, 'rewritten': 3}), result

    regions_world(scenario)


def test_an_object_older_than_its_image_is_linked_in_the_copy(regions_world, monkeypatch):
    """5.3: an object added before its image (a lower id), then pointed at it, and batches of one: its copy is linked to
    the copy of its image."""

    async def scenario(r):
        signed_in(r, monkeypatch)
        source = await sdk(make, 'source', [an_object(f'{OLD}/crops/early.png', None, obj='early')])
        await sdk(source.add_samples, [Sample(filepath=f'{OLD}/late.jpg', metadata={'img': 'late'})])
        rows = {planted(row): row for row in (await stored(r, source.id)).values()}
        assert rows['early']['id'] < rows['late']['id']

        def point() -> None:
            early = source[f'{OLD}/crops/early.png']
            early.metadata['parent_id'] = rows['late']['id']
            early.save()

        await sdk(point)
        assert await named(r, source.id, objects_of(rows['late']['id'])) == {'early'}
        target = await sdk(make, 'target', [], False)
        result = await sdk(target.add_samples_from, source, batch_size=1)
        assert result == {'seen': 2, 'added': 2, 'skipped': 0, 'objects_without_image': 0}, result
        copied = {planted(row): row for row in (await stored(r, target.id)).values()}
        assert await named(r, target.id, objects_of(copied['late']['id'])) == {'early'}
        await assert_no_source_id_in_objects(r, source.id, target.id)

    regions_world(scenario)


def test_an_interrupted_copy_run_again_ends_with_every_object_linked(regions_world, monkeypatch):
    """5.3 and 1.4: part of the source copied first (images A and B and A's object A0, as an interrupted run leaves
    it: an image before its objects), then the whole with the same arguments: the part is skipped, the rest added,
    and every object is linked to the copy of its image."""

    async def scenario(r):
        signed_in(r, monkeypatch)
        source, ids = await object_world(r, 'source')
        a0 = next(row['id'] for row in (await stored(r, source.id)).values() if row['metadata'].get('obj') == 'A0')
        target = await sdk(make, 'target', [], False)
        part = await sdk(
            target.add_samples_from,
            source.filter(id__in=[ids['A'], ids['B'], a0]),
            path_prefixes={OLD: NEW},
        )
        assert part == {'seen': 3, 'added': 3, 'skipped': 0, 'rewritten': 3, 'objects_without_image': 0}, part
        rest = await sdk(target.add_samples_from, source, path_prefixes={OLD: NEW}, batch_size=2)
        assert counts(rest) == {'seen': 6, 'added': 3, 'skipped': 3}, rest
        assert (rest.get('rewritten'), rest.get('objects_without_image')) == (3, 0), rest
        await assert_linked_as_in_source(r, source.id, target.id)

    regions_world(scenario)


def test_a_parent_id_that_is_not_a_sample_id_and_a_non_objects_parent_id_are_copied_as_they_are(
    regions_world,
    monkeypatch,
):
    """5.4 and 5.2: objects whose `parent_id` is not a sample id (text, a negative number, a fraction) keep it, and
    objects with none (null, or no key) stay without; all five have no image in the target and are counted in
    `objects_without_image`. A non-object (a crop of `to_patches`) whose `parent_id` names an image of another dataset
    keeps it, and isn't counted."""

    async def scenario(r):
        signed_in(r, monkeypatch)
        original = await sdk(make, 'original', [Sample(filepath='/media/original/o.jpg', metadata={'img': 'O'})])
        o_id = (await stored(r, original.id))['/media/original/o.jpg']['id']
        samples = [
            an_object(f'{OLD}/crops/text.png', 'abc', obj='text'),
            an_object(f'{OLD}/crops/negative.png', -1, obj='negative'),
            an_object(f'{OLD}/crops/fraction.png', 1.5, obj='fraction'),
            an_object(f'{OLD}/crops/null.png', None, obj='null'),
            Sample(filepath=f'{OLD}/patches/crop.png', metadata={'img': 'crop', 'parent_id': o_id}),
        ]
        keyless = Sample(filepath=f'{OLD}/crops/keyless.png', metadata={'obj': 'keyless'})
        keyless.media_type = 'object'
        source = await sdk(make, 'source', [*samples, keyless])
        target = await sdk(make, 'target', [], False)
        result = await sdk(target.add_samples_from, source)
        assert result == {'seen': 6, 'added': 6, 'skipped': 0, 'objects_without_image': 5}, result
        copied = {planted(row): row['metadata'].get('parent_id') for row in (await stored(r, target.id)).values()}
        assert copied == {
            'text': 'abc',
            'negative': -1,
            'fraction': 1.5,
            'null': None,
            'keyless': None,
            'crop': o_id,
        }, copied

    regions_world(scenario)


def test_a_non_object_naming_an_image_of_the_source_keeps_that_number(regions_world, monkeypatch):
    """5.4: a sample that is not an object keeps its `metadata.parent_id` as it is, also when it names an image of the
    source itself (5.2's "no copied object carries a source id" is about objects)."""

    async def scenario(r):
        signed_in(r, monkeypatch)
        source = await sdk(make, 'source', [Sample(filepath=f'{OLD}/i.jpg', metadata={'img': 'I'})])
        i_id = (await stored(r, source.id))[f'{OLD}/i.jpg']['id']
        await sdk(source.add_samples, [Sample(filepath=f'{OLD}/n.jpg', metadata={'img': 'N', 'parent_id': i_id})])
        target = await sdk(make, 'target', [], False)
        await sdk(target.add_samples_from, source)
        assert (await stored(r, target.id))[f'{OLD}/n.jpg']['metadata'] == {'img': 'N', 'parent_id': i_id}

    regions_world(scenario)


def test_a_cut_view_keeps_objects_with_their_images(regions_world, monkeypatch):
    """5.5: a view cut by `limit` (images A, B, C and the object A0) links A0's copy to A's; one cut by `skip` (the
    three objects alone), into a target that already holds A's path, links A0 and A1 to that sample and leaves B0's
    copy linked to nothing, counted."""

    async def scenario(r):
        signed_in(r, monkeypatch)
        source, _ = await object_world(r, 'source')
        first = await sdk(make, 'first', [], False)
        result = await sdk(first.add_samples_from, source.all().limit(4))
        assert result == {'seen': 4, 'added': 4, 'skipped': 0, 'objects_without_image': 0}, result
        copied = {planted(row): row for row in (await stored(r, first.id)).values()}
        assert await named(r, first.id, objects_of(copied['A']['id'])) == {'A0'}
        second = await sdk(make, 'second', [Sample(filepath=f'{OLD}/A.jpg', metadata={'img': 'A-own'})], False)
        result = await sdk(second.add_samples_from, source.all().skip(3))
        assert result == {'seen': 3, 'added': 3, 'skipped': 0, 'objects_without_image': 1}, result
        copied = {planted(row): row for row in (await stored(r, second.id)).values()}
        assert await named(r, second.id, objects_of(copied['A-own']['id'])) == {'A0', 'A1'}
        assert copied['B0']['metadata'].get('parent_id') is None, copied['B0']['metadata']
        await assert_no_source_id_in_objects(r, source.id, first.id)
        await assert_no_source_id_in_objects(r, source.id, second.id)

    regions_world(scenario)


def test_objects_an_earlier_copy_left_are_skipped_not_repaired(regions_world, monkeypatch):
    """5.3, a pin (passes today): an object already in the target at its path, naming the source's image as an earlier
    SDK's copy left it, is skipped by a new copy and left exactly as it was; its image is added."""

    async def scenario(r):
        signed_in(r, monkeypatch)
        source, ids = await object_world(r, 'source')
        left = an_object(f'{OLD}/crops/A0.png', ids['A'], obj='A0', label='cat')
        target = await sdk(make, 'target', [left], False)
        before = (await stored(r, target.id))[f'{OLD}/crops/A0.png']
        result = await sdk(target.add_samples_from, source)
        assert counts(result) == {'seen': 6, 'added': 5, 'skipped': 1}, result
        after = await stored(r, target.id)
        assert after[f'{OLD}/crops/A0.png'] == before
        assert f'{OLD}/A.jpg' in after

    regions_world(scenario)


@pytest.mark.parametrize('rule', [None, {OLD: NEW}], ids=['no-rule', 'rule'])
def test_a_heading_without_a_location_is_kept(regions_world, monkeypatch, rule):
    """1.3: a sample that has a heading and no location keeps its heading in the copy, with or without a rule: one
    added with a heading alone, one given its heading by an edit that sets nothing else.

    On main e0f754fe the copy drops it (the samples endpoints nest the heading inside the location, so a sample with no
    location is read without its heading)."""

    async def scenario(r):
        signed_in(r, monkeypatch)
        source = await sdk(
            make,
            'source',
            [
                Sample(filepath=f'{OLD}/added.jpg', metadata={'img': 'added'}, heading=33.0),
                Sample(filepath=f'{OLD}/edited.jpg', metadata={'img': 'edited'}),
            ],
        )
        edited = (await stored(r, source.id))[f'{OLD}/edited.jpg']['id']
        response = await r.call(ADMIN, 'PATCH', f'/api/datasets/{source.id}/samples/{edited}', json={'heading': 44.0})
        assert response.status_code == 200, response.text
        before = {planted(row): (row['heading'], row['location']) for row in (await stored(r, source.id)).values()}
        assert before == {'added': (33.0, None), 'edited': (44.0, None)}, before
        target = await sdk(make, 'target', [], False)
        given = {} if rule is None else {'path_prefixes': rule}
        await sdk(target.add_samples_from, source, **given)
        after = {planted(row): (row['heading'], row['location']) for row in (await stored(r, target.id)).values()}
        assert after == before, after

    regions_world(scenario)


# --- 1.8: a version's view is copied as the version holds it ---


async def delete(r: Regions, dataset_id: int, sample_id: int) -> None:
    response = await r.call(ADMIN, 'DELETE', f'/api/datasets/{dataset_id}/samples/{sample_id}')
    assert response.status_code == 200, response.text


async def versioned(r: Regions) -> Dataset:
    """A dataset saved as version `v1` and changed after it: `a` edited (metadata and tags), `b` deleted, `d` added.
    `a`, `b` and `c` carry a media path inside their metadata."""
    samples = [
        Sample(
            filepath=f'{OLD}/{n}.jpg',
            tags=[f'tag-{n}'],
            metadata={'img': n, 'state': 'saved', 'mask': f'{OLD}/{n}.png'},
        )
        for n in 'abc'
    ]
    ds = await sdk(make, 'source', samples)
    await sdk(ds.create_version, 'v1')
    rows = {planted(row): row for row in (await stored(r, ds.id)).values()}
    response = await r.call(
        ADMIN,
        'PATCH',
        f'/api/datasets/{ds.id}/samples/{rows["a"]["id"]}',
        json={'metadata': {'state': 'edited after'}, 'tags': ['changed']},
    )
    assert response.status_code == 200, response.text
    await delete(r, ds.id, rows['b']['id'])
    await sdk(ds.add_samples, [Sample(filepath=f'{OLD}/d.jpg', metadata={'img': 'd', 'state': 'added after'})])
    return ds


def as_saved(root: str, names: str = 'abc') -> dict[str, tuple[str, list[str], dict[str, Any]]]:
    """The version's samples by name, as the copy must hold them under `root`: path, tags, metadata."""
    return {
        n: (f'{root}/{n}.jpg', [f'tag-{n}'], {'img': n, 'state': 'saved', 'mask': f'{root}/{n}.png'}) for n in names
    }


async def held_by_name(r: Regions, dataset_id: int) -> dict[str, tuple[str, list[str], dict[str, Any]]]:
    return {
        planted(row): (row['filepath'], row['tags'], row['metadata']) for row in (await stored(r, dataset_id)).values()
    }


def test_a_copy_of_a_versions_view_holds_the_versions_samples(regions_world, monkeypatch):
    """1.8, a pin (passes today): the view of a version, copied without a rule after the dataset changed, gives the
    version's samples as it holds them (the edited one as it was, the deleted one, not the added one)."""

    async def scenario(r):
        signed_in(r, monkeypatch)
        source = await versioned(r)
        target = await sdk(make, 'target', [], False)
        v1 = await sdk(source.version, 'v1')
        result = await sdk(target.add_samples_from, v1)
        assert result == {'seen': 3, 'added': 3, 'skipped': 0}, result
        assert await held_by_name(r, target.id) == as_saved(OLD)

    regions_world(scenario)


def test_a_copy_of_a_versions_view_with_a_rule_rewrites_the_versions_paths(regions_world, monkeypatch):
    """1.8 with 1.2, 1.3, 1.7 and 1.4: with a rule, the version's samples land at their new paths with their metadata
    media paths rewritten, `rewritten` counts them, and the same copy run again skips them all."""

    async def scenario(r):
        signed_in(r, monkeypatch)
        source = await versioned(r)
        target = await sdk(make, 'target', [], False)
        v1 = await sdk(source.version, 'v1')
        result = await sdk(target.add_samples_from, v1, path_prefixes={OLD: NEW}, batch_size=2)
        assert result == {'seen': 3, 'added': 3, 'skipped': 0, 'rewritten': 3}, result
        assert await held_by_name(r, target.id) == as_saved(NEW)
        again = await sdk(target.add_samples_from, v1, path_prefixes={OLD: NEW}, batch_size=2)
        assert again == {'seen': 3, 'added': 0, 'skipped': 3, 'rewritten': 0}, again
        assert await held_by_name(r, target.id) == as_saved(NEW)

    regions_world(scenario)


@pytest.mark.parametrize('rule', [None, {OLD: NEW}], ids=['no-rule', 'rule'])
def test_a_limited_versions_view_copies_the_versions_rows(regions_world, monkeypatch, rule):
    """1.8: a version's view limited to its first two samples (a, edited since, and b, deleted since) copies them as
    the version holds them, never the dataset's current rows with those ids. Without a rule this passes today."""

    async def scenario(r):
        signed_in(r, monkeypatch)
        source = await versioned(r)
        target = await sdk(make, 'target', [], False)
        given = {} if rule is None else {'path_prefixes': rule}
        v1 = await sdk(source.version, 'v1')
        result = await sdk(target.add_samples_from, v1.limit(2), **given)
        expected = {'seen': 2, 'added': 2, 'skipped': 0}
        assert result == (expected if rule is None else {**expected, 'rewritten': 2}), result
        assert await held_by_name(r, target.id) == as_saved(OLD if rule is None else NEW, 'ab')

    regions_world(scenario)


@pytest.mark.parametrize('rule', [None, {OLD: NEW}], ids=['no-rule', 'rule'])
def test_a_versions_objects_are_linked_to_their_images_copies(regions_world, monkeypatch, rule):
    """1.8 with section 5: a version holding images and objects, copied after image A (and with it its objects) was
    deleted from the dataset and an image D added: every object of the version is linked to the copy of its image,
    the finder finds none unlinked, `objects_without_image` is 0, and no copied object names a source id."""

    async def scenario(r):
        signed_in(r, monkeypatch)
        source, ids = await object_world(r, 'source')
        source_ids = {row['id'] for row in (await stored(r, source.id)).values()}
        await sdk(source.create_version, 'v1')
        await delete(r, source.id, ids['A'])
        await sdk(source.add_samples, [Sample(filepath=f'{OLD}/D.jpg', metadata={'img': 'D'})])
        target = await sdk(make, 'target', [], False)
        given = {} if rule is None else {'path_prefixes': rule}
        v1 = await sdk(source.version, 'v1')
        result = await sdk(target.add_samples_from, v1, batch_size=2, **given)
        expected = {'seen': 6, 'added': 6, 'skipped': 0, 'objects_without_image': 0}
        assert result == (expected if rule is None else {**expected, 'rewritten': 6}), result
        assert await links(r, target.id) == {'A': {'A0', 'A1'}, 'B': {'B0'}, 'C': set()}
        answer = await unlinked(r, target.id)
        assert (answer['total'], answer['linked']) == (0, 0), answer
        for row in (await stored(r, target.id)).values():
            assert row['metadata'].get('parent_id') not in source_ids, row

    regions_world(scenario)


def test_a_collision_in_the_version_is_refused_and_one_only_in_the_dataset_is_not(regions_world, monkeypatch):
    """1.8 with 1.5: the version holds `/media/old/a.jpg` and `/media/new/a.jpg` (the second deleted from the dataset
    since): copying the version with {old: new} is refused before anything is written. The dataset now holds
    `/media/new/b.jpg`, added since, beside `/media/old/b.jpg`: the version doesn't, and copying the version of b alone
    is not refused."""

    async def scenario(r):
        signed_in(r, monkeypatch)
        samples = [
            Sample(filepath=path, metadata={'p': path}) for path in (f'{OLD}/a.jpg', f'{NEW}/a.jpg', f'{OLD}/b.jpg')
        ]
        source = await sdk(make, 'source', samples)
        await sdk(source.create_version, 'v1')
        await delete(r, source.id, (await stored(r, source.id))[f'{NEW}/a.jpg']['id'])
        await sdk(source.add_samples, [Sample(filepath=f'{NEW}/b.jpg', metadata={'p': f'{NEW}/b.jpg'})])
        target = await sdk(make, 'target', [Sample(filepath='kept/z.jpg', metadata={'p': 'kept'})], False)
        before = await stored(r, target.id)
        v1 = await sdk(source.version, 'v1')
        with pytest.raises(ValueError) as refused:
            await sdk(target.add_samples_from, v1, path_prefixes={OLD: NEW}, batch_size=1)
        assert f'{NEW}/a.jpg' in str(refused.value) or f'{OLD}/a.jpg' in str(refused.value), refused.value
        assert await stored(r, target.id) == before
        b_only = v1.filter(source.samples.p == f'{OLD}/b.jpg')
        result = await sdk(target.add_samples_from, b_only, path_prefixes={OLD: NEW})
        assert result == {'seen': 1, 'added': 1, 'skipped': 0, 'rewritten': 1}, result
        assert sorted(await stored(r, target.id)) == [f'{NEW}/b.jpg', 'kept/z.jpg']

    regions_world(scenario)
