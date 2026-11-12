"""The SDK sends a Python datetime with no time zone with the machine's offset, so the server reads the instant the
script meant (it reads text without an offset as UTC), in metadata and in filter values alike. A datetime with a time
zone, a date and text are sent as they are. Stubbed: no server."""

import datetime
import json
import os
import time
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from fridge.core.client import Client
from fridge.core.dataset import Dataset
from fridge.core.sample import Sample, _jsonable
from fridge.core.view import F

from backend.datetimes import parse_instant

WINTER = datetime.datetime(2026, 1, 15, 10, 30)  # 16:30 UTC in Chicago (UTC-6)
SUMMER = datetime.datetime(2026, 7, 15, 10, 30, 0, 250000)  # 15:30:00.25 UTC in Chicago (UTC-5)


@pytest.fixture
def chicago():
    saved = os.environ.get('TZ')
    os.environ['TZ'] = 'America/Chicago'
    time.tzset()
    yield
    if saved is None:
        del os.environ['TZ']
    else:
        os.environ['TZ'] = saved
    time.tzset()


class _Server(requests.adapters.BaseAdapter):
    """The other end of the client's real session: answers every request with ``{}`` and keeps what was sent,
    so the request is encoded exactly as it would be for a server."""

    def __init__(self):
        super().__init__()
        self.sent = []

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        self.sent.append((request.url, json.loads(request.body) if request.body else None))
        response = requests.models.Response()
        response.status_code = 200
        response._content = b'{}'
        response.url, response.request = request.url, request
        return response

    def close(self):
        pass


def _client():
    client = Client('http://t/api', api_token='test-token')
    server = _Server()
    client.session.mount('http://t/', server)
    return client, server


def test_a_naive_datetime_is_sent_with_the_machine_offset(chicago):
    sent = _jsonable({'captured_at': WINTER, 'nested': [{'at': SUMMER}]})
    assert sent == {'captured_at': '2026-01-15T10:30:00-06:00', 'nested': [{'at': '2026-07-15T10:30:00.250000-05:00'}]}
    assert parse_instant(sent['captured_at']) == datetime.datetime(2026, 1, 15, 16, 30, tzinfo=datetime.timezone.utc)
    assert json.loads(json.dumps(sent)) == sent


class _Timestamp(datetime.datetime):
    """Like pandas' Timestamp, whose astimezone refuses a naive value."""

    def astimezone(self, tz=None):
        raise TypeError('tz_convert() takes exactly 2 positional arguments (1 given)')


def test_a_naive_datetime_subclass_is_sent_with_the_offset_too(chicago):
    assert _jsonable(_Timestamp(2026, 1, 15, 10, 30)) == '2026-01-15T10:30:00-06:00'


def test_aware_datetimes_dates_and_text_are_sent_as_they_are(chicago):
    east = datetime.timezone(datetime.timedelta(hours=2))
    assert _jsonable(
        [
            WINTER.replace(tzinfo=datetime.timezone.utc),
            WINTER.replace(tzinfo=east),
            WINTER.date(),
            '2026-01-15T10:30:00',
            7,
        ],
    ) == ['2026-01-15T10:30:00+00:00', '2026-01-15T10:30:00+02:00', '2026-01-15', '2026-01-15T10:30:00', 7]


def test_every_metadata_write_sends_the_offset(chicago):
    client, server = _client()
    expected = {'captured_at': '2026-01-15T10:30:00-06:00'}
    assert Sample('a.jpg', metadata={'captured_at': WINTER}).to_dict()['metadata'] == expected
    client.add_samples(1, [{'filepath': 'a.jpg', 'metadata': {'captured_at': WINTER}}])
    client.update_sample(1, 2, metadata={'captured_at': WINTER})
    client.bulk_set_metadata(1, sample_ids=[2], metadata={'captured_at': WINTER})
    client.bulk_set_metadata(1, updates=[{'sample_id': 2, 'metadata': {'captured_at': WINTER}}])
    add, update, same_value, per_sample = [body for _url, body in server.sent]
    assert add['samples'][0]['metadata'] == expected
    assert update['metadata'] == expected
    assert same_value['metadata'] == expected
    assert per_sample['updates'] == [{'sample_id': 2, 'metadata': expected}]


def test_a_filter_value_gets_the_same_offset_as_the_data(chicago):
    assert (F('captured_at') >= WINTER).to_dict() == {
        'field': 'captured_at',
        'operator': '>=',
        'value': '2026-01-15T10:30:00-06:00',
    }
    both = ((F('captured_at') >= WINTER) & F('captured_at').is_in([SUMMER, '2026-01-02'])).to_dict()
    assert [condition['value'] for condition in both['filters']] == [
        '2026-01-15T10:30:00-06:00',
        ['2026-07-15T10:30:00.250000-05:00', '2026-01-02'],
    ]
    dataset = Dataset.__new__(Dataset)  # no network: filter() only builds the view
    view = dataset.filter(captured_at__lt=SUMMER.date(), taken__gte=WINTER)
    assert [condition['value'] for condition in view._filters['filters']] == ['2026-07-15', '2026-01-15T10:30:00-06:00']


def test_a_datetime_anywhere_in_a_request_is_sent_the_same_way(chicago):
    """Every JSON body goes through one encoder, so a filter dict a script builds itself and lookup values get the
    offset too, instead of "Object of type datetime is not JSON serializable"."""
    client, server = _client()
    raw = {'field': 'captured_at', 'operator': '>=', 'value': WINTER}
    sent = {'field': 'captured_at', 'operator': '>=', 'value': '2026-01-15T10:30:00-06:00'}
    client.query_samples(1, filters=raw)
    client.lookup_samples(1, 'captured_at', [WINTER, SUMMER.date(), '2026-01-02'])
    client.get_schema(1, filters=raw)
    (_, query), (_, lookup), (schema_url, _) = server.sent
    assert query['filters'] == sent
    assert lookup['values'] == ['2026-01-15T10:30:00-06:00', '2026-07-15', '2026-01-02']
    assert json.loads(parse_qs(urlsplit(schema_url).query)['filters'][0]) == sent
    with pytest.raises(TypeError, match='not JSON serializable'):
        client.query_samples(1, filters={'field': 'x', 'operator': '=', 'value': object()})
