"""Every write checks the caller's level in the dataset, not their server-wide role (issue 12, step 2).

Written from the specification (issue 12, its plan, the plan's review and the owner's decisions), not from the code,
and tested where a user meets it: the HTTP API, signed in with real API tokens, on the test database. The rule:

- A user's level in a dataset is the highest of their own share, their groups' shares and, on a global dataset, its
  everyone level. An admin has manage everywhere; a guest has view at most. Each level includes the ones below it.
- view: every read (the POSTs that only read included), search, export, version comparison, creating and sharing
  one's own views; a selection token, except that guests are refused one.
- tag: the `tags` field only, through bulk-tag or a sample PATCH that changes only tags, new tag names included.
- edit: samples, fields, embeddings, hashes, quality, concepts, schedules, refresh-stats, versions (create, revert),
  the display settings of PATCH /datasets/{id}, and other users' public views.
- manage: who has access (users and groups, up to manage), is_global, everyone_level and name, deleting a version,
  deleting the dataset and resuming its deletion, and the access candidates.
- The admin role, whatever the level: creating datasets, storage_config_id, patch imports, index reviews and their
  schedules, groups, users, storage, followers, primaries, workers, backups.
- PATCH /datasets/{id} needs the highest level among the fields sent.
- Jobs: manage on the job's dataset, or the job's starter while they hold the level its kind needs; map re-fits at
  edit; jobs on no dataset are admin only.
- Refusals: below the level, 403 "This needs <level> access to the dataset"; no level at all, 403 "Access denied for
  this dataset"; a view or job addressed by its own id in a dataset the caller can't open, 404; not signed in, 401.
- Machine routes (/follower/*, /workers/*) take their own tokens and are not part of this. A compute worker acts with
  its owner's rights: it claims a dataset's remote jobs, and keeps a lease on one, only while its owner has edit there.
- On a mirror, its own tags (@local.tags) are tag level like tags; its other own fields (@local.<name>) edit.

The matrix sends each write a valid body, so a refusal that came after body validation can't pass as "not refused"
by answering 422: below the level it asserts the refusal's exact detail, at the level and above that the request was
not refused (and, where that's cheap, that it succeeded).
"""

import base64
import os
import secrets
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

import httpx
import pytest

from backend import job_queue, main
from backend.crypto import hash_token
from backend.local_fields import LOCAL_TAGS
from backend.routers import datasets
from backend.tests import test_access_rule_spec, test_field_registry_postgres
from backend.tests.test_access_rule_spec import (
    ADMIN,
    GUEST,
    LEVELS,
    OTHER_GUEST,
    OTHER_POWER,
    POWER,
    ROLES,
    World,
    number,
)

registry_database = test_field_registry_postgres.registry_database
world = test_access_rule_spec.world
pytestmark = pytest.mark.skipif(not os.getenv('FRIDGE_TEST_POSTGRES_DSN'), reason='Requires disposable PostgreSQL')

DENIED = 'Access denied for this dataset'
SEALED = 'Dataset is being deleted; writes are disabled. Resume deletion to finish.'
ADMIN_USER = {'id': ADMIN, 'role': 'admin', 'username': f'user-{ADMIN}'}
VECTORS = [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.7, 0.7, 0.0, 0.0]]
# A 1x1 PNG.
PNG = base64.b64encode(
    bytes.fromhex(
        '89504e470d0a1a0a0000000d4948445200000001000000010806000000'
        '1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082',
    ),
).decode()


def needs(level: str) -> str:
    return f'This needs {level} access to the dataset'


def rank(level: Optional[str]) -> int:
    return 0 if level is None else LEVELS.index(level) + 1


def detail(response: httpx.Response) -> Any:
    try:
        return response.json().get('detail')
    except (ValueError, AttributeError):
        return None


def refused(response: httpx.Response) -> bool:
    """A refusal for who the caller is: 401, or 403 whatever it says."""
    return response.status_code in (401, 403)


# --- Callers ---


@dataclass(frozen=True)
class Caller:
    """Who calls, and where their level in the caller's own dataset comes from: 'admin', 'none', 'own' (a share),
    'plain' (a share written without a level, as the upgrade leaves one), 'group' (a group's share), 'everyone' (a
    global dataset's everyone level), 'upgraded' (a global dataset at the edit level the upgrade gave the datasets
    already global) or 'fresh' (a dataset made global without a level: view)."""

    name: str
    user: int
    source: str
    share: Optional[str] = None

    @property
    def level(self) -> Optional[str]:
        """The level the specification gives the caller in their dataset."""
        if self.source == 'admin':
            return 'manage'
        if self.source == 'none':
            return None
        given = {'plain': 'edit', 'upgraded': 'edit', 'fresh': 'view'}.get(self.source, self.share)
        return 'view' if ROLES[self.user] == 'guest' else given

    @property
    def guest(self) -> bool:
        return ROLES[self.user] == 'guest'


CALLERS = [
    Caller('admin', ADMIN, 'admin'),
    Caller('power user, no access', POWER, 'none'),
    *[Caller(f'power user, own {level} share', POWER, 'own', level) for level in LEVELS],
    *[Caller(f'power user, group {level} share', POWER, 'group', level) for level in LEVELS],
    *[Caller(f'power user, everyone level {level}', POWER, 'everyone', level) for level in ('view', 'tag', 'edit')],
    Caller('guest, own manage share', GUEST, 'own', 'manage'),
    # The rows an upgrade leaves (plain shares; the datasets already global, at edit): the upgrade promise.
    Caller('power user, share written without a level', POWER, 'plain'),
    Caller('power user, global dataset the upgrade left at edit', POWER, 'upgraded'),
    Caller('guest, share written without a level', GUEST, 'plain'),
    Caller('guest, global dataset the upgrade left at edit', GUEST, 'upgraded'),
    # A dataset made global without a level gives no-share users view.
    Caller('power user, global dataset at its default everyone level (view)', POWER, 'fresh'),
    Caller('guest, global dataset at its default everyone level (view)', GUEST, 'fresh'),
]


class Ctx:
    """One caller's own dataset in one route's test: three samples, the caller's access to it, and the rows the route
    acts on, seeded as an admin."""

    def __init__(self, w: World, caller: Caller, dataset: int):
        self.w, self.caller, self.d = w, caller, dataset
        self.samples: list[int] = []
        self.version: Optional[int] = None
        self.version2: Optional[int] = None
        self.view: Optional[int] = None
        self.concept: Optional[int] = None
        self.schedule: Optional[int] = None
        self.job: Optional[str] = None
        self.group: Optional[int] = None
        self.user: Optional[int] = None
        self.config: Optional[int] = None
        self.follower: Optional[int] = None

    @property
    def s0(self) -> int:
        return self.samples[0]

    @property
    def pool(self) -> Any:
        return self.w.database.pool

    async def admin(self, method: str, path: str, **kwargs: Any) -> Any:
        """An admin's request in a seed: it must succeed."""
        response = await self.w.call(ADMIN, method, path, **kwargs)
        assert response.is_success, f'seed {method} {path}: {response.status_code} {response.text}'
        return response.json() if response.content else None


async def add_samples(dataset_id: int, count: int = 3, prefix: str = 'sample') -> list[int]:
    labels = ['cat', 'dog', 'cat']
    samples = [
        {
            'filepath': f'{prefix}-{dataset_id}-{index}.jpg',
            'tags': ['seen'],
            'metadata': {'label': labels[index % 3], 'score': 0.25 * (index + 1), 'note': f'n{index}'},
        }
        for index in range(count)
    ]
    added = await datasets.add_samples(dataset_id, {'samples': samples}, user=ADMIN_USER)
    return added['ids']


async def open_dataset(w: World, caller: Caller) -> Ctx:
    """A new dataset with three samples, which `caller` has their level in."""
    dataset_id = await w.database.fetchval('SELECT 100 + count(*) FROM datasets WHERE id >= 100 AND id < 1000')
    if caller.source == 'everyone':
        await w.dataset(dataset_id, is_global=True, everyone=caller.share)
    elif caller.source == 'upgraded':
        await w.dataset(dataset_id, is_global=True, everyone='edit')
    elif caller.source == 'fresh':
        await w.dataset(dataset_id, is_global=True)  # the column's default: where a dataset made global starts
    else:
        await w.dataset(dataset_id)
    if caller.source == 'own':
        await w.share(dataset_id, caller.user, caller.share)
    elif caller.source == 'plain':
        await w.share(dataset_id, caller.user)
    elif caller.source == 'group':
        await w.share_with_group(dataset_id, 'team', caller.share)
    ctx = Ctx(w, caller, dataset_id)
    ctx.samples = await add_samples(dataset_id)
    return ctx


# --- Seeds: what a route acts on ---


async def seed_embeddings(c: Ctx) -> None:
    body = [{'sample_id': s, 'vector': VECTORS[i], 'field_name': 'clip'} for i, s in enumerate(c.samples)]
    await c.admin('POST', f'/api/datasets/{c.d}/embeddings', json=body)


async def seed_map_embeddings(c: Ctx) -> None:
    """Enough vectors for a map: four more samples, eight vectors in all."""
    c.samples += await add_samples(c.d, 5, prefix='more')
    body = [
        {'sample_id': s, 'vector': [float(i % 3), float(i % 2), float(i % 5) / 4, 1.0], 'field_name': 'clip'}
        for i, s in enumerate(c.samples)
    ]
    await c.admin('POST', f'/api/datasets/{c.d}/embeddings', json=body)


async def seed_patches(c: Ctx) -> None:
    body = [
        {'sample_id': s, 'vector': VECTORS[i], 'field_name': 'clip', 'patch_scale': '2x2', 'patch_row': 0}
        for i, s in enumerate(c.samples)
    ]
    await c.admin('POST', f'/api/datasets/{c.d}/embeddings/patches', json=body)


async def seed_hashes(c: Ctx) -> None:
    body = [{'sample_id': s, 'field_name': 'phash', 'hash_hex': '0f0f0f0f0f0f0f0f'} for s in c.samples]
    await c.admin('POST', f'/api/datasets/{c.d}/hashes/upload', json=body)


async def version_id(c: Ctx, name: str) -> int:
    await c.admin('POST', f'/api/datasets/{c.d}/versions', json={'name': name})
    found = await c.w.database.fetchval(
        'SELECT id FROM dataset_versions WHERE dataset_id = $1 AND name = $2',
        c.d,
        name,
    )
    assert found is not None
    return found


async def seed_version(c: Ctx) -> None:
    c.version = await version_id(c, 'v1')


async def seed_two_versions(c: Ctx) -> None:
    c.version = await version_id(c, 'v1')
    await add_samples(c.d, 1, prefix='later')
    c.version2 = await version_id(c, 'v2')


async def seed_array_field(c: Ctx) -> None:
    await c.admin('POST', f'/api/datasets/{c.d}/array-fields', json={'field_name': 'labels_x'})


async def insert_view(c: Ctx, owner: int, public: bool, name: str) -> int:
    return await c.w.database.fetchval(
        'INSERT INTO views (dataset_id, user_id, name, slug, content, is_public) VALUES ($1, $2, $3, $4, $5, $6) '
        'RETURNING id',
        c.d,
        owner,
        name,
        f'{name}-{c.d}',
        {'filters': []},
        public,
    )


async def seed_own_view(c: Ctx) -> None:
    c.view = await insert_view(c, c.caller.user, False, 'mine')


async def seed_others_public_view(c: Ctx) -> None:
    c.view = await insert_view(c, OTHER_POWER, True, 'theirs')


async def seed_concept(c: Ctx) -> None:
    await seed_embeddings(c)
    c.concept = await c.w.database.fetchval(
        "INSERT INTO embedding_concepts (dataset_id, name, embedding_field, kind, weight, spec) "
        "VALUES ($1, 'concept', 'clip', 'seed', '[1,0,0,0]', $2) RETURNING id",
        c.d,
        {'seed_ids': [c.s0]},
    )


async def seed_schedule(c: Ctx) -> None:
    await seed_embeddings(c)
    made = await c.admin('POST', f'/api/datasets/{c.d}/schedules', json={'job_type': 'map_refit', 'target': 'clip'})
    c.schedule = made['id']


async def seed_review_schedule(c: Ctx) -> None:
    made = await c.admin('POST', f'/api/datasets/{c.d}/schedules', json={'job_type': 'index_review'})
    c.schedule = made['id']


async def seed_object_vector(c: Ctx) -> None:
    """A crop's vector (clip_obj) for the first sample, the seed of an object-to-patches search."""
    body = [{'sample_id': c.s0, 'vector': VECTORS[0], 'field_name': 'clip_obj'}]
    await c.admin('POST', f'/api/datasets/{c.d}/embeddings', json=body)


async def seed_others_job(c: Ctx) -> None:
    """A bulk job an admin started in the dataset (queued: no runner in these tests)."""
    made = await c.admin(
        'POST',
        f'/api/datasets/{c.d}/samples/bulk-set',
        json={'sample_ids': [c.s0], 'metadata': {'label': 'set'}, 'background': True},
    )
    c.job = made['job_id']


async def seed_others_stopped_job(c: Ctx) -> None:
    await seed_others_job(c)
    await c.admin('POST', f'/api/jobs/{c.job}/cancel')


async def seed_refit_job(c: Ctx) -> None:
    """A map re-fit someone else's schedule started."""
    job = await job_queue.enqueue_job(c.pool, 'map_refit', dataset_id=c.d, created_by=OTHER_POWER)
    c.job = str(job['id'])


async def seed_global_job(c: Ctx) -> None:
    """A job on no dataset (a backup an admin started)."""
    job = await job_queue.enqueue_job(c.pool, 'backup', created_by=ADMIN)
    c.job = str(job['id'])


async def seed_cancelled_deletion(c: Ctx) -> None:
    deletion = await c.admin('DELETE', f'/api/datasets/{c.d}')
    await c.admin('POST', f'/api/jobs/{deletion["job_id"]}/cancel')


async def seed_other_share(c: Ctx) -> None:
    await c.w.share(c.d, OTHER_POWER, 'view')


async def seed_group(c: Ctx) -> None:
    await c.admin('POST', '/api/groups', json={'name': f'other-{c.d}'})
    c.group = await c.w.database.fetchval('SELECT id FROM groups WHERE name = $1', f'other-{c.d}')


async def seed_group_share(c: Ctx) -> None:
    await seed_group(c)
    await c.admin('POST', f'/api/datasets/{c.d}/access', json={'group': f'other-{c.d}', 'level': 'view'})


async def seed_group_member(c: Ctx) -> None:
    await seed_group(c)
    await seed_user(c)
    await c.admin('PUT', f'/api/groups/{c.group}/members/{c.user}')


async def seed_user(c: Ctx) -> None:
    """A user nobody else in the test uses."""
    c.user = 1000 + c.d
    await c.w.database.execute(
        "INSERT INTO users (id, username, role) VALUES ($1, $2, 'power_user')",
        c.user,
        f'target-{c.d}',
    )


async def seed_assistant_group(c: Ctx) -> None:
    """A group the assistant's settings let in."""
    await seed_group(c)
    await c.admin('PUT', f'/api/admin/assistant/groups/{c.group}')


async def seed_assistant_user(c: Ctx) -> None:
    """A user with an answer of their own in the assistant's settings."""
    await seed_user(c)
    await c.admin('PUT', f'/api/admin/assistant/users/{c.user}', json={'allowed': True})


async def seed_storage_config(c: Ctx) -> None:
    made = await c.admin('POST', '/api/storage/configs', json={'name': f'config-{c.d}', 'bucket_name': 'bucket'})
    c.config = made['id']


async def seed_follower(c: Ctx) -> None:
    await c.admin('POST', '/api/followers', json={'name': f'follower-{c.d}'})
    c.follower = await c.w.database.fetchval('SELECT id FROM followers WHERE name = $1', f'follower-{c.d}')


# --- The route table ---


@dataclass
class Call:
    url: str
    json: Any = None
    params: Optional[dict] = None

    def kwargs(self) -> dict:
        given: dict[str, Any] = {}
        if self.json is not None:
            given['json'] = self.json
        if self.params is not None:
            given['params'] = self.params
        return given


@dataclass(frozen=True)
class Route:
    """A route, the level the specification gives it, and how to call it validly in a caller's dataset.

    level: view, view-not-guest, tag, edit, manage, or admin (the role, whatever the level).
    case: which of several entries for one route (a body or a row that needs another level).
    by_row: the route finds its row by the row's own id (a view, a job): no level at all answers 404.
    succeeds: at the level the call must succeed (2xx); False where the test environment can't complete it (S3, an
    image encoder, another server, a backup directory) or the row named doesn't exist, and then only "not refused" is
    asserted at the level.
    owners_only: the row is the caller's own (their view): a caller with no level isn't asserted (see the report).
    not_a_refusal: the start of a 403 detail that is about the request, not the caller (a bucket nothing serves).
    """

    method: str
    path: str
    level: str
    call: Callable[[Ctx], Call]
    seeds: tuple[Callable[[Ctx], Awaitable[None]], ...] = ()
    case: str = ''
    by_row: bool = False
    succeeds: bool = True
    owners_only: bool = False
    not_a_refusal: str = ''

    @property
    def id(self) -> str:
        return f'{self.method} {self.path}' + (f' [{self.case}]' if self.case else '')


D = '/api/datasets/{id}'
F = '/api/datasets/{id}/fields/{field_name}'
E = '/api/datasets/{id}/embeddings'


def route(method: str, path: str, level: str, call: Callable[[Ctx], Call], *seeds: Any, **options: Any) -> Route:
    return Route(method, path, level, call, tuple(seeds), **options)


def patch_dataset(field_name: str, level: str, value: Callable[[Ctx], Any], *seeds: Any) -> Route:
    return route(
        'PATCH',
        D,
        level,
        lambda c: Call(f'/api/datasets/{c.d}', {field_name: value(c)}),
        *seeds,
        case=field_name,
    )


ROUTES: list[Route] = [
    # --- datasets.py ---
    route('POST', '/api/datasets', 'admin', lambda c: Call('/api/datasets', {'name': f'created-{c.d}'})),
    # The assistant's settings: administrators only.
    route(
        'PATCH',
        '/api/admin/assistant',
        'admin',
        lambda c: Call('/api/admin/assistant', {'new_datasets_closed': False}),
    ),
    route(
        'PUT',
        '/api/admin/assistant/groups/{group_id}',
        'admin',
        lambda c: Call(f'/api/admin/assistant/groups/{c.group}'),
        seed_group,
    ),
    route(
        'DELETE',
        '/api/admin/assistant/groups/{group_id}',
        'admin',
        lambda c: Call(f'/api/admin/assistant/groups/{c.group}'),
        seed_assistant_group,
    ),
    route(
        'PUT',
        '/api/admin/assistant/users/{user_id}',
        'admin',
        lambda c: Call(f'/api/admin/assistant/users/{c.user}', {'allowed': True}),
        seed_user,
    ),
    route(
        'DELETE',
        '/api/admin/assistant/users/{user_id}',
        'admin',
        lambda c: Call(f'/api/admin/assistant/users/{c.user}'),
        seed_assistant_user,
    ),
    # Queues the boundary set's download; no runner runs it here.
    route(
        'POST',
        '/api/admin/regions/load',
        'admin',
        lambda c: Call('/api/admin/regions/load', {'set': 'natural-earth-admin1'}),
    ),
    patch_dataset('name', 'manage', lambda c: f'renamed-{c.d}'),
    patch_dataset('description', 'edit', lambda c: 'notes'),
    patch_dataset('media_fields', 'edit', lambda c: ['filepath']),
    patch_dataset('grid_media_field', 'edit', lambda c: 'filepath'),
    patch_dataset('modal_media_field', 'edit', lambda c: 'filepath'),
    patch_dataset('segmentation_config', 'edit', lambda c: {}),
    patch_dataset('default_view_id', 'edit', lambda c: c.view, seed_others_public_view),
    patch_dataset('is_global', 'manage', lambda c: True),
    patch_dataset('everyone_level', 'manage', lambda c: 'view'),
    patch_dataset('assistant_closed', 'manage', lambda c: True),
    patch_dataset('storage_config_id', 'admin', lambda c: c.config, seed_storage_config),
    route('PUT', D + '/class-rules/{field_name}', 'edit', lambda c: Call(f'/api/datasets/{c.d}/class-rules/label', {})),
    route('DELETE', D, 'manage', lambda c: Call(f'/api/datasets/{c.d}')),
    route(
        'POST',
        D + '/deletion/resume',
        'manage',
        lambda c: Call(f'/api/datasets/{c.d}/deletion/resume'),
        seed_cancelled_deletion,
    ),
    route(
        'POST',
        D + '/access',
        'manage',
        lambda c: Call(f'/api/datasets/{c.d}/access', {'user_id': OTHER_POWER, 'level': 'view'}),
        case='a user',
    ),
    route(
        'POST',
        D + '/access',
        'manage',
        lambda c: Call(f'/api/datasets/{c.d}/access', {'group': f'other-{c.d}', 'level': 'tag'}),
        seed_group,
        case='a group',
    ),
    route(
        'DELETE',
        D + '/access/{user_id}',
        'manage',
        lambda c: Call(f'/api/datasets/{c.d}/access/{OTHER_POWER}'),
        seed_other_share,
    ),
    route(
        'DELETE',
        D + '/access/groups/{group_id}',
        'manage',
        lambda c: Call(f'/api/datasets/{c.d}/access/groups/{c.group}'),
        seed_group_share,
    ),
    route('GET', D + '/access', 'manage', lambda c: Call(f'/api/datasets/{c.d}/access')),
    route('GET', D + '/access/candidates', 'manage', lambda c: Call(f'/api/datasets/{c.d}/access/candidates')),
    route('POST', D + '/field-stats', 'view', lambda c: Call(f'/api/datasets/{c.d}/field-stats', {})),
    route(
        'POST',
        '/api/datasets/refresh-stats',
        'admin',
        lambda c: Call('/api/datasets/refresh-stats', params={'wait': 'false'}),
    ),
    route(
        'POST',
        D + '/refresh-stats',
        'edit',
        lambda c: Call(f'/api/datasets/{c.d}/refresh-stats', params={'wait': 'false'}),
    ),
    route(
        'POST',
        D + '/array-fields',
        'edit',
        lambda c: Call(f'/api/datasets/{c.d}/array-fields', {'field_name': 'labels_y'}),
    ),
    route(
        'POST',
        F + '/promote',
        'edit',
        lambda c: Call(f'/api/datasets/{c.d}/fields/score/promote', params={'wait': 'false'}),
    ),
    route(
        'POST',
        F + '/demote',
        'edit',
        lambda c: Call(f'/api/datasets/{c.d}/fields/score/demote', params={'wait': 'false'}),
    ),
    route(
        'PATCH',
        F + '/type',
        'edit',
        lambda c: Call(f'/api/datasets/{c.d}/fields/note/type', {'field_type': 'unknown'}),
    ),
    route(
        'POST',
        F + '/display-type',
        'edit',
        lambda c: Call(f'/api/datasets/{c.d}/fields/score/display-type', {'display_type': 'enum'}),
    ),
    route(
        'PATCH',
        F + '/description',
        'edit',
        lambda c: Call(
            f'/api/datasets/{c.d}/fields/score/description',
            {'description': 'How sure the model is.', 'unit': 'probability', 'values_private': True},
        ),
    ),
    route(
        'POST',
        F + '/autocomplete',
        'view',
        lambda c: Call(f'/api/datasets/{c.d}/fields/label/autocomplete', {'q': 'c'}),
    ),
    route('POST', D + '/map_points', 'view', lambda c: Call(f'/api/datasets/{c.d}/map_points', {'zoom': 3})),
    route('POST', D + '/map_extent', 'view', lambda c: Call(f'/api/datasets/{c.d}/map_extent', {})),
    route('POST', D + '/selection_token', 'view-not-guest', lambda c: Call(f'/api/datasets/{c.d}/selection_token', {})),
    route(
        'PUT',
        '/api/datasets/{id}/fields/{field}/overlay_config',
        'edit',
        lambda c: Call(f'/api/datasets/{c.d}/fields/score/overlay_config', {'colormap': 'viridis', 'opacity': 0.5}),
    ),
    route(
        'POST',
        D + '/samples/lookup',
        'view',
        lambda c: Call(f'/api/datasets/{c.d}/samples/lookup', {'field': 'label', 'values': ['cat']}),
    ),
    route('POST', D + '/samples/query', 'view', lambda c: Call(f'/api/datasets/{c.d}/samples/query', {})),
    route('POST', D + '/histogram', 'view', lambda c: Call(f'/api/datasets/{c.d}/histogram', {'field': 'score'})),
    route(
        'POST',
        D + '/histogram/preview',
        'view',
        lambda c: Call(f'/api/datasets/{c.d}/histogram/preview', {'field': 'score'}),
    ),
    route(
        'POST',
        D + '/samples',
        'edit',
        lambda c: Call(f'/api/datasets/{c.d}/samples', {'samples': [{'filepath': f'added-{c.d}.jpg', 'metadata': {}}]}),
    ),
    route('DELETE', D + '/samples/{sample_id}', 'edit', lambda c: Call(f'/api/datasets/{c.d}/samples/{c.s0}')),
    route(
        'PATCH',
        D + '/samples/{sample_id}',
        'tag',
        lambda c: Call(f'/api/datasets/{c.d}/samples/{c.s0}', {'tags': ['seen', 'brand-new']}),
        case='tags only',
    ),
    route(
        'PATCH',
        D + '/samples/{sample_id}',
        'edit',
        lambda c: Call(f'/api/datasets/{c.d}/samples/{c.s0}', {'metadata': {'label': 'bird'}}),
        case='metadata',
    ),
    route(
        'PATCH',
        D + '/samples/{sample_id}',
        'edit',
        lambda c: Call(f'/api/datasets/{c.d}/samples/{c.s0}', {'tags': ['seen'], 'metadata': {'label': 'bird'}}),
        case='tags and metadata',
    ),
    route(
        'PATCH',
        D + '/samples/{sample_id}',
        'edit',
        lambda c: Call(f'/api/datasets/{c.d}/samples/{c.s0}', {'array_fields': {'labels_x': ['a']}}),
        seed_array_field,
        case='another array field',
    ),
    route(
        'POST',
        D + '/samples/bulk-delete',
        'edit',
        lambda c: Call(f'/api/datasets/{c.d}/samples/bulk-delete', {'sample_ids': [c.s0]}),
    ),
    route('POST', D + '/objects/unlinked', 'view', lambda c: Call(f'/api/datasets/{c.d}/objects/unlinked')),
    route(
        'POST',
        D + '/objects/unlinked/delete',
        'edit',
        lambda c: Call(f'/api/datasets/{c.d}/objects/unlinked/delete', {}),
    ),
    route(
        'POST',
        D + '/embeddings/{field_name}/delete',
        'edit',
        lambda c: Call(f'/api/datasets/{c.d}/embeddings/clip/delete', {'sample_ids': [c.s0]}),
        seed_embeddings,
    ),
    route(
        'POST',
        D + '/samples/bulk-tag',
        'tag',
        lambda c: Call(f'/api/datasets/{c.d}/samples/bulk-tag', {'sample_ids': [c.s0], 'add_tags': ['brand-new']}),
        case='tags',
    ),
    route(
        'POST',
        D + '/samples/bulk-tag',
        'tag',
        lambda c: Call(
            f'/api/datasets/{c.d}/samples/bulk-tag',
            {'sample_ids': [c.s0], 'remove_tags': ['seen'], 'field_name': 'tags'},
        ),
        case='tags removed',
    ),
    route(
        'POST',
        D + '/samples/bulk-tag',
        'edit',
        lambda c: Call(
            f'/api/datasets/{c.d}/samples/bulk-tag',
            {'sample_ids': [c.s0], 'add_tags': ['a'], 'field_name': 'labels_x'},
        ),
        seed_array_field,
        case='another array field',
    ),
    route(
        'POST',
        D + '/samples/bulk-set',
        'edit',
        lambda c: Call(f'/api/datasets/{c.d}/samples/bulk-set', {'sample_ids': [c.s0], 'metadata': {'label': 'bird'}}),
    ),
    route(
        'POST',
        F + '/rename',
        'edit',
        lambda c: Call(f'/api/datasets/{c.d}/fields/note/rename', {'new_name': 'note2'}),
    ),
    route(
        'POST',
        F + '/clone',
        'edit',
        lambda c: Call(f'/api/datasets/{c.d}/fields/note/clone', {'new_name': 'note3'}),
    ),
    route('POST', F + '/clear', 'edit', lambda c: Call(f'/api/datasets/{c.d}/fields/note/clear')),
    route(
        'POST',
        D + '/aggregate',
        'view',
        lambda c: Call(f'/api/datasets/{c.d}/aggregate', {'aggregations': [{'field': 'score', 'op': 'mean'}]}),
    ),
    route(
        'POST',
        D + '/samples/tag-counts',
        'view',
        lambda c: Call(f'/api/datasets/{c.d}/samples/tag-counts', {'sample_ids': c.samples}),
    ),
    # --- search.py: the dataset is in the body ---
    route(
        'POST',
        '/api/search',
        'view',
        lambda c: Call('/api/search', {'dataset_id': c.d, 'vector': VECTORS[0], 'field_name': 'clip'}),
        seed_embeddings,
    ),
    route(
        'POST',
        '/api/search/by-image',
        'view',
        lambda c: Call('/api/search/by-image', {'dataset_id': c.d, 'image_base64': PNG}),
        seed_embeddings,
        succeeds=False,  # encodes the image with a CLIP model, which the test environment doesn't have
    ),
    route(
        'POST',
        '/api/search/patches',
        'view',
        lambda c: Call('/api/search/patches', {'dataset_id': c.d, 'sample_id': c.s0, 'patch_scale': '2x2'}),
        seed_patches,
    ),
    route(
        'POST',
        '/api/search/object-to-patches',
        'view',
        lambda c: Call('/api/search/object-to-patches', {'dataset_id': c.d, 'object_sample_id': c.s0}),
        seed_patches,
        seed_object_vector,
    ),
    # --- users.py ---
    route(
        'POST',
        '/api/users',
        'admin',
        lambda c: Call(
            '/api/users',
            {'username': f'new-{c.d}', 'password': 'Correct-Horse-Battery-9', 'role': 'guest'},
        ),
    ),
    route(
        'PATCH',
        '/api/users/{id}',
        'admin',
        lambda c: Call(f'/api/users/{c.user}', {'email': f'u{c.d}@example.com'}),
        seed_user,
    ),
    route('DELETE', '/api/users/{id}', 'admin', lambda c: Call(f'/api/users/{c.user}'), seed_user),
    route(
        'POST',
        '/api/users/{id}/reset-password',
        'admin',
        lambda c: Call(f'/api/users/{c.user}/reset-password', {'password': 'Correct-Horse-Battery-9'}),
        seed_user,
    ),
    route('POST', '/api/groups', 'admin', lambda c: Call('/api/groups', {'name': f'made-{c.d}'})),
    route(
        'PATCH',
        '/api/groups/{id}',
        'admin',
        lambda c: Call(f'/api/groups/{c.group}', {'name': f'renamed-{c.d}'}),
        seed_group,
    ),
    route('DELETE', '/api/groups/{id}', 'admin', lambda c: Call(f'/api/groups/{c.group}'), seed_group),
    route(
        'PUT',
        '/api/groups/{id}/members/{user_id}',
        'admin',
        lambda c: Call(f'/api/groups/{c.group}/members/{c.user}'),
        seed_group,
        seed_user,
    ),
    route(
        'DELETE',
        '/api/groups/{id}/members/{user_id}',
        'admin',
        lambda c: Call(f'/api/groups/{c.group}/members/{c.user}'),
        seed_group_member,
    ),
    # --- views.py ---
    route(
        'POST',
        D + '/views',
        'view',
        lambda c: Call(f'/api/datasets/{c.d}/views', {'name': 'made', 'content': {'filters': []}}),
    ),
    route(
        'DELETE',
        '/api/views/{id}',
        'view',
        lambda c: Call(f'/api/views/{c.view}'),
        seed_own_view,
        case='own view',
        owners_only=True,
    ),
    route(
        'DELETE',
        '/api/views/{id}',
        'edit',
        lambda c: Call(f'/api/views/{c.view}'),
        seed_others_public_view,
        case="another user's public view",
        by_row=True,
    ),
    route(
        'PATCH',
        D + '/views/{view_id}/content',
        'view',
        lambda c: Call(f'/api/datasets/{c.d}/views/{c.view}/content', {'content': {'filters': []}}),
        seed_own_view,
        case='own view',
    ),
    route(
        'PATCH',
        D + '/views/{view_id}/content',
        'edit',
        lambda c: Call(f'/api/datasets/{c.d}/views/{c.view}/content', {'content': {'filters': []}}),
        seed_others_public_view,
        case="another user's public view",
    ),
    route(
        'PATCH',
        D + '/views/{view_id}/share',
        'view',
        lambda c: Call(f'/api/datasets/{c.d}/views/{c.view}/share', {'is_public': True, 'shared_with_user_ids': []}),
        seed_own_view,
        case='own view',
    ),
    route(
        'PATCH',
        D + '/views/{view_id}/share',
        'edit',
        lambda c: Call(
            f'/api/datasets/{c.d}/views/{c.view}/share',
            {'is_public': True, 'shared_with_user_ids': [OTHER_POWER]},
        ),
        seed_others_public_view,
        case="another user's public view",
    ),
    # --- filter_query.py ---
    route(
        'POST',
        D + '/filter_query/parse',
        'view',
        lambda c: Call(f'/api/datasets/{c.d}/filter_query/parse', {'query': 'label = "cat"'}),
    ),
    route(
        'POST',
        D + '/filter_query/format',
        'view',
        lambda c: Call(
            f'/api/datasets/{c.d}/filter_query/format',
            {'filters': [{'field': 'label', 'operator': '=', 'value': 'cat'}]},
        ),
    ),
    # --- embeddings.py ---
    route(
        'POST',
        E + '/patches/import',
        'admin',
        lambda c: Call(f'/api/datasets/{c.d}/embeddings/patches/import', {'prefix': 's3://no-such-bucket/patches/'}),
        succeeds=False,  # reads an S3 prefix
        not_a_refusal='No storage configuration serves',
    ),
    route(
        'POST',
        E + '/patches',
        'edit',
        lambda c: Call(
            f'/api/datasets/{c.d}/embeddings/patches',
            [{'sample_id': c.s0, 'vector': VECTORS[0], 'patch_scale': '2x2', 'patch_row': 1, 'patch_col': 1}],
        ),
    ),
    route(
        'POST',
        E,
        'edit',
        lambda c: Call(
            f'/api/datasets/{c.d}/embeddings',
            [{'sample_id': c.s0, 'vector': VECTORS[0], 'field_name': 'clip'}],
        ),
    ),
    route(
        'POST',
        E + '/field-model',
        'edit',
        lambda c: Call(
            f'/api/datasets/{c.d}/embeddings/field-model',
            {'field_name': 'clip', 'model': 'openai/clip-vit-base-patch32'},
        ),
        seed_embeddings,
    ),
    route(
        'POST',
        E + '/reduce',
        'edit',
        lambda c: Call(f'/api/datasets/{c.d}/embeddings/reduce', {'field_name': 'clip', 'n_neighbors': 3}),
        seed_map_embeddings,
    ),
    route(
        'POST',
        E + '/query',
        'view',
        lambda c: Call(f'/api/datasets/{c.d}/embeddings/query', {'field': 'clip'}),
        seed_embeddings,
    ),
    route(
        'POST',
        E + '/select-region',
        'view',
        lambda c: Call(
            f'/api/datasets/{c.d}/embeddings/select-region',
            {'field': 'clip', 'polygon': [[0, 0], [1, 0], [1, 1]]},
        ),
        seed_embeddings,
    ),
    route(
        'POST',
        E + '/lookup',
        'view',
        lambda c: Call(f'/api/datasets/{c.d}/embeddings/lookup', {'sample_ids': [c.s0]}),
        seed_embeddings,
    ),
    route(
        'POST',
        E + '/lookup-patches',
        'view',
        lambda c: Call(
            f'/api/datasets/{c.d}/embeddings/lookup-patches',
            {'patches': [{'sample_id': c.s0, 'field_name': 'clip_patch_2x2_0_0'}]},
        ),
        seed_patches,
    ),
    route(
        'POST',
        E + '/vectors/by-ids',
        'view',
        lambda c: Call(
            f'/api/datasets/{c.d}/embeddings/vectors/by-ids',
            {'sample_ids': [c.s0], 'field_names': ['clip']},
        ),
        seed_embeddings,
    ),
    route(
        'POST',
        E + '/coordinates',
        'edit',
        lambda c: Call(
            f'/api/datasets/{c.d}/embeddings/coordinates',
            [{'sample_id': c.s0, 'x': 0.5, 'y': 0.5}],
            params={'field_name': 'clip'},
        ),
        seed_embeddings,
    ),
    route(
        'POST',
        E + '/recompute-viz-rank',
        'edit',
        lambda c: Call(f'/api/datasets/{c.d}/embeddings/recompute-viz-rank', params={'field_name': 'clip'}),
        seed_embeddings,
    ),
    route(
        'PUT',
        E + '/placement-hold',
        'edit',
        lambda c: Call(f'/api/datasets/{c.d}/embeddings/placement-hold', params={'field_name': 'clip'}),
        seed_embeddings,
    ),
    route(
        'DELETE',
        E + '/placement-hold',
        'edit',
        lambda c: Call(f'/api/datasets/{c.d}/embeddings/placement-hold', params={'field_name': 'clip'}),
        seed_embeddings,
    ),
    # --- auth.py, tokens.py: the admin-only ones (the rest are each user's own) ---
    route('DELETE', '/api/auth/cleanup-demo-accounts', 'admin', lambda c: Call('/api/auth/cleanup-demo-accounts')),
    route('DELETE', '/api/users/{user_id}/tokens', 'admin', lambda c: Call(f'/api/users/{c.user}/tokens'), seed_user),
    # --- storage.py ---
    route(
        'POST',
        '/api/storage/configs',
        'admin',
        lambda c: Call('/api/storage/configs', {'name': f'made-{c.d}', 'bucket_name': 'bucket'}),
    ),
    route(
        'PUT',
        '/api/storage/configs/{config_id}',
        'admin',
        lambda c: Call(f'/api/storage/configs/{c.config}', {'name': f'renamed-{c.d}'}),
        seed_storage_config,
    ),
    route(
        'DELETE',
        '/api/storage/configs/{config_id}',
        'admin',
        lambda c: Call(f'/api/storage/configs/{c.config}'),
        seed_storage_config,
    ),
    route(
        'POST',
        '/api/storage/configs/{config_id}/test',
        'admin',
        lambda c: Call(f'/api/storage/configs/{c.config}/test'),
        seed_storage_config,
        succeeds=False,  # connects to the bucket
    ),
    # --- hashes.py ---
    route(
        'POST',
        D + '/hashes/by-ids',
        'view',
        lambda c: Call(f'/api/datasets/{c.d}/hashes/by-ids', {'sample_ids': [c.s0], 'field_names': ['phash']}),
        seed_hashes,
    ),
    route('POST', D + '/hashes/stats', 'view', lambda c: Call(f'/api/datasets/{c.d}/hashes/stats', {}), seed_hashes),
    route(
        'POST',
        D + '/hashes/duplicates',
        'view',
        lambda c: Call(f'/api/datasets/{c.d}/hashes/duplicates', {'field_name': 'phash'}),
        seed_hashes,
    ),
    route(
        'POST',
        D + '/hashes/sample-duplicates',
        'view',
        lambda c: Call(f'/api/datasets/{c.d}/hashes/sample-duplicates', {'sample_id': c.s0}),
        seed_hashes,
    ),
    route(
        'POST',
        D + '/hashes/upload',
        'edit',
        lambda c: Call(
            f'/api/datasets/{c.d}/hashes/upload',
            [{'sample_id': c.s0, 'field_name': 'phash', 'hash_hex': 'ffff0000ffff0000'}],
        ),
    ),
    route(
        'POST',
        D + '/hashes/refresh-duplicates',
        'edit',
        lambda c: Call(f'/api/datasets/{c.d}/hashes/refresh-duplicates', params={'config': 'phash', 'wait': 'false'}),
        seed_hashes,
    ),
    # --- versions.py ---
    route('POST', D + '/versions', 'edit', lambda c: Call(f'/api/datasets/{c.d}/versions', {'name': 'made'})),
    route(
        'POST',
        D + '/versions/{version_id}/aggregate',
        'view',
        lambda c: Call(
            f'/api/datasets/{c.d}/versions/{c.version}/aggregate',
            {'aggregations': [{'field': 'score', 'op': 'mean'}]},
        ),
        seed_version,
    ),
    route(
        'POST',
        D + '/versions/{version_id}/samples/query',
        'view',
        lambda c: Call(f'/api/datasets/{c.d}/versions/{c.version}/samples/query', {}),
        seed_version,
    ),
    route(
        'DELETE',
        D + '/versions/{version_id}',
        'manage',
        lambda c: Call(f'/api/datasets/{c.d}/versions/{c.version}'),
        seed_version,
    ),
    route(
        'POST',
        D + '/versions/diff/{version_a}/{version_b}/select',
        'view',
        lambda c: Call(f'/api/datasets/{c.d}/versions/diff/{c.version}/{c.version2}/select'),
        seed_two_versions,
    ),
    route(
        'POST',
        D + '/versions/{version_id}/revert',
        'edit',
        lambda c: Call(f'/api/datasets/{c.d}/versions/{c.version}/revert'),
        seed_version,
    ),
    route(
        'POST',
        D + '/versions/current-diff/{version_id}/select',
        'view',
        lambda c: Call(f'/api/datasets/{c.d}/versions/current-diff/{c.version}/select'),
        seed_version,
    ),
    # --- sampling.py ---
    route(
        'POST',
        '/api/datasets/{dataset_id}/sparse-sample',
        'view',
        lambda c: Call(f'/api/datasets/{c.d}/sparse-sample', {'fields': ['score'], 'count': 2}),
    ),
    # --- quality.py ---
    route(
        'POST',
        D + '/quality/ann-index',
        'edit',
        lambda c: Call(f'/api/datasets/{c.d}/quality/ann-index', {'field': 'clip'}, params={'wait': 'false'}),
        seed_embeddings,
    ),
    route(
        'POST',
        D + '/quality/uniqueness/compute',
        'edit',
        lambda c: Call(
            f'/api/datasets/{c.d}/quality/uniqueness/compute',
            {'field': 'clip', 'k': 1, 'allow_seq_scan': True},
        ),
        seed_embeddings,
    ),
    route(
        'POST',
        D + '/quality/representativeness/centroid',
        'view',
        lambda c: Call(f'/api/datasets/{c.d}/quality/representativeness/centroid', {'field': 'clip'}),
        seed_embeddings,
    ),
    route(
        'POST',
        D + '/quality/representativeness/compute',
        'edit',
        lambda c: Call(
            f'/api/datasets/{c.d}/quality/representativeness/compute',
            {'field': 'clip', 'centroid': '[0.5,0.5,0,0]'},
        ),
        seed_embeddings,
    ),
    route(
        'POST',
        D + '/quality/normalize',
        'edit',
        lambda c: Call(f'/api/datasets/{c.d}/quality/normalize', {'metadata_field': 'score', 'lo': 0.0, 'span': 1.0}),
    ),
    route(
        'POST',
        D + '/quality/duplicates/compute',
        'edit',
        lambda c: Call(
            f'/api/datasets/{c.d}/quality/duplicates/compute',
            {'field': 'clip', 'token': f'dups-{c.d}', 'allow_seq_scan': True},
        ),
        seed_embeddings,
    ),
    route(
        'POST',
        D + '/quality/duplicates/finalize',
        'edit',
        lambda c: Call(f'/api/datasets/{c.d}/quality/duplicates/finalize', {'token': f'dups-{c.d}'}),
    ),
    route(
        'POST',
        D + '/quality/duplicates/groups',
        'view',
        lambda c: Call(f'/api/datasets/{c.d}/quality/duplicates/groups', {}),
    ),
    # --- concepts.py ---
    route(
        'POST',
        D + '/concepts',
        'edit',
        lambda c: Call(
            f'/api/datasets/{c.d}/concepts',
            {'name': 'made', 'kind': 'seed', 'seed_ids': [c.s0]},
            params={'wait': 'false'},
        ),
        seed_embeddings,
    ),
    route(
        'POST',
        D + '/concepts/{concept_id}/rescore',
        'edit',
        lambda c: Call(f'/api/datasets/{c.d}/concepts/{c.concept}/rescore', params={'wait': 'false'}),
        seed_concept,
    ),
    route(
        'POST',
        D + '/concepts/{concept_id}/rebuild',
        'edit',
        lambda c: Call(
            f'/api/datasets/{c.d}/concepts/{c.concept}/rebuild',
            {'seed_ids': [c.s0]},
            params={'wait': 'false'},
        ),
        seed_concept,
    ),
    route(
        'POST',
        D + '/concepts/{concept_id}/refine',
        'edit',
        lambda c: Call(
            f'/api/datasets/{c.d}/concepts/{c.concept}/refine',
            {'exclude_ids': [c.s0]},
            params={'wait': 'false'},
        ),
        seed_concept,
    ),
    route(
        'POST',
        D + '/concepts/{concept_id}/auto-rescore',
        'edit',
        lambda c: Call(f'/api/datasets/{c.d}/concepts/{c.concept}/auto-rescore', {'enabled': False}),
        seed_concept,
    ),
    route(
        'DELETE',
        D + '/concepts/{concept_id}',
        'edit',
        lambda c: Call(f'/api/datasets/{c.d}/concepts/{c.concept}'),
        seed_concept,
    ),
    # --- jobs.py: the job's row decides ---
    route(
        'POST',
        '/api/jobs/{job_id}/cancel',
        'manage',
        lambda c: Call(f'/api/jobs/{c.job}/cancel'),
        seed_others_job,
        case="another user's job",
        by_row=True,
    ),
    route(
        'POST',
        '/api/jobs/{job_id}/cancel',
        'edit',
        lambda c: Call(f'/api/jobs/{c.job}/cancel'),
        seed_refit_job,
        case="another user's map re-fit",
        by_row=True,
    ),
    route(
        'POST',
        '/api/jobs/{job_id}/cancel',
        'admin',
        lambda c: Call(f'/api/jobs/{c.job}/cancel'),
        seed_global_job,
        case='a job on no dataset',
    ),
    route(
        'POST',
        '/api/jobs/{job_id}/resume',
        'manage',
        lambda c: Call(f'/api/jobs/{c.job}/resume'),
        seed_others_stopped_job,
        case="another user's stopped bulk job",
        by_row=True,
    ),
    route(
        'POST',
        '/api/jobs/{job_id}/discard',
        'manage',
        lambda c: Call(f'/api/jobs/{c.job}/discard'),
        seed_others_stopped_job,
        case="another user's stopped bulk job",
        by_row=True,
    ),
    # --- backup_settings.py ---
    route(
        'PUT',
        '/api/admin/backups/schedule',
        'admin',
        lambda c: Call('/api/admin/backups/schedule', {'interval_hours': 24, 'start_hour': 3, 'keep': 7}),
    ),
    route(
        'POST',
        '/api/admin/backups/run',
        'admin',
        lambda c: Call('/api/admin/backups/run'),
        succeeds=False,  # needs a backup directory configured
    ),
    # --- schedules.py ---
    route(
        'POST',
        D + '/schedules',
        'edit',
        lambda c: Call(f'/api/datasets/{c.d}/schedules', {'job_type': 'map_refit', 'target': 'clip'}),
        seed_embeddings,
        case='a map re-fit',
    ),
    route(
        'POST',
        D + '/schedules',
        'admin',
        lambda c: Call(f'/api/datasets/{c.d}/schedules', {'job_type': 'index_review'}),
        case='an index review',
    ),
    route(
        'PUT',
        D + '/schedules/{schedule_id}',
        'edit',
        lambda c: Call(f'/api/datasets/{c.d}/schedules/{c.schedule}', {'frequency': 'daily', 'enabled': True}),
        seed_schedule,
    ),
    route(
        'DELETE',
        D + '/schedules/{schedule_id}',
        'edit',
        lambda c: Call(f'/api/datasets/{c.d}/schedules/{c.schedule}'),
        seed_schedule,
    ),
    route(
        'POST',
        D + '/schedules/{schedule_id}/run',
        'edit',
        lambda c: Call(f'/api/datasets/{c.d}/schedules/{c.schedule}/run'),
        seed_schedule,
    ),
    route(
        'PUT',
        D + '/schedules/{schedule_id}',
        'admin',
        lambda c: Call(f'/api/datasets/{c.d}/schedules/{c.schedule}', {'frequency': 'daily', 'enabled': True}),
        seed_review_schedule,
        case='an index review',
    ),
    route(
        'DELETE',
        D + '/schedules/{schedule_id}',
        'admin',
        lambda c: Call(f'/api/datasets/{c.d}/schedules/{c.schedule}'),
        seed_review_schedule,
        case='an index review',
    ),
    route(
        'POST',
        D + '/schedules/{schedule_id}/run',
        'admin',
        lambda c: Call(f'/api/datasets/{c.d}/schedules/{c.schedule}/run'),
        seed_review_schedule,
        case='an index review',
    ),
    # --- index_advisor.py ---
    route(
        'POST',
        D + '/index-advisor/review',
        'admin',
        lambda c: Call(f'/api/datasets/{c.d}/index-advisor/review', params={'wait': 'false'}),
    ),
    route(
        'POST',
        '/api/admin/index-advisor/review',
        'admin',
        lambda c: Call('/api/admin/index-advisor/review', params={'wait': 'false'}),
    ),
    route(
        'PUT',
        '/api/admin/index-advisor/schedule',
        'admin',
        lambda c: Call('/api/admin/index-advisor/schedule', {'enabled': False}),
    ),
    route(
        'POST',
        '/api/admin/indexes/{name}/remove',
        'admin',
        lambda c: Call(
            '/api/admin/indexes/idx_no_such_index/remove',
            {'definition': 'CREATE INDEX idx_no_such_index ON samples (id)'},
            params={'wait': 'false'},
        ),
        succeeds=False,  # an index that doesn't exist
    ),
    route(
        'POST',
        '/api/admin/indexes/{name}/restore',
        'admin',
        lambda c: Call('/api/admin/indexes/idx_no_such_index/restore', params={'wait': 'false'}),
        succeeds=False,  # an index that was never removed
    ),
    # --- followers.py: the primary's side (the follower's own routes are machine routes) ---
    route('POST', '/api/followers', 'admin', lambda c: Call('/api/followers', {'name': f'made-{c.d}'})),
    route(
        'PUT',
        '/api/followers/{follower_id}/datasets/{dataset_id}',
        'admin',
        lambda c: Call(f'/api/followers/{c.follower}/datasets/{c.d}', {}),
        seed_follower,
    ),
    route(
        'DELETE',
        '/api/followers/{follower_id}/datasets/{dataset_id}',
        'admin',
        lambda c: Call(f'/api/followers/{c.follower}/datasets/{c.d}'),
        seed_follower,
        succeeds=False,  # the dataset isn't followed
    ),
    route(
        'POST',
        '/api/followers/{follower_id}/reset-instance',
        'admin',
        lambda c: Call(f'/api/followers/{c.follower}/reset-instance'),
        seed_follower,
    ),
    route(
        'DELETE',
        '/api/followers/{follower_id}',
        'admin',
        lambda c: Call(f'/api/followers/{c.follower}'),
        seed_follower,
    ),
    # --- primaries.py: the follower's side, links to another server ---
    route(
        'POST',
        '/api/primaries',
        'admin',
        lambda c: Call('/api/primaries', {'url': 'http://127.0.0.1:1', 'token': 'not-a-token'}),
        succeeds=False,  # reaches the primary
    ),
    route(
        'POST',
        '/api/primaries/mirrors/{dataset_id}/keep-separate',
        'admin',
        lambda c: Call(f'/api/primaries/mirrors/{c.d}/keep-separate'),
        succeeds=False,  # the dataset is no mirror
    ),
    route(
        'POST',
        '/api/primaries/mirrors/{dataset_id}/restore-decision',
        'admin',
        lambda c: Call(f'/api/primaries/mirrors/{c.d}/restore-decision', {'decision': 'keep'}),
        succeeds=False,  # the dataset is no mirror
    ),
    route('DELETE', '/api/primaries/{link_id}', 'admin', lambda c: Call('/api/primaries/999999'), succeeds=False),
    # --- compute_launcher.py ---
    route('PUT', '/api/compute-launcher', 'admin', lambda c: Call('/api/compute-launcher', {'enabled': False})),
    route(
        'POST',
        '/api/compute-launcher/check',
        'admin',
        lambda c: Call('/api/compute-launcher/check'),
        succeeds=False,
    ),
]

# Routes authenticated by their own machine tokens, outside the level rule.
MACHINE = {
    ('POST', '/api/workers/register'): 'a compute worker token',
    ('POST', '/api/workers/{worker_id}/heartbeat'): 'a compute worker token',
    ('POST', '/api/workers/{worker_id}/claim'): 'a compute worker token',
    ('POST', '/api/workers/{worker_id}/jobs/{job_id}/heartbeat'): 'a compute worker token',
    ('POST', '/api/workers/{worker_id}/jobs/{job_id}/complete'): 'a compute worker token',
    ('POST', '/api/workers/{worker_id}/jobs/{job_id}/fail'): 'a compute worker token',
    ('POST', '/api/workers/{worker_id}/jobs/{job_id}/release'): 'a compute worker token',
    ('PUT', '/api/workers/{worker_id}/jobs/{job_id}/files/{name}'): 'a compute worker token',
    ('POST', '/api/follower/leave'): "a follower token, allowed by the admin's subscription",
    ('POST', '/api/follower/return'): "a follower token, allowed by the admin's subscription",
    (
        'POST',
        '/api/follower/datasets/{dataset_id}/blocks/samples',
    ): "a follower token, allowed by the admin's subscription",
    ('POST', '/api/follower/datasets/{dataset_id}/vectors'): "a follower token, allowed by the admin's subscription",
    (
        'POST',
        '/api/follower/datasets/{dataset_id}/coordinates',
    ): "a follower token, allowed by the admin's subscription",
    ('POST', '/api/follower/datasets/{dataset_id}/ack'): "a follower token, allowed by the admin's subscription",
    ('POST', '/api/follower/datasets/{dataset_id}/fields'): "a follower token, allowed by the admin's subscription",
    ('POST', '/api/follower/searches/{search_id}'): "a follower token, allowed by the admin's subscription",
    (
        'POST',
        '/api/follower/datasets/{dataset_id}/tags',
    ): "a follower token, allowed by the admin's both-ways subscription",
    (
        'POST',
        '/api/follower/datasets/{dataset_id}/views',
    ): "a follower token, allowed by the admin's both-ways subscription",
    (
        'DELETE',
        '/api/follower/datasets/{dataset_id}/views/{view_id}',
    ): "a follower token, allowed by the admin's both-ways subscription",
}

# Each signed-in user's own: their session, password and tokens.
SELF = {
    ('POST', '/api/auth/login'): 'signing in',
    ('POST', '/api/auth/logout'): 'signing out',
    ('POST', '/api/auth/change-password'): 'their own password',
    ('POST', '/api/auth/demo-login'): 'a demo guest account',
    ('POST', '/api/tokens'): 'their own API token (power_user and up)',
    ('DELETE', '/api/tokens/{token_id}'): 'their own API token',
}


def app_routes() -> set[tuple[str, str]]:
    """(method, path) of every route the app serves, HEAD and OPTIONS aside."""
    found: set[tuple[str, str]] = set()
    pending: list[Any] = list(main.app.routes)
    while pending:
        item = pending.pop()
        contexts = getattr(item, 'effective_route_contexts', None)
        if contexts is not None:  # FastAPI 0.137+: an included router
            pending.extend(contexts())
            continue
        for method in getattr(item, 'methods', None) or ():
            if method not in ('HEAD', 'OPTIONS'):
                found.add((method, item.path))
    return found


def test_every_write_route_has_a_level():
    """A new write endpoint must be given a level here (or be named a machine or self route), and every entry here must
    still be a route of the app."""
    writes = {(method, path) for method, path in app_routes() if method != 'GET'}
    table = {(r.method, r.path) for r in ROUTES if r.method != 'GET'} | MACHINE.keys() | SELF.keys()
    missing = sorted(writes - table)
    stale = sorted(table - writes)
    assert not missing, f'write routes with no level in this table: {missing}'
    assert not stale, f'entries in this table that are no routes of the app: {stale}'
    assert not set(MACHINE) & set(SELF)
    for item in ROUTES:
        assert item.level in ('view', 'view-not-guest', 'tag', 'edit', 'manage', 'admin'), item.id


def expected(item: Route, caller: Caller) -> Optional[tuple[str, Optional[str]]]:
    """('allowed' | 'needs' | 'denied' | 'missing' | 'refused', the level named) for the caller, None: not asserted."""
    if item.level == 'admin':
        return ('allowed', None) if caller.source == 'admin' else ('refused', None)
    if caller.level is None:
        if item.owners_only:
            return None
        return ('missing', None) if item.by_row else ('denied', None)
    if item.level == 'view-not-guest':
        return ('refused', None) if caller.guest else ('allowed', None)
    if rank(caller.level) < rank(item.level):
        return ('needs', item.level)
    return ('allowed', None)


def judge(item: Route, caller: Caller, response: httpx.Response) -> Optional[str]:
    """What's wrong with the response for this caller, or None."""
    outcome = expected(item, caller)
    if outcome is None:
        return None
    kind, level = outcome
    got = f'got {response.status_code} {response.text[:300]}'
    if kind == 'needs':
        if response.status_code != 403 or detail(response) != needs(level or ''):
            return f'expected 403 "{needs(level or "")}", {got}'
    elif kind == 'denied':
        if response.status_code != 403 or detail(response) != DENIED:
            return f'expected 403 "{DENIED}", {got}'
    elif kind == 'missing':
        if response.status_code != 404:
            return f'expected 404 (a row of a dataset the caller cannot open), {got}'
    elif kind == 'refused':
        if response.status_code != 403:
            return f'expected 403, {got}'
    else:
        about_the_request = item.not_a_refusal and str(detail(response)).startswith(item.not_a_refusal)
        if refused(response) and not about_the_request:
            return f'expected the request not to be refused, {got}'
        if item.succeeds and not response.is_success:
            return f'expected success, {got}'
    return None


async def prepare(w: World) -> None:
    """The group the group callers' shares go to, and room in the users' ids for the users the routes create."""
    await w.group('team', POWER)
    await w.database.execute("SELECT setval(pg_get_serial_sequence('users', 'id'), 50000)")


async def call_as(c: Ctx, item: Route) -> httpx.Response:
    for seed in item.seeds:
        await seed(c)
    request = item.call(c)
    return await c.w.call(c.caller.user, item.method, request.url, **request.kwargs())


@pytest.mark.parametrize('item', ROUTES, ids=lambda item: item.id)
def test_each_route_checks_the_callers_level(world, item):
    """Each caller in their own dataset: refused with the exact text below the route's level, not refused at it and
    above. A failure names the route (the test id), each caller and the level the route needs."""

    async def scenario(w):
        await prepare(w)
        failures = []
        for caller in CALLERS:
            c = await open_dataset(w, caller)
            response = await call_as(c, item)
            problem = judge(item, caller, response)
            if problem:
                failures.append(f'  {caller.name} (level {caller.level}): {problem}')
        assert not failures, f'{item.id} needs {item.level}:\n' + '\n'.join(failures)

    world(scenario)


# --- Focused tests ---


async def new_dataset(w: World, *, everyone: Optional[str] = None, is_global: bool = False) -> Ctx:
    """A dataset with three samples only the admins have a level in (and everyone, when it is global)."""
    return await open_dataset(
        w,
        Caller('admin', ADMIN, 'everyone', everyone) if is_global else Caller('', ADMIN, 'admin'),
    )


async def set_share(w: World, dataset_id: int, user_id: int, level: Optional[str]) -> None:
    """The user's own share in the dataset set to `level` (None: removed), behind the API's back."""
    await w.database.execute('DELETE FROM dataset_access WHERE dataset_id = $1 AND user_id = $2', dataset_id, user_id)
    if level is not None:
        await w.share(dataset_id, user_id, level)


def assert_needs(response: httpx.Response, level: str) -> None:
    assert (response.status_code, detail(response)) == (403, needs(level)), response.text


async def sample_state(w: World, sample_id: int) -> tuple:
    row = await w.database.fetchrow('SELECT tags, metadata, labels_x FROM samples WHERE id = $1', sample_id)
    return tuple(row)


DATASET_COLUMNS = (
    'name, description, media_fields, grid_media_field, modal_media_field, segmentation_config, storage_config_id, '
    'is_global, everyone_level, default_view_id'
)


async def dataset_state(w: World, dataset_id: int) -> tuple:
    return tuple(await w.database.fetchrow(f'SELECT {DATASET_COLUMNS} FROM datasets WHERE id = $1', dataset_id))


def test_a_tag_level_changes_the_tags_field_and_nothing_else(world):
    """tag: `tags` through bulk-tag and through a sample PATCH that changes only tags, new tag names included. Any
    other field, alone or beside tags, is edit; refused, it changes nothing."""

    async def scenario(w):
        await prepare(w)
        c = await new_dataset(w)
        await seed_array_field(c)
        await w.share(c.d, POWER, 'tag')
        bulk, sample = f'/api/datasets/{c.d}/samples/bulk-tag', f'/api/datasets/{c.d}/samples/{c.s0}'
        for body in (
            {'sample_ids': [c.s0], 'add_tags': ['never-used-before']},
            {'sample_ids': [c.s0], 'add_tags': ['also-new'], 'field_name': 'tags'},
            {'sample_ids': [c.s0], 'remove_tags': ['seen']},
        ):
            tagged = await w.call(POWER, 'POST', bulk, json=body)
            assert tagged.status_code == 200, (body, tagged.text)
        patched = await w.call(POWER, 'PATCH', sample, json={'tags': ['never-used-before', 'also-new', 'third-new']})
        assert patched.status_code == 200, patched.text
        before = await sample_state(w, c.s0)
        assert before[0] == ['never-used-before', 'also-new', 'third-new']
        changes: list[dict[str, Any]] = [
            {'metadata': {'label': 'bird'}},
            {'tags': ['x'], 'metadata': {'label': 'bird'}},
            {'remove_metadata_keys': ['note']},
            {'tags': ['x'], 'remove_metadata_keys': ['note']},
            {'heading': 90.0},
            {'tags': ['x'], 'heading': 90.0},
            {'array_fields': {'labels_x': ['a']}},
            {'tags': ['x'], 'array_fields': {'labels_x': ['a']}},
        ]
        for change in changes:
            assert_needs(await w.call(POWER, 'PATCH', sample, json=change), 'edit')
        other_field = {'sample_ids': [c.s0], 'add_tags': ['a'], 'field_name': 'labels_x'}
        assert_needs(await w.call(POWER, 'POST', bulk, json=other_field), 'edit')
        bulk_set = {'sample_ids': [c.s0], 'metadata': {'label': 'bird'}}
        assert_needs(await w.call(POWER, 'POST', f'/api/datasets/{c.d}/samples/bulk-set', json=bulk_set), 'edit')
        assert await sample_state(w, c.s0) == before
        # Below tag, the tags are refused too.
        await set_share(w, c.d, POWER, 'view')
        assert_needs(await w.call(POWER, 'POST', bulk, json={'sample_ids': [c.s0], 'add_tags': ['y']}), 'tag')
        assert_needs(await w.call(POWER, 'PATCH', sample, json={'tags': ['y']}), 'tag')
        assert await sample_state(w, c.s0) == before

    world(scenario)


def test_patch_dataset_needs_the_highest_level_among_the_fields_sent(world):
    """One PATCH can carry fields of edit, manage and admin: it needs the highest, and refused it changes nothing (no
    field of it is applied). The matrix has one case per field."""

    async def scenario(w):
        await prepare(w)
        c = await new_dataset(w)
        await seed_storage_config(c)
        shown = await insert_view(c, OTHER_POWER, True, 'shown')
        cases: list[tuple[dict[str, Any], str]] = [
            (
                {'description': 'a', 'media_fields': ['filepath'], 'segmentation_config': {}, 'default_view_id': shown},
                'edit',
            ),
            ({'description': 'b', 'is_global': False}, 'manage'),
            ({'modal_media_field': 'filepath', 'everyone_level': 'tag'}, 'manage'),
            ({'grid_media_field': 'filepath', 'name': f'renamed-{c.d}'}, 'manage'),
            ({'description': 'c', 'storage_config_id': c.config}, 'admin'),
            ({'name': f'again-{c.d}', 'storage_config_id': None}, 'admin'),
            ({'everyone_level': 'view', 'storage_config_id': c.config}, 'admin'),
        ]
        for body, wanted in cases:
            for level in LEVELS:
                await set_share(w, c.d, POWER, level)
                before = await dataset_state(w, c.d)
                response = await w.call(POWER, 'PATCH', f'/api/datasets/{c.d}', json=body)
                if wanted == 'admin' or rank(level) < rank(wanted):
                    assert response.status_code == 403, (body, level, response.text)
                    if wanted != 'admin':
                        assert detail(response) == needs(wanted), (body, level, response.text)
                    assert await dataset_state(w, c.d) == before, (body, level)
                else:
                    assert response.status_code == 200, (body, level, response.text)
                    assert {key: response.json()[key] for key in body} == body, (body, level)
            done = await w.call(ADMIN, 'PATCH', f'/api/datasets/{c.d}', json=body)
            assert done.status_code == 200, (body, done.text)

    world(scenario)


def candidates_in(body: Any) -> tuple[list[dict], list[dict]]:
    """The users (objects with a username) and the groups (other objects with an id and a name) anywhere in a body."""
    users: list[dict] = []
    groups: list[dict] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if 'username' in node:
                users.append(node)
            elif 'id' in node and 'name' in node:
                groups.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(body)
    return users, groups


async def access_rows(w: World, dataset_id: int) -> list:
    return [
        [tuple(row) for row in await w.database.fetch(query, dataset_id)]
        for query in (
            'SELECT user_id, level FROM dataset_access WHERE dataset_id = $1 ORDER BY 1',
            'SELECT group_id, level FROM dataset_group_access WHERE dataset_id = $1 ORDER BY 1',
            'SELECT is_global, everyone_level, name FROM datasets WHERE id = $1',
        )
    ]


def test_a_manager_who_is_not_an_admin_decides_who_has_the_dataset(world):
    """manage: list, grant (up to manage), change and revoke the shares of users and groups, is_global and its
    everyone level, and the access candidates (users as id and username, groups as id and name). An edit user can do
    none of it, and a manager makes no groups, users or datasets."""

    async def scenario(w):
        await prepare(w)
        c = await new_dataset(w)
        red = await w.group('red', OTHER_POWER)
        await w.share(c.d, POWER, 'manage')
        access = f'/api/datasets/{c.d}/access'
        listed = await w.call(POWER, 'GET', access)
        assert listed.status_code == 200, listed.text
        stored = 'SELECT level FROM dataset_access WHERE dataset_id = $1 AND user_id = $2'
        for level in LEVELS:
            granted = await w.call(POWER, 'POST', access, json={'user_id': OTHER_POWER, 'level': level})
            assert granted.is_success, (level, granted.text)
            assert await w.database.fetchval(stored, c.d, OTHER_POWER) == number(level)
        plain = await w.call(POWER, 'POST', access, json={'user_id': GUEST})
        assert plain.is_success, plain.text
        assert await w.database.fetchval(stored, c.d, GUEST) == number('edit')
        group_stored = 'SELECT level FROM dataset_group_access WHERE dataset_id = $1 AND group_id = $2'
        for level in ('tag', 'manage'):
            granted = await w.call(POWER, 'POST', access, json={'group': 'red', 'level': level})
            assert granted.is_success, (level, granted.text)
            assert await w.database.fetchval(group_stored, c.d, red) == number(level)
        beyond = await w.call(POWER, 'POST', access, json={'user_id': OTHER_POWER, 'level': 'admin'})
        assert beyond.status_code in (400, 422), beyond.text
        assert await w.database.fetchval(stored, c.d, OTHER_POWER) == number('manage')
        for body, column, value in (
            ({'is_global': True}, 'is_global', True),
            ({'everyone_level': 'tag'}, 'everyone_level', number('tag')),
        ):
            changed = await w.call(POWER, 'PATCH', f'/api/datasets/{c.d}', json=body)
            assert changed.status_code == 200, (body, changed.text)
            assert await w.database.fetchval(f'SELECT {column} FROM datasets WHERE id = $1', c.d) == value
        candidates = await w.call(POWER, 'GET', f'{access}/candidates')
        assert candidates.status_code == 200, candidates.text
        users, groups = candidates_in(candidates.json())
        assert {user['id'] for user in users} >= {ADMIN, POWER, OTHER_POWER, GUEST}
        assert all(set(user) == {'id', 'username'} for user in users), users
        assert {user['username'] for user in users if user['id'] == OTHER_POWER} == {f'user-{OTHER_POWER}'}
        assert {(group['id'], group['name']) for group in groups} >= {(red, 'red')}
        assert all(set(group) == {'id', 'name'} for group in groups), groups
        revoked = await w.call(POWER, 'DELETE', f'{access}/{OTHER_POWER}')
        assert revoked.is_success, revoked.text
        assert await w.database.fetchval(stored, c.d, OTHER_POWER) is None
        revoked = await w.call(POWER, 'DELETE', f'{access}/groups/{red}')
        assert revoked.is_success, revoked.text
        assert await w.database.fetchval(group_stored, c.d, red) is None

        # Not the server's: groups, users, datasets.
        servers: list[tuple[str, str, Any]] = [
            ('POST', '/api/groups', {'name': 'leads'}),
            ('PUT', f'/api/groups/{red}/members/{POWER}', None),
            ('POST', '/api/users', {'username': 'hired', 'password': 'Correct-Horse-Battery-9', 'role': 'guest'}),
            ('POST', '/api/datasets', {'name': 'mine'}),
        ]
        for method, path, body in servers:
            response = await w.call(POWER, method, path, **({} if body is None else {'json': body}))
            assert response.status_code == 403, (method, path, response.text)

        # An edit user: none of it, and nothing changes.
        await w.share(c.d, OTHER_POWER, 'edit')
        await w.share_with_group(c.d, 'red', 'view')
        before = await access_rows(w, c.d)
        managers: list[tuple[str, str, Any]] = [
            ('GET', access, None),
            ('GET', f'{access}/candidates', None),
            ('POST', access, {'user_id': OTHER_POWER, 'level': 'manage'}),
            ('POST', access, {'user_id': OTHER_GUEST, 'level': 'view'}),
            ('POST', access, {'group': 'red', 'level': 'edit'}),
            ('DELETE', f'{access}/{POWER}', None),
            ('DELETE', f'{access}/groups/{red}', None),
            ('PATCH', f'/api/datasets/{c.d}', {'is_global': False}),
            ('PATCH', f'/api/datasets/{c.d}', {'everyone_level': 'edit'}),
            ('PATCH', f'/api/datasets/{c.d}', {'name': 'taken-over'}),
        ]
        for method, path, body in managers:
            response = await w.call(OTHER_POWER, method, path, **({} if body is None else {'json': body}))
            assert_needs(response, 'manage')
        assert await access_rows(w, c.d) == before

    world(scenario)


def test_views_own_ones_at_view_others_public_ones_at_edit_others_private_ones_never(world):
    """Anyone at view makes, changes, shares and deletes their own views; another user's public view needs edit; another
    user's private view is no one's but its owner's and the admins' (as before)."""

    async def scenario(w):
        await prepare(w)
        c = await new_dataset(w)
        await w.share(c.d, POWER, 'view')
        await w.share(c.d, GUEST, 'manage')
        views = f'/api/datasets/{c.d}/views'
        for user_id in (POWER, GUEST):
            made = await w.call(user_id, 'POST', views, json={'name': f'by-{user_id}', 'content': {'filters': []}})
            assert made.status_code == 200, made.text
            view = made.json()['id']
            own: list[tuple[str, str, Any]] = [
                (
                    'PATCH',
                    f'{views}/{view}/content',
                    {'content': {'filters': [{'field': 'label', 'operator': '=', 'value': 'cat'}]}},
                ),
                ('PATCH', f'{views}/{view}/share', {'is_public': True, 'shared_with_user_ids': []}),
                ('DELETE', f'/api/views/{view}', None),
            ]
            for method, path, body in own:
                response = await w.call(user_id, method, path, **({} if body is None else {'json': body}))
                assert response.status_code == 200, (user_id, method, path, response.text)

        public = await insert_view(c, OTHER_POWER, True, 'theirs')
        others: list[tuple[str, str, Any]] = [
            ('PATCH', f'{views}/{public}/content', {'content': {'filters': []}}),
            ('PATCH', f'{views}/{public}/share', {'is_public': True, 'shared_with_user_ids': [GUEST]}),
            ('DELETE', f'/api/views/{public}', None),
        ]
        for level in ('view', 'tag'):
            await set_share(w, c.d, POWER, level)
            for method, path, body in others:
                assert_needs(await w.call(POWER, method, path, **({} if body is None else {'json': body})), 'edit')
        assert_needs(await w.call(GUEST, 'DELETE', f'/api/views/{public}'), 'edit')  # a guest is at view
        assert await w.database.fetchval('SELECT count(*) FROM views WHERE id = $1', public) == 1
        await set_share(w, c.d, POWER, 'edit')
        for method, path, body in others:
            response = await w.call(POWER, method, path, **({} if body is None else {'json': body}))
            assert response.status_code == 200, (method, path, response.text)
        assert await w.database.fetchval('SELECT count(*) FROM views WHERE id = $1', public) == 0

        private = await insert_view(c, OTHER_POWER, False, 'private')
        await set_share(w, c.d, POWER, 'manage')
        private_calls: list[tuple[str, str, Any, str]] = [
            ('PATCH', f'{views}/{private}/content', {'content': {'filters': []}}, 'Not authorized to update this view'),
            (
                'PATCH',
                f'{views}/{private}/share',
                {'is_public': True, 'shared_with_user_ids': []},
                'Not authorized to share this view',
            ),
            ('DELETE', f'/api/views/{private}', None, 'Not authorized to delete this view'),
        ]
        for method, path, body, refusal in private_calls:
            response = await w.call(POWER, method, path, **({} if body is None else {'json': body}))
            assert (response.status_code, detail(response)) == (403, refusal), (method, path, response.text)
        row = await w.database.fetchrow('SELECT is_public, content FROM views WHERE id = $1', private)
        assert (row['is_public'], row['content']) == (False, {'filters': []})
        assert (await w.call(ADMIN, 'DELETE', f'/api/views/{private}')).status_code == 200

    world(scenario)


async def bulk_job(w: World, user_id: int, dataset_id: int, sample_id: int, operation: str = 'set') -> str:
    """A bulk job `user_id` starts (queued: no runner in these tests)."""
    if operation == 'delete':
        path, body = 'bulk-delete', {'sample_ids': [sample_id], 'background': True}
    else:
        path, body = 'bulk-set', {'sample_ids': [sample_id], 'metadata': {'label': 'set'}, 'background': True}
    started = await w.call(user_id, 'POST', f'/api/datasets/{dataset_id}/samples/{path}', json=body)
    assert started.status_code == 202, started.text
    return started.json()['job_id']


def test_job_control_goes_by_the_level_in_the_jobs_dataset(world):
    """Cancel, resume and discard: manage on the job's dataset, or the job's starter while they hold the level its kind
    needs; a map re-fit at edit; a job on no dataset, admins only; a job of a dataset the caller can't open, 404."""

    async def scenario(w):
        await prepare(w)
        c = await new_dataset(w)
        # A manager stops, resumes and discards someone else's job.
        await w.share(c.d, POWER, 'manage')
        job = await bulk_job(w, ADMIN, c.d, c.s0)
        assert (await w.call(POWER, 'POST', f'/api/jobs/{job}/cancel')).status_code == 200
        assert (await w.call(POWER, 'POST', f'/api/jobs/{job}/resume')).status_code == 202
        assert (await w.call(POWER, 'POST', f'/api/jobs/{job}/cancel')).status_code == 200
        assert (await w.call(POWER, 'POST', f'/api/jobs/{job}/discard')).status_code == 200
        # Below manage, someone else's job is refused.
        await set_share(w, c.d, POWER, 'edit')
        other = await bulk_job(w, ADMIN, c.d, c.s0)
        assert_needs(await w.call(POWER, 'POST', f'/api/jobs/{other}/cancel'), 'manage')
        # The starter, while they hold the job's level (bulk set: edit).
        own = await bulk_job(w, POWER, c.d, c.s0)
        assert (await w.call(POWER, 'POST', f'/api/jobs/{own}/cancel')).status_code == 200
        later = await bulk_job(w, POWER, c.d, c.s0)
        await set_share(w, c.d, POWER, 'view')
        lost = await w.call(POWER, 'POST', f'/api/jobs/{later}/cancel')
        assert lost.status_code == 403, lost.text
        assert detail(lost) in (needs('edit'), needs('manage')), lost.text
        assert await w.database.fetchval('SELECT state FROM jobs WHERE id = $1::uuid', later) == 'queued'
        await set_share(w, c.d, POWER, 'edit')
        assert (await w.call(POWER, 'POST', f'/api/jobs/{later}/cancel')).status_code == 200
        # Resuming a bulk delete the starter stopped: edit now (admins only before).
        deleting = await bulk_job(w, POWER, c.d, c.s0, 'delete')
        assert (await w.call(POWER, 'POST', f'/api/jobs/{deleting}/cancel')).status_code == 200
        resumed = await w.call(POWER, 'POST', f'/api/jobs/{deleting}/resume')
        assert resumed.status_code == 202, resumed.text
        # A map re-fit someone else's schedule started: edit.
        refit = str(
            (await job_queue.enqueue_job(w.database.pool, 'map_refit', dataset_id=c.d, created_by=OTHER_POWER))['id'],
        )
        await set_share(w, c.d, POWER, 'tag')
        assert_needs(await w.call(POWER, 'POST', f'/api/jobs/{refit}/cancel'), 'edit')
        await set_share(w, c.d, POWER, 'edit')
        assert (await w.call(POWER, 'POST', f'/api/jobs/{refit}/cancel')).status_code == 200
        # A job on no dataset: admins only, whatever the caller's levels.
        await set_share(w, c.d, POWER, 'manage')
        backup = str((await job_queue.enqueue_job(w.database.pool, 'backup', created_by=ADMIN))['id'])
        assert (await w.call(POWER, 'POST', f'/api/jobs/{backup}/cancel')).status_code == 403
        assert (await w.call(ADMIN, 'POST', f'/api/jobs/{backup}/cancel')).status_code == 200
        # A job of an admin-only kind (a patch vector import, a follower sync) is the admins' to control even in a
        # dataset the caller manages, and even when they started it.
        for kind in ('patch_vector_import', 'follower_sync'):
            for starter in (ADMIN, POWER):
                admins = await job_queue.enqueue_job(w.database.pool, kind, dataset_id=c.d, created_by=starter)
                for action in ('cancel', 'resume', 'discard'):
                    response = await w.call(POWER, 'POST', f"/api/jobs/{admins['id']}/{action}")
                    assert response.status_code == 403, (kind, starter, action, response.text)
                assert await w.database.fetchval('SELECT state FROM jobs WHERE id = $1', admins['id']) == 'queued'
                assert (await w.call(ADMIN, 'POST', f"/api/jobs/{admins['id']}/cancel")).status_code == 200
        # No level in the job's dataset: as a job that doesn't exist.
        await set_share(w, c.d, POWER, None)
        hidden = await bulk_job(w, ADMIN, c.d, c.s0)
        for action in ('cancel', 'resume', 'discard'):
            response = await w.call(POWER, 'POST', f'/api/jobs/{hidden}/{action}')
            assert (response.status_code, detail(response)) == (404, 'Job not found'), (action, response.text)

    world(scenario)


def test_a_selection_token_needs_view_and_guests_get_none(world):
    async def scenario(w):
        await prepare(w)
        c = await new_dataset(w)
        everyone = await new_dataset(w, is_global=True, everyone='view')
        await w.share(c.d, POWER, 'view')
        await w.share(c.d, GUEST, 'manage')
        for dataset_id in (c.d, everyone.d):
            made = await w.call(POWER, 'POST', f'/api/datasets/{dataset_id}/selection_token', json={})
            assert made.status_code == 200, made.text
            assert made.json()['count'] == 3 and made.json()['token']
            refused_guest = await w.call(GUEST, 'POST', f'/api/datasets/{dataset_id}/selection_token', json={})
            assert refused_guest.status_code == 403, refused_guest.text
        nobody = await w.call(OTHER_POWER, 'POST', f'/api/datasets/{c.d}/selection_token', json={})
        assert (nobody.status_code, detail(nobody)) == (403, DENIED)

    world(scenario)


# The six actions that were admin-only and are edit now.
SIX = {
    'delete a sample': ('DELETE', D + '/samples/{sample_id}', ''),
    'bulk delete': ('POST', D + '/samples/bulk-delete', ''),
    'delete embeddings': ('POST', D + '/embeddings/{field_name}/delete', ''),
    'delete unlinked objects': ('POST', D + '/objects/unlinked/delete', ''),
    'revert to a version': ('POST', D + '/versions/{version_id}/revert', ''),
    'display settings: description': ('PATCH', D, 'description'),
    'display settings: media fields': ('PATCH', D, 'media_fields'),
    'display settings: grid media field': ('PATCH', D, 'grid_media_field'),
    'display settings: modal media field': ('PATCH', D, 'modal_media_field'),
    'display settings: segmentation config': ('PATCH', D, 'segmentation_config'),
    'display settings: default view': ('PATCH', D, 'default_view_id'),
}


def six_routes() -> dict[str, Route]:
    found = {
        name: next(item for item in ROUTES if (item.method, item.path, item.case) == key) for name, key in SIX.items()
    }
    assert all(item.level == 'edit' for item in found.values())
    return found


def test_the_six_formerly_admin_only_actions_are_open_at_edit_and_refused_at_tag(world):
    async def scenario(w):
        await prepare(w)
        for name, item in six_routes().items():
            for caller in (
                Caller('', POWER, 'own', 'edit'),
                Caller('', POWER, 'group', 'edit'),
                Caller('', POWER, 'plain'),
            ):
                response = await call_as(await open_dataset(w, caller), item)
                assert response.is_success, (name, caller.source, response.text)
            for caller in (Caller('', POWER, 'own', 'tag'), Caller('', POWER, 'everyone', 'tag')):
                assert_needs(await call_as(await open_dataset(w, caller), item), 'edit')

    world(scenario)


def test_reads_sent_as_posts_work_at_view_for_guests_too(world):
    """The POSTs that only read (queries, histograms, map points, hashes and vectors by ids, duplicates, version queries
    and aggregates, sparse samples, filter-query parse and format, finding unlinked objects, search) at view."""

    async def scenario(w):
        await prepare(w)
        reads = [item for item in ROUTES if item.method == 'POST' and item.level == 'view' and 'views' not in item.path]
        assert len(reads) >= 30
        for item in reads:
            for caller in (
                Caller('guest', GUEST, 'own', 'manage'),
                Caller('guest on a global dataset', GUEST, 'upgraded'),
                Caller('power user', POWER, 'own', 'view'),
                Caller('power user, everyone view', POWER, 'everyone', 'view'),
            ):
                response = await call_as(await open_dataset(w, caller), item)
                assert not refused(response), (item.id, caller.name, response.text)
                if item.succeeds:
                    assert response.is_success, (item.id, caller.name, response.text)

    world(scenario)


async def linkable_object(c: Ctx) -> int:
    """An object sample whose image is in the dataset but which isn't linked to it yet."""
    added = await datasets.add_samples(
        c.d,
        {'samples': [{'filepath': f'object-{c.d}.jpg', 'media_type': 'object', 'metadata': {'parent_id': c.s0}}]},
        user=ADMIN_USER,
    )
    object_id = added['ids'][0]
    # The triggers keep parent_id linked; an object written before them (or restored past them) is not.
    async with c.pool.acquire() as connection, connection.transaction():
        await connection.execute('SET LOCAL session_replication_role = replica')
        await connection.execute('UPDATE samples SET parent_id = NULL WHERE id = $1', object_id)
    return object_id


def test_finding_unlinked_objects_links_them_only_for_an_edit_user(world):
    """Finding objects with no image is a read (view); the find links the ones whose image is there only for a caller
    who may edit the dataset."""

    async def scenario(w):
        await prepare(w)
        c = await new_dataset(w)
        loose = await linkable_object(c)
        parent = 'SELECT parent_id FROM samples WHERE id = $1'
        for user_id, level in ((POWER, 'view'), (POWER, 'tag'), (GUEST, 'manage')):
            await set_share(w, c.d, user_id, level)
            found = await w.call(user_id, 'POST', f'/api/datasets/{c.d}/objects/unlinked')
            assert found.status_code == 200, (user_id, level, found.text)
            assert found.json()['linked'] == 0, (user_id, level, found.text)
            assert await w.database.fetchval(parent, loose) is None
        await set_share(w, c.d, POWER, 'edit')
        found = await w.call(POWER, 'POST', f'/api/datasets/{c.d}/objects/unlinked')
        assert found.status_code == 200, found.text
        assert found.json()['linked'] == 1, found.text
        assert await w.database.fetchval(parent, loose) == c.s0

    world(scenario)


def test_losing_a_level_takes_effect_on_the_next_request(world):
    async def scenario(w):
        await prepare(w)
        c = await new_dataset(w)
        red = await w.group('red', POWER)
        sample = f'/api/datasets/{c.d}/samples/{c.s0}'
        retag = {'sample_ids': [c.s0], 'add_tags': ['again']}
        bulk = f'/api/datasets/{c.d}/samples/bulk-tag'
        await w.share(c.d, POWER, 'edit')
        assert (await w.call(POWER, 'PATCH', sample, json={'metadata': {'label': 'one'}})).status_code == 200
        lowered = await w.call(ADMIN, 'POST', f'/api/datasets/{c.d}/access', json={'user_id': POWER, 'level': 'tag'})
        assert lowered.is_success, lowered.text
        assert_needs(await w.call(POWER, 'PATCH', sample, json={'metadata': {'label': 'two'}}), 'edit')
        assert (await w.call(POWER, 'POST', bulk, json=retag)).status_code == 200
        lowered = await w.call(ADMIN, 'POST', f'/api/datasets/{c.d}/access', json={'user_id': POWER, 'level': 'view'})
        assert lowered.is_success, lowered.text
        assert_needs(await w.call(POWER, 'POST', bulk, json=retag), 'tag')
        await w.share_with_group(c.d, 'red', 'edit')
        assert (await w.call(POWER, 'PATCH', sample, json={'metadata': {'label': 'three'}})).status_code == 200
        assert (await w.call(ADMIN, 'DELETE', f'/api/groups/{red}/members/{POWER}')).is_success
        assert_needs(await w.call(POWER, 'PATCH', sample, json={'metadata': {'label': 'four'}}), 'edit')
        assert (await w.call(ADMIN, 'DELETE', f'/api/datasets/{c.d}/access/{POWER}')).is_success
        denied = await w.call(POWER, 'POST', bulk, json=retag)
        assert (denied.status_code, detail(denied)) == (403, DENIED)
        # The everyone level of a global dataset, lowered.
        everyone = await new_dataset(w, is_global=True, everyone='edit')
        open_sample = f'/api/datasets/{everyone.d}/samples/{everyone.s0}'
        assert (await w.call(POWER, 'PATCH', open_sample, json={'metadata': {'label': 'one'}})).status_code == 200
        lowered = await w.call(ADMIN, 'PATCH', f'/api/datasets/{everyone.d}', json={'everyone_level': 'view'})
        assert lowered.status_code == 200, lowered.text
        assert_needs(await w.call(POWER, 'PATCH', open_sample, json={'tags': ['x']}), 'tag')

    world(scenario)


def test_a_view_or_job_by_id_is_missing_without_access_and_refused_by_level_with_it(world):
    """A view or a job addressed by its own id, in a dataset the caller can't open: 404, as an id that doesn't exist.
    Once they can open it, they see the row, so too low a level is a 403 naming the level."""

    async def scenario(w):
        await prepare(w)
        c = await new_dataset(w)
        public = await insert_view(c, OTHER_POWER, True, 'theirs')
        job = await bulk_job(w, ADMIN, c.d, c.s0)
        stopped = await bulk_job(w, ADMIN, c.d, c.s0)
        assert (await w.call(ADMIN, 'POST', f'/api/jobs/{stopped}/cancel')).status_code == 200
        missing_view = await w.call(POWER, 'DELETE', '/api/views/999999')
        for method, path in (
            ('DELETE', f'/api/views/{public}'),
            ('POST', f'/api/jobs/{job}/cancel'),
            ('POST', f'/api/jobs/{stopped}/resume'),
            ('POST', f'/api/jobs/{stopped}/discard'),
        ):
            response = await w.call(POWER, method, path)
            assert response.status_code == 404, (method, path, response.text)
        hidden_view = await w.call(POWER, 'DELETE', f'/api/views/{public}')
        assert (hidden_view.status_code, hidden_view.json()) == (missing_view.status_code, missing_view.json())
        await w.share(c.d, POWER, 'view')
        assert_needs(await w.call(POWER, 'DELETE', f'/api/views/{public}'), 'edit')
        assert_needs(await w.call(POWER, 'POST', f'/api/jobs/{job}/cancel'), 'manage')
        assert_needs(await w.call(POWER, 'POST', f'/api/jobs/{stopped}/resume'), 'manage')
        assert_needs(await w.call(POWER, 'POST', f'/api/jobs/{stopped}/discard'), 'manage')
        assert await w.database.fetchval('SELECT count(*) FROM views WHERE id = $1', public) == 1

    world(scenario)


def test_with_only_the_rows_an_upgrade_leaves_power_users_gain_the_six_and_no_more(world):
    """Plain shares (edit) and the datasets already global at the upgrade, at the everyone level it gives them (edit): a
    power user does what they did before plus the six formerly admin-only actions; what was admin-only and is manage
    now stays refused; guests write nothing; admins do everything."""

    async def scenario(w):
        await prepare(w)
        before = [
            item
            for item in ROUTES
            if item.method != 'GET'
            and item.level in ('view-not-guest', 'tag', 'edit')
            and item not in six_routes().values()
        ]
        manage = [item for item in ROUTES if item.level == 'manage' and item.method != 'GET']
        for caller in (Caller('', POWER, 'plain'), Caller('', POWER, 'upgraded')):
            for item in [*before, *six_routes().values()]:
                response = await call_as(await open_dataset(w, caller), item)
                assert not refused(response), (caller.source, item.id, response.text)
            for item in manage:
                assert_needs(await call_as(await open_dataset(w, caller), item), 'manage')
        for caller in (Caller('', GUEST, 'plain'), Caller('', GUEST, 'upgraded')):
            for item in [*before, *six_routes().values(), *manage]:
                response = await call_as(await open_dataset(w, caller), item)
                assert response.status_code == 403, (caller.source, item.id, response.text)
        for item in [*six_routes().values(), *manage]:
            response = await call_as(await open_dataset(w, Caller('', ADMIN, 'admin')), item)
            assert response.is_success, (item.id, response.text)

    world(scenario)


def test_a_dataset_being_deleted_refuses_writes_at_every_level_and_still_answers_reads(world):
    """While a dataset is being deleted every write is refused with 409, as before, whoever asks (managers and admins
    included), the checks made inside handlers too; reads still work, and resuming the deletion is allowed."""

    async def scenario(w):
        await prepare(w)
        for caller in (
            Caller('tag', POWER, 'own', 'tag'),
            Caller('edit', POWER, 'own', 'edit'),
            Caller('manage', POWER, 'group', 'manage'),
            Caller('admin', ADMIN, 'admin'),
        ):
            c = await open_dataset(w, caller)
            public = await insert_view(c, OTHER_POWER, True, 'theirs')
            await c.admin('DELETE', f'/api/datasets/{c.d}')
            writes = [
                ('tag', 'POST', f'/api/datasets/{c.d}/samples/bulk-tag', {'sample_ids': [c.s0], 'add_tags': ['x']}),
                ('tag', 'PATCH', f'/api/datasets/{c.d}/samples/{c.s0}', {'tags': ['x']}),
                (
                    'edit',
                    'POST',
                    f'/api/datasets/{c.d}/samples',
                    {'samples': [{'filepath': 'late.jpg', 'metadata': {}}]},
                ),
                ('edit', 'POST', f'/api/datasets/{c.d}/samples/bulk-set', {'sample_ids': [c.s0], 'metadata': {'a': 1}}),
                ('edit', 'PATCH', f'/api/datasets/{c.d}', {'description': 'late'}),
                ('edit', 'DELETE', f'/api/views/{public}', None),
                ('edit', 'POST', f'/api/datasets/{c.d}/versions', {'name': 'late'}),
                ('manage', 'PATCH', f'/api/datasets/{c.d}', {'name': f'late-{c.d}'}),
                ('manage', 'POST', f'/api/datasets/{c.d}/access', {'user_id': OTHER_POWER, 'level': 'view'}),
            ]
            for level, method, path, body in writes:
                if rank(caller.level) < rank(level):
                    continue
                response = await w.call(caller.user, method, path, **({} if body is None else {'json': body}))
                assert (response.status_code, detail(response)) == (409, SEALED), (
                    caller.name,
                    method,
                    path,
                    response.text,
                )
            for method, path, body in (
                ('GET', f'/api/datasets/{c.d}', None),
                ('POST', f'/api/datasets/{c.d}/samples/query', {}),
                ('POST', f'/api/datasets/{c.d}/histogram', {'field': 'score'}),
            ):
                response = await w.call(caller.user, method, path, **({} if body is None else {'json': body}))
                assert response.status_code == 200, (caller.name, method, path, response.text)
            if rank(caller.level) >= rank('manage'):
                resumed = await w.call(caller.user, 'POST', f'/api/datasets/{c.d}/deletion/resume')
                assert resumed.status_code == 202, (caller.name, resumed.text)

    world(scenario)


def test_a_followers_write_back_is_allowed_by_its_subscription_not_by_a_level(world):
    """A follower's tag and view edits are made on the primary as its account (follower:<name>, a power user with no
    share), allowed by the admin's both-ways subscription: a level check must not refuse them on a private dataset."""

    async def scenario(w):
        await prepare(w)
        c = await new_dataset(w)
        made = await c.admin('POST', '/api/followers', json={'name': 'lab'})
        follower = await w.database.fetchval("SELECT id FROM followers WHERE name = 'lab'")
        await c.admin('PUT', f'/api/followers/{follower}/datasets/{c.d}', json={'tags': 'both', 'views': 'both'})
        headers = {'Authorization': f"Bearer {made['token']}", 'X-Fridge-Instance': 'access-spec-follower'}
        base = f'/api/follower/datasets/{c.d}'
        tagged = await w.client.post(
            f'{base}/tags',
            json={'sample_ids': [c.s0], 'add_tags': ['from-lab']},
            headers=headers,
        )
        assert tagged.status_code == 200, tagged.text
        tags = await w.database.fetchval('SELECT tags FROM samples WHERE id = $1', c.s0)
        assert 'from-lab' in tags
        view = await w.client.post(
            f'{base}/views',
            json={'name': 'made there', 'content': {'filters': []}},
            headers=headers,
        )
        assert view.status_code == 201, view.text
        removed = await w.client.delete(f"{base}/views/{view.json()['id']}", headers=headers)
        assert removed.is_success, removed.text

    world(scenario)


def test_creating_an_api_token_stays_power_user_and_up(world):
    """Not a dataset's: a power user with no level anywhere makes their own token; a guest, even with a manage share,
    makes none."""

    async def scenario(w):
        await prepare(w)
        c = await new_dataset(w)
        await w.share(c.d, GUEST, 'manage')
        made = await w.call(POWER, 'POST', '/api/tokens', json={'name': 'scripts'})
        assert made.is_success, made.text
        refused_guest = await w.call(GUEST, 'POST', '/api/tokens', json={'name': 'scripts'})
        assert refused_guest.status_code == 403, refused_guest.text

    world(scenario)


async def worker_headers(w: World, user_id: int) -> dict:
    """The Authorization header of a new compute worker token of the user."""
    raw = secrets.token_urlsafe(32)
    await w.database.execute(
        "INSERT INTO api_tokens (user_id, token, name, scope) VALUES ($1, $2, 'worker', 'worker')",
        user_id,
        hash_token(raw),
    )
    return {'Authorization': f'Bearer {raw}'}


async def remote_refit(w: World, dataset_id: Optional[int]) -> str:
    """A map re-fit at its fitting step, the part a compute worker runs."""
    job = await job_queue.enqueue_job(w.database.pool, 'map_refit', dataset_id=dataset_id, placement='compute')
    await w.database.execute('UPDATE jobs SET checkpoint = $2 WHERE id = $1', job['id'], {'phase': 'fit'})
    return str(job['id'])


def test_a_compute_worker_claims_and_keeps_a_datasets_jobs_only_while_its_owner_has_edit(world):
    """A worker acts with its owner's rights: it claims a dataset's remote jobs (a map re-fit), and keeps the lease,
    only while its owner has edit there; at view or tag it claims nothing from it, and an owner lowered below edit
    loses the lease (the worker routes answer 409 lease_lost, as for a lease that isn't the worker's). A job on no
    dataset is only an admin's worker's."""

    async def scenario(w):
        await prepare(w)
        mine, admins = await worker_headers(w, POWER), await worker_headers(w, ADMIN)
        registration = {'name': 'desk', 'kinds': ['map_refit']}
        registered = await w.client.post('/api/workers/register', json=registration, headers=mine)
        assert registered.status_code == 200, registered.text
        worker = registered.json()['worker_id']
        registered = await w.client.post('/api/workers/register', json=registration, headers=admins)
        assert registered.status_code == 200, registered.text
        admins_worker = registered.json()['worker_id']

        async def claim(worker_id, headers):
            return await w.client.post(f'/api/workers/{worker_id}/claim', json={}, headers=headers)

        c = await new_dataset(w)
        refit = await remote_refit(w, c.d)
        everyone = await new_dataset(w, is_global=True, everyone='tag')
        open_refit = await remote_refit(w, everyone.d)
        for level in ('view', 'tag'):
            await set_share(w, c.d, POWER, level)
            nothing = await claim(worker, mine)
            assert nothing.status_code == 204, (level, nothing.text)
        await set_share(w, c.d, POWER, 'edit')
        claimed = await claim(worker, mine)
        assert claimed.status_code == 200, claimed.text
        assert claimed.json()['id'] == refit
        lease = {'lease_token': claimed.json()['lease_token']}
        job_path = f'/api/workers/{worker}/jobs/{refit}'
        renewed = await w.client.post(f'{job_path}/heartbeat', json=lease, headers=mine)
        assert renewed.status_code == 200, renewed.text
        await set_share(w, c.d, POWER, 'view')
        for action in ('heartbeat', 'complete'):
            lost = await w.client.post(f'{job_path}/{action}', json=lease, headers=mine)
            assert lost.status_code == 409, (action, lost.text)
            assert lost.json()['detail']['code'] == 'lease_lost', (action, lost.text)
        # The everyone level raised to edit: now its job is the worker's to claim.
        raised = await w.call(ADMIN, 'PATCH', f'/api/datasets/{everyone.d}', json={'everyone_level': 'edit'})
        assert raised.status_code == 200, raised.text
        claimed = await claim(worker, mine)
        assert claimed.status_code == 200, claimed.text
        assert claimed.json()['id'] == open_refit
        # A job on no dataset: not for a manager's worker; an admin's.
        unscoped = await remote_refit(w, None)
        await set_share(w, c.d, POWER, 'manage')
        assert (await claim(worker, mine)).status_code == 204
        taken = await claim(admins_worker, admins)
        assert taken.status_code == 200, taken.text
        assert taken.json()['id'] == unscoped

    world(scenario)


def test_a_mirrors_own_tags_are_tag_level_and_its_other_own_fields_edit(world):
    """On a mirror, its own tags (@local.tags) need tag, like tags: through bulk-tag and through a sample PATCH whose
    only change is that field. Any other of its own fields (@local.<name>) needs edit; refused, nothing changes."""

    async def scenario(w):
        await prepare(w)
        c = await new_dataset(w)
        await w.share(c.d, POWER, 'tag')
        # A mirror of a dataset on another server (origin dataset 9), its samples copied before it became one.
        await w.database.execute(
            'INSERT INTO mirrors (dataset_id, origin_dataset_id, feed_cursor) VALUES ($1, 9, 0)',
            c.d,
        )
        await w.database.execute("UPDATE dataset_fields SET owner = 'primary' WHERE dataset_id = $1", c.d)
        bulk, sample = f'/api/datasets/{c.d}/samples/bulk-tag', f'/api/datasets/{c.d}/samples/{c.s0}'
        local = 'SELECT local_tags, local_metadata FROM samples WHERE id = $1'
        tagged = await w.call(
            POWER,
            'POST',
            bulk,
            json={'sample_ids': [c.s0], 'add_tags': ['seen'], 'field_name': LOCAL_TAGS},
        )
        assert tagged.status_code == 200, tagged.text
        patched = await w.call(POWER, 'PATCH', sample, json={'array_fields': {LOCAL_TAGS: ['checked', 'redo']}})
        assert patched.status_code == 200, patched.text
        before = tuple(await w.database.fetchrow(local, c.s0))
        assert before[0] == ['checked', 'redo']
        changes: list[dict[str, Any]] = [
            {'metadata': {'@local.weeds': 3}},
            {'remove_metadata_keys': ['@local.weeds']},
            {'array_fields': {LOCAL_TAGS: ['x']}, 'metadata': {'@local.weeds': 1}},
        ]
        for change in changes:
            assert_needs(await w.call(POWER, 'PATCH', sample, json=change), 'edit')
        own_field = {'sample_ids': [c.s0], 'metadata': {'@local.weeds': 2}}
        assert_needs(await w.call(POWER, 'POST', f'/api/datasets/{c.d}/samples/bulk-set', json=own_field), 'edit')
        assert tuple(await w.database.fetchrow(local, c.s0)) == before
        await set_share(w, c.d, POWER, 'view')
        local_tag = {'sample_ids': [c.s0], 'add_tags': ['late'], 'field_name': LOCAL_TAGS}
        assert_needs(await w.call(POWER, 'POST', bulk, json=local_tag), 'tag')
        assert_needs(await w.call(POWER, 'PATCH', sample, json={'array_fields': {LOCAL_TAGS: ['late']}}), 'tag')
        assert tuple(await w.database.fetchrow(local, c.s0)) == before
        await set_share(w, c.d, POWER, 'edit')
        written = await w.call(POWER, 'PATCH', sample, json={'metadata': {'@local.weeds': 3}})
        assert written.status_code == 200, written.text
        assert (await w.database.fetchrow(local, c.s0))['local_metadata'] == {'weeds': 3}

    world(scenario)


# An example change for each field of the sample-update body but tags (a valid one: each succeeds at edit).
SAMPLE_CHANGES: dict[str, Any] = {
    'metadata': {'label': 'bird'},
    'remove_metadata_keys': ['note'],
    'location': {'type': 'Point', 'coordinates': [10.0, 20.0]},
    'heading': 90.0,
    'array_fields': {'labels_x': ['a']},
}
# Fields of the body that no edit ever writes, so no change of them succeeds at any level: a sample's path can't be
# changed (paths contract 2.3). The body may carry one so the server can refuse a different value; what each level
# may do with it is test_a_sample_patch_never_changes_the_filepath_at_any_level.
NEVER_WRITTEN = {'filepath'}


def test_a_sample_patch_at_tag_level_changes_tags_and_no_other_field_of_the_model(world):
    """Every field of the sample-update body (datasets.SampleUpdate) other than tags needs edit, alone or beside
    tags. The fields come from the model, so one added later is covered, and one with no example here fails; a field
    no edit ever writes (NEVER_WRITTEN) is checked on its own, below."""
    fields = set(datasets.SampleUpdate.model_fields) - {'tags'} - NEVER_WRITTEN
    assert 'tags' in datasets.SampleUpdate.model_fields
    missing, stale = sorted(fields - SAMPLE_CHANGES.keys()), sorted(SAMPLE_CHANGES.keys() - fields)
    assert not missing, f'fields of SampleUpdate with no example change in SAMPLE_CHANGES: {missing}'
    assert not stale, f'SAMPLE_CHANGES names fields SampleUpdate no longer has: {stale}'

    async def scenario(w):
        await prepare(w)
        c = await new_dataset(w)
        await seed_array_field(c)
        await w.share(c.d, POWER, 'tag')
        sample = f'/api/datasets/{c.d}/samples/{c.s0}'
        state = 'SELECT tags, metadata, labels_x, location::text, heading FROM samples WHERE id = $1'
        before = tuple(await w.database.fetchrow(state, c.s0))
        for field in sorted(fields):
            for body in ({field: SAMPLE_CHANGES[field]}, {'tags': ['x'], field: SAMPLE_CHANGES[field]}):
                assert_needs(await w.call(POWER, 'PATCH', sample, json=body), 'edit')
        assert tuple(await w.database.fetchrow(state, c.s0)) == before
        await set_share(w, c.d, POWER, 'edit')
        for field in sorted(fields):
            done = await w.call(POWER, 'PATCH', sample, json={field: SAMPLE_CHANGES[field]})
            assert done.status_code == 200, (field, done.text)

    world(scenario)


def test_a_sample_patch_never_changes_the_filepath_at_any_level(world):
    """Paths contract 2.3, the field NEVER_WRITTEN: a body carrying a filepath other than the sample's is refused at
    every level and writes nothing, its tags included (below edit by the level or by the path; at edit with 422 and
    the text `filepath can't be changed` as its detail). The sample's own path sent back doesn't raise the level an
    edit needs: tags and the path are still a tag-level edit, and write the tags."""

    async def scenario(w):
        await prepare(w)
        c = await new_dataset(w)
        sample = f'/api/datasets/{c.d}/samples/{c.s0}'
        own = await w.database.fetchval('SELECT filepath FROM samples WHERE id = $1', c.s0)
        state = 'SELECT filepath, tags, metadata, last_modified_at FROM samples WHERE id = $1'
        other = f'moved/{own}'
        for level in ('tag', 'edit'):
            await set_share(w, c.d, POWER, level)
            before = tuple(await w.database.fetchrow(state, c.s0))
            for body in ({'filepath': other}, {'tags': ['x'], 'filepath': other}):
                refused = await w.call(POWER, 'PATCH', sample, json=body)
                said = (refused.status_code, detail(refused))
                if level == 'tag' and refused.status_code == 403:
                    assert said == (403, needs('edit')), (level, body, refused.text)
                else:
                    assert refused.status_code == 422, (level, body, refused.text)
                    assert isinstance(said[1], str) and "filepath can't be changed" in said[1], (level, refused.text)
            assert tuple(await w.database.fetchrow(state, c.s0)) == before, level
        await set_share(w, c.d, POWER, 'tag')
        kept = await w.call(POWER, 'PATCH', sample, json={'tags': ['kept'], 'filepath': own})
        assert kept.status_code == 200, kept.text
        row = await w.database.fetchrow(state, c.s0)
        assert (row['filepath'], row['tags']) == (own, ['kept']), tuple(row)

    world(scenario)


def test_the_access_candidates_are_people_and_groups_not_machine_or_inactive_accounts(world):
    """The candidates a manager picks from are people and groups: no follower's account (follower:<name>; compute
    workers have none, they use their owner's), and no inactive user."""

    async def scenario(w):
        await prepare(w)
        c = await new_dataset(w)
        await w.share(c.d, POWER, 'manage')
        await c.admin('POST', '/api/followers', json={'name': 'lab'})
        machine = await w.database.fetchval("SELECT id FROM users WHERE username = 'follower:lab'")
        assert machine is not None
        await worker_headers(w, OTHER_POWER)  # a worker token: no account of its own
        await w.database.execute('UPDATE users SET is_active = FALSE WHERE id = $1', OTHER_GUEST)
        red = await w.group('red', OTHER_POWER)
        listed = await w.call(POWER, 'GET', f'/api/datasets/{c.d}/access/candidates')
        assert listed.status_code == 200, listed.text
        users, groups = candidates_in(listed.json())
        ids = {user['id'] for user in users}
        assert ids >= {ADMIN, POWER, OTHER_POWER, GUEST}
        assert machine not in ids and OTHER_GUEST not in ids, users
        assert not [user for user in users if user['username'].startswith('follower:')], users
        assert {(group['id'], group['name']) for group in groups} >= {(red, 'red')}
        all_users = await w.database.fetchval('SELECT count(*) FROM users')
        assert len(ids) == all_users - 2, (users, all_users)  # everyone else, once

    world(scenario)


def test_an_edit_user_may_reshare_another_users_public_view_even_make_it_private_and_its_owner_keeps_it(world):
    """Changing another user's public view is edit, its sharing included: an edit user may share it on, or make it
    private. It stays its owner's, who still changes it; private now, no one else does but an admin, a manager not."""

    async def scenario(w):
        await prepare(w)
        c = await new_dataset(w)
        await w.share(c.d, POWER, 'edit')
        await w.share(c.d, OTHER_POWER, 'view')  # its owner, who can open the dataset
        view = await insert_view(c, OTHER_POWER, True, 'theirs')
        views = f'/api/datasets/{c.d}/views/{view}'
        for sharing in (
            {'is_public': True, 'shared_with_user_ids': [GUEST]},
            {'is_public': False, 'shared_with_user_ids': []},
        ):
            shared = await w.call(POWER, 'PATCH', f'{views}/share', json=sharing)
            assert shared.status_code == 200, (sharing, shared.text)
        row = await w.database.fetchrow('SELECT user_id, is_public FROM views WHERE id = $1', view)
        assert (row['user_id'], row['is_public']) == (OTHER_POWER, False)
        calls: list[tuple[str, str, Any, str]] = [
            ('PATCH', f'{views}/content', {'content': {'filters': []}}, 'Not authorized to update this view'),
            (
                'PATCH',
                f'{views}/share',
                {'is_public': True, 'shared_with_user_ids': []},
                'Not authorized to share this view',
            ),
            ('DELETE', f'/api/views/{view}', None, 'Not authorized to delete this view'),
        ]
        for level in ('edit', 'manage'):
            await set_share(w, c.d, POWER, level)
            for method, path, body, refusal in calls:
                response = await w.call(POWER, method, path, **({} if body is None else {'json': body}))
                assert (response.status_code, detail(response)) == (403, refusal), (level, method, path, response.text)
        kept = await w.call(OTHER_POWER, 'PATCH', f'{views}/content', json={'content': {'filters': [CAT_FILTER]}})
        assert kept.status_code == 200, kept.text
        reshared = await w.call(
            OTHER_POWER,
            'PATCH',
            f'{views}/share',
            json={'is_public': True, 'shared_with_user_ids': []},
        )
        assert reshared.status_code == 200, reshared.text

    world(scenario)


CAT_FILTER = {'field': 'label', 'operator': '=', 'value': 'cat'}


# The callers of the job-history table: a power user each, but for the admin and the guest.
MANAGER, EDITOR, OTHER_EDITOR, TAGGER, VIEWER = 30, 31, 32, 33, 34
CONTROL_CALLERS = {
    ADMIN: 'the admin',
    MANAGER: 'the manager',
    EDITOR: 'the edit user who started jobs',
    OTHER_EDITOR: 'an edit user who started none',
    TAGGER: 'the tag user',
    VIEWER: 'the view user',
    GUEST: 'a guest (manage share)',
}


def test_each_jobs_can_control_in_a_datasets_history_matches_what_cancel_accepts(world):
    """Each row of GET /datasets/{id}/jobs says whether this caller may stop, resume or discard the job (can_control),
    exactly as the job-control routes decide: an admin; a manager of the dataset; the job's starter while they hold the
    level its kind needs (a bulk tag of tags or @local.tags: tag; other bulk jobs and unknown kinds: edit; a dataset
    deletion: manage; a patch vector import or a follower sync: admins only, managers included); a map re-fit: anyone
    with edit. An index review of the dataset (a job of no dataset) is the admins'; a guest controls none. For every
    caller and job the flag agrees with what POST /jobs/{id}/cancel answers, and a starter's lowered level flips theirs
    on the next request."""

    async def scenario(w):
        await prepare(w)
        for user_id in (MANAGER, EDITOR, OTHER_EDITOR, TAGGER, VIEWER):
            await w.user(user_id, 'power_user')
        c = await new_dataset(w)
        await seed_array_field(c)
        shares = {
            MANAGER: 'manage',
            EDITOR: 'edit',
            OTHER_EDITOR: 'edit',
            TAGGER: 'edit',
            VIEWER: 'tag',
            GUEST: 'manage',
        }
        for user_id, level in shares.items():
            await w.share(c.d, user_id, level)
        cats = {'field': 'label', 'operator': '=', 'value': 'cat'}

        async def started(user_id: int, path: str, body: dict) -> str:
            """A bulk job the user starts through the API (queued: nothing runs it here)."""
            response = await w.call(
                user_id,
                'POST',
                f'/api/datasets/{c.d}/samples/{path}',
                json={**body, 'background': True},
            )
            assert response.status_code == 202, (user_id, path, response.text)
            return response.json()['job_id']

        async def queued(kind: str, starter: Optional[int], *, dataset: Optional[int] = c.d, args: Any = None) -> str:
            job = await job_queue.enqueue_job(w.database.pool, kind, dataset_id=dataset, created_by=starter, args=args)
            return str(job['id'])

        # Each job, and the callers who control it.
        jobs: dict[str, tuple[str, set[int]]] = {}
        body = {'sample_ids': [c.s0], 'metadata': {'checked': True}}
        jobs['bulk set, by the edit user'] = (await started(EDITOR, 'bulk-set', body), {ADMIN, MANAGER, EDITOR})
        body = {'sample_ids': [c.s0], 'add_tags': ['a'], 'field_name': 'labels_x'}
        jobs['bulk tag of another array field, by the tag user (at edit then)'] = (
            await started(TAGGER, 'bulk-tag', body),
            {ADMIN, MANAGER},
        )
        await set_share(w, c.d, TAGGER, 'tag')
        body = {'filters': cats, 'add_tags': ['seen']}
        jobs['bulk tag of tags, by the tag user'] = (await started(TAGGER, 'bulk-tag', body), {ADMIN, MANAGER, TAGGER})
        body = {'filters': cats, 'add_tags': ['also']}
        jobs['bulk tag of tags, by the view user (at tag then)'] = (
            await started(VIEWER, 'bulk-tag', body),
            {ADMIN, MANAGER},
        )
        await set_share(w, c.d, VIEWER, 'view')
        # The dataset made a mirror of one on another server, for its own tags (@local.tags).
        await w.database.execute(
            'INSERT INTO mirrors (dataset_id, origin_dataset_id, feed_cursor) VALUES ($1, 7, 0)',
            c.d,
        )
        await w.database.execute("UPDATE dataset_fields SET owner = 'primary' WHERE dataset_id = $1", c.d)
        body = {'filters': cats, 'add_tags': ['mine'], 'field_name': LOCAL_TAGS}
        jobs['bulk tag of @local.tags, by the tag user'] = (
            await started(TAGGER, 'bulk-tag', body),
            {ADMIN, MANAGER, TAGGER},
        )
        jobs['map re-fit, by the system'] = (await queued('map_refit', None), {ADMIN, MANAGER, EDITOR, OTHER_EDITOR})
        jobs['dataset deletion, by the edit user'] = (await queued('dataset_deletion', EDITOR), {ADMIN, MANAGER})
        jobs['patch vector import, by the manager'] = (await queued('patch_vector_import', MANAGER), {ADMIN})
        jobs['follower sync, by the manager'] = (await queued('follower_sync', MANAGER), {ADMIN})
        jobs['a kind of no known level, by the edit user'] = (
            await queued('access_spec_unknown_kind', EDITOR),
            {ADMIN, MANAGER, EDITOR},
        )
        jobs['a kind of no known level, by the tag user'] = (
            await queued('access_spec_unknown_kind', TAGGER),
            {ADMIN, MANAGER},
        )
        review = {'scope': 'dataset', 'dataset_id': c.d}
        jobs['index review of the dataset'] = (
            await queued('index_review', MANAGER, dataset=None, args=review),
            {ADMIN},
        )

        async def flags(user_id: int) -> dict[str, bool]:
            listed = await w.call(user_id, 'GET', f'/api/datasets/{c.d}/jobs', params={'limit': 100})
            assert listed.status_code == 200, (user_id, listed.text)
            items = {item['id']: item for item in listed.json()['items']}
            found = {}
            for name, (job_id, _) in jobs.items():
                assert job_id in items, (CONTROL_CALLERS[user_id], name, 'not in the history')
                flag = items[job_id].get('can_control')
                assert isinstance(flag, bool), (CONTROL_CALLERS[user_id], name, items[job_id])
                found[name] = flag
            return found

        problems = []
        for user_id in CONTROL_CALLERS:
            got = await flags(user_id)
            for name, (_, controllers) in jobs.items():
                if got[name] != (user_id in controllers):
                    problems.append(f'{CONTROL_CALLERS[user_id]} / {name}: can_control is {got[name]}')
        assert not problems, 'can_control wrong:\n' + '\n'.join(problems)

        # A starter's level lowered: their flags follow on the next request.
        await set_share(w, c.d, EDITOR, 'tag')
        assert [name for name, flag in (await flags(EDITOR)).items() if flag] == []
        await set_share(w, c.d, EDITOR, 'edit')
        assert (await flags(EDITOR))['bulk set, by the edit user'] is True

        # The route agrees for every pair: refused (403) where the flag is false, accepted where it is true. Refusals
        # first, so a job already cancelled can't hide one.
        for name, (job_id, controllers) in jobs.items():
            for user_id in sorted(CONTROL_CALLERS, key=lambda caller: caller in controllers):
                response = await w.call(user_id, 'POST', f'/api/jobs/{job_id}/cancel')
                wanted = 200 if user_id in controllers else 403
                assert response.status_code == wanted, (CONTROL_CALLERS[user_id], name, response.text)

    world(scenario)
