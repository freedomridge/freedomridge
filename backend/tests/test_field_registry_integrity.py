"""Field rename / clone / clear / promote guard rails.

Renaming or cloning onto a key that already holds values overwrote it on every sample.
Promoting a field with no values built an index on nothing; any display_type string and
an empty media_fields list (or a grid field outside it) were stored.
"""

import asyncio
from contextlib import asynccontextmanager

import pytest
from fastapi import HTTPException

from backend.routers import datasets

USER = {"id": 1, "role": "admin"}


class FakeDB:
    """fetchval answers by substring; nothing else should be reached on a refused request."""

    def __init__(self, answers):
        self.answers = answers
        self.writes: list[str] = []
        self.pool = self

    @asynccontextmanager
    async def acquire(self):
        yield self

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def fetchval(self, query, *args):
        if 'pg_try_advisory' in query:
            return True
        if 'require_writable_dataset' in query:
            return None
        if 'SELECT origin' in query:
            return 'metadata'
        for key, value in self.answers.items():
            if key in query:
                return value
        raise AssertionError(f"unexpected fetchval: {query}")

    async def fetchrow(self, query, *args):
        if 'SELECT origin' in query:
            return {'origin': 'metadata'}
        for key, value in self.answers.items():
            if key in query:
                return value
        raise AssertionError(f"unexpected fetchrow: {query}")

    async def execute(self, query, *args):
        if 'pg_advisory_xact_lock' in query or 'pg_advisory_unlock' in query:
            return 'SELECT 1'
        self.writes.append(query)
        return "UPDATE 0"


def _refused(coro) -> HTTPException:
    with pytest.raises(HTTPException) as exc:
        asyncio.run(coro)
    return exc.value


@pytest.mark.parametrize("endpoint", [datasets.rename_field, datasets.clone_field])
@pytest.mark.parametrize("background", [False, True])
def test_onto_a_key_with_values_is_refused_without_overwrite(monkeypatch, endpoint, background):
    fake = FakeDB({"metadata ? $2": True, "is_promoted": False})
    monkeypatch.setattr(datasets, "db", fake)
    err = _refused(endpoint(1, "label2", datasets.FieldRenameRequest(new_name="dt", background=background), user=USER))
    assert err.status_code == 409 and "overwrite=true" in err.detail
    assert fake.writes == []  # nothing touched


@pytest.mark.parametrize("endpoint", [datasets.rename_field, datasets.clone_field])
@pytest.mark.parametrize("background", [False, True])
def test_overwriting_a_promoted_key_asks_to_demote_first(monkeypatch, endpoint, background):
    fake = FakeDB({"metadata ? $2": True, "is_promoted": True})
    monkeypatch.setattr(datasets, "db", fake)
    err = _refused(
        endpoint(
            1,
            "label2",
            datasets.FieldRenameRequest(new_name="dt", overwrite=True, background=background),
            user=USER,
        ),
    )
    assert err.status_code == 409 and "demote" in err.detail
    assert fake.writes == []


@pytest.mark.parametrize("endpoint", [datasets.rename_field, datasets.clone_field])
@pytest.mark.parametrize("background", [False, True])
def test_renaming_onto_itself_is_a_400(monkeypatch, endpoint, background):
    fake = FakeDB({})
    monkeypatch.setattr(datasets, "db", fake)
    err = _refused(endpoint(1, "dt", datasets.FieldRenameRequest(new_name="dt", background=background), user=USER))
    assert err.status_code == 400
    assert fake.writes == []


def test_promoting_a_field_with_no_values_is_a_404(monkeypatch):
    monkeypatch.setattr(
        datasets,
        "db",
        FakeDB({"FROM datasets": {"id": 1}, "FROM dataset_fields": None, "jsonb_typeof": None}),
    )
    err = _refused(datasets.promote_field(1, "ghost", user=USER))
    assert err.status_code == 404


def test_display_type_is_a_closed_set(monkeypatch):
    monkeypatch.setattr(datasets, "db", FakeDB({}))
    err = _refused(
        datasets.set_field_display_type(1, "num", datasets.SetDisplayTypeRequest(display_type="banana"), user=USER),
    )
    assert err.status_code == 400


def test_media_fields_must_be_non_empty_and_contain_the_grid_field(monkeypatch):
    monkeypatch.setattr(datasets, "db", FakeDB({"SELECT media_fields": ["filepath"]}))
    err = _refused(datasets.update_dataset(1, datasets.DatasetUpdate(media_fields=[]), user=USER))
    assert err.status_code == 400
    err = _refused(datasets.update_dataset(1, datasets.DatasetUpdate(grid_media_field="dt"), user=USER))
    assert err.status_code == 400 and "filepath" in err.detail
