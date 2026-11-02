"""Pure-function tests for patch-scale virtual-field expansion (the compute_visualization fix).

A patch-scale field like 'clip_patch_8x8' is *virtual*: it has no embeddings of its own — its data
lives across one regular field per tile ('clip_patch_8x8_r0c0' … '_r7c7'). compute_visualization
expands it via _patch_tile_fields so it can fetch/upload each tile through the normal API, which is
what lets a several-hundred-thousand-tile field reduce client-side (the server-side in-process
/reduce endpoint holds every tile vector in the worker and drops the connection on large fields).

These lock the tile-name reconstruction so it stays byte-identical to the backend's
_patch_field_name (`{base}_patch_{scale}_r{r}c{c}`) — a mismatch would silently fetch zero vectors.
"""

from fridge.core.dataset import _patch_tile_fields


def _backend_tile_name(base, scale, r, c):
    # Mirrors backend.routers.embeddings._patch_field_name exactly.
    return f"{base}_patch_{scale}_r{r}c{c}"


def test_expands_full_grid_in_row_major_order():
    tiles = _patch_tile_fields("clip_patch_8x8")
    assert len(tiles) == 64
    assert tiles[0] == "clip_patch_8x8_r0c0"
    assert tiles[-1] == "clip_patch_8x8_r7c7"
    assert tiles[1] == "clip_patch_8x8_r0c1"  # row-major
    assert tiles[8] == "clip_patch_8x8_r1c0"


def test_nonsquare_grid():
    assert _patch_tile_fields("emb_patch_2x3") == [
        "emb_patch_2x3_r0c0",
        "emb_patch_2x3_r0c1",
        "emb_patch_2x3_r0c2",
        "emb_patch_2x3_r1c0",
        "emb_patch_2x3_r1c1",
        "emb_patch_2x3_r1c2",
    ]


def test_multi_digit_grid():
    tiles = _patch_tile_fields("clip_patch_10x10")
    assert len(tiles) == 100
    assert tiles[-1] == "clip_patch_10x10_r9c9"


def test_names_match_backend_patch_field_name():
    tiles = _patch_tile_fields("clip_patch_4x4")
    expected = [_backend_tile_name("clip", "4x4", r, c) for r in range(4) for c in range(4)]
    assert tiles == expected


def test_non_patch_fields_return_none():
    assert _patch_tile_fields("clip_img") is None
    assert _patch_tile_fields("clip_obj") is None
    assert _patch_tile_fields("embeddings_clip_vit_base32") is None
    # An already-expanded tile name is NOT itself a patch-scale virtual field (else we'd recurse).
    assert _patch_tile_fields("clip_patch_4x4_r0c0") is None


def test_degenerate_grid_returns_none():
    # A 0xN / Nx0 grid is malformed, not a real patch-scale field — must not yield an empty tile
    # list that a caller would treat as "found 0 vectors".
    assert _patch_tile_fields("x_patch_0x0") is None
    assert _patch_tile_fields("x_patch_0x4") is None
    assert _patch_tile_fields("x_patch_4x0") is None
