"""GET /embeddings/vectors: the binary page must mean exactly what the JSON page means.

Why this endpoint changed: it parsed
pgvector's TEXT form with a Python float() comprehension, then — having no response_model — fell
through to FastAPI's jsonable_encoder and json.dumps of a ~208MB string, all on the event loop.
About 8-9s of uninterruptible CPU per 10k x 1024-d page, and compute_visualization pages the whole
field, so one UMAP fit froze a uvicorn worker in ~8s blocks roughly 320 times in a row.

The fix adds an opt-in columnar binary format and moves BOTH formats off the loop. A faster encoder
that returns different numbers would be far worse than the slow one, so the assertion that carries
the weight here is the round-trip: decode(binary) == json, exactly, bit for bit.
"""

import json
import struct

import numpy as np
import pytest
from fastapi import HTTPException
from fridge.core.client import _decode_vectors_binary

from backend.routers.embeddings import (
    _MAX_VECTOR_PAGE,
    _vectors_binary_response,
    _vectors_json_response,
    _vectors_to_matrix,
)


def _pg_text(vec) -> str:
    """pgvector renders as '[1,2,3]' text over asyncpg."""
    return "[" + ",".join(repr(float(v)) for v in vec) + "]"


def _rows(vectors, first_id=100):
    return [{"sample_id": first_id + i, "vector": _pg_text(v)} for i, v in enumerate(vectors)]


@pytest.mark.parametrize("n,dim", [(1, 1), (3, 4), (64, 128)])
def test_binary_decodes_to_exactly_the_json_payload(n, dim):
    rng = np.random.default_rng(0)
    vectors = rng.random((n, dim), dtype=np.float32)
    rows = _rows(vectors)

    as_json = json.loads(bytes(_vectors_json_response(rows, total=n).body))
    decoded = _decode_vectors_binary(_vectors_binary_response(rows, total=n).body)

    assert decoded["total"] == as_json["total"] == n
    assert len(decoded["items"]) == len(as_json["items"]) == n
    for got, want in zip(decoded["items"], as_json["items"]):
        assert got["sample_id"] == want["sample_id"]
        # float32 both sides — the values must match bit for bit, not merely approximately.
        np.testing.assert_array_equal(
            np.asarray(got["vector"], dtype=np.float32),
            np.asarray(want["vector"], dtype=np.float32),
        )


def test_binary_round_trip_preserves_float32_values_exactly():
    """Values that a float32 can hold must survive unchanged, including the awkward ones."""
    vec = np.array([0.0, -0.0, 1.0, -1.0, 0.1, 1e-8, 1e8, np.float32(1 / 3)], dtype=np.float32)
    rows = _rows([vec])
    decoded = _decode_vectors_binary(_vectors_binary_response(rows, total=1).body)
    np.testing.assert_array_equal(decoded["items"][0]["vector"], vec)


def test_empty_page_is_valid_in_both_formats():
    assert json.loads(bytes(_vectors_json_response([], total=0).body)) == {"items": [], "total": 0}
    decoded = _decode_vectors_binary(_vectors_binary_response([], total=0).body)
    assert decoded == {"items": [], "total": 0}


def test_header_is_4_byte_aligned_and_self_describing():
    rows = _rows(np.zeros((2, 3), dtype=np.float32))
    body = _vectors_binary_response(rows, total=7).body
    (header_len,) = struct.unpack_from("<I", body, 0)
    assert (4 + header_len) % 4 == 0, "columns must start 4-byte aligned for np.frombuffer"
    header = json.loads(bytes(body[4 : 4 + header_len]).decode().rstrip())
    assert header["count"] == 2 and header["dim"] == 3 and header["total"] == 7
    # header + int32 ids + float32 matrix, exactly — no trailing slack.
    assert len(body) == 4 + header_len + 2 * 4 + 2 * 3 * 4


def test_total_none_on_later_pages_survives_the_round_trip():
    """Only the first page carries `total`; later pages send None and callers rely on that."""
    rows = _rows(np.zeros((1, 2), dtype=np.float32))
    assert _decode_vectors_binary(_vectors_binary_response(rows, total=None).body)["total"] is None


def test_mixed_dimensions_fail_loudly_instead_of_corrupting():
    """A rectangular payload cannot represent a ragged field, so it must refuse — not truncate.

    Wrong-dimension uploads are a real, known way to poison a field, and silently reshaping them
    would hand the caller plausible-looking garbage.
    """
    rows = _rows([np.zeros(4, dtype=np.float32)]) + _rows([np.zeros(8, dtype=np.float32)], first_id=200)
    with pytest.raises(HTTPException) as e:
        _vectors_binary_response(rows, total=2)
    assert e.value.status_code == 409
    assert "mixed vector dimensions" in e.value.detail
    assert "200" in e.value.detail, "the offending sample id should be named"
    # The JSON format can still read the ragged field, which is what the error tells you to use.
    items = json.loads(bytes(_vectors_json_response(rows, total=2).body))["items"]
    assert [len(i["vector"]) for i in items] == [4, 8]


def test_parse_matches_a_plain_float_conversion():
    """_vectors_to_matrix replaced a float() list comprehension — it must agree with it."""
    rng = np.random.default_rng(1)
    vectors = rng.random((5, 16), dtype=np.float32)
    rows = _rows(vectors)
    ids, mat = _vectors_to_matrix(rows)
    naive = np.array([[float(x) for x in r["vector"].strip("[]").split(",")] for r in rows], dtype=np.float32)
    np.testing.assert_array_equal(mat, naive)
    np.testing.assert_array_equal(ids, np.arange(100, 105, dtype="<i4"))


def test_page_limit_is_bounded_but_paging_still_reaches_everything():
    """The cap bounds one PAGE, not what is reachable — keyset paging still walks the field.

    `limit` used to be unvalidated, so any caller with dataset access could ask for an arbitrarily
    large page and scale the per-request CPU/memory with it.
    """
    import inspect

    from backend.routers.embeddings import get_vectors

    assert _MAX_VECTOR_PAGE >= 10_000, "the default page size must still be allowed"
    sig = inspect.signature(get_vectors)
    bounds = {type(m).__name__: getattr(m, type(m).__name__.lower()) for m in sig.parameters["limit"].default.metadata}
    assert bounds.get("Le") == _MAX_VECTOR_PAGE, f"limit must declare an upper bound, got {bounds}"
    assert bounds.get("Ge") == 1, f"limit must be at least 1, got {bounds}"
    # after_id is what makes the bound harmless: callers page past it rather than asking for more.
    assert "after_id" in sig.parameters


def test_handler_dispatches_on_format_and_never_encodes_on_the_event_loop(monkeypatch):
    """The handler must return a pre-rendered Response for both formats.

    Returning a dict would put the payload back through FastAPI's jsonable_encoder on the event
    loop, which is the whole problem the binary page fixed.
    """
    import asyncio

    from fastapi import Response

    from backend.routers import embeddings as mod

    vectors = np.arange(6, dtype=np.float32).reshape(2, 3)
    rows = _rows(vectors)

    class FakeDB:
        async def fetch(self, *a, **k):
            return rows

        async def fetchval(self, *a, **k):
            return 2

    monkeypatch.setattr(mod, "db", FakeDB())

    binary = asyncio.run(mod.get_vectors(id=1, field_name="clip", response_format="binary"))
    as_json = asyncio.run(mod.get_vectors(id=1, field_name="clip", response_format="json"))

    assert isinstance(binary, Response) and binary.media_type == "application/octet-stream"
    assert isinstance(as_json, Response) and as_json.media_type == "application/json"
    # ...and the two still agree.
    decoded = _decode_vectors_binary(binary.body)
    parsed = json.loads(bytes(as_json.body))
    assert decoded["total"] == parsed["total"] == 2
    for got, want in zip(decoded["items"], parsed["items"]):
        assert got["sample_id"] == want["sample_id"]
        np.testing.assert_array_equal(got["vector"], np.asarray(want["vector"], dtype=np.float32))
