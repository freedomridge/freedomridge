"""Dataset's sharing methods in the SDK: grant_access, revoke_access, set_everyone_level and access_level.

What they refuse before sending anything (ValueError), what they send (no level unless one is given, so an old
script's grant_access(user_id) keeps an existing share's level), and a server's refusal reaching the caller as the
SDK's usual error with the server's text. No server: the SDK's own session sends every request to a transport that
records it and answers as the server does for dataset 9. The live suite (python/fridge/test_sdk.py, Access levels)
checks the same methods against a real backend."""

import json
from urllib.parse import urlsplit

import pytest
import requests
from fridge.core import client as client_module
from fridge.core.client import Client
from fridge.core.dataset import Dataset

BASE = 'http://sdk-access.invalid/api'
ACCESS = '/api/datasets/9/access'
LEVELS = ['view', 'tag', 'edit', 'manage']
REFUSAL = 'This needs manage access to the dataset'
REASONS = {200: 'OK', 201: 'Created', 204: 'No Content', 403: 'Forbidden', 404: 'Not Found'}


class Server(requests.adapters.BaseAdapter):
    """Records each request the SDK sends, as (method, path, JSON body), and answers it as the server would."""

    def __init__(self):
        super().__init__()
        self.sent: list = []
        self.refuse = False  # answer every request with the server's refusal
        self.level = 'manage'  # the caller's level in dataset 9
        self.levels = True  # False: a server from before access levels, which ignores everyone_level and names none
        self.refuse_patch = False  # refuse a change to the dataset it created
        self.made: dict = {}  # the dataset a create made (id 12), as the server holds it

    def dataset(self, **changes):
        row = {
            'id': 9,
            'name': 'train',
            'description': None,
            'media_fields': ['filepath'],
            'is_global': False,
            'everyone_level': 'view',  # a private dataset holds view: where it starts if made global
            'access_level': self.level,
        }
        return {**row, **changes}

    def answer(self, method, path, body):
        if self.refuse:
            return 403, {'detail': REFUSAL}
        if (method, path) == ('GET', '/api/datasets/9'):
            return 200, self.dataset()
        if (method, path) == ('PATCH', '/api/datasets/9'):
            return 200, self.dataset(**body)
        if (method, path) == ('GET', '/api/datasets/9/array-fields'):
            return 200, []
        if (method, path) == ('GET', ACCESS):
            group = {'group_id': 3, 'name': 'team-2', 'level': 'view'}
            user = {'user_id': 5, 'username': 'alex', 'level': 'edit'}
            return 200, {'is_global': False, 'everyone_level': 'view', 'grants': [user], 'group_grants': [group]}
        if method == 'GET' and path in ('/api/datasets', '/api/datasets/'):
            return 200, []  # no dataset of that name yet: Dataset(name) creates it
        if (method, path) == ('POST', '/api/datasets'):
            # The created dataset as the server answers it: private unless is_global, at the level the body names,
            # else view. An older server ignores the level and names none.
            given = {key: value for key, value in body.items() if value is not None}
            self.made = self.dataset(**{'id': 12, 'is_global': False, 'everyone_level': 'view', **given})
            if not self.levels:
                del self.made['everyone_level'], self.made['access_level']
            return 200, dict(self.made)
        if (method, path) == ('GET', '/api/datasets/12'):
            return 200, dict(self.made)
        if (method, path) == ('PATCH', '/api/datasets/12'):
            if self.refuse_patch:
                return 403, {'detail': REFUSAL}
            self.made.update({key: value for key, value in body.items() if value is not None})
            return 200, dict(self.made)
        if (method, path) == ('GET', ACCESS + '/candidates'):
            return 200, {'users': [{'id': 5, 'username': 'alex'}], 'groups': [{'id': 3, 'name': 'team-2'}]}
        if (method, path) == ('POST', ACCESS):
            if body.get('user_id') is not None:
                target = {'user_id': body['user_id']}
            else:
                target = {'group_id': 3, 'group': body['group']}
            return 201, {'dataset_id': 9, **target, 'level': body.get('level') or 'edit'}
        if method == 'DELETE' and path.startswith(ACCESS + '/'):
            return 204, None
        return 404, {'detail': 'Not Found'}

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        path = urlsplit(request.url).path
        body = json.loads(request.body) if request.body else None
        self.sent.append((request.method, path, body))
        status, answer = self.answer(request.method, path, body)
        response = requests.Response()
        response.status_code = status
        response.reason = REASONS.get(status, '')
        response.url = request.url
        response.request = request
        if answer is not None:
            response._content = json.dumps(answer).encode()
            response.headers['Content-Type'] = 'application/json'
        else:
            response._content = b''
        return response

    def close(self):
        pass


@pytest.fixture
def server(monkeypatch):
    fake = Server()
    sdk = Client(BASE)
    sdk.session.mount('http://sdk-access.invalid/', fake)
    monkeypatch.setattr(client_module, '_client', sdk)
    return fake


@pytest.fixture
def dataset(server):
    """Dataset 9 as the SDK opens it, with what opening it sent forgotten."""
    opened = Dataset.from_id(9)
    server.sent.clear()
    return opened


def changes(server):
    """What the SDK sent that would change something: every request but a read."""
    return [(method, path, body) for method, path, body in server.sent if method != 'GET']


def without_nulls(body):
    """A JSON body as the server reads it: a null is a field not sent."""
    return {key: value for key, value in body.items() if value is not None}


@pytest.mark.parametrize(
    'grant',
    [lambda ds: ds.grant_access(5), lambda ds: ds.grant_access(user_id=5)],
    ids=['positional', 'keyword'],
)
def test_grant_access_to_a_user_without_a_level_sends_no_level(server, dataset, grant):
    """An old script's grant_access(user_id): a new share is edit and an existing one keeps its level, because no
    level is sent."""
    assert isinstance(grant(dataset), dict)
    [(method, path, body)] = changes(server)
    assert (method, path) == ('POST', ACCESS)
    assert without_nulls(body) == {'user_id': 5}


def test_grant_access_to_a_group_without_a_level_sends_no_level(server, dataset):
    assert isinstance(dataset.grant_access(group='team-2'), dict)
    [(method, path, body)] = changes(server)
    assert (method, path) == ('POST', ACCESS)
    assert without_nulls(body) == {'group': 'team-2'}


@pytest.mark.parametrize('level', LEVELS)
def test_grant_access_sends_the_level_given(server, dataset, level):
    dataset.grant_access(5, level=level)
    dataset.grant_access(group='team-2', level=level)
    sent = changes(server)
    assert [(method, path) for method, path, _ in sent] == [('POST', ACCESS)] * 2
    assert [without_nulls(body) for _, _, body in sent] == [
        {'user_id': 5, 'level': level},
        {'group': 'team-2', 'level': level},
    ]


@pytest.mark.parametrize(
    'kwargs',
    [
        {},
        {'level': 'view'},
        {'user_id': None, 'group': None},
        {'user_id': 5, 'group': 'team-2'},
        {'user_id': 5, 'group': 'team-2', 'level': 'view'},
    ],
    ids=['neither', 'neither-with-level', 'both-none', 'both', 'both-with-level'],
)
def test_grant_access_needs_exactly_one_target_and_sends_nothing_otherwise(server, dataset, kwargs):
    with pytest.raises(ValueError):
        dataset.grant_access(**kwargs)
    assert server.sent == []


@pytest.mark.parametrize('level', ['owner', 'admin', 'read', 'write', ''])
def test_grant_access_refuses_another_level_and_sends_nothing(server, dataset, level):
    with pytest.raises(ValueError):
        dataset.grant_access(5, level=level)
    with pytest.raises(ValueError):
        dataset.grant_access(group='team-2', level=level)
    assert server.sent == []


@pytest.mark.parametrize(
    'kwargs',
    [{}, {'user_id': None, 'group': None}, {'user_id': 5, 'group': 'team-2'}],
    ids=['neither', 'both-none', 'both'],
)
def test_revoke_access_needs_exactly_one_target_and_sends_nothing_otherwise(server, dataset, kwargs):
    with pytest.raises(ValueError):
        dataset.revoke_access(**kwargs)
    assert server.sent == []


@pytest.mark.parametrize(
    'revoke',
    [lambda ds: ds.revoke_access(5), lambda ds: ds.revoke_access(user_id=5)],
    ids=['positional', 'keyword'],
)
def test_revoke_access_of_a_user_deletes_their_share(server, dataset, revoke):
    assert revoke(dataset) is None
    assert changes(server) == [('DELETE', ACCESS + '/5', None)]


@pytest.mark.parametrize('level', ['manage', 'admin', 'private', 'none', ''])
def test_set_everyone_level_refuses_manage_and_other_levels_and_sends_nothing(server, dataset, level):
    with pytest.raises(ValueError):
        dataset.set_everyone_level(level)
    assert server.sent == []


def patched(server):
    """The dataset fields the SDK's PATCH requests set, in the order sent."""
    sent = changes(server)
    assert sent and all((method, path) == ('PATCH', '/api/datasets/9') for method, path, _ in sent), sent
    fields: dict = {}
    for _, _, body in sent:
        fields.update(body)
    return fields


@pytest.mark.parametrize('level', ['view', 'tag', 'edit'])
def test_set_everyone_level_makes_the_dataset_global_at_that_level(server, dataset, level):
    assert dataset.set_everyone_level(level) is None
    fields = patched(server)
    assert fields['is_global'] is True and fields['everyone_level'] == level, fields


def test_set_everyone_level_none_makes_the_dataset_private(server, dataset):
    assert dataset.set_everyone_level(None) is None
    assert patched(server)['is_global'] is False


@pytest.mark.parametrize('level', LEVELS)
def test_access_level_is_the_callers_level_from_the_server(server, level):
    server.level = level
    assert Dataset.from_id(9).access_level == level


@pytest.mark.parametrize(
    'call',
    [
        lambda ds: ds.list_access(),
        lambda ds: ds.grant_access(5),
        lambda ds: ds.grant_access(5, level='view'),
        lambda ds: ds.grant_access(group='team-2', level='tag'),
        lambda ds: ds.revoke_access(5),
        lambda ds: ds.revoke_access(group='team-2'),
        lambda ds: ds.set_everyone_level('view'),
        lambda ds: ds.set_everyone_level(None),
    ],
    ids=[
        'list_access',
        'grant-user',
        'grant-user-level',
        'grant-group-level',
        'revoke-user',
        'revoke-group',
        'everyone-view',
        'everyone-none',
    ],
)
def test_a_refusal_reaches_the_caller_as_the_sdk_error_with_the_server_text(server, dataset, call):
    server.refuse = True
    with pytest.raises(requests.HTTPError, match=REFUSAL) as raised:
        call(dataset)
    assert raised.value.response is not None and raised.value.response.status_code == 403


# --- Creating a dataset with an everyone level ---

CREATES = {
    'client.create_dataset': lambda **kwargs: client_module._client.create_dataset('made', **kwargs),
    'Dataset': lambda **kwargs: Dataset('made', **kwargs),
}


def created(server):
    """The body of the one create request the SDK sent."""
    [(method, path, body)] = changes(server)
    assert (method, path) == ('POST', '/api/datasets')
    return body


@pytest.mark.parametrize('make', CREATES.values(), ids=CREATES.keys())
@pytest.mark.parametrize('given', [{}, {'everyone_level': None}], ids=['left out', 'None'])
def test_a_dataset_created_without_an_everyone_level_sends_nothing_for_it(server, make, given):
    """Without it, nothing is sent for it: a dataset created global is at view, the server's start."""
    make(is_global=True, **given)
    body = created(server)
    assert 'everyone_level' not in body, body
    assert body['is_global'] is True


@pytest.mark.parametrize('make', CREATES.values(), ids=CREATES.keys())
def test_a_dataset_created_global_at_edit_is_one_create_carrying_both(server, make):
    make(is_global=True, everyone_level='edit')
    body = created(server)
    assert (body['is_global'], body['everyone_level']) == (True, 'edit'), body
    assert (server.made['is_global'], server.made['everyone_level']) == (True, 'edit')


@pytest.mark.parametrize('make', CREATES.values(), ids=CREATES.keys())
@pytest.mark.parametrize('level', ['view', 'tag'])
def test_a_dataset_created_global_at_view_or_tag_is_created_private_with_the_level_then_made_global(
    server,
    make,
    level,
):
    """Below edit, the dataset is never global before its level is set: an older server, where global means every
    power user edits, would otherwise hold it so. First the create, private with the level; then, made global."""
    make(is_global=True, everyone_level=level)
    [(post, posted, create), (patch, patched, change)] = changes(server)
    assert (post, posted, patch, patched) == ('POST', '/api/datasets', 'PATCH', '/api/datasets/12')
    assert (bool(create.get('is_global')), create['everyone_level']) == (False, level), create
    change = without_nulls(change)
    assert change.get('is_global') is True and change.get('everyone_level', level) == level, change
    assert set(change) <= {'is_global', 'everyone_level'}, change
    assert (server.made['is_global'], server.made['everyone_level']) == (True, level)  # the end state, as before


@pytest.mark.parametrize('make', CREATES.values(), ids=CREATES.keys())
@pytest.mark.parametrize('level', ['view', 'tag'])
def test_an_older_server_that_ignores_levels_never_has_the_dataset_global(server, make, level):
    """A server that answers the create without an everyone level ignores levels: nothing more is sent, RuntimeError,
    and the dataset it made stays private (and empty)."""
    server.levels = False
    with pytest.raises(RuntimeError):
        make(is_global=True, everyone_level=level)
    body = created(server)  # exactly one request that changes something: the create
    assert (bool(body.get('is_global')), body['everyone_level']) == (False, level), body
    assert server.made['is_global'] is False


@pytest.mark.parametrize('make', CREATES.values(), ids=CREATES.keys())
def test_making_it_global_failing_after_a_good_create_raises_the_servers_error(server, make):
    server.refuse_patch = True
    with pytest.raises((requests.HTTPError, RuntimeError), match=REFUSAL):
        make(is_global=True, everyone_level='tag')
    assert [(method, path) for method, path, _ in changes(server)] == [
        ('POST', '/api/datasets'),
        ('PATCH', '/api/datasets/12'),
    ]
    assert server.made['is_global'] is False


def test_the_client_raises_the_sdk_error_with_the_status_when_making_it_global_fails(server):
    server.refuse_patch = True
    with pytest.raises(requests.HTTPError, match=REFUSAL) as raised:
        client_module._client.create_dataset('made', is_global=True, everyone_level='view')
    assert raised.value.response is not None and raised.value.response.status_code == 403


@pytest.mark.parametrize('make', CREATES.values(), ids=CREATES.keys())
def test_an_everyone_level_without_is_global_is_sent_with_the_dataset_private(server, make):
    """Private, it holds the level for when it is made global."""
    make(everyone_level='tag')
    body = created(server)
    assert (bool(body.get('is_global')), body['everyone_level']) == (False, 'tag'), body


@pytest.mark.parametrize('make', CREATES.values(), ids=CREATES.keys())
@pytest.mark.parametrize('level', ['manage', 'owner', 'admin', 'Edit', ''])
def test_another_everyone_level_raises_value_error_before_any_request(server, make, level):
    with pytest.raises(ValueError):
        make(is_global=True, everyone_level=level)
    assert server.sent == []
