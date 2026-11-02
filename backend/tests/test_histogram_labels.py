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

from backend.routers.datasets import _utc_datetime, _utc_label, parse_filter_node


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
    epoch = datetime.datetime(2024, 2, 1, tzinfo=datetime.timezone.utc).timestamp()
    assert datetime.datetime.fromtimestamp(epoch).hour == 19  # the old label: Jan 31, 19:00 local
    assert _utc_label(epoch) == "2024-02-01T00:00:00"
    assert _utc_label(epoch + 0.5) == "2024-02-01T00:00:00.500000"


def test_label_is_zone_less_so_it_sorts_before_stored_offsets():
    # A stored '…T00:00:00+00:00' must satisfy `>= label`; a 'Z' or '+00:00' suffix would not.
    label = _utc_label(0.0)
    assert label == "1970-01-01T00:00:00"
    assert "1970-01-01T00:00:00+00:00" >= label


def test_unrepresentable_epoch_gives_no_label():
    assert _utc_label(1e20) is None


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
