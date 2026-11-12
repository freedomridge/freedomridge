"""Histogram datetime labels and created_at comparisons are UTC whatever the host's timezone.

Bin labels once came from `datetime.fromtimestamp(epoch)` — the backend host's LOCAL time — so
on a host west of UTC every label was hours early and clicking a bar staged a filter for the
wrong window (0 rows). CI runs in UTC, where the bug cannot show, so these tests pin the process
timezone to America/New_York for their duration.
"""

import datetime
import os
import time

import pytest

from backend.datetimes import literal_window
from backend.routers.datasets import _utc_datetime, _utc_micros_label, parse_filter_node


@pytest.fixture
def non_utc_host():
    old = os.environ.get("TZ")
    os.environ["TZ"] = "America/New_York"
    time.tzset()
    yield
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


def test_bin_labels_are_utc_on_a_non_utc_host(non_utc_host):
    micros = int(datetime.datetime(2024, 2, 1, tzinfo=datetime.timezone.utc).timestamp()) * 1_000_000
    assert datetime.datetime.fromtimestamp(micros / 1e6).hour == 19  # the old label: Jan 31, 19:00 local
    assert _utc_micros_label(micros) == "2024-02-01T00:00:00.000000"
    assert _utc_micros_label(micros + 500_001) == "2024-02-01T00:00:00.500001"


def test_a_label_names_its_own_microsecond():
    # Six fraction digits: a filter reads the label as that exact microsecond, in UTC (no offset), so a bar's
    # `>= min_label AND <= max_label` covers its microseconds and no others.
    label = _utc_micros_label(-1)
    assert label == "1969-12-31T23:59:59.999999"
    window = literal_window(label)
    assert window is not None
    start, end = window
    assert end is None and start == datetime.datetime(1969, 12, 31, 23, 59, 59, 999999, tzinfo=datetime.timezone.utc)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("2024-02-01T00:00:00", datetime.datetime(2024, 2, 1, tzinfo=datetime.timezone.utc)),
        ("2024-02-01", datetime.datetime(2024, 2, 1, tzinfo=datetime.timezone.utc)),
        ("2024-02-01T00:00:00Z", datetime.datetime(2024, 2, 1, tzinfo=datetime.timezone.utc)),
        (
            "2024-02-01T00:00:00-05:00",
            datetime.datetime(2024, 2, 1, tzinfo=datetime.timezone(datetime.timedelta(hours=-5))),
        ),
    ],
)
def test_zone_less_created_at_values_mean_utc(non_utc_host, text, expected):
    # asyncpg binds a NAIVE datetime to timestamptz as the host's local time; an aware one is exact.
    got = _utc_datetime(text)
    assert got.tzinfo is not None and got == expected


def test_created_at_filters_bind_aware_utc(non_utc_host):
    args: list = []
    sql, _ = parse_filter_node({"field": "created_at", "operator": ">=", "value": "2026-06-17T12:00:00"}, args, 1)
    assert sql == "created_at >= $1"
    assert args == [datetime.datetime(2026, 6, 17, 12, tzinfo=datetime.timezone.utc)]
    args = []
    parse_filter_node({"field": "created_at", "operator": "=", "value": "2026-06-17T12:00"}, args, 1)
    assert all(a.tzinfo is not None for a in args)
