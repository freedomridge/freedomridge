"""SDK paging and hash transfer.

DatasetView.iter_samples paged with OFFSET and never sent skip_count, so the backend ran a
full `SELECT count(*)` on EVERY page. On a 3M-sample dataset `for s in dataset.all()` meant
~3,000 requests, ~3,000 full counts and ~4.5e9 rows skipped by OFFSET. Dataset.iter_samples next
door had already been fixed to keyset + skip_count; this makes the two agree.

sync_between_instances read hashes from `sample['metadata'][field_name]` — where a hash has
never lived — so it transferred ZERO hashes and then printed "✅ Sync complete". The read side it
needed (`GET /hashes/export`) simply didn't exist.

Stubbed and self-contained (no DB, no TestClient) to match the rest of the suite.
"""

import asyncio

import pytest
from fridge.core.view import DatasetView

from backend.routers import hashes as hashes_router

# --------------------------------------------------------------------------------------------
# DatasetView.iter_samples
# --------------------------------------------------------------------------------------------


class FakeResponse:
    ok = True
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class RecordingSession:
    """Serves `n_rows` rows in pages, recording every request payload.

    Ids are deliberately NON-CONTIGUOUS (`(i+1) * id_stride`). Real sample ids have gaps — deletes,
    other datasets, concurrent inserts — and a fake with ids 1..n lets a cursor bug hide, because
    `after_id` and `offset` happen to coincide numerically.

    Models the server's two branches (backend/routers/datasets.py): `after_id` -> `id > after_id
    ORDER BY id LIMIT n` with OFFSET ignored; otherwise `LIMIT n OFFSET k`. `total` is omitted when
    skip_count is set, mirroring the endpoint.
    """

    def __init__(self, n_rows, page_size, id_stride=7):
        self.n_rows, self.page_size = n_rows, page_size
        self.all_ids = [(i + 1) * id_stride for i in range(n_rows)]
        self.requests = []

    def post(self, url, json=None, **kw):
        self.requests.append(json)
        limit = json.get("limit", 100)
        if json.get("after_id") is not None:
            # Keyset: the server ignores OFFSET entirely on this branch.
            remaining = [i for i in self.all_ids if i > json["after_id"]]
        else:
            remaining = self.all_ids[json.get("offset", 0) :]
        ids = remaining[:limit]
        payload = {"items": [{"id": i, "filepath": f"s3://x/{i}.jpg"} for i in ids]}
        if not json.get("skip_count"):
            payload["total"] = self.n_rows
        return FakeResponse(payload)


class FakeClient:
    base_url = "http://test/api"

    def __init__(self, session):
        self.session = session


class FakeDataset:
    _id = 1

    def __init__(self, session):
        self.client = FakeClient(session)


def _iterate(n_rows=25, page_size=10, sort_by=None, skip=0, limit=None):
    session = RecordingSession(n_rows, page_size)
    view = DatasetView(FakeDataset(session))
    if sort_by:
        view._sort_by, view._sort_order = sort_by, "asc"
    if skip:
        view._offset_val = skip
    if limit:
        view._limit_val = limit
    ids = [s.id for s in view.iter_samples(batch_size=page_size)]
    return ids, session.requests, session.all_ids


def test_iteration_returns_every_row_exactly_once():
    """The whole point — a faster pager that drops or repeats rows is worthless."""
    ids, _, expected = _iterate(n_rows=25, page_size=10)
    assert ids == expected


@pytest.mark.parametrize("n_rows", [0, 1, 9, 10, 11, 100])
def test_iteration_is_exact_at_page_boundaries(n_rows):
    """Off-by-one at a page boundary is the classic keyset bug."""
    ids, _, expected = _iterate(n_rows=n_rows, page_size=10)
    assert ids == expected


# skip()/offset() is public, chainable API and docs/SDK.md documents
# `skip(0).limit(100)` / `skip(100).limit(100)` as THE pagination recipe. The first cut of the
# keyset rewrite zeroed the offset on every page, so an unsorted view ignored skip() entirely and
# that recipe returned page 1 forever — silently. These are the guards.


@pytest.mark.parametrize("skip", [0, 1, 5, 10, 24, 25, 30])
@pytest.mark.parametrize("sort_by", [None, "brightness"])
def test_skip_is_honoured_on_both_paths(skip, sort_by):
    ids, _, all_ids = _iterate(n_rows=25, page_size=10, skip=skip, sort_by=sort_by)
    assert ids == all_ids[skip:], f"skip({skip}) with sort_by={sort_by!r} returned the wrong window"


def test_the_documented_pagination_recipe_returns_disjoint_pages():
    """docs/SDK.md: page_1 = skip(0).limit(N); page_2 = skip(N).limit(N) — must not overlap."""
    page_1, _, all_ids = _iterate(n_rows=25, page_size=10, skip=0, limit=10)
    page_2, _, _ = _iterate(n_rows=25, page_size=10, skip=10, limit=10)
    assert page_1 == all_ids[:10]
    assert page_2 == all_ids[10:20]
    assert not set(page_1) & set(page_2), "consecutive pages must be disjoint"


def test_skip_still_pays_only_one_offset_then_rides_the_cursor():
    """The offset is applied once; later pages stay on the cheap keyset path."""
    _, requests, _ = _iterate(n_rows=25, page_size=10, skip=5)
    assert requests[0].get("offset") == 5 and requests[0].get("after_id") is None
    assert all(r.get("after_id") is not None for r in requests[1:])
    assert all(r.get("offset") in (0, None) for r in requests[1:])


def test_counts_once_and_pages_by_keyset():
    """One COUNT for the whole iteration, and no OFFSET re-scans."""
    _, requests, _ = _iterate(n_rows=25, page_size=10)
    counted = [r for r in requests if not r.get("skip_count")]
    assert len(counted) == 1, f"expected exactly ONE counting request, got {len(counted)}"
    assert requests[0].get("after_id") is None, "the first page has no cursor"
    assert all(r.get("after_id") is not None for r in requests[1:]), "later pages must ride the cursor"
    assert all(r.get("offset") in (0, None) for r in requests), "keyset paging must not use OFFSET"


def test_a_requested_sort_falls_back_to_offset():
    """The server's keyset branch orders by id, so after_id would silently discard sort_by."""
    ids, requests, expected = _iterate(n_rows=25, page_size=10, sort_by="brightness")
    assert ids == expected
    assert all(r.get("after_id") is None for r in requests), "sorted iteration must not use keyset"
    assert [r.get("offset") for r in requests] == [0, 10, 20]
    # still counts only once
    assert len([r for r in requests if not r.get("skip_count")]) == 1


def test_limit_is_respected_and_stops_early():
    ids, _, all_ids = _iterate(n_rows=1000, page_size=10, limit=15)
    assert ids == all_ids[:15]


def test_execute_query_sends_the_paging_params_it_is_given():
    session = RecordingSession(5, 10)
    DatasetView(FakeDataset(session))._execute_query(after_id=7, skip_count=True)
    assert session.requests[-1]["after_id"] == 7
    assert session.requests[-1]["skip_count"] is True


# --------------------------------------------------------------------------------------------
# GET /hashes/export
# --------------------------------------------------------------------------------------------


class FakeHashDB:
    """One hash field over sample ids 1..n, honouring the keyset window."""

    def __init__(self, n):
        self.n = n
        self.queries = []

    async def fetch(self, query, *args):
        self.queries.append((query, args))
        _dataset_id, field_name, after_id, limit = args
        ids = [i for i in range(after_id + 1, min(after_id + limit, self.n) + 1)]
        return [{"sample_id": i, "field_name": field_name, "hash_hex": f"{i:016x}"} for i in ids]


def test_export_pages_the_whole_field_by_keyset(monkeypatch):
    db = FakeHashDB(25)
    monkeypatch.setattr(hashes_router, "db", db)

    seen, after = [], 0
    while True:
        items = asyncio.run(hashes_router.export_hashes(id=1, field_name="phash", after_id=after, limit=10))["items"]
        if not items:
            break
        seen.extend(items)
        after = items[-1]["sample_id"]
        if len(items) < 10:
            break

    assert [i["sample_id"] for i in seen] == list(range(1, 26)), "export must return every hash once"
    assert all(i["hash_hex"] for i in seen), "hash_hex must be populated — the whole point"


def test_export_is_scoped_to_the_dataset_and_field():
    """A cross-dataset read here would leak hashes between datasets."""
    import inspect

    src = inspect.getsource(hashes_router.export_hashes)
    assert "s.dataset_id = $1" in src, "export must be scoped to the dataset"
    assert "h.field_name = $2" in src
    assert "h.sample_id > $3" in src and "ORDER BY h.sample_id" in src, "must be keyset-ordered"


def test_export_page_is_bounded_but_paging_reaches_everything():
    import inspect

    sig = inspect.signature(hashes_router.export_hashes)
    bounds = {type(m).__name__: getattr(m, type(m).__name__.lower()) for m in sig.parameters["limit"].default.metadata}
    assert bounds.get("Le") == hashes_router._MAX_EXPORT_PAGE
    assert "after_id" in sig.parameters, "the bound is only harmless because callers can page past it"


def test_sync_no_longer_reads_hashes_out_of_sample_metadata():
    """The original bug, pinned: hashes are rows in sample_hashes, never in a sample's metadata."""
    import inspect

    from fridge import sync_between_instances

    src = inspect.getsource(sync_between_instances.sync)
    assert "export_hashes" in src, "the sync must page the real hash export"
    assert ".get('metadata', {}).get(field_name)" not in src, "hashes are not in sample metadata"
    # A short transfer must not be reported as success.
    assert "Sync INCOMPLETE" in src and "SystemExit(1)" in src
