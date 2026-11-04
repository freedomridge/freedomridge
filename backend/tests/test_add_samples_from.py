"""Dataset.add_samples_from copies samples (with embeddings and hashes) between datasets.

merge_samples only merges metadata into samples that already exist. This inserts the missing ones.
Self-contained, no database: a real Client and Dataset talk to an in-memory fake of the endpoints
involved, so the wire payloads (what is actually sent to the target) are what the tests assert on.
Backend handlers for the two new read endpoints are exercised against a stubbed `db`.
"""

import asyncio

import pytest
from fridge.core.client import Client
from fridge.core.dataset import Dataset
from fridge.core.sample import Sample
from fridge.core.view import _matches_filter

from backend.routers import embeddings as embeddings_router
from backend.routers import hashes as hashes_router


class FakeResponse:
    ok = True
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _as_api_row(row):
    """A stored row as the samples endpoints return it: heading nested in location, no heading key."""
    row = dict(row)
    heading = row.pop("heading", None)
    if row.get("location") and heading is not None:
        row["location"] = {**row["location"], "heading": heading}
    return row


class FakeServer:
    """Two datasets' worth of rows, served through the same URLs the SDK calls."""

    def __init__(self):
        self.rows = {}  # dataset_id -> {sample_id: row}
        self.vectors = {}  # dataset_id -> {(sample_id, field): vector}
        self.hashes = {}  # dataset_id -> {(sample_id, field): hex}
        self.array_fields = {}  # dataset_id -> [names]
        self.next_id = 1000
        self.requests = []

    def add_dataset(self, dataset_id):
        self.rows[dataset_id], self.vectors[dataset_id], self.hashes[dataset_id] = {}, {}, {}
        self.array_fields[dataset_id] = []

    def seed(self, dataset_id, sample_id, filepath, *, vectors=None, hashes=None, **row):
        row = {"id": sample_id, "filepath": filepath, "media_type": "image", "tags": [], "metadata": {}, **row}
        self.rows[dataset_id][sample_id] = row
        for field, vec in (vectors or {}).items():
            self.vectors[dataset_id][(sample_id, field)] = vec
        for field, hex_ in (hashes or {}).items():
            self.hashes[dataset_id][(sample_id, field)] = hex_

    def _dataset_id(self, url):
        return int(url.split("/datasets/")[1].split("/")[0])

    def get(self, url, **kw):
        d = self._dataset_id(url)
        if url.endswith("/embeddings/field-inventory"):
            fields = sorted({f for (_, f) in self.vectors[d]})
            return FakeResponse([{"field_name": f, "count": 1} for f in fields])
        if url.endswith("/hashes/fields"):
            fields = sorted({f for (_, f) in self.hashes[d]})
            return FakeResponse([{"field_name": f, "count": 1} for f in fields])
        if url.endswith("/array-fields"):
            return FakeResponse([{"name": n} for n in self.array_fields[d]])
        raise AssertionError(f"unexpected GET {url}")

    def post(self, url, json=None, **kw):
        d = self._dataset_id(url)
        self.requests.append((url, json))
        if url.endswith("/samples/query"):
            rows = [r for _, r in sorted(self.rows[d].items()) if r["id"] > (json.get("after_id") or 0)]
            if json.get("filters"):
                rows = [r for r in rows if _matches_filter(Sample.from_row(r), json["filters"])]
            return FakeResponse({"items": [_as_api_row(r) for r in rows[: json["limit"]]], "total": len(rows)})
        if url.endswith("/embeddings/vectors/by-ids"):
            items = [
                {"sample_id": s, "field_name": f, "vector": v}
                for (s, f), v in sorted(self.vectors[d].items())
                if s in json["sample_ids"] and f in json["field_names"]
            ]
            return FakeResponse({"items": items})
        if url.endswith("/hashes/by-ids"):
            items = [
                {"sample_id": s, "field_name": f, "hash_hex": h}
                for (s, f), h in sorted(self.hashes[d].items())
                if s in json["sample_ids"] and f in json["field_names"]
            ]
            return FakeResponse({"items": items})
        if url.endswith("/array-fields"):
            self.array_fields[d].append(json["field_name"])
            return FakeResponse({})
        if url.endswith(f"/datasets/{d}/samples"):
            assert json["on_duplicate"] == "skip"
            existing = {r["filepath"] for r in self.rows[d].values()}
            new_ids: list = []
            n_skipped = 0
            for payload in json["samples"]:
                if payload["filepath"] in existing:
                    n_skipped += 1
                    continue
                self.next_id += 1
                row = {k: payload[k] for k in ("filepath", "media_type", "tags", "metadata", "location", "heading")}
                row["id"] = self.next_id
                if row["location"]:  # stored as a point; heading is its own column
                    row["location"] = {k: row["location"][k] for k in ("type", "coordinates")}
                for name, values in (payload.get("array_fields") or {}).items():
                    assert name in self.array_fields[d], f"array field {name} not registered on target"
                    row[name] = values
                self.rows[d][self.next_id] = row
                for field, vec in (payload.get("embeddings") or {}).items():
                    self.vectors[d][(self.next_id, field)] = vec
                for field, hex_ in (payload.get("hashes") or {}).items():
                    self.hashes[d][(self.next_id, field)] = hex_
                new_ids.append(self.next_id)
            result: dict = {"ids": new_ids}
            if n_skipped:
                result["skipped"] = n_skipped
            return FakeResponse(result)
        raise AssertionError(f"unexpected POST {url}")


class World:
    def __init__(self):
        self.server = FakeServer()
        self.client = Client.__new__(Client)
        self.client.base_url = "http://test.invalid/api"
        self.client.session = self.server
        self.source = self._dataset(1, "source")
        self.target = self._dataset(2, "target")

    def _dataset(self, dataset_id, name):
        self.server.add_dataset(dataset_id)
        ds = Dataset.__new__(Dataset)
        ds._id, ds.name, ds.client, ds._dirty, ds._array_field_names = dataset_id, name, self.client, False, []
        return ds


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setattr(Sample, "_known_array_fields", set())
    w = World()
    s = w.server
    s.seed(
        1,
        10,
        "a.jpg",
        tags=["keep"],
        metadata={"source_id": "m1", "score": 0.5},
        location={"type": "Point", "coordinates": [-93.5, 41.2]},
        heading=90.0,
        vectors={"dino": [1.0, 2.0], "clip": [3.0]},
        hashes={"phash": "ff00"},
        inferred_tags=["cat"],
    )
    s.seed(1, 20, "b.jpg", metadata={"source_id": "m2"}, vectors={"dino": [4.0, 5.0]}, hashes={"phash": "0a0b"})
    s.seed(1, 30, "c.jpg", metadata={"source_id": "m3"})
    s.array_fields[1] = ["inferred_tags"]
    return w


def _target_by_path(world):
    return {r["filepath"]: r for r in world.server.rows[2].values()}


def _target_id(world, filepath):
    return _target_by_path(world)[filepath]["id"]


def test_copies_missing_samples_with_everything_they_carry(world):
    result = world.target.add_samples_from(world.source)

    assert result == {"seen": 3, "added": 3, "skipped": 0}
    a = _target_by_path(world)["a.jpg"]
    assert a["tags"] == ["keep"]
    assert a["metadata"] == {"source_id": "m1", "score": 0.5}
    assert a["location"] == {"type": "Point", "coordinates": [-93.5, 41.2]}
    assert a["heading"] == 90.0
    assert a["inferred_tags"] == ["cat"]
    aid, bid = _target_id(world, "a.jpg"), _target_id(world, "b.jpg")
    assert world.server.vectors[2][(aid, "dino")] == [1.0, 2.0]
    assert world.server.vectors[2][(aid, "clip")] == [3.0]
    assert world.server.vectors[2][(bid, "dino")] == [4.0, 5.0]
    assert world.server.hashes[2][(aid, "phash")] == "ff00"
    assert world.server.hashes[2][(bid, "phash")] == "0a0b"
    assert (_target_id(world, "c.jpg"), "dino") not in world.server.vectors[2]
    assert world.target._dirty is True


def test_source_is_untouched(world):
    before = {k: dict(v) for k, v in world.server.rows[1].items()}
    world.target.add_samples_from(world.source)
    assert world.server.rows[1] == before


def test_registers_missing_array_fields_on_the_target(world):
    world.target.add_samples_from(world.source)
    assert world.server.array_fields[2] == ["inferred_tags"]


def test_existing_target_samples_are_left_exactly_as_they_are(world):
    world.server.seed(2, 500, "a.jpg", tags=["mine"], metadata={"source_id": "other"}, vectors={"dino": [9.0, 9.0]})

    result = world.target.add_samples_from(world.source)

    assert result == {"seen": 3, "added": 2, "skipped": 1}
    kept = world.server.rows[2][500]
    assert kept["tags"] == ["mine"] and kept["metadata"] == {"source_id": "other"}
    assert world.server.vectors[2][(500, "dino")] == [9.0, 9.0]
    assert (500, "clip") not in world.server.vectors[2]
    assert (500, "phash") not in world.server.hashes[2]
    assert len(world.server.rows[2]) == 3


def test_running_it_twice_adds_nothing_the_second_time(world):
    world.target.add_samples_from(world.source)
    assert world.target.add_samples_from(world.source) == {"seen": 3, "added": 0, "skipped": 3}
    assert len(world.server.rows[2]) == 3


def test_copies_only_the_selected_view_by_metadata_key(world):
    view = world.source.filter(source_id__in=["m2", "m3"])

    result = world.target.add_samples_from(view)

    assert result["added"] == 2
    assert sorted(_target_by_path(world)) == ["b.jpg", "c.jpg"]


def test_copies_a_view_selected_by_id(world):
    result = world.target.add_samples_from(world.source.filter(id__in=[10]))
    assert result["added"] == 1 and list(_target_by_path(world)) == ["a.jpg"]


def test_only_the_selected_samples_vectors_and_hashes_are_requested(world):
    world.target.add_samples_from(world.source.filter(source_id="m2"))

    vector_requests = [j for u, j in world.server.requests if u.endswith("/vectors/by-ids")]
    hash_requests = [j for u, j in world.server.requests if u.endswith("/hashes/by-ids")]
    assert [j["sample_ids"] for j in vector_requests] == [[20]]
    assert [j["sample_ids"] for j in hash_requests] == [[20]]


def test_streams_in_batches_without_dropping_or_repeating(world):
    for i in range(5):
        world.server.seed(1, 100 + i, f"x{i}.jpg", vectors={"dino": [float(i), 0.0]})

    result = world.target.add_samples_from(world.source, batch_size=3)

    assert result == {"seen": 8, "added": 8, "skipped": 0}
    posts = [j for u, j in world.server.requests if u.endswith("/datasets/2/samples")]
    assert [len(p["samples"]) for p in posts] == [3, 3, 2]
    vector_requests = [j for u, j in world.server.requests if u.endswith("/vectors/by-ids")]
    assert all(len(j["sample_ids"]) <= 3 for j in vector_requests)


def test_embedding_and_hash_fields_can_be_chosen_or_turned_off(world):
    world.target.add_samples_from(world.source, embedding_fields=["dino"], hash_fields=[])

    assert {f for (_, f) in world.server.vectors[2]} == {"dino"}
    assert world.server.hashes[2] == {}
    assert not [u for u, _ in world.server.requests if u.endswith("/hashes/by-ids")]


def test_unknown_field_is_an_error_not_a_silent_skip(world):
    with pytest.raises(ValueError, match=r"no embedding field\(s\) \['nope'\]"):
        world.target.add_samples_from(world.source, embedding_fields=["nope"])
    with pytest.raises(ValueError, match=r"no hash field\(s\) \['nope'\]"):
        world.target.add_samples_from(world.source, hash_fields=["nope"])
    assert world.server.rows[2] == {}


def test_refuses_to_copy_a_dataset_into_itself(world):
    with pytest.raises(ValueError, match="same dataset"):
        world.source.add_samples_from(world.source)
    with pytest.raises(ValueError, match="same dataset"):
        world.source.add_samples_from(world.source.filter(source_id="m1"))


def test_bad_batch_size_is_rejected(world):
    with pytest.raises(ValueError, match="batch_size"):
        world.target.add_samples_from(world.source, batch_size=0)


# --------------------------------------------------------------------------------------------
# Backend read endpoints (stubbed db)
# --------------------------------------------------------------------------------------------


class RecordingDB:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    async def fetch(self, query, *args):
        self.calls.append((query, args))
        return self.rows


def test_vectors_by_ids_endpoint_is_scoped_to_the_dataset_and_parses_vectors(monkeypatch):
    db = RecordingDB([{"sample_id": 7, "field_name": "dino", "vector": "[1.5,2.5]"}])
    monkeypatch.setattr(embeddings_router, "db", db)
    request = embeddings_router.VectorsByIdsRequest(sample_ids=[7, 8], field_names=["dino"])

    result = asyncio.run(embeddings_router.get_vectors_by_ids(3, request))

    assert result == {"items": [{"sample_id": 7, "field_name": "dino", "vector": [1.5, 2.5]}]}
    query, args = db.calls[0]
    assert "s.dataset_id = $1" in query
    assert args == (3, [7, 8], ["dino"])


def test_hashes_by_ids_endpoint_is_scoped_to_the_dataset(monkeypatch):
    db = RecordingDB([{"sample_id": 7, "field_name": "phash", "hash_hex": "ff00"}])
    monkeypatch.setattr(hashes_router, "db", db)
    request = hashes_router.HashesByIdsRequest(sample_ids=[7], field_names=["phash"])

    result = asyncio.run(hashes_router.hashes_by_ids(3, request))

    assert result == {"items": [{"sample_id": 7, "field_name": "phash", "hash_hex": "ff00"}]}
    query, args = db.calls[0]
    assert "s.dataset_id = $1" in query
    assert args == (3, [7], ["phash"])


@pytest.mark.parametrize("model", ["VectorsByIdsRequest", "HashesByIdsRequest"])
def test_by_ids_requests_are_bounded_and_need_a_field(model):
    from pydantic import ValidationError

    cls = getattr(embeddings_router, model, None) or getattr(hashes_router, model)
    with pytest.raises(ValidationError):
        cls(sample_ids=list(range(10_001)), field_names=["x"])
    with pytest.raises(ValidationError):
        cls(sample_ids=[1], field_names=[])
