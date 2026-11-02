"""Ingest contract .

regression: a client-supplied created_at was silently dropped, so syncing between instances reset every
sample's date. regression: metadata keys naming sample columns (id, dataset_id, ...) were registered as
fields beside the real columns. The live round trip (insert honours it, update keeps the original,
the response counts distinct samples) was verified against a backend serving this branch.
"""

import datetime

import pytest
from pydantic import ValidationError

from backend.routers.datasets import SampleCreate

UTC = datetime.timezone.utc


def test_created_at_is_accepted_and_zone_less_means_utc():
    assert SampleCreate(filepath="x.jpg").created_at is None  # -> now() on insert
    s = SampleCreate(filepath="x.jpg", created_at="2001-06-01T12:00:00")
    assert s.created_at == datetime.datetime(2001, 6, 1, 12, tzinfo=UTC)
    s = SampleCreate(filepath="x.jpg", created_at="2001-01-01T00:00:00-06:00")
    assert s.created_at is not None and s.created_at.utcoffset() == datetime.timedelta(hours=-6)


@pytest.mark.parametrize("key", ["id", "dataset_id", "filepath", "created_at", "last_modified_at"])
def test_metadata_keys_naming_sample_columns_are_refused(key):
    with pytest.raises(ValidationError) as exc:
        SampleCreate(filepath="x.jpg", metadata={key: 1, "ok": 2})
    assert key in str(exc.value)


def test_imported_metadata_keys_stay_allowed():
    # Importers may carry heading and tags in metadata alongside the sample columns.
    s = SampleCreate(filepath="x.jpg", metadata={"heading": 12.5, "tags": "x", "media_type": "image"})
    assert s.metadata["heading"] == 12.5
