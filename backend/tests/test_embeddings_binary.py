"""Lock the columnar binary scatter payload format (Part 2 of the embeddings
sample-priority work) — what _embeddings_binary_response packs MUST match what the
frontend's parseEmbeddingsBinary reads, especially the 4-byte alignment of the
columns (Int32Array/Float32Array views require a multiple-of-4 byteOffset).

Pure function, no DB / no async — runs anywhere.
"""

import json
import struct

import numpy as np

from backend.routers.embeddings import _embeddings_binary_response


def _parse(buf: bytes):
    """Mirror frontend parseEmbeddingsBinary."""
    header_len = struct.unpack_from("<I", buf, 0)[0]
    assert (4 + header_len) % 4 == 0, "columns must start on a 4-byte boundary"
    header = json.loads(buf[4 : 4 + header_len])
    n = header["count"]
    off = 4 + header_len
    ids = np.frombuffer(buf, "<i4", n, off)
    off += n * 4
    xs = np.frombuffer(buf, "<f4", n, off)
    off += n * 4
    ys = np.frombuffer(buf, "<f4", n, off)
    return header, ids, xs, ys


def test_binary_round_trip_values_and_alignment():
    rows = [{"sample_id": i, "x": i * 0.5, "y": -i * 0.25} for i in range(5)]
    header, ids, xs, ys = _parse(bytes(_embeddings_binary_response(rows, total=10).body))
    assert header == {
        "count": 5,
        "total": 10,
        "sampled": True,
        "rank_stale": False,
        "map_state": None,
        "layout_id": None,
        "fields": ["sample_id", "x", "y"],
    }
    assert list(ids) == [0, 1, 2, 3, 4]
    assert np.allclose(xs, [0, 0.5, 1.0, 1.5, 2.0])
    assert np.allclose(ys, [0, -0.25, -0.5, -0.75, -1.0])


def test_binary_not_sampled_when_count_equals_total():
    rows = [{"sample_id": 1, "x": 1.0, "y": 2.0}]
    header, *_ = _parse(bytes(_embeddings_binary_response(rows, total=1).body))
    assert header["count"] == 1 and header["sampled"] is False


def test_binary_empty():
    header, ids, xs, ys = _parse(bytes(_embeddings_binary_response([], total=0).body))
    assert header["count"] == 0 and len(ids) == 0 == len(xs) == len(ys)


def test_alignment_holds_across_header_lengths():
    # Header length varies with the integers in it; the space-padding must keep the
    # column start 4-byte aligned regardless. Probe a range of counts/totals.
    for n in range(0, 40):
        rows = [{"sample_id": k, "x": 0.0, "y": 0.0} for k in range(n)]
        buf = bytes(_embeddings_binary_response(rows, total=n * 7 + 1).body)
        header_len = struct.unpack_from("<I", buf, 0)[0]
        assert (4 + header_len) % 4 == 0, f"misaligned at n={n}"
        _parse(buf)  # must not raise
