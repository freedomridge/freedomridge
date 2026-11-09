"""
Server-side heatmap compositing for the grid view.

The modal renders heatmap overlays on a client-side canvas (interactive,
per-pixel). The grid can't afford that — hundreds of cells, each decoding
a multi-MB PNG and holding its own canvas, blows out browser memory.
So the grid requests pre-composited PNGs from this module instead and
renders them as plain `<img>` tags.

The painting convention (shared with frontend/components/annotations/HeatmapRenderer.tsx):
  * pixel value 0 → fully transparent (background, base image shows through)
  * values in [range_min, range_max] → colormap-indexed RGB
  * values outside that range clamp to the endpoints
  * alpha is constant per nonzero pixel (not intensity-scaled)

Colormap LUTs come from `backend/colormaps.py`. Those are shared with the
frontend version at `frontend/lib/colormaps.ts` — regenerate both together
so the modal (canvas) and grid (composite) produce identical colors.

Regenerate LUTs with:
    python -c "import matplotlib, matplotlib.cm, numpy as np; \\
        [print(n, (matplotlib.colormaps[n](np.linspace(0,1,256))[:,:3]*255).astype('u1').flatten().tolist()) \\
         for n in ('viridis','magma','plasma','inferno','turbo','gray')]"
"""

from __future__ import annotations

import io

import numpy as np
from PIL import Image

from .colormaps import COLORMAPS, DEFAULT_COLORMAP
from .image_modes import to_rgb


def _max_pool_resize(src: np.ndarray, target_w: int, target_h: int) -> np.ndarray:
    """Downsample a 2D uint8 array to (target_h, target_w) using max pooling.

    For each output pixel, we take the MAX value over its corresponding source
    block. This preserves hot pixels — a 3×3 cluster of 80% probability won't
    disappear when the display shrinks to the point where that cluster maps to
    a single output pixel. Nearest-neighbor would pick one source pixel out of
    each block and could easily pick a cold neighbor instead.

    Only valid when src dimensions are ≥ target dimensions in both axes. Caller
    is expected to fall back to nearest-neighbor for upsampling (where max-pool
    has no benefit).
    """
    src_h, src_w = src.shape
    # Block boundaries computed with linspace so we handle non-integer ratios
    # without repeated rounding drift. Each output row i covers source rows
    # [y_edges[i], y_edges[i+1]); similarly for columns.
    y_edges = np.linspace(0, src_h, target_h + 1, dtype=np.int32)
    x_edges = np.linspace(0, src_w, target_w + 1, dtype=np.int32)

    # Vectorized max-pool via np.maximum.reduceat. reduceat reduces over each
    # [edge[i], edge[i+1]) segment (and to the array end for the last cell, which
    # lines up because y_edges[-1] == src_h). When two consecutive edges are
    # equal (a degenerate zero-height/width block on a barely-upsampled axis),
    # reduceat returns that single source pixel — exactly what the old per-cell
    # `y1 = y0 + 1` guard produced. This is byte-identical to the previous double
    # loop (verified across thousands of random + edge-case shapes) but ~60× faster,
    # so grid composites don't stall the thread pool when heatmaps exceed the
    # display size.
    pooled_rows = np.maximum.reduceat(src, y_edges[:-1].astype(np.intp), axis=0)
    pooled = np.maximum.reduceat(pooled_rows, x_edges[:-1].astype(np.intp), axis=1)
    return pooled.astype(src.dtype, copy=False)


def composite_heatmap(
    base_png_bytes: bytes,
    heatmap_png_bytes: bytes,
    range_start: float,
    range_end: float,
    colormap: str = DEFAULT_COLORMAP,
    alpha: float = 0.6,
) -> bytes:
    """Composite a colormap-painted heatmap on top of a base image.

    Args:
        base_png_bytes: encoded bytes of the base image (any format Pillow reads).
        heatmap_png_bytes: encoded bytes of the single-channel heatmap.
        range_start, range_end: the pixel-value range to stretch across the
            colormap. Values at ``range_start`` map to LUT[0], values at
            ``range_end`` map to LUT[255]; anything outside clamps to the
            endpoints. **Order is meaningful** — passing ``range_start > range_end``
            inverts the colormap (dark-at-high, bright-at-low). Degenerate (``range_start == range_end``) falls
            back to a span of 1 and pretty much everything ends up transparent.
        colormap: colormap name (see ``COLORMAPS`` keys). Unknown names fall
            back to the default.
        alpha: opacity of the overlay at nonzero pixels, in [0, 1].

    Returns:
        PNG bytes of the composited image (RGB, fully opaque — the overlay is
        already blended onto the base so no alpha channel is needed).
    """
    lut_tuples = COLORMAPS.get(colormap) or COLORMAPS[DEFAULT_COLORMAP]
    lut = np.asarray(lut_tuples, dtype=np.uint8)  # shape (256, 3)

    base_img = to_rgb(Image.open(io.BytesIO(base_png_bytes)))
    heatmap_img = Image.open(io.BytesIO(heatmap_png_bytes))
    base_w, base_h = base_img.size

    # Extract a single-channel value map BEFORE any resize, so max-pool
    # downsampling operates on the raw heatmap values. For RGB(A) sources we
    # take the red channel, so a grey map saved as RGB(A) reads the same as an L-mode PNG.
    if heatmap_img.mode == "L":
        hm_src = np.asarray(heatmap_img, dtype=np.uint8)
    elif heatmap_img.mode == "P":
        # Palette-indexed: the raw indices are the heatmap values.
        hm_src = np.asarray(heatmap_img, dtype=np.uint8)
    elif heatmap_img.mode in ("RGB", "RGBA"):
        hm_src = np.asarray(heatmap_img, dtype=np.uint8)[:, :, 0]
    else:
        # 16-bit, float, 1-bit, etc. — coerce to 8-bit grayscale. Heatmap overlays
        # are all 8-bit today (verified 2026-04-16) so this is the edge case.
        hm_src = np.asarray(heatmap_img.convert("L"), dtype=np.uint8)

    src_h, src_w = hm_src.shape
    if (src_w, src_h) == (base_w, base_h):
        hm = hm_src
    elif src_w >= base_w or src_h >= base_h:
        # ANY axis that needs downsampling routes us through max-pool so the
        # down axis preserves hot pixels. On the other axis (if it's actually
        # upsampling), _max_pool_resize's degenerate-block guard collapses the
        # pool to a single source pixel — i.e. nearest-neighbor. So max-pool
        # handles pure-downsample AND mixed-axis correctly in one branch.
        hm = _max_pool_resize(hm_src, base_w, base_h)
    else:
        # Pure upsampling on both axes — max-pool would just be nearest with
        # extra overhead, so take the fast path directly.
        hm = np.asarray(
            Image.fromarray(hm_src, mode="L").resize((base_w, base_h), Image.Resampling.NEAREST),
            dtype=np.uint8,
        )

    base_arr = np.asarray(base_img, dtype=np.float32)  # (H, W, 3)

    # Keep the SIGN of range_span: a negative span means reversed range, which
    # inverts the colormap via the division (value close to range_start maps to
    # 0, value close to range_end maps to 1). Only the exactly-zero case is
    # degenerate — guard it with a span of 1 so the division doesn't produce
    # NaN. Earlier versions guarded `<= 0` here, which silently clobbered the
    # sign and broke reversed ranges, which Sample.add_heatmap documents as supported.
    range_span = float(range_end) - float(range_start)
    if range_span == 0:
        range_span = 1.0

    normalized = (hm.astype(np.float32) - float(range_start)) / range_span
    np.clip(normalized, 0.0, 1.0, out=normalized)
    indices = (normalized * 255.0).astype(np.uint8)  # (H, W) in [0, 255]
    colored = lut[indices].astype(np.float32)  # (H, W, 3)

    # Pixel value 0 is the "no overlay" sentinel. Multiplying alpha by the
    # zero-mask makes those pixels fall through to the base image unchanged.
    nonzero = (hm > 0).astype(np.float32)
    a = (nonzero * float(alpha))[:, :, None]  # (H, W, 1)

    out = base_arr * (1.0 - a) + colored * a
    np.clip(out, 0, 255, out=out)
    out_img = Image.fromarray(out.astype(np.uint8), mode="RGB")

    buf = io.BytesIO()
    out_img.save(buf, format="PNG")
    return buf.getvalue()
