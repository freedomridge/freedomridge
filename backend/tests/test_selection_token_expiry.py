"""An unknown or expired selection token is a 410, not an empty selection.

Tokens (a Select-All over a large view) are pruned from region_selections after 2 hours. Before,
bulk-tag with such a token answered 200 "Updated tags for 0 samples" while the UI still said
"N selected", and tag-counts showed 0/N for every tag.
"""

import asyncio

import pytest
from fastapi import HTTPException

from backend.routers import datasets


class FakeDB:
    def __init__(self, token_exists):
        self.token_exists = token_exists

    async def fetchval(self, query, *args):
        assert "region_selections" in query
        return self.token_exists


def test_expired_token_is_a_410_for_tagging_and_counts(monkeypatch):
    monkeypatch.setattr(datasets, "db", FakeDB(token_exists=False))
    for call in (
        datasets.bulk_tag_samples(
            1,
            datasets.BulkTagRequest(selection_token="gone", add_tags=["x"]),
            user={"id": 1, "role": "admin"},
        ),
        datasets.get_tag_counts(1, datasets.TagCountsRequest(selection_token="gone")),
    ):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(call)
        assert exc.value.status_code == 410 and "expired" in exc.value.detail


def test_live_token_passes_the_check(monkeypatch):
    monkeypatch.setattr(datasets, "db", FakeDB(token_exists=True))
    asyncio.run(datasets._require_live_selection("alive"))
    asyncio.run(datasets._require_live_selection(None))  # id-list mode: nothing to check
