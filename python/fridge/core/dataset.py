import io
import numbers
import os
import re
import warnings
from itertools import islice
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import requests

from .client import get_client
from .sample import Sample
from .version_view import VersionView
from .view import Condition, DatasetView, SampleQuery, parse_query_string

# `field__op=value` suffixes accepted by Dataset.filter(), mapped to the wire operator that
# backend/routers/datasets.py's parse_filter_node actually implements.
#
# An unknown suffix RAISES — it must never silently degrade to equality. `brightness__between=[..]`
# used to become `brightness = '[0.3, 0.8]'` and `scene__nin=[...]` became `scene = [...]`, both
# matching zero rows with no error, so a typo returned a plausible wrong answer.
_FILTER_OPS: Dict[str, str] = {
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
    "ne": "!=",
    "in": "in",
    "nin": "not in",
    "not_in": "not in",
    "contains": "contains",
    "exists": "exists",
    "not_exists": "not_exists",
    "is_null": "is_null",
    "is_not_null": "is_not_null",
}

# Unary operators: the kwarg's bool chooses the polarity, so `reviewed__exists=False` means "the
# key is absent" rather than being compared against False. (when_true, when_false)
_FILTER_UNARY_OPS: Dict[str, tuple] = {
    "exists": ("exists", "not_exists"),
    "not_exists": ("not_exists", "exists"),
    "is_null": ("is_null", "is_not_null"),
    "is_not_null": ("is_not_null", "is_null"),
}


def _cgroup_headroom_bytes() -> Optional[int]:
    """Memory this process's cgroup (v2) may still allocate, or None without a limit. A memory-capped
    child (the scheduled map re-fit runs under systemd-run -p MemoryMax=...) is killed at its limit,
    not when the host runs out, so its fit must be sized by what the limit leaves."""
    try:
        with open("/proc/self/cgroup") as f:
            path = next(line.split(":", 2)[2].strip() for line in f if line.startswith("0::"))
        folder = os.path.join("/sys/fs/cgroup", path.lstrip("/"))
        with open(os.path.join(folder, "memory.max")) as f:
            limit = f.read().strip()
        if limit == "max":
            return None
        with open(os.path.join(folder, "memory.current")) as f:
            used = int(f.read().strip())
        # Page cache of files read (the exported vectors) is charged to the cgroup but reclaimed
        # before an out-of-memory kill, like the host's MemAvailable counts it as available.
        with open(os.path.join(folder, "memory.stat")) as f:
            stat = dict(line.split() for line in f)
        cached = int(stat.get("active_file", 0)) + int(stat.get("inactive_file", 0))
        return max(0, int(limit) - used + cached)
    except Exception:
        return None


def _available_ram_bytes() -> int:
    """Best-effort available RAM in bytes (psutil → /proc/meminfo → 8 GB), never more than the
    process's own cgroup memory limit leaves."""
    available = None
    try:
        import psutil

        available = int(psutil.virtual_memory().available)
    except Exception:
        pass
    if available is None:
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemAvailable:"):
                        available = int(line.split()[1]) * 1024
                        break
        except Exception:
            pass
    if available is None:
        available = 8 * 1024**3
    headroom = _cgroup_headroom_bytes()
    return available if headroom is None else min(available, headroom)


# The fit's peak memory per fit point, in float32 vectors of the field's dimension. Measured
# with `fridge.map_fit`'s code path in a cgroup scope (peak anonymous memory minus the
# resident vectors, per fit point; umap-learn defaults, cosine, 15 neighbours):
#   512-d: 18.3 KB at 300k fit points, 21.5 KB at 600k, 26.4 KB at 1.2M
#   1024-d: 29.8 KB at 300k, 35.9 KB at 600k
# plus one more vector (the sampled subset's copy) when the fit is a sample of the rows. The cost grows
# with the fit size until pynndescent's tree count stops growing (~1.4M points). 13 vectors covers the
# measured 512-d cost at that plateau to within the 25% headroom `_auto_fit_sample_size` keeps, and
# over-estimates 1024-d; the scheduled map re-fit then measures each fit and sizes the next from that.
# Was 10, which proved too small for 512-d fits inside a memory cap.
FIT_VECTOR_COPIES = 13
# The smallest fit _auto_fit_sample_size runs (or all points, if fewer): below it a map is not usable.
MINIMUM_FIT_POINTS = 50_000


def default_fit_bytes_per_point(dim: int) -> int:
    """The fit's memory per fit point when nothing better was measured: FIT_VECTOR_COPIES float32
    vectors of `dim` (it scales with the dimension; no fixed term, no cap)."""
    return dim * 4 * FIT_VECTOR_COPIES


def _auto_fit_sample_size(total: int, dim: int, bytes_per_point: Optional[int] = None) -> int:
    """Pick how many vectors to FIT UMAP on, scaled to available RAM (RAM is the only limit).

    ALL `total` points are projected regardless — this only sizes the fit subset. UMAP's manifold
    is well captured by a large random sample (more = better, with diminishing returns), so we fit
    on as many as RAM allows, up to `total` (fit-on-all = best fidelity). The full float32 vector
    array (total*dim*4) is already resident; `budget` is the headroom left for the fit's extra
    allocations, and the per-point cost scales with `dim`, so a 1024-d field is allotted ~half the
    points of a 512-d one — matching that clip@512 fit ALL where dinov3@1024 OOM'd at a similar N.
    `bytes_per_point` replaces that default with a measured cost (the scheduled map re-fit measures each
    fit's memory and sizes the next one from it).
    """
    # avail is read AFTER the vector array is resident, so it already excludes that array.
    avail = _available_ram_bytes()
    # Use ~75% of remaining free RAM for the fit; the other ~25% is headroom (the measured cost per point
    # still grows with the fit size). Want dinov3 to fit MORE? Free the prior field's RAM before fitting
    # the next one — see compute_visualization's caller. With FIT_VECTOR_COPIES=13 a 1024-d field gets
    # ~1.37M fit points at ~110 GB free. The scheduled re-fit measures its cost and sizes from that.
    budget = int(avail * 0.75)
    per_point = bytes_per_point or default_fit_bytes_per_point(dim)
    allowed = budget // per_point if per_point else total
    fit_n = int(min(total, max(0, allowed)))
    fit_n = max(min(total, MINIMUM_FIT_POINTS), fit_n)  # always fit on a usable minimum
    pct = 100.0 * fit_n / total
    tail = " — ALL points (max fidelity)" if fit_n >= total else ""
    print(f"  Auto fit-size: ~{avail / 1e9:.0f} GB free → fit UMAP on {fit_n:,}/{total:,} ({pct:.0f}%){tail}")
    if fit_n > 800_000:
        print(
            f"    (fitting on {fit_n:,} is high quality but CPU-heavy — can take 30+ min to a "
            f"few hours; pass a smaller fit_sample_size to trade a little fidelity for speed.)",
        )
    return fit_n


# The current map model loaded by project_pending, by model id: one at a time (a model can be 20 GB).
_MAP_MODELS: Dict[str, Any] = {}


def load_map_model(state):
    """Load a map's current model (``get_map_state``) from the model folder, verifying its checksum
    once per model id. The folder is ``FRIDGE_UMAP_MODEL_DIR`` (default ``$FRIDGE_DATA_ROOT/umap_models``),
    else the server's own path for it on this machine."""
    model_id = state["model_id"]
    if model_id in _MAP_MODELS:
        return _MAP_MODELS[model_id]
    import hashlib

    import joblib

    root = os.environ.get("FRIDGE_UMAP_MODEL_DIR") or os.path.join(
        os.environ.get("FRIDGE_DATA_ROOT", os.path.expanduser("~/.fridge")),
        "umap_models",
    )
    candidates = [os.path.join(root, state["artifact_location"]), state.get("artifact_path")]
    path = next((candidate for candidate in candidates if candidate and os.path.exists(candidate)), None)
    if path is None:
        raise FileNotFoundError(
            f"The current map model {state['artifact_location']} is not readable on this machine; set "
            f"FRIDGE_UMAP_MODEL_DIR to the server's model folder",
        )
    if state.get("artifact_checksum"):
        digest = hashlib.sha256()
        with open(path, "rb") as source:
            for block in iter(lambda: source.read(16 * 1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != state["artifact_checksum"]:
            raise ValueError(f"{path} does not match the current map model's checksum; not projecting with it")
    model = joblib.load(path)
    _MAP_MODELS.clear()
    _MAP_MODELS[model_id] = model
    return model


def _parallel_queries(reducer):
    """Make the fitted UMAP project new points on every core: pynndescent answers a batch of queries on
    one thread unless ``parallel_batch_queries`` is set (measured 4x faster on 32 cores, 2026-10-21; the
    per-thread working memory is a visited-bitset of the index, ~n/8 bytes). The search function is
    rebuilt lazily with the setting, and a saved model keeps it, so the projection daemon gains too.

    Not reproducible run to run for ~0.4% of the points (measured with a 150k fit, 30k projected,
    512-d): pynndescent's parallel search shares one random state across threads, so the hardest points
    get other neighbours, and ~0.3% land far from where another run put them. In aggregate the result
    is the same as serial (recall@15 0.974 either way). The re-fit computes coordinates once; only a
    comparison of two runs, or the daemon re-placing a point, sees the difference. Serial is exact but
    about 3x slower."""
    index = getattr(reducer, "_knn_search_index", None)  # None for small fits (exact neighbours)
    if index is None or getattr(index, "parallel_batch_queries", True):
        return
    index.parallel_batch_queries = True
    for compiled in ("_search_function", "_deheap_function"):
        if compiled in index.__dict__:
            delattr(index, compiled)


def fit_reducer(vectors, fit_sample_size, method="umap", timings=None):
    """Fit UMAP (or PCA) on all ``vectors`` or a random subset of ``fit_sample_size`` (-1 = all), then
    project the rest through the fitted model in batches, on all cores. Returns ``(reducer, coords)``
    with one 2D row per input row; ``timings`` (a dict), if given, gets ``fit_seconds`` and
    ``transform_seconds``. Shared by ``Dataset.compute_visualization`` and the scheduled map re-fit's
    memory-capped child (``fridge.map_fit``)."""
    import time

    import numpy as np

    tqdm: Any
    try:
        from tqdm import tqdm  # type: ignore[no-redef]
    except ImportError:
        tqdm = None

    total = len(vectors)
    if method == "umap":
        try:
            import umap
        except ImportError:
            raise ImportError(
                "umap-learn is required. Install with: pip install umap-learn (or: pip install \"freedomridge[all]\")",
            )
        reducer = umap.UMAP(
            n_components=2,
            n_neighbors=min(15, total - 1),
            min_dist=0.1,
            metric='cosine',
            verbose=True,
        )
    elif method == "pca":
        from sklearn.decomposition import PCA

        reducer = PCA(n_components=2)
    else:
        raise ValueError(f"Unknown visualization method: {method!r} (use 'umap', 'pca', or 'tsne')")

    coords = np.empty((total, 2), dtype=np.float32)
    started = time.monotonic()
    if fit_sample_size == -1 or total <= fit_sample_size:
        print(f"Running {method.upper()} on all {total:,} vectors...")
        coords = reducer.fit_transform(vectors)
        fitted = time.monotonic()
    else:
        # Fit on a random subset, then project the rest in batches.
        print(f"Fitting {method.upper()} on {fit_sample_size:,} random samples...")
        rng = np.random.default_rng(42)
        fit_indices = rng.choice(total, size=fit_sample_size, replace=False)
        fit_indices.sort()
        remaining_indices = np.setdiff1d(np.arange(total), fit_indices)

        coords[fit_indices] = reducer.fit_transform(vectors[fit_indices])
        fitted = time.monotonic()
        _parallel_queries(reducer)

        transform_batch = 50000
        print(f"Transforming remaining {len(remaining_indices):,} vectors...")
        pbar = tqdm(total=len(remaining_indices), desc="Transforming") if tqdm else None
        for i in range(0, len(remaining_indices), transform_batch):
            batch_idx = remaining_indices[i : i + transform_batch]
            coords[batch_idx] = reducer.transform(vectors[batch_idx])
            if pbar:
                pbar.update(len(batch_idx))
        if pbar:
            pbar.close()
    if timings is not None:
        timings["fit_seconds"] = round(fitted - started, 1)
        timings["transform_seconds"] = round(time.monotonic() - fitted, 1)
    return reducer, coords


_PATCH_SCALE_RE = re.compile(r"^(?P<base>.+)_patch_(?P<rows>\d+)x(?P<cols>\d+)$")


def _patch_tile_fields(field_name: str) -> Optional[List[str]]:
    """Expand a patch-scale virtual field into its per-tile field names.

    A patch-scale field like ``clip_patch_8x8`` has no embeddings of its own — its data lives across
    one regular field per tile (``clip_patch_8x8_r0c0`` … ``_r7c7``, the full NxN grid). Returns that
    tile list (each a normal field the API already serves and accepts coordinates for), or None if
    ``field_name`` is not a patch-scale field.
    """
    m = _PATCH_SCALE_RE.match(field_name)
    if not m:
        return None
    rows, cols = int(m.group("rows")), int(m.group("cols"))
    if rows < 1 or cols < 1:
        return None  # a malformed 0xN / Nx0 grid is not a real patch-scale field
    return [f"{field_name}_r{r}c{c}" for r in range(rows) for c in range(cols)]


def _media_metadata(img, filepath):
    """Basic media metadata from a PIL image: width/height/aspect_ratio/megapixels,
    plus file_size_bytes when the filepath is a readable local file. PIL reads image
    dimensions from the header without decoding pixels, so this is cheap."""
    w, h = img.size
    meta = {
        "width": int(w),
        "height": int(h),
        "aspect_ratio": round(w / h, 4) if h else 0.0,
        "megapixels": round(w * h / 1e6, 3),
    }
    try:
        if os.path.isfile(filepath):
            meta["file_size_bytes"] = os.path.getsize(filepath)
    except OSError:
        pass
    return meta


def _saturation(img):
    """Mean saturation (0-255) via PIL's HSV conversion — washed-out/grayscale images
    score low, vivid ones high."""
    import numpy as np

    return float(np.asarray(img.convert("HSV"))[:, :, 1].mean())


def _exposure_clipping(gray):
    """(pct_underexposed, pct_overexposed): percentage of pixels clipped at the
    histogram extremes of the grayscale image — crushed shadows / blown highlights."""
    import numpy as np

    g = np.asarray(gray)
    return float((g <= 5).mean() * 100.0), float((g >= 250).mean() * 100.0)


def _require_metric_dependencies():
    try:
        import cv2  # noqa: F401
        import numpy  # noqa: F401
        from PIL import ImageStat  # noqa: F401
    except ImportError:
        raise ImportError(
            "Please install: pip install opencv-python-headless numpy Pillow  (or: pip install \"freedomridge[all]\")",
        )


def compute_image_metrics(img):
    """Image-quality metrics for one PIL image, exactly as ``Dataset.compute_metrics`` stores them.

    Returns a dict with the keys ``brightness``, ``blurriness``, ``contrast``, ``entropy``,
    ``salt_and_pepper``, ``saturation``, ``pct_underexposed`` and ``pct_overexposed`` (all floats).
    A non-RGB image is converted to RGB first. Use this to compute the same metrics before upload.

    Requires ``opencv-python-headless``, ``numpy`` and ``Pillow`` (``pip install "freedomridge[all]"``).
    """
    _require_metric_dependencies()
    import cv2
    import numpy as np
    from PIL import ImageStat

    if img.mode != "RGB":
        img = img.convert("RGB")
    gray = img.convert("L")

    stat = ImageStat.Stat(img)
    try:
        r, g, b = stat.mean
    except ValueError:
        r = g = b = stat.mean[0]
    brightness = float(np.sqrt(0.241 * r * r + 0.691 * g * g + 0.068 * b * b))

    blurriness = float(cv2.Laplacian(np.array(gray), cv2.CV_64F).var())

    hist, _ = np.histogram(cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR), bins=256, range=(0, 256))
    contrast = float(np.max(np.where(hist > 0)) - np.min(np.where(hist > 0)))

    gray_pixels = np.array(gray)
    salt = gray_pixels >= 244
    pepper = gray_pixels <= 10
    kernel = np.ones((2, 2), np.uint8)
    salt_open = cv2.morphologyEx(salt.astype(np.uint8), cv2.MORPH_OPEN, kernel)
    pepper_open = cv2.morphologyEx(pepper.astype(np.uint8), cv2.MORPH_OPEN, kernel)
    isolated = np.sum(salt & ~salt_open.astype(np.uint8)) + np.sum(pepper & ~pepper_open.astype(np.uint8))
    salt_and_pepper = float(isolated / gray_pixels.size * 100)

    under, over = _exposure_clipping(gray)
    return {
        "brightness": brightness,
        "blurriness": blurriness,
        "contrast": contrast,
        "entropy": float(img.entropy()),
        "salt_and_pepper": salt_and_pepper,
        "saturation": _saturation(img),
        "pct_underexposed": under,
        "pct_overexposed": over,
    }


def _default_s3_image_loader():
    """Returns a callable that downloads images from S3 or local paths.

    Supported inputs:
      - s3://bucket/key       → bucket is parsed from the path (no env var needed)
      - /media/s3/key         → legacy; requires S3_BUCKET env var to be set
                                (the backend no longer emits this form, but old
                                cached URLs may still surface)
      - any other path        → opened as a local file via PIL; relative paths
                                use FRIDGE_DATA_ROOT when set

    The loader uses the ambient boto3 credential chain (env vars, ~/.aws,
    IAM role) — for per-dataset credentials in the web UI, use the backend's
    /api/samples/{id}/media proxy instead.
    """
    from PIL import Image

    s3_client = None
    # No default bucket — the library is deployment-agnostic. Legacy
    # /media/s3/ paths that don't embed the bucket require the caller to
    # set S3_BUCKET themselves.
    legacy_bucket = os.environ.get("S3_BUCKET")

    def open_rgb(path):
        with Image.open(path) as image:
            return image.convert('RGB')

    def loader(filepath: str) -> Image.Image | None:
        nonlocal s3_client
        if filepath.startswith("s3://"):
            parts = filepath[5:].split("/", 1)
            b, key = parts[0], parts[1]
        elif filepath.startswith("/media/s3/"):
            if not legacy_bucket:
                raise ValueError(
                    f"Cannot load legacy /media/s3/ path without S3_BUCKET env var set: {filepath}. "
                    "Prefer raw s3://bucket/key paths (what the backend returns by default).",
                )
            b, key = legacy_bucket, filepath[len("/media/s3/") :]
        elif filepath.startswith("/media/"):
            # A real local file under a /media mount (e.g. an external data drive mounted at
            # /media/<user>/...) takes precedence — only fall back to the legacy proxy resolution
            # (re-rooting under FRIDGE_DATA_ROOT) when no literal file exists at the path.
            if os.path.exists(filepath):
                return open_rgb(filepath)
            data_root = os.environ.get("FRIDGE_DATA_ROOT")
            if not data_root:
                raise ValueError(f"Cannot resolve legacy /media/ path without FRIDGE_DATA_ROOT set: {filepath}")
            # An absolute path already under the data root that simply isn't present on THIS host
            # (remote SDK, unmounted drive, moved file) is a missing file — re-rooting it would
            # double-nest the data root into a path that can never exist. Only re-root genuine
            # legacy proxy paths ("/media/<rel-to-root>"), never an absolute data-root path.
            if filepath == data_root or filepath.startswith(data_root.rstrip("/") + "/"):
                raise FileNotFoundError(f"No such media file: {filepath}")
            return open_rgb(os.path.join(data_root, filepath[len("/media/") :]))
        else:
            if not os.path.isabs(filepath):
                filepath = os.path.join(os.environ.get('FRIDGE_DATA_ROOT', ''), filepath)
            return open_rgb(filepath)

        # Local demo backfills need neither boto3 nor an AWS credential lookup.
        if s3_client is None:
            import boto3

            s3_client = boto3.client('s3')
        buf = io.BytesIO()
        s3_client.download_fileobj(Bucket=b, Key=key, Fileobj=buf)
        buf.seek(0)
        return open_rgb(buf)

    return loader


class Dataset:
    """
    The main interface for working with Freedom Ridge datasets.

    Creating/Loading:
        dataset = Dataset("my_dataset")  # Creates or loads

    Querying (3 syntaxes):
        # 1. Direct attribute access
        s = dataset.samples
        view = s[(s.brightness > 0.5) & (s.scene == "highway")]

        # 2. SQL-like string
        view = dataset.query("brightness > 0.5 AND scene = 'highway'")

        # 3. Django-style keyword args
        view = dataset.filter(scene="highway", brightness__gt=0.5)

    Iterating:
        for sample in view:
            print(sample.filepath)
    """

    def __init__(
        self,
        name: str,
        description: Optional[str] = None,
        media_fields: Optional[List[str]] = None,
        storage_config_id: Optional[int] = None,
        is_global: bool = False,
    ):
        """Load an existing dataset by name, or create it if none exists.

        Args:
            name: Dataset name (unique).
            description, media_fields, storage_config_id, is_global: used only
                when CREATING a new dataset; ignored if one named ``name``
                already exists. ``media_fields`` defaults to ``["filepath"]``.
                A new dataset is private unless ``is_global=True``: admins, the
                owner and granted users see it.

        To load strictly by numeric id, use ``Dataset.from_id(id)``.
        """
        self.name = name
        self.client = get_client()
        self._id: Optional[int] = None
        self._info: Optional[dict] = None  # cached dataset row (description, etc.)
        self._array_field_names: List[str] = []  # cached so __getitem__ avoids a network call
        self._samples_query: Optional[SampleQuery] = None
        self._dirty: bool = False  # Track if changes were made that need stats refresh
        self._create_kwargs = {
            "description": description,
            "media_fields": media_fields,
            "storage_config_id": storage_config_id,
            "is_global": is_global,
        }
        self._load_or_create()

    @classmethod
    def from_id(cls, dataset_id: int) -> "Dataset":
        """Load an existing dataset by its numeric id (does not create)."""
        client = get_client()
        data = client.get_dataset_by_id(dataset_id)
        ds = cls.__new__(cls)
        ds.name = data["name"]
        ds.client = client
        ds._id = data["id"]
        ds._info = data
        ds._array_field_names = []
        ds._samples_query = None
        ds._dirty = False
        ds._create_kwargs = {}
        try:
            ds.list_array_fields()
        except Exception:
            pass
        return ds

    @property
    def id(self) -> Optional[int]:
        """The dataset's numeric id."""
        return self._id

    @property
    def description(self) -> Optional[str]:
        """The dataset description (None if unset)."""
        return (self._info or {}).get("description")

    @property
    def media_fields(self) -> Optional[List[str]]:
        """The dataset's media field names."""
        return (self._info or {}).get("media_fields")

    @property
    def created_at(self):
        """When the dataset was created."""
        return (self._info or {}).get("created_at")

    def __enter__(self):
        """Context manager for automatic stats refresh on exit.

        Example:
            with Dataset("my_dataset") as ds:
                for sample in ds.iter_samples():
                    ds.client.update_sample(ds._id, sample.id, metadata={"reviewed": True})
                # Stats automatically refresh when the block exits
        """
        self._dirty = False
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._dirty:
            self.refresh_stats()
        return False

    def mark_dirty(self):
        """Mark that changes were made requiring a stats refresh."""
        self._dirty = True

    def _load_or_create(self):
        data = self.client.get_dataset(self.name)
        if data:
            self._id = data['id']
            self._info = data
            print(f"Loaded dataset '{self.name}' (id: {self._id})")
            # Populate known array fields
            try:
                self.list_array_fields()
            except Exception:
                pass
        else:
            # Create new, passing through any creation kwargs from __init__.
            try:
                data = self.client.create_dataset(self.name, **self._create_kwargs)
                self._id = data['id']
                self._info = data
                print(f"Created dataset '{self.name}' (id: {self._id})")
            except Exception as e:
                raise RuntimeError(f"Failed to create dataset: {e}")

    def create_array_field(self, name: str):
        """Create a new tag-like array field for this dataset.

        Creates a real TEXT[] column with a GIN index on the samples table.
        Once created, you can set it on samples: sample.my_field = ["val1", "val2"]

        Example:
            dataset.create_array_field("inferred_tags")
            sample.inferred_tags = ["ANIMAL:DOG", "VEHICLE:TRUCK"]
        """
        result = self.client.create_array_field(self._id, name)
        Sample._known_array_fields.add(name)
        return result

    def list_array_fields(self) -> list:
        """List all array fields available for this dataset (refreshes the cache)."""
        fields = self.client.list_array_fields(self._id)
        for f in fields:
            Sample._known_array_fields.add(f['name'])
        self._array_field_names = [f['name'] for f in fields]
        return list(self._array_field_names)

    def add_samples(self, samples: List[Sample], on_duplicate: str = 'error'):
        """Add samples and return the response (includes 'ids' of created samples).

        Embeddings AND hashes set on a Sample ride inline in the same request, written
        in the same transaction as the row (see Sample.to_dict). To backfill hashes onto
        skipped/existing samples, use missing()/coverage() + set_hashes/compute_hashes.

        Args:
            samples: List of Sample objects to add.
            on_duplicate: How to handle duplicate filepaths:
                'error' — add new samples and return duplicate conflicts with ids + existing (default)
                'skip'  — silently skip duplicates
                'update' — overwrite existing samples with new data

        In 'error' mode, a duplicate HTTP 409 is returned as a dictionary, not raised.
        Validation and write failures still raise and roll back the request.
        """
        if not samples:
            return {"ids": []}

        payload = [s.to_dict() for s in samples]
        result = self.client.add_samples(self._id, payload, on_duplicate=on_duplicate)

        sample_ids = result.get("ids", [])
        if sample_ids:
            self._dirty = True
        added = len(sample_ids)
        skipped = result.get("skipped", 0)
        existing = result.get("existing", {})
        if existing:
            print(f"Duplicates: {len(existing)} already exist in '{self.name}'")
        elif skipped:
            print(f"Added {added} samples to '{self.name}' ({skipped} duplicates skipped)")
        else:
            print(f"Added {added} samples to dataset '{self.name}'")
        return result

    def __getitem__(self, filepath: str) -> Sample:
        """Get a sample by filepath: ``dataset[filepath]``.

        Args:
            filepath: The filepath to look up.

        Returns:
            Sample object with id, metadata, tags, etc.

        Raises:
            KeyError: If no sample with that filepath exists.
        """
        result = self.client.lookup_samples(self._id, 'filepath', [filepath])
        if filepath not in result:
            raise KeyError(f"No sample with filepath '{filepath}' in dataset '{self.name}'")

        sample_id = result[filepath]
        # Fetch full sample data
        resp = self.client.query_samples(
            self._id,
            limit=1,
            offset=0,
            filters={"field": "id", "operator": "=", "value": sample_id},
        )
        items = resp.get("items", [])
        if not items:
            raise KeyError(f"Sample {sample_id} not found")

        row = items[0]
        sample = Sample(
            filepath=row.get("filepath", ""),
            tags=row.get("tags", []),
            metadata=row.get("metadata", {}),
        )
        sample.id = row["id"]
        sample._dataset = self
        if row.get("location"):
            sample.location = row["location"]
        if row.get("heading") is not None:
            sample.heading = row["heading"]
        # Populate array fields (uses the cached names — no extra network call)
        for af in self._array_field_names:
            if af != 'tags' and af in row:
                sample.array_fields[af] = row[af] or []
        return sample

    def get_or_create(self, filepath: str, **kwargs) -> tuple:
        """Get existing sample by filepath, or create it if not found.

        Args:
            filepath: Sample filepath.
            **kwargs: Passed to Sample() if creating (tags, metadata, location, etc.)

        Returns:
            (sample, created) tuple. created is True if the sample was newly added.
        """
        try:
            sample = self[filepath]
            return sample, False
        except KeyError:
            sample = Sample(filepath=filepath, **kwargs)
            result = self.add_samples([sample])
            existing = result.get("existing", {})
            if existing:
                # Race condition — another process added it between our lookup and insert
                return self[filepath], False
            sample.id = result["ids"][0]
            sample._dataset = self
            return sample, True

    def create_view(
        self,
        name: str,
        view_stages: list,
        description: Optional[str] = None,
        is_public: bool = True,
    ) -> dict:
        """Create a saved view on this dataset.

        Args:
            name: View name (must be unique per dataset)
            view_stages: List of ViewStage dicts (same format as FilterContext)
            description: Optional description
            is_public: Whether the view is visible to all users

        Returns:
            dict: Created view object with id, slug, etc.

        Example:
            dataset.create_view("My Selection", view_stages=[{
                "type": "selection",
                "description": "500 Selected",
                "filter": {"field": "id", "operator": "in", "value": [1, 2, 3]},
            }])
        """
        return self.client.create_view(
            self._id,
            name,
            content={"viewStages": view_stages},
            description=description,
            is_public=is_public,
        )

    def view(self):
        """Print the web UI URL for this dataset.

        Derived from the client's base_url (minus the trailing /api) rather than hard-coded —
        it used to print port 5173, a Vite dev port this app has never served, so the SDK's only
        "open the UI" helper always handed out a URL that failed to connect.
        """
        base = re.sub(r"/api/?$", "", self.client.base_url)
        # The API and the UI are the same origin behind nginx; in local dev the UI is on :3000.
        if re.search(r":8000$", base):
            base = base[: -len(":8000")] + ":3000"
        print(f"View your dataset at: {base}/datasets/{self._id}")

    # =========================================================================
    # DATASET METADATA & FIELD MANAGEMENT
    # =========================================================================

    def delete(self, *, wait=True, timeout=1800):
        """Delete this dataset, or return its Job with wait=False."""
        return self.client.delete_dataset(self._id, wait=wait, timeout=timeout)

    def resume_deletion(self, *, wait=True, timeout=1800):
        """Resume a cancelled or failed deletion; already removed data stays removed."""
        return self.client.resume_dataset_deletion(self._id, wait=wait, timeout=timeout)

    def delete_samples(self, sample_ids, *, wait=True, timeout=1800):
        """Delete IDs or a captured view in resumable chunks (admin only)."""
        target = sample_ids._bulk_target() if hasattr(sample_ids, '_bulk_target') else {'sample_ids': sample_ids}
        return self.client.bulk_delete_samples(self._id, wait=wait, timeout=timeout, **target)

    def delete_sample(self, sample_id: int) -> dict:
        """Delete a sample from this dataset."""
        return self.client.delete_sample(self._id, sample_id)

    def get_schema(self, filters=None) -> list:
        """Get the field schema for this dataset.

        Returns list of field dicts with name, type, min/max, values, etc.
        """
        return self.client.get_schema(self._id, filters=filters)

    def set_field_type(self, field_name: str, field_type: str) -> dict:
        """Correct a metadata declaration without rewriting sample values.

        Choose unknown, boolean, number, string, datetime, list, or object.
        Demote indexed fields first. Existing statistics are invalidated and refresh separately.
        """
        result = self.client.set_field_type(self._id, field_name, field_type)
        self._dirty = True
        return result

    def promote_field(self, field_name: str, *, wait=True, timeout=3600):
        """Promote a field for indexed filtering (the index builds in a server job;
        ``wait=False`` returns its Job)."""
        return self.client.promote_field(self._id, field_name, wait=wait, timeout=timeout)

    def demote_field(self, field_name: str, *, wait=True, timeout=3600):
        """Demote a field (its index is removed online in a server job; ``wait=False`` returns its Job)."""
        return self.client.demote_field(self._id, field_name, wait=wait, timeout=timeout)

    def rename_field(self, field_name: str, new_name: str, *, wait=True, timeout=1800):
        """Rename a metadata field across every sample in the dataset."""
        result = self.client.rename_field(self._id, field_name, new_name, wait=wait, timeout=timeout)
        self._dirty = True
        return result

    def clone_field(self, field_name: str, new_name: str, *, wait=True, timeout=1800):
        """Copy a metadata field's values into a new field on every sample."""
        result = self.client.clone_field(self._id, field_name, new_name, wait=wait, timeout=timeout)
        self._dirty = True
        return result

    def clear_field(self, field_name: str, *, wait=True, timeout=1800):
        """Remove a metadata field from every sample in the dataset."""
        result = self.client.clear_field(self._id, field_name, wait=wait, timeout=timeout)
        self._dirty = True
        return result

    def merge_samples(self, source, key_field: str = "filepath", fields=None, skip_existing: bool = False) -> int:
        """Merge metadata (incl. typed labels) from ``source`` into existing samples.

        Args:
            source: iterable of Sample objects whose metadata should be merged in.
            key_field: how to match a source sample to an existing one —
                "filepath" or any metadata key (e.g. "source_id").
            fields: optional list of metadata keys to merge (default: all).
            skip_existing: if True, don't overwrite keys already present on the
                target (default False = source wins).

        Returns the number of samples updated.
        """
        src = {}
        for s in source:
            key = s.filepath if key_field == "filepath" else s.metadata.get(key_field)
            if key is None:
                continue
            md = dict(s.metadata)
            if fields is not None:
                md = {k: v for k, v in md.items() if k in fields}
            if md:
                src[key] = md
        if not src:
            return 0

        key_to_id = self.client.lookup_samples(self._id, key_field, list(src.keys()))
        updates = {sid: src[key] for key, sid in key_to_id.items() if key in src}

        if skip_existing and updates:
            existing = self.client.query_samples(
                self._id,
                limit=len(updates),
                filters={"field": "id", "operator": "in", "value": list(updates.keys())},
            )
            existing_md = {it["id"]: (it.get("metadata") or {}) for it in existing.get("items", [])}
            for sid in list(updates.keys()):
                updates[sid] = {k: v for k, v in updates[sid].items() if k not in existing_md.get(sid, {})}
                if not updates[sid]:
                    del updates[sid]

        n = self.set_values_per_sample_multi(updates) if updates else 0
        print(f"merge_samples: merged into {n} samples")
        return n

    def list_views(self) -> list:
        """List all saved views for this dataset."""
        return self.client.list_views(self._id)

    def delete_view(self, view_id: int) -> dict:
        """Delete a saved view by ID."""
        return self.client.delete_view(view_id)

    def revert(
        self,
        version_name: Optional[str] = None,
        version_id: Optional[int] = None,
        restore_deleted: bool = False,
        exact_membership: bool = False,
    ) -> dict:
        """Revert dataset to a specific version.

        Args:
            version_name: Version name (e.g., "v1.0")
            version_id: Or version ID directly
            restore_deleted: Also re-create samples deleted since the version,
                from their snapshot (original id, filepath, metadata, tags,
                location, heading, array fields). Embeddings/hashes are NOT
                restored — backfill via missing()/coverage().
            exact_membership: Restore deleted samples and remove samples added
                since the version. Defaults to False. Dataset settings are not
                changed; use checkout() to read the exact historical sample state.
        """
        if version_id is None:
            versions = self.list_versions()
            match = [v for v in versions if v['name'] == version_name]
            if not match:
                raise ValueError(f"Version '{version_name}' not found")
            version_id = match[0]['id']
        result = self.client.revert_version(
            self._id,
            version_id,
            restore_deleted=restore_deleted,
            exact_membership=exact_membership,
        )
        self._dirty = True
        return result

    def get_embedding_fields(self) -> list:
        """List available embedding field names (e.g., ['clip', 'dinov2'])."""
        return self.client.get_embedding_fields(self._id)

    def get_hash_fields(self) -> list:
        """List available hash field names (e.g., ['dhash_8', 'phash_8'])."""
        return self.client.get_hash_fields(self._id)

    def get_hash_stats(self, filters=None) -> dict:
        """Get hash coverage and duplicate statistics."""
        return self.client.get_hash_stats(self._id, filters=filters)

    # =========================================================================
    # QUERY API - Three ways to filter/query your data
    # =========================================================================

    @property
    def samples(self) -> SampleQuery:
        """
        Access samples via attribute-based querying.

        Example:
            s = dataset.samples
            view = s[(s.brightness > 0.5) & (s.scene == "highway")]
            view = s[(s.weather == "rainy") | (s.weather == "foggy")]
            view = s[~(s.tags.contains("invalid"))]  # NOT

        Returns:
            SampleQuery: A query builder for constructing filters
        """
        if self._samples_query is None:
            self._samples_query = SampleQuery(self)
        return self._samples_query

    def query(self, query_string: str) -> DatasetView:
        """
        Query samples using SQL-like syntax.

        Example:
            view = dataset.query("brightness > 0.5 AND scene = 'highway'")
            view = dataset.query("scene IN ('highway', 'city') AND weather != 'sunny'")
            view = dataset.query("brightness BETWEEN 0.3 AND 0.8")
            view = dataset.query("filename LIKE '%night%'")

        Supported operators:
            - Comparison: =, !=, <>, <, >, <=, >=
            - Logical: AND, OR, NOT
            - Set: IN (value1, value2, ...)
            - Range: BETWEEN min AND max
            - String: LIKE '%pattern%', CONTAINS 'pattern'
            - Null: IS NULL, IS NOT NULL

        Args:
            query_string: SQL-like filter expression

        Returns:
            DatasetView: A filtered view of the dataset
        """
        filters = parse_query_string(query_string)
        view = DatasetView(self)
        if filters:
            view._filters = filters
        return view

    def filter(self, *conditions: Condition, **kwargs) -> DatasetView:
        """
        Filter samples using Django-style keyword arguments.

        Example:
            view = dataset.filter(scene="highway")
            view = dataset.filter(brightness__gt=0.5, brightness__lt=0.9)
            view = dataset.filter(scene__in=["highway", "city"])
            view = dataset.filter(tags__contains="validated")

        Supported operators (via double underscore):
            - field=value      → equals
            - field__gt=5      → greater than
            - field__gte=5     → greater than or equal
            - field__lt=5      → less than
            - field__lte=5     → less than or equal
            - field__ne=5      → not equal
            - field__in=[...]  → in list
            - field__nin=[...] / field__not_in=[...] → not in list
            - field__contains="x" → contains substring
            - field__exists=True   → the metadata key is present (False → absent)
            - field__is_null=True  → the value is NULL (False → NOT NULL)

        An unrecognised suffix raises ValueError. It used to fall back to equality, so
        ``filter(brightness__between=[0.3, 0.8])`` quietly became ``brightness = '[0.3, 0.8]'``
        and returned an empty view — a typo produced a plausible wrong answer instead of an error.
        A field whose NAME contains ``__`` is still reachable: pass it as a ``Condition``
        (``F("odd__name") == 1``) or via ``query()``.

        Args:
            *conditions: Optional Condition objects to apply
            **kwargs: Field conditions as keyword arguments

        Returns:
            DatasetView: A filtered view of the dataset

        Raises:
            ValueError: if a ``__`` suffix is not a supported operator.
        """
        view = DatasetView(self)
        all_conditions = []

        # Add any Condition objects passed directly
        for cond in conditions:
            if isinstance(cond, Condition):
                all_conditions.append(cond.to_dict())

        # Parse kwargs into conditions
        for key, value in kwargs.items():
            if "__" in key:
                field, op = key.rsplit("__", 1)
                if op not in _FILTER_OPS:
                    # NEVER fall back to equality. `brightness__between=[0.3, 0.8]` used to become
                    # `brightness = '[0.3, 0.8]'` -> 0 rows, and `scene__nin=[...]` (a natural
                    # guess) became `scene = [...]` -> 0 rows. The caller saw a plausible empty
                    # result and no error, which is worse than a crash.
                    raise ValueError(
                        f"unknown filter operator {op!r} in {key!r}; supported: "
                        f"{sorted(_FILTER_OPS)}. If {field + '__' + op!r} is a real field name, "
                        f"use a Condition (F({key!r}) == value) or query() instead.",
                    )
                operator = _FILTER_OPS[op]
                # exists/is_null are unary on the wire; the kwarg's bool picks the polarity so
                # `reviewed__exists=False` means "the key is absent" rather than "== False".
                if op in _FILTER_UNARY_OPS:
                    if not isinstance(value, bool):
                        raise ValueError(f"{key!r} takes True or False, got {value!r}")
                    when_true, when_false = _FILTER_UNARY_OPS[op]
                    operator = when_true if value else when_false
                    value = None
            else:
                field = key
                operator = "="

            all_conditions.append({"field": field, "operator": operator, "value": value})

        if all_conditions:
            if len(all_conditions) == 1:
                view._filters = all_conditions[0]
            else:
                view._filters = {"logic": "AND", "filters": all_conditions}

        return view

    def all(self) -> DatasetView:
        """
        Get a view of all samples (no filters).

        Example:
            for sample in dataset.all():
                print(sample.filepath)

        Returns:
            DatasetView: An unfiltered view of the dataset
        """
        return DatasetView(self)

    # =========================================================================
    # ACCESS CONTROL API
    # =========================================================================

    def grant_access(self, user_id: int) -> dict:
        """Grant a user access to this dataset (admin only).

        Only meaningful for non-global datasets. Idempotent.
        """
        return self.client.grant_dataset_access(self._id, user_id)

    def revoke_access(self, user_id: int) -> None:
        """Revoke a user's access to this dataset (admin only). No-op if not granted."""
        return self.client.revoke_dataset_access(self._id, user_id)

    def list_access(self) -> dict:
        """List users with explicit access to this dataset (admin only).

        Returns {"is_global": bool, "grants": [{user_id, username, ...}, ...]}.
        """
        return self.client.list_dataset_access(self._id)

    # =========================================================================
    # VERSIONING API
    # =========================================================================

    def create_version(self, name: str, description: Optional[str] = None, include_metadata: bool = True) -> dict:
        """Create a named snapshot of the current dataset state.

        Args:
            name: Version name (e.g., "v1.0")
            description: Optional description
            include_metadata: If True, stores full metadata per sample
                and dataset settings for historical browsing and checkout.
                If False, stores hashes and array fields for limited
                comparisons; historical checkout and restore are unavailable.

        Example:
            dataset.create_version("v1.0", "Initial import")
            dataset.create_version("v2.0", include_metadata=False)  # lightweight
        """
        result = self.client.create_version(self._id, name, description, include_metadata)
        print(f"Created version '{name}' ({result['sample_count']:,} samples)")
        return result

    def list_versions(self) -> list:
        """List all versions, newest first."""
        return self.client.list_versions(self._id)

    def version_status(self) -> dict:
        """Current version status: latest version + whether there are uncommitted changes.

        Returns a dict with ``has_changes`` and (if any version exists) the
        current/latest version info.
        """
        return self.client.get_version_status(self._id)

    def diff(self, version_name: Optional[str] = None, version_id: Optional[int] = None) -> dict:
        """Compare a version against the current dataset state.

        Args:
            version_name: Version name to compare (e.g., "v1.0")
            version_id: Or version ID directly

        Returns:
            Dict with added, removed, modified, unchanged counts

        Example:
            diff = dataset.diff("v1.0")
            print(f"{diff['added']} added, {diff['modified']} modified")
        """
        if version_id is None:
            versions = self.list_versions()
            match = [v for v in versions if v['name'] == version_name]
            if not match:
                raise ValueError(f"Version '{version_name}' not found")
            version_id = match[0]['id']

        return self.client.get_current_diff(self._id, version_id)

    def version(self, version_name: Optional[str] = None, version_id: Optional[int] = None) -> VersionView:
        """A read-only, queryable view of a version (snapshot): count, distinct, filter, iterate.

        Queries run server-side against the version's stored data, like a DatasetView on the live
        dataset. With no argument, the newest version.

        Args:
            version_name: Version name (e.g., "v1.8")
            version_id: Or version ID directly

        Example:
            v = dataset.version("v1.8")
            v.count_values("split")
            v.filter(F("split").is_in(["train", "val"])).select_fields(["source_id"]).distinct("source_id")
        """
        versions = self.list_versions()
        if version_id is not None:
            match = [v for v in versions if v['id'] == version_id]
            label = f"id {version_id}"
        elif version_name is not None:
            match = [v for v in versions if v['name'] == version_name]
            label = f"'{version_name}'"
        else:
            match, label = versions[:1], "(newest)"
        if not match:
            raise ValueError(f"Version {label} not found")
        if match[0].get('include_metadata') is False:
            raise ValueError(f"Version '{match[0]['name']}' is hash-only and has no sample data to query")
        return VersionView(self, match[0])

    def checkout(self, version_name: Optional[str] = None, version_id: Optional[int] = None):
        """Iterate over the samples as they were at a specific version.

        Returns samples with their snapshotted metadata, tags, and filepath —
        not the current state. Snapshot samples are read-only; save() is refused.

        Args:
            version_name: Version name (e.g., "v1.0")
            version_id: Or version ID directly

        Yields:
            Sample objects with the versioned metadata

        Example:
            for sample in dataset.checkout("v1.0"):
                print(sample.filepath, sample.metadata.get("reviewed"))
        """
        if version_id is None:
            versions = self.list_versions()
            match = [v for v in versions if v['name'] == version_name]
            if not match:
                raise ValueError(f"Version '{version_name}' not found")
            version_id = match[0]['id']

        after_id = None
        offset = 0
        batch_size = 500

        while True:
            data = self.client.get_version_samples(
                self._id,
                version_id,
                limit=batch_size,
                offset=offset,
                full=True,
                after_id=after_id,
            )
            items = data.get('items', [])

            if not items:
                break
            if after_id is not None and items[0]['id'] <= after_id:
                raise ValueError("Snapshot cursor did not advance; update the backend before checking out this version")

            for item in items:
                yield Sample.from_row({**item, "version_id": version_id}, self)

            if 'snapshot_format' in data:
                after_id = items[-1]['id']
                offset = 0
            else:
                offset += len(items)
            if len(items) < batch_size:
                break

    # =========================================================================
    # SAMPLE MODIFICATION API
    # =========================================================================

    def tag_samples(self, sample_ids, tags: list, *, wait=True, timeout=1800):
        """
        Add tags to multiple samples.

        Args:
            sample_ids: List of sample IDs to tag (or a DatasetView)
            tags: List of tags to add

        Returns:
            dict: Update confirmation with count

        Example:
            # Tag samples from a view
            view = dataset.filter(scene="highway").limit(100)
            dataset.tag_samples(view.ids(), ["reviewed", "highway_subset"])
        """
        # Handle DatasetView passed directly
        target = sample_ids._bulk_target() if hasattr(sample_ids, '_bulk_target') else {'sample_ids': sample_ids}

        return self.client.bulk_tag_samples(self._id, add_tags=tags, wait=wait, timeout=timeout, **target)

    def untag_samples(self, sample_ids, tags: list, *, wait=True, timeout=1800):
        """
        Remove tags from multiple samples.

        Args:
            sample_ids: List of sample IDs to untag (or a DatasetView)
            tags: List of tags to remove

        Returns:
            dict: Update confirmation with count

        Example:
            # Remove tags from samples
            view = dataset.filter(tags__contains="needs_review")
            dataset.untag_samples(view.ids(), ["needs_review"])
        """
        # Handle DatasetView passed directly
        target = sample_ids._bulk_target() if hasattr(sample_ids, '_bulk_target') else {'sample_ids': sample_ids}

        return self.client.bulk_tag_samples(self._id, remove_tags=tags, wait=wait, timeout=timeout, **target)

    def set_values(self, sample_ids, field: str, value, *, wait=True, timeout=1800):
        """
        Set a metadata field value on multiple samples.

        Args:
            sample_ids: List of sample IDs to update (or a DatasetView)
            field: Name of the metadata field to set
            value: Value to set

        Returns:
            int: Number of samples updated

        Example:
            # Mark samples as reviewed
            view = dataset.filter(scene="highway").limit(100)
            dataset.set_values(view.ids(), "reviewed_by", "john@example.com")
        """
        # Handle DatasetView passed directly
        target = sample_ids._bulk_target() if hasattr(sample_ids, '_bulk_target') else {'sample_ids': sample_ids}

        # One bulk request instead of a PATCH per sample.
        result = self.client.bulk_set_metadata(self._id, metadata={field: value}, wait=wait, timeout=timeout, **target)
        self._dirty = True
        return result["updated_count"] if wait else result

    def lookup_samples(self, field: str, values: list) -> dict:
        """Fast bulk lookup: metadata field values -> sample IDs.

        Args:
            field: Metadata field name (e.g. 'source_id', 'filepath')
            values: Values to look up

        Returns:
            Dict mapping field values to sample IDs

        Example:
            name_to_id = dataset.lookup_samples('source_id', ['img1.jpg', 'img2.jpg'])
        """
        return self.client.lookup_samples(self._id, field, values)

    def set_hashes(self, sample_ids, hashes: dict):
        """Set image hashes on existing samples.

        Args:
            sample_ids: List of sample IDs (or a DatasetView)
            hashes: Dict of {field_name: hash_hex} to set on each sample

        Example:
            dataset.set_hashes([1, 2, 3], {"dhash_8": "abc123", "phash_8": "def456"})

            # Or from a view:
            view = dataset.filter(tags__contains="needs_hashing")
            dataset.set_hashes(view.ids(), {"dhash_8": "abc123"})
        """
        if hasattr(sample_ids, 'ids'):
            sample_ids = sample_ids.ids()

        payload = []
        for sid in sample_ids:
            for field_name, hash_hex in hashes.items():
                payload.append(
                    {
                        "sample_id": sid,
                        "field_name": field_name,
                        "hash_hex": hash_hex,
                    },
                )

        if payload:
            self.client.upload_hashes(self._id, payload)

        return len(sample_ids)

    def compute_hashes(self, image_loader=None, algorithms=None, batch_size=100, overwrite=False):
        """Compute missing perceptual image hashes, skipping object rows.

        Existing fields are read through the paginated hash export. Fully hashed
        images are never opened; partially hashed images compute only missing
        fields. Memory stays bounded by one export page per requested field.
        Pixels are converted to RGB without EXIF transposition, then imagehash
        performs grayscale conversion. Hashes are zero-padded lowercase hex.

        Args:
            image_loader: Callable(filepath) -> PIL.Image. If None, uses
                          the default local/S3 loader.
            algorithms: Standard imagehash names: dhash_N, phash_N, ahash_N or
                        whash_N, e.g. ['dhash_8', 'phash_16']. None uses dhash_8 +
                        phash_8. Custom hash functions are not accepted.
            batch_size: Samples per iteration/upload batch (default: 100).
            overwrite: Recompute existing fields when True (default: False).

        Returns:
            Counts of computed images, already-hashed images skipped, object rows
            skipped, and images with loading or hashing errors.

        Example:
            dataset.compute_hashes()

            # Other standard hashes (overwrite=True recomputes existing values):
            dataset.compute_hashes(algorithms=['dhash_16', 'whash_8'])
        """
        from .hashes import resolve_hash_algorithms

        if isinstance(algorithms, dict):
            raise ValueError(
                'compute_hashes takes standard hash names (dhash_N, phash_N, ahash_N, whash_N), not custom functions',
            )
        return self._compute_hashes(image_loader, resolve_hash_algorithms(algorithms), batch_size, overwrite)

    def _compute_hashes(self, image_loader, algorithms, batch_size, overwrite):
        """Hash image rows with {field: function(PIL.Image) -> str}; see compute_hashes."""
        if batch_size < 1:
            raise ValueError('batch_size must be positive')

        def stored_ids(field):
            # Both exports and iter_samples use ascending sample-ID keysets.
            after_id = 0
            while True:
                items = self.client.export_hashes(self._id, field, after_id=after_id, limit=batch_size)['items']
                yield from (item['sample_id'] for item in items)
                if len(items) < batch_size:
                    break
                after_id = items[-1]['sample_id']

        exports = {} if overwrite else {name: iter(stored_ids(name)) for name in algorithms}
        current = {name: next(ids, None) for name, ids in exports.items()}
        counts = dict(computed=0, skipped=0, objects_skipped=0, errors=0)
        hash_batch = []
        for sample in self.iter_samples(
            selected_fields=['filepath', 'media_type'],
            progress=True,
            batch_size=batch_size,
        ):
            if sample.media_type == 'object':
                counts['objects_skipped'] += 1
                continue
            missing = {}
            for name, function in algorithms.items():
                if not overwrite:
                    while current[name] is not None and current[name] < sample.id:
                        current[name] = next(exports[name], None)
                if overwrite or current[name] != sample.id:
                    missing[name] = function
            if not missing:
                counts['skipped'] += 1
                continue
            img = None
            try:
                if image_loader is None:
                    image_loader = _default_s3_image_loader()
                img = image_loader(sample.filepath)
                if img is None:
                    counts['errors'] += 1
                    continue
                if img.mode != 'RGB':
                    rgb = img.convert('RGB')
                    img.close()
                    img = rgb
                failed = False
                for name, function in missing.items():
                    try:
                        hash_batch.append(
                            {'sample_id': sample.id, 'field_name': name, 'hash_hex': function(img)},
                        )
                    except Exception:
                        failed = True
                counts['errors'] += int(failed)
                counts['computed'] += 1
            except Exception:
                counts['errors'] += 1
            finally:
                if img is not None:
                    img.close()

            if len(hash_batch) >= batch_size * len(algorithms):
                self.client.upload_hashes(self._id, hash_batch)
                hash_batch = []

        if hash_batch:
            self.client.upload_hashes(self._id, hash_batch)

        print(
            f"Hashes: {counts['computed']} computed, {counts['skipped']} already hashed, "
            f"{counts['objects_skipped']} objects skipped, {counts['errors']} errors",
        )
        return counts

    def patch_similarity_search(
        self,
        sample_id,
        patch_scale="4x4",
        patch_row=0,
        patch_col=0,
        field_name="clip",
        limit=50,
        filters=None,
    ):
        """Search for images with patches similar to a selected patch.

        Returns the best-matching patch per image with its grid position.

        Example:
            results = dataset.patch_similarity_search(
                sample_id=12345, patch_scale="4x4", patch_row=2, patch_col=3
            )
            for r in results:
                print(f"Sample {r['sample_id']}: patch r{r['patch_row']}c{r['patch_col']}")
        """
        return self.client.patch_similarity_search(
            dataset_id=self._id,
            sample_id=sample_id,
            field_name=field_name,
            patch_scale=patch_scale,
            patch_row=patch_row,
            patch_col=patch_col,
            limit=limit,
            filters=filters,
        )

    def list_jobs(self, *, limit=50, cursor=None, include_system_statistics=False) -> dict:
        """Page history, optionally including successful automatic incremental statistics from the last day."""
        return self.client.list_jobs(
            self._id,
            limit=limit,
            cursor=cursor,
            include_system_statistics=include_system_statistics,
        )

    def schedules(self) -> list:
        """This dataset's schedules (Jobs panel → Schedules), each with its last and next run."""
        return self.client.list_schedules(self._id)

    def add_schedule(self, field_name, **rule) -> dict:
        """Schedule map re-fits of an embedding field. Defaults: weekly on Sunday at 03:00 UTC, only
        with >= 20,000 new samples, no minimum gap, switched off (pass ``enabled=True``)."""
        return self.client.create_schedule(self._id, field_name, **rule)

    def run_schedule(self, schedule_id, *, wait=False, timeout=6 * 3600):
        """Run a schedule's job now, ignoring its conditions. Returns the Job, or its result with wait=True."""
        job = self.client.run_schedule(self._id, schedule_id)
        return job.wait(timeout=timeout) if wait else job

    def map_state(self, field_name) -> dict:
        """The field's map: current model, ``refitted_at``, and ``active`` while a re-fit runs."""
        return self.client.get_map_state(self._id, field_name)

    def refresh_stats(self, *, wait=True, timeout=1800, force=False):
        """Refresh committed changes without waiting for future incoming samples.

        Returns the completed result by default, or a reconnectable Job with wait=False.
        force=True rebuilds all statistics; ordinary calls process changed blocks only.
        Timeout raises JobTimeout with the job ID and does not cancel server work.
        """
        result = self.client.refresh_stats(self._id, wait=wait, timeout=timeout, force=force)
        if wait:
            print(f"Field stats refreshed for '{self.name}'")
        return result

    def compute_metadata(self, image_loader=None, batch_size=500, skip_existing=True):
        """Record basic media metadata for every sample: width, height, aspect_ratio,
        megapixels, and file_size_bytes (local files). Enables filtering/faceting by
        resolution and aspect ratio.

        Cheap: PIL reads dimensions from the image header without decoding pixels.
        Needs only the base install (Pillow). Skips samples that already have 'width'
        unless skip_existing=False.
        """
        try:
            from PIL import Image  # noqa: F401 — base dep, but fail with a clear message
        except ImportError:
            raise ImportError("Pillow is required: pip install Pillow")

        if image_loader is None:
            image_loader = _default_s3_image_loader()

        processed = 0
        skipped = 0
        errors = 0
        pending: Dict[int, Dict[str, Any]] = {}

        def _flush():
            if pending:
                self.set_values_per_sample_multi(pending)
                pending.clear()

        for sample in self.iter_samples(progress=True, batch_size=batch_size):
            if skip_existing and sample.metadata.get("width") is not None:
                skipped += 1
                continue
            try:
                img = image_loader(sample.filepath)
                if img is None:
                    errors += 1
                    continue
            except Exception as e:
                print(f"  Error loading {sample.filepath}: {e}")
                errors += 1
                continue
            pending[sample.id] = _media_metadata(img, sample.filepath)
            processed += 1
            if len(pending) >= batch_size:
                _flush()

        _flush()
        print(f"Metadata: {processed} computed, {skipped} skipped, {errors} errors")
        if processed > 0:
            self.refresh_stats()

    def compute_metrics(self, image_loader=None, batch_size=100, skip_existing=True):
        """Compute image quality metrics (brightness, blurriness, contrast, entropy,
        salt_and_pepper, saturation, pct_underexposed, pct_overexposed).

        Downloads each image, computes metrics, and updates sample metadata.
        Automatically refreshes field stats when done.

        Args:
            image_loader: Callable(filepath) -> PIL.Image. If None, uses a default
                          S3 loader (requires boto3 and AWS credentials).
            batch_size: Samples per iteration batch (default: 100)
            skip_existing: Skip samples that already have a 'brightness' field (default: True)

        Example:
            dataset = Dataset("my_dataset")
            dataset.compute_metrics()

            # Custom loader for local files:
            dataset.compute_metrics(image_loader=lambda p: Image.open(p))
        """
        _require_metric_dependencies()
        if image_loader is None:
            image_loader = _default_s3_image_loader()

        processed = 0
        skipped = 0
        errors = 0
        pending: Dict[int, Dict[str, Any]] = {}  # sample_id -> metrics; flushed in batches (bulk, not N+1 PATCH)

        def _flush():
            if pending:
                self.set_values_per_sample_multi(pending)
                pending.clear()

        for sample in self.iter_samples(progress=True, batch_size=batch_size):
            if skip_existing and sample.metadata.get("brightness") is not None:
                skipped += 1
                continue

            try:
                img = image_loader(sample.filepath)
                if img is None:
                    errors += 1
                    continue
                if img.mode != "RGB":
                    img = img.convert("RGB")
            except Exception as e:
                print(f"  Error loading {sample.filepath}: {e}")
                errors += 1
                continue

            pending[sample.id] = compute_image_metrics(img)
            processed += 1
            if len(pending) >= batch_size:
                _flush()

        _flush()
        print(f"Metrics: {processed} computed, {skipped} skipped, {errors} errors")
        if processed > 0:
            self.refresh_stats()

    def compute_embeddings(
        self,
        model_name="clip-ViT-B-32",
        field_name="clip",
        image_loader=None,
        batch_size=50,
        force_register=False,
        skip_existing=False,
    ):
        """Compute CLIP embeddings for every sample (or just those lacking the field) and upload them.

        Loads each image, encodes a batch through the model, and uploads the
        vectors. All requests go through the authenticated SDK client, so this
        works on private (non-global) datasets.

        Args:
            model_name: sentence-transformers model id (default: "clip-ViT-B-32").
            field_name: embedding field to write (default: "clip").
            image_loader: Callable(filepath) -> PIL.Image. If None, uses the
                default loader (handles s3://, /media/s3/, /media/, and local
                paths; S3 needs boto3 + AWS credentials).
            force_register: re-point the field's model registration when it is already
                registered to a DIFFERENT checkpoint. Without it the mismatch is reported as a
                warning and the old registration stands, so text search keeps using the old
                text tower against the vectors you just wrote.
            batch_size: images encoded/uploaded per batch (default: 50).
            skip_existing: embed only samples that have no ``field_name`` vector yet (listed by the
                server via ``missing('embedding', field_name)``), e.g. a nightly run after new samples
                were added. Default False re-embeds every sample and overwrites existing vectors,
                which is what a model change needs. Existing vectors are not checked against
                ``model_name``, so do not use it to switch models on a field.

        Returns:
            Counts for total, written and dropped embeddings, plus dropped_sample_ids.
            Missing sample IDs are warned about and dropped; other upload errors raise.

        Example:
            dataset.compute_embeddings()
            # local files only:
            from PIL import Image
            dataset.compute_embeddings(image_loader=lambda p: Image.open(p))
        """
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            raise ImportError(
                "Please install sentence-transformers: pip install sentence-transformers Pillow "
                "(or: pip install \"freedomridge[all]\")",
            )

        if image_loader is None:
            image_loader = _default_s3_image_loader()

        print(f"Loading model {model_name}...")
        model = SentenceTransformer(model_name)

        processed = 0
        written_total = 0
        dropped_total = 0
        dropped_sample_ids: set[int] = set()
        errors = 0
        batch_imgs: List[Any] = []
        batch_ids: List[int] = []

        def flush():
            nonlocal processed, written_total, dropped_total, batch_imgs, batch_ids
            if not batch_imgs:
                return
            vectors = model.encode(batch_imgs, normalize_embeddings=True)
            payload = [
                {"sample_id": sid, "vector": vec.tolist(), "field_name": field_name}
                for sid, vec in zip(batch_ids, vectors)
            ]
            result = self.client.upload_embeddings(self._id, payload)
            processed += len(payload)
            written_total += result["written"]
            dropped_total += result["dropped"]
            dropped_sample_ids.update(result["dropped_sample_ids"])
            print(f"Uploaded {result['written']} embeddings (total: {written_total})")
            batch_imgs = []
            batch_ids = []

        def missing_samples():
            """Samples lacking field_name, paged by id cursor so ones that fail to load are not re-listed."""
            after_id = None
            while True:
                page = self.missing("embedding", field=field_name, limit=1000, after_id=after_id)
                if after_id is None:
                    print(f"{page['total']} samples lack '{field_name}'")
                if not page["items"]:
                    return
                yield from (SimpleNamespace(**item) for item in page["items"])
                after_id = page["items"][-1]["id"]

        if skip_existing:
            samples = missing_samples()
        else:
            samples = self.iter_samples(progress=True, batch_size=1000)
        for sample in samples:
            try:
                img = image_loader(sample.filepath)
                if img is None:
                    errors += 1
                    continue
                if img.mode != "RGB":
                    img = img.convert("RGB")
            except Exception as e:
                errors += 1
                if errors <= 3:
                    print(f"  Error loading {sample.filepath}: {e}")
                continue
            batch_imgs.append(img)
            batch_ids.append(sample.id)
            if len(batch_imgs) >= batch_size:
                flush()
        flush()

        print(f"Embeddings: {written_total} written, {dropped_total} dropped, {errors} load errors")

        # Register which checkpoint produced this field (text search consults it to
        # pick the matching CLIP text tower). Best-effort ONLY for the 404 an older server
        # returns — a bare `except Exception: pass` also swallowed the 409 that means "this
        # field is already registered to a DIFFERENT checkpoint". Re-embedding with another
        # same-dimension model (clip-ViT-B-32 -> openai/clip-vit-base-patch32, both 512-d, so
        # the dim guard doesn't fire) then overwrote every vector while text search kept using
        # the OLD model's text tower — quietly wrong nearest neighbours, no error anywhere.
        if processed:
            try:
                self.client.register_field_model(self._id, field_name, model_name, force=force_register)
            except requests.HTTPError as e:
                status = e.response.status_code if e.response is not None else None
                if status == 404:
                    pass  # server predates the registry
                elif status == 409:
                    warnings.warn(
                        f"Field '{field_name}' is registered to a different model. Its vectors were "
                        f"just overwritten with '{model_name}', but text search and text concepts "
                        f"will keep using the previously registered checkpoint — results will be "
                        f"wrong. Re-run with force_register=True to re-point the field.\n  {e}",
                        stacklevel=2,
                    )
                else:
                    raise

        return {
            "total": processed,
            "written": written_total,
            "dropped": dropped_total,
            "dropped_sample_ids": sorted(dropped_sample_ids),
        }

    def compute_patch_embeddings(
        self,
        model_name="openai/clip-vit-base-patch32",
        field_name="clip",
        scales=None,
        image_loader=None,
        batch_size=50,
        reduce=True,
        partition=None,
        total_partitions=None,
        num_threads=4,
    ):
        """Compute CLIP embeddings for image patches at specified grid scales.

        Divides each image into grid patches, encodes each through CLIP, and
        uploads to Freedom Ridge. Uses a threaded pipeline for performance:
        [thread pool: load+crop] → [GPU: encode] → [background: upload]

        Args:
            model_name: HuggingFace CLIP model ID (default: openai/clip-vit-base-patch32)
            field_name: Base embedding field name (default: "clip")
            scales: Grid scales e.g. ["2x2", "4x4"]. Default: ["2x2", "4x4"]
            image_loader: Optional callable(filepath) -> PIL.Image
            batch_size: Images per GPU batch
            reduce: Run UMAP after computing for embeddings panel (default: True)
            partition: Which partition this worker handles (1-indexed). None = all.
            total_partitions: Total number of partitions. Required if partition is set.
            num_threads: Threads for image loading (default: 4)

        Returns:
            Counts for total, written and dropped patches, plus dropped_sample_ids.
            Missing sample IDs are warned about and dropped; other upload errors raise.

        Distributed usage:
            # On worker 1 of 10:
            dataset.compute_patch_embeddings(partition=1, total_partitions=10)
            # On worker 2 of 10:
            dataset.compute_patch_embeddings(partition=2, total_partitions=10)
        """
        try:
            import re as _re
            import threading
            from concurrent.futures import ThreadPoolExecutor, as_completed

            import torch
            from transformers import CLIPModel, CLIPProcessor
        except ImportError:
            raise ImportError("pip install transformers torch Pillow")

        tqdm: Any
        try:
            from tqdm import tqdm  # type: ignore[no-redef]
        except ImportError:
            tqdm = None

        if scales is None:
            scales = ["2x2", "4x4"]

        if (partition is None) != (total_partitions is None):
            raise ValueError("partition and total_partitions must both be set or both be None")
        if partition is not None:
            if partition < 1 or partition > total_partitions:
                raise ValueError(f"partition must be 1..{total_partitions}, got {partition}")

        parsed_scales = []
        for s in scales:
            if not _re.match(r'^\d+x\d+$', s):
                raise ValueError(f"Invalid scale format '{s}', expected NxN (e.g. '2x2', '4x4')")
            parts = s.split("x")
            parsed_scales.append((int(parts[0]), int(parts[1]), s))

        # Load model
        print(f"Loading CLIP model {model_name}...")
        device = "cuda" if torch.cuda.is_available() else "cpu"
        model = CLIPModel.from_pretrained(model_name).to(device)
        processor = CLIPProcessor.from_pretrained(model_name)
        print(f"Using device: {device}")

        # Reuse the shared loader (handles s3://, /media/s3/, /media/, and local
        # paths) so patch embeddings work on S3-backed datasets and a missing
        # FRIDGE_DATA_ROOT surfaces loudly instead of silently using './data'.
        loader = image_loader or _default_s3_image_loader()

        # Get total sample count
        count_data = self.client.get_samples(self._id, limit=1, offset=0)
        total_samples = count_data.get("total", 0)

        # Compute partition range
        if partition is not None:
            chunk_size = (total_samples + total_partitions - 1) // total_partitions
            start_offset = (partition - 1) * chunk_size
            end_offset = min(partition * chunk_size, total_samples)
            partition_size = end_offset - start_offset
            print(
                f"Partition {partition}/{total_partitions}: samples {start_offset}-{end_offset} ({partition_size} samples)",  # noqa: E501
            )
        else:
            start_offset = 0
            end_offset = total_samples
            partition_size = total_samples

        total_patches_per_image = sum(r * c for r, c, _ in parsed_scales)
        print(f"Computing patch embeddings for {partition_size} samples")
        print(f"  Scales: {scales} ({total_patches_per_image} patches/image)")
        print(f"  Total patches: ~{partition_size * total_patches_per_image:,}")
        print(f"  Threads: {num_threads}")

        # Background upload thread — uses its own requests.Session for thread safety, with the
        # same cookies, headers and Bearer auth (else token-authed uploads 401 or fall back to netrc).
        upload_client = self.client.clone()
        upload_error = None
        upload_lock = threading.Lock()
        upload_totals = {"total": 0, "written": 0, "dropped": 0}
        dropped_sample_ids: set[int] = set()

        def upload_batch(payload):
            nonlocal upload_error
            try:
                result = upload_client.upload_patch_embeddings(self._id, payload)
                for key in upload_totals:
                    upload_totals[key] += result[key]
                dropped_sample_ids.update(result["dropped_sample_ids"])
            except Exception as e:
                with upload_lock:
                    upload_error = e

        # Threaded image load + crop
        def load_and_crop(item):
            """Load image and crop all patches. Returns list of (crop, sample_id, row, col, scale)."""
            try:
                img = loader(item["filepath"])
            except Exception:
                return None, item.get("filepath", "?")

            w, h = img.size
            patches = []
            for rows, cols, scale_str in parsed_scales:
                pw, ph = w // cols, h // rows
                for r in range(rows):
                    for c in range(cols):
                        x0, y0 = c * pw, r * ph
                        x1 = w if c == cols - 1 else x0 + pw
                        y1 = h if r == rows - 1 else y0 + ph
                        crop = img.crop((x0, y0, x1, y1))
                        patches.append((crop, item["id"], r, c, scale_str))
            return patches, None

        offset = start_offset
        processed = 0
        load_failures = 0
        pbar = tqdm(total=partition_size, desc="Patch embeddings") if tqdm else None
        upload_thread = None

        while offset < end_offset:
            fetch_size = min(batch_size, end_offset - offset)
            data = self.client.get_samples(self._id, limit=fetch_size, offset=offset)
            items = data.get("items", [])
            if not items:
                break
            # Patches are fixed-grid tiles of SOURCE images. Skip media_type='object' rows
            # (per-detection crops in the object/patch view) — tiling a crop is meaningless
            # and would write clip_patch_* onto object rows. In a pure-image dataset this is
            # a no-op. (Offsets still advance over the skipped rows — count is approximate.)
            items = [it for it in items if it.get("media_type") != "object"]

            # Stage 1: Load + crop in thread pool
            all_patches = []
            with ThreadPoolExecutor(max_workers=num_threads) as pool:
                futures = {pool.submit(load_and_crop, item): item for item in items}
                for future in as_completed(futures):
                    patches, err = future.result()
                    if patches is None:
                        load_failures += 1
                        if load_failures <= 3:
                            print(f"  Warning: failed to load {err}")
                        elif load_failures == 4:
                            print("  (suppressing further load warnings)")
                    else:
                        all_patches.extend(patches)

            if not all_patches:
                offset += fetch_size
                continue

            # Wait for previous upload to finish before reusing the GPU
            if upload_thread is not None:
                upload_thread.join()
                if upload_error:
                    raise RuntimeError(f"Upload failed: {upload_error}")

            # Stage 2: Encode on GPU
            patch_images = [p[0] for p in all_patches]
            from .clip import pixel_inputs

            inputs = pixel_inputs(processor, patch_images, padding=True).to(device)
            with torch.no_grad():
                from .clip import image_vectors

                image_features = image_vectors(model.get_image_features(**inputs))
                embeddings = (image_features / image_features.norm(dim=-1, keepdim=True)).cpu().numpy()

            # Build payload using the dedicated patch format
            payload = []
            for (_, sample_id, r, c, scale_str), emb in zip(all_patches, embeddings):
                payload.append(
                    {
                        "sample_id": sample_id,
                        "vector": emb.tolist(),
                        "field_name": field_name,
                        "patch_scale": scale_str,
                        "patch_row": r,
                        "patch_col": c,
                    },
                )

            # Stage 3: Upload in background thread
            upload_thread = threading.Thread(target=upload_batch, args=(payload,))
            upload_thread.start()

            processed += len(items)
            if pbar:
                pbar.update(len(items))
            else:
                print(f"  {processed}/{partition_size} images ({len(payload)} patches)")

            offset += fetch_size

        # Wait for final upload
        if upload_thread is not None:
            upload_thread.join()
            if upload_error:
                raise RuntimeError(f"Upload failed: {upload_error}")

        if pbar:
            pbar.close()

        if load_failures > 0:
            print(f"Warning: {load_failures} images failed to load")
        print(f"Done. Processed {processed} images, ~{processed * total_patches_per_image} patches.")

        # Run UMAP reduction (only if this is a full run or explicitly requested).
        # Use the client-side compute_visualization (fetches tiles, fits RAM-scaled, uploads coords
        # over the API) rather than the server-side in-process /reduce endpoint — the latter holds
        # every tile vector in the uvicorn worker and drops the connection on large patch fields
        # (e.g. an 8x8 grid over thousands of frames is hundreds of thousands of tiles).
        if reduce and partition is None:
            failed = []
            for _, _, scale_str in parsed_scales:
                patch_field = f"{field_name}_patch_{scale_str}"
                print(f"\nRunning UMAP reduction for {patch_field}...")
                try:
                    self.compute_visualization(field_name=patch_field)
                except Exception as e:
                    print(f"  UMAP reduction failed for {patch_field}: {e}")
                    failed.append((patch_field, e))
            if failed:
                # Don't accept a failed reduce as success — a non-interactive job must fail loudly
                # (the patch vectors are already uploaded; only the regenerable scatter is missing).
                names = ", ".join(f for f, _ in failed)
                raise RuntimeError(
                    f"Patch embeddings were computed, but UMAP reduction failed for: {names}. "
                    f"Re-run e.g. Dataset(name).compute_visualization(field_name='{failed[0][0]}').",
                )
        elif reduce and partition is not None:
            # compute_visualization (client-side, RAM-scaled) — NOT client.reduce_embeddings, whose
            # in-process server fit drops the connection on large patch fields.
            print("\nSkipping UMAP reduction (run after all partitions complete):")
            for _, _, scale_str in parsed_scales:
                print(f"  Dataset(name).compute_visualization(field_name='{field_name}_patch_{scale_str}')")

        return {**upload_totals, "dropped_sample_ids": sorted(dropped_sample_ids)}

    def iter_samples(self, selected_fields=None, progress=False, batch_size=1000, filters=None):
        """
        Iterate over samples in the dataset.

        Args:
            selected_fields: Optional list of field names for faster iteration.
                           Examples: ["filepath", "tags"], ["metadata.weather", "filepath"]
                           When set, only those columns are fetched from the DB.
            progress: If True, display a progress bar using tqdm (default: False)
            batch_size: Number of samples per API call (default: 1000)
            filters: Optional filter dict to scope the iteration

        Yields:
            Sample: Sample objects with requested fields populated
        """
        tqdm: Any
        try:
            from tqdm import tqdm  # type: ignore[no-redef]
        except ImportError:
            if progress:
                print("Warning: tqdm not installed. Install with: pip install tqdm")
            tqdm = None

        use_slim = selected_fields is not None
        fields_str = ','.join(selected_fields) if selected_fields else None

        # First batch: get the total count (for progress bar)
        data = self.client.query_samples(
            self._id,
            limit=batch_size,
            offset=0,
            filters=filters,
            fields=fields_str,
            slim=use_slim,
            skip_count=False,
        )
        total = data.get('total', 0)
        items = data.get('items', [])

        pbar = None
        if progress and tqdm and total > 0:
            pbar = tqdm(total=total, desc=f"Iterating {self.name}", unit="samples")

        try:
            last_id = None
            while True:
                if not items:
                    break

                for item in items:
                    # Pass self so `for s in dataset.iter_samples(): s.x = 1; s.save()` works —
                    # it used to omit the dataset and raise ValueError on save().
                    sample = Sample.from_row(item, self)
                    last_id = sample.id
                    yield sample

                    if pbar:
                        pbar.update(1)

                # End of data?
                if len(items) < batch_size:
                    break

                # Subsequent batches: keyset pagination (id > last_id), skip COUNT
                data = self.client.query_samples(
                    self._id,
                    limit=batch_size,
                    filters=filters,
                    fields=fields_str,
                    slim=use_slim,
                    skip_count=True,
                    after_id=last_id,
                )
                items = data.get('items', [])

        finally:
            if pbar:
                pbar.close()

    def _map_state(self, field_name):
        """Servers predating /map-state have unmanaged maps; other failures still propagate."""
        try:
            return self.client.get_map_state(self._id, field_name)
        except requests.HTTPError as error:
            if error.response is None or error.response.status_code != 404:
                raise
            return {}

    def compute_visualization(self, field_name="clip", fit_sample_size="auto", method="umap", save_model_path=None):
        """Compute 2D coordinates for embedding visualization.

        Fetches vectors via API, reduces them locally, uploads coordinates back.
        Works over the network — no direct database connection needed.
        Managed maps are skipped before fetching vectors or fitting: run a scheduled re-fit to
        change their layout. Incremental placement uses ``project_pending``.

        Args:
            field_name: Embedding field name (default: "clip")
            fit_sample_size: How many samples to FIT UMAP/PCA on; ALL points are
                then projected through the fitted model in batches (every sample
                gets a position). "auto" (default) scales the fit to available RAM
                — a big-memory host fits on more (better fidelity), up to all.
                Pass an int to fix it, or -1 to fit on all. Ignored by t-SNE.
            method: "umap" (default), "pca" (cheap/deterministic), or "tsne".
            save_model_path: if set (umap only), the fitted reducer is saved with
                joblib to this path (+ a .meta.json sidecar) so new points can later
                be projected via reducer.transform() without refitting.

        Returns:
            Counts in total_embeddings, reduced (points placed), and fit_sample_size
            (resolved fit size), plus ranked (False when the coordinates were saved but
            the map's ranking failed), or skipped=True with reason='map_managed'.

        Example:
            dataset.compute_visualization(field_name="embeddings_clip_vit_base32_torch")
        """
        import numpy as np

        method = method.lower()

        tqdm: Any
        try:
            from tqdm import tqdm  # type: ignore[no-redef]
        except ImportError:
            tqdm = None

        # --- Step 1: Fetch IDs + vectors, streaming each page straight into a
        # preallocated float32 array. We never build a Python list of all vectors
        # or a float64 copy, so peak RAM for the raw data is ~total*dim*4 bytes
        # (≈4 GB for 2M×512, ≈8 GB for 2M×1024) on ANY machine. ---
        # A patch-scale field ('clip_patch_8x8') has no embeddings of its own — its data lives across
        # one regular field per tile. Expand it and reduce ALL tiles jointly (one shared 2D space),
        # fetching/uploading per tile through the normal API; this is why a field of hundreds of
        # thousands of tiles reduces here (client-side, RAM-scaled) when the server-side in-process
        # endpoint cannot. None ⇒ a regular single field.
        tile_fields = _patch_tile_fields(field_name)
        # Condition on `tile_fields is not None` (not the bool alias) so the type narrows to list[str].
        fetch_fields = tile_fields if tile_fields is not None else [field_name]
        is_patch = tile_fields is not None
        for field in dict.fromkeys([field_name, *fetch_fields]):
            state = self._map_state(field)
            if state.get('managed') or state.get('model_id'):
                print(
                    f"Skipping '{field_name}': This map is managed by scheduled re-fits; "
                    "run a re-fit to change its layout.",
                )
                return {
                    'skipped': True,
                    'reason': 'map_managed',
                    'field_name': field_name,
                    'total_embeddings': 0,
                    'reduced': 0,
                }

        suffix = f" ({len(fetch_fields)} tiles)" if is_patch else ""
        print(f"Fetching embeddings for field '{field_name}'{suffix}...")
        page_size = 10000
        all_ids: list = []
        all_field_idx: list = []  # index into fetch_fields per point (patch only — for grouped upload)
        all_vectors_np: Any = None
        dim = 0
        filled = 0

        for fidx, fld in enumerate(fetch_fields):
            after = 0  # keyset cursor: last sample_id fetched (0 = from the start)
            while True:
                data = self.client.get_vectors(self._id, field_name=fld, limit=page_size, after_id=after)
                items = data.get("items", [])
                if not items:
                    break

                if all_vectors_np is None:
                    dim = len(items[0]["vector"])
                    # Lower-bound preallocation, grown below if under-estimated. For a patch field the
                    # true total is this tile's count × tile count (every tile is the same full grid).
                    cap = max(int(data.get("total", 0)) * len(fetch_fields), len(items))
                    all_vectors_np = np.empty((cap, dim), dtype=np.float32)
                    print(f"  ~{cap:,} embeddings — {dim}-d, float32 ≈ {cap * dim * 4 / 1e9:.1f} GB")

                for item in items:
                    if filled >= all_vectors_np.shape[0]:
                        # under-estimated — extend the buffer by one page.
                        all_vectors_np = np.concatenate(
                            [all_vectors_np, np.empty((page_size, dim), dtype=np.float32)],
                            axis=0,
                        )
                    all_vectors_np[filled] = item["vector"]
                    all_ids.append(item["sample_id"])
                    if is_patch:
                        all_field_idx.append(fidx)
                    filled += 1

                after = items[-1]["sample_id"]  # items are ORDER BY sample_id → last is the max
                if len(items) < page_size:
                    break

        total = filled
        if total < 5:
            raise ValueError(f"Not enough embeddings for field '{field_name}' (found {total}, need at least 5)")
        if all_vectors_np is not None and all_vectors_np.shape[0] != total:
            all_vectors_np = all_vectors_np[:total]  # trim any over-allocation
        print(f"Fetched {total:,} embeddings")

        # Resolve fit size. "auto" scales the UMAP fit to available RAM, so a
        # big-memory host fits on (and best-represents) more points; all points
        # are projected either way.
        if fit_sample_size in (None, "auto"):
            fit_sample_size = _auto_fit_sample_size(total, all_vectors_np.shape[1])

        # --- Step 2: Reduce to 2D (method-specific) ---

        if method == "tsne":
            # t-SNE has no transform() for new points, so it must embed everything
            # at once — slow/memory-heavy for very large N (no subset fitting).
            from sklearn.manifold import TSNE

            if fit_sample_size != -1 and total > fit_sample_size:
                print(
                    f"  Note: t-SNE embeds all {total:,} points at once (it can't "
                    f"project new points); this is slow/memory-heavy for large datasets.",
                )
            print(f"Running t-SNE on {total:,} vectors...")
            perplexity = min(30, max(1, total - 1))  # must be < n_samples
            all_coords_2d = TSNE(n_components=2, metric='cosine', init='pca', perplexity=perplexity).fit_transform(
                all_vectors_np,
            )
        else:
            reducer, all_coords_2d = fit_reducer(all_vectors_np, fit_sample_size, method)

        # Free the full vectors array
        del all_vectors_np

        # --- Step 3: Upload coordinates via API ---
        print(f"Uploading {total:,} 2D coordinates...")
        upload_batch = 5000
        pbar = tqdm(total=total, desc="Uploading") if tqdm else None

        if is_patch:
            # Coordinates are keyed by (field, sample_id), so group the jointly-reduced points by
            # their tile field and upload each tile's points under that tile's own field name.
            from collections import defaultdict

            idx_by_field: Dict[int, list] = defaultdict(list)
            for j in range(total):
                idx_by_field[all_field_idx[j]].append(j)
            for fidx, idxs in idx_by_field.items():
                fld = fetch_fields[fidx]
                for i in range(0, len(idxs), upload_batch):
                    js = idxs[i : i + upload_batch]
                    batch = [
                        {"sample_id": int(all_ids[j]), "x": float(all_coords_2d[j][0]), "y": float(all_coords_2d[j][1])}
                        for j in js
                    ]
                    self.client.upload_coordinates(self._id, fld, batch, batch_size=len(batch))
                    if pbar:
                        pbar.update(len(js))
        else:
            for i in range(0, total, upload_batch):
                batch = [
                    {"sample_id": int(all_ids[j]), "x": float(all_coords_2d[j][0]), "y": float(all_coords_2d[j][1])}
                    for j in range(i, min(i + upload_batch, total))
                ]
                self.client.upload_coordinates(self._id, field_name, batch, batch_size=len(batch))
                if pbar:
                    pbar.update(len(batch))

        if pbar:
            pbar.close()

        # Publish model files only after every coordinate batch was accepted. A refusal must
        # leave the last usable model intact, never install a model for an unpublished layout.
        if save_model_path and method == "umap":
            import json
            from datetime import datetime, timezone

            import joblib

            os.makedirs(os.path.dirname(save_model_path) or ".", exist_ok=True)
            tmp_path = save_model_path + ".tmp"
            joblib.dump(reducer, tmp_path)
            os.replace(tmp_path, save_model_path)  # atomic: serve never reads a half-written model
            with open(save_model_path + ".meta.json", "w") as f:
                json.dump(
                    {
                        "field_name": field_name,
                        "method": method,
                        "n_total": int(total),
                        "fit_size": int(fit_sample_size) if fit_sample_size != -1 else int(total),
                        "dim": int(dim),
                        "n_neighbors": int(min(15, total - 1)),
                        "min_dist": 0.1,
                        "metric": "cosine",
                        "fitted_at": datetime.now(timezone.utc).isoformat(),
                    },
                    f,
                    indent=2,
                )
            print(f"Saved fitted UMAP model → {save_model_path}")

        # Precompute the even-coverage scatter sample-priority (viz_cell_rank) so the visualization
        # query is an index range scan instead of a per-request grid window-sort. full=True re-ranks
        # all points for this fresh projection. A patch-scale reduce spans many tile fields with no
        # single field to rank (the patch scatter uses the grid sampler), so skip it for patches.
        ranked = True
        if not is_patch:
            try:
                print("Computing scatter sample-priority (viz_cell_rank)...")
                self.client.recompute_viz_rank(self._id, field_name, full=True)
            except Exception as e:
                ranked = False
                print(
                    f"  Warning: ranking the map failed ({e}); the coordinates are saved, but the Embeddings "
                    f"panel does not list '{field_name}' until ranking succeeds. Retry without re-fitting: "
                    f"client.recompute_viz_rank({self._id}, {field_name!r}, full=True)",
                )

        print(f"Done! {total:,} embeddings reduced.")
        return {
            "total_embeddings": total,
            "reduced": total,
            "fit_sample_size": total if method == "tsne" or fit_sample_size == -1 else min(total, fit_sample_size),
            "ranked": ranked,
        }

    def embedding_fields(self, include_patch=False):
        """List this dataset's embedding field names.

        Whole-image fields only by default; patch-scale virtual fields are excluded
        unless ``include_patch=True``.
        """
        fields = self.client.get_embedding_fields(self._id)
        if include_patch:
            return fields
        return [f for f in fields if "_patch_" not in f]

    def compute_all(self, field_name="clip", model_name="clip-ViT-B-32", image_loader=None):
        """One call from bare filepaths to a fully-explorable dataset.

        Chains media metadata → embeddings → UMAP coordinates → perceptual hashes → quality
        metrics, skipping any step ``coverage()`` says is already complete (so
        it's cheap to re-run after adding samples). Each step is also available
        individually (``compute_embeddings`` / ``compute_visualization`` /
        ``compute_hashes`` / ``compute_metrics``).

        Needs the compute extras: ``pip install "freedomridge[all]"``.
        """
        cov = self.coverage()
        total = cov.get("total_samples", 0)
        if not total:
            print("Dataset has no samples — nothing to compute.")
            return cov

        print("[1/5] media metadata (width/height/aspect — incremental)")
        self.compute_metadata(image_loader=image_loader)

        emb = next((e for e in cov.get("embeddings", []) if e.get("field") == field_name), None)
        did_embed = emb is None or emb.get("missing", 0) > 0
        print(
            f"[2/5] embeddings '{field_name}': "
            + (
                f"{emb['present']:,}/{total:,} present — recomputing (upsert)"
                if emb and did_embed
                else ("none yet — computing" if did_embed else "complete, skipping")
            ),
        )
        if did_embed:
            self.compute_embeddings(model_name=model_name, field_name=field_name, image_loader=image_loader)

        # Probe for ACTUAL 2-D coordinates. (get_embedding_fields is wrong here: for a
        # dataset with no ranked field at all it falls back to listing coord-less fields,
        # which made this step skip for inline-ingested vectors — a blank scatter.)
        try:
            probe = self.client.query_embeddings(self._id, field=field_name, sample_pct=1)
            has_coords = bool(probe.get("points"))
        except Exception:
            has_coords = False
        print("[3/5] UMAP coordinates: " + ("computing" if did_embed or not has_coords else "present, skipping"))
        if did_embed or not has_coords:
            self.compute_visualization(field_name=field_name)

        hashes_done = bool(cov.get("hashes")) and any(h.get("missing", 0) == 0 for h in cov["hashes"])
        print("[4/5] perceptual hashes: " + ("complete, skipping" if hashes_done else "computing"))
        if not hashes_done:
            self.compute_hashes(image_loader=image_loader)

        print("[5/5] quality metrics (incremental)")
        self.compute_metrics(image_loader=image_loader, skip_existing=True)

        print(f"✅ compute_all done — {total:,} samples ready to explore.")
        return self.coverage()

    def coverage(self):
        """Derived-artifact coverage for this dataset: per-field present/missing counts
        for embeddings and hashes, plus location, vs the sample total. Shows what an
        uploader still needs to fill (and powers an "N of M" signal)."""
        return self.client.get_coverage(self._id)

    def missing(self, kind, field=None, limit=1000, offset=0, after_id=None):
        """Samples still lacking a derived artifact, for resumable backfill.

        kind='embedding'|'hash' (needs field=) or 'location'. Returns
        {items:[{id, filepath}], total}. Page, fill each batch (which drops those rows
        from the result), repeat until empty. Pass after_id=<last id seen> to walk
        forward by id instead, skipping samples that could not be filled.
        """
        return self.client.get_missing(self._id, kind, field=field, limit=limit, offset=offset, after_id=after_id)

    def project_pending(self, field_name, model=None, batch=5000):
        """Project samples that still lack 2D coords for ``field_name`` through a
        fitted ``model`` (e.g. one saved by ``compute_visualization(save_model_path=...)``
        and reloaded with joblib), and upload the coordinates. Returns the count.

        This is the incremental-ingestion path: newly-added samples get a position via
        ``model.transform()`` without refitting, so existing points don't move.

        A map with scheduled re-fits (Jobs → Schedules) has one current model: it is used instead of
        ``model`` (loaded from the server's model folder, ``FRIDGE_UMAP_MODEL_DIR``, on this machine).
        While a re-fit of the map runs nothing is projected (returns 0); new samples are placed with the
        new model after it finishes.
        """
        if _patch_tile_fields(field_name):
            # A patch-scale field's data lives across per-tile fields; exact-matching the virtual
            # name would silently find nothing and report moved=0 (failure disguised as no-op).
            raise ValueError(
                f"'{field_name}' is a patch-scale field (data lives in per-tile fields like "
                f"{field_name}_r0c0); project_pending operates per sample. Project tiles via "
                f"compute_visualization(field_name='{field_name}'), which expands them.",
            )
        import numpy as np

        state = self._map_state(field_name)
        if state.get("active"):
            print(f"  A map re-fit of '{field_name}' is running; new points are placed when it finishes")
            return 0
        model_id = state.get("model_id")
        if model_id:
            if model is not None:
                warnings.warn(
                    f"'{field_name}' has scheduled map re-fits; projecting with its current model {model_id} "
                    f"(re-fitted {state.get('refitted_at')}) instead of the model passed",
                    stacklevel=2,
                )
            model = load_map_model(state)
        elif model is None:
            raise ValueError(f"'{field_name}' has no re-fitted map yet: pass the fitted model to project with")

        moved = 0
        last_first = -1
        last_id = None
        while True:
            data = self.client.get_vectors(
                self._id,
                field_name=field_name,
                limit=batch,
                after_id=last_id,
                pending_only=True,
            )
            items = data.get("items", [])
            if not items:
                break
            first = items[0]["sample_id"]
            if first <= last_first:
                # The previous batch didn't drop out of "pending" — stop, don't loop.
                break
            last_first = first
            X = np.asarray([it["vector"] for it in items], dtype=np.float32)
            expected_dim = getattr(model, "n_features_in_", None)
            if expected_dim is not None and X.shape[1] != expected_dim:
                raise ValueError(
                    f"Vector dim {X.shape[1]} for field '{field_name}' != the model's "
                    f"fitted dim {expected_dim} — pass the model fit on this field.",
                )
            coords = model.transform(X)
            payload = [
                {
                    "sample_id": it["sample_id"],
                    "x": float(c[0]),
                    "y": float(c[1]),
                    **({"vector_version": it["vector_version"]} if "vector_version" in it else {}),
                }
                for it, c in zip(items, coords)
            ]
            try:
                self.client.upload_coordinates(
                    self._id,
                    field_name,
                    payload,
                    **({"model_id": model_id} if model_id else {}),
                )
            except requests.HTTPError as error:
                if error.response is None or error.response.status_code != 409:
                    raise
                # A re-fit started, or a new map was published, meanwhile: the next pass uses it.
                print(f"  Stopped projecting '{field_name}': {error}")
                break
            moved += len(items)
            last_id = items[-1]["sample_id"]

        # Triggers already rank these points in ranked fields. Keep the compatibility
        # refresh also heals a failed initial rank.
        if moved:
            try:
                self.client.recompute_viz_rank(self._id, field_name, full=False)
            except Exception as e:
                print(f"  Warning: incremental viz_cell_rank failed: {e}")
        return moved

    # =========================================================================
    # DATASET QUALITY & DEDUP
    # =========================================================================

    def set_values_per_sample(self, field: str, values_by_id: dict, batch_size: int = 5000, *, wait=True, timeout=1800):
        """Set values in bounded requests; completed batches survive a later failure."""
        return self._set_values_batches(
            ((sid, {field: value}) for sid, value in values_by_id.items()),
            batch_size,
            wait,
            timeout,
        )

    def set_values_per_sample_multi(self, updates_by_id: dict, batch_size: int = 5000, *, wait=True, timeout=1800):
        """Merge patches in requests of at most 5,000 samples, returning the total count.

        With wait=False, return a Job for one batch or a list of Jobs for multiple
        batches. Each batch is independently resumable; there is no cross-batch rollback.
        """
        return self._set_values_batches(iter(updates_by_id.items()), batch_size, wait, timeout)

    def _set_values_batches(self, items, batch_size, wait, timeout):
        # numbers.Integral also admits NumPy integers; bool is not a batch size.
        if not isinstance(batch_size, numbers.Integral) or isinstance(batch_size, bool) or batch_size < 1:
            raise ValueError('batch_size must be a positive integer')
        size = min(int(batch_size), 5000)
        total, jobs = 0, []
        while batch := list(islice(items, size)):
            updates = [{'sample_id': sid, 'metadata': patch} for sid, patch in batch]
            result = self.client.bulk_set_metadata(self._id, updates=updates, wait=wait, timeout=timeout)
            self._dirty = True
            if wait:
                total += result['updated_count']
            else:
                jobs.append(result)
        return total if wait else jobs[0] if len(jobs) == 1 else jobs

    def build_ann_index(self, field_name="clip", *, wait=True, timeout=3700):
        """Build the per-field HNSW index server-side (idempotent, no write lock).

        One-time per embedding field; required by ``compute_uniqueness`` /
        ``find_duplicates`` and what makes similarity search an index scan.
        Those methods call this automatically when the index is missing.
        The build is a server job; ``wait=False`` returns its Job.
        """
        return self.client.build_ann_index(self._id, field_name, wait=wait, timeout=timeout)

    def _drive_ann(self, step, field_name, label=None):
        """``_drive``, but when the ANN endpoint 409s for a missing HNSW index,
        build it server-side (one-time) and retry — instead of dead-ending SDK
        users at an operator-run script they may not be able to execute."""
        import requests

        try:
            return self._drive(step, label=label)
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if status != 409 or "No HNSW index" not in str(e):
                raise
            print(f"  No HNSW index for '{field_name}' yet — building it server-side (one-time; can take minutes)…")
            self.build_ann_index(field_name)
            return self._drive(step, label=label)

    @staticmethod
    def _drive(step, label=None):
        """Drive a keyset-chunked quality endpoint to completion. ``step(after_id)``
        returns {processed, last_id, done}. Returns total processed."""
        total, after_id = 0, 0
        while True:
            res = step(after_id)
            total += res.get("processed", 0)
            after_id = res.get("last_id", after_id)
            if label:
                print(f"  {label}: {total:,} processed", end="\r", flush=True)
            if res.get("done") or res.get("processed", 0) == 0:
                break
        if label:
            print()
        return total

    def _fetch_all_vectors(self, field_name: str, max_samples=None):
        """Fetch up to ``max_samples`` (sample_ids, vectors) into a preallocated
        array (only used by ``find_unique``, which is inherently in-memory). Pass
        ``max_samples=None`` for ALL (O(n^2) memory — large datasets should bound it)."""
        if _patch_tile_fields(field_name):
            # find_unique is per-sample; a patch-scale virtual name exact-matches zero rows and
            # would return an empty coreset as if it were the answer. Fail loudly instead.
            raise ValueError(
                f"'{field_name}' is a patch-scale field (data lives in per-tile fields like "
                f"{field_name}_r0c0); find_unique operates per sample. Use a single tile field, or "
                f"compute_visualization(field_name='{field_name}') to reduce all tiles jointly.",
            )
        import numpy as np

        page_size = 10000
        total = self.client.get_vectors(self._id, field_name=field_name, limit=1, offset=0).get("total", 0)
        if total == 0:
            return [], np.zeros((0, 0), dtype=np.float32)
        target = total if not max_samples else min(total, max_samples)
        if max_samples is None and total > 200000:
            print(f"  find_unique: loading ALL {total:,} vectors — O(n^2) in memory. Pass sample_size= to bound it.")
        ids: list = []
        arr, after = None, 0
        while len(ids) < target:
            items = self.client.get_vectors(self._id, field_name=field_name, limit=page_size, after_id=after).get(
                "items",
                [],
            )
            if not items:
                break
            for it in items:
                if len(ids) >= target:
                    break
                v = it["vector"]
                if arr is None:
                    arr = np.empty((target, len(v)), dtype=np.float32)
                arr[len(ids)] = v
                ids.append(it["sample_id"])
            after = items[-1]["sample_id"]  # keyset cursor
            if len(items) < page_size:
                break
        return ids, (arr[: len(ids)] if arr is not None else np.zeros((0, 0), dtype=np.float32))

    def _normalize_field(self, metadata_field):
        """Min-max normalize a metadata field to [0,1] in place (chunked, server-side)."""
        rng = self.client.quality_range(self._id, metadata_field)
        lo, hi = rng.get("min"), rng.get("max")
        if lo is None or hi is None:
            return
        span = hi - lo
        self._drive(
            lambda aid: self.client.quality_normalize(self._id, metadata_field, lo, span, aid, 50000),
            label="normalize",
        )

    def compute_uniqueness(
        self,
        field_name="clip",
        k=3,
        metadata_field="uniqueness",
        max_samples=None,
        ef_search=64,
        chunk=5000,
    ):
        """Score how unique each sample is vs its nearest neighbors, and store it.

        Higher = more unique/isolated, lower = more redundant. Computed SERVER-SIDE
        via the per-field HNSW index (mean cosine distance to the k nearest),
        min-max normalized to [0, 1], written to ``metadata[metadata_field]``. The
        work is chunked (bounded memory) and resumable; requires the field's HNSW
        index (build with scripts/build_ann_indexes.py).
        """
        if max_samples is not None:
            print("  Note: max_samples is ignored — uniqueness now scores ALL samples server-side.")
        c = self.client
        n = self._drive_ann(
            lambda aid: c.quality_uniqueness_compute(self._id, field_name, k, ef_search, aid, chunk, metadata_field),
            field_name,
            label="uniqueness",
        )
        self._normalize_field(metadata_field)
        self.refresh_stats()
        print(f"Uniqueness computed for {n} samples → metadata['{metadata_field}']")
        return {"count": n, "field": metadata_field}

    def compute_representativeness(
        self,
        field_name="clip",
        metadata_field="representativeness",
        max_samples=None,
        chunk=20000,
    ):
        """Score how representative each sample is (cosine similarity to the dataset centroid).

        Higher = closer to the 'center' of the data (typical), lower = atypical.
        Computed SERVER-SIDE (centroid = mean of unit vectors), min-max normalized
        to [0, 1], written to ``metadata[metadata_field]``. Chunked + bounded memory.
        """
        if max_samples is not None:
            print("  Note: max_samples is ignored — representativeness now scores ALL samples server-side.")
        c = self.client
        centroid = c.quality_representativeness_centroid(self._id, field_name)["centroid"]
        n = self._drive(
            lambda aid: c.quality_representativeness_compute(
                self._id,
                field_name,
                centroid,
                aid,
                chunk,
                metadata_field,
            ),
            label="representativeness",
        )
        self._normalize_field(metadata_field)
        self.refresh_stats()
        print(f"Representativeness computed for {n} samples → metadata['{metadata_field}']")
        return {"count": n, "field": metadata_field}

    def compute_hardness(self, logits_field, metadata_field="hardness"):
        """Score prediction difficulty from a per-sample logits/probabilities field.

        Reads ``metadata[logits_field]`` (a list of class logits or probabilities)
        and writes a hardness score in [0, 1] = normalized softmax entropy
        (1 = maximally uncertain). Samples missing the field are skipped. Requires
        you to have stored model outputs per sample — Freedom Ridge does not run
        inference itself.
        """
        import math

        import numpy as np

        values = {}
        for sample in self.iter_samples(progress=True):
            logits = sample.metadata.get(logits_field)
            if not isinstance(logits, (list, tuple)) or len(logits) < 2:
                continue
            a = np.asarray(logits, dtype=np.float64)
            # If already a probability distribution, use it directly; otherwise
            # treat as logits and softmax. (Avoids double-softmaxing probs.)
            if a.min() >= 0 and abs(float(a.sum()) - 1.0) < 1e-3:
                p = np.clip(a, 1e-12, 1.0)
            else:
                e = np.exp(a - a.max())
                p = np.clip(e / e.sum(), 1e-12, 1.0)
            ent = float(-np.sum(p * np.log(p)))
            values[sample.id] = ent / math.log(len(p))

        if values:
            self.set_values_per_sample(metadata_field, values)
            self.refresh_stats()
        print(f"Hardness computed for {len(values)} samples → metadata['{metadata_field}']")
        return {"count": len(values), "field": metadata_field}

    def find_unique(self, count, field_name="clip", sample_size=200000, max_samples=None):
        """Select ``count`` maximally-distinct samples (a coreset) via farthest-point sampling.

        Returns the chosen sample IDs — a diverse subset, useful for choosing what
        to label first. APPROXIMATE-BY-SAMPLE: farthest-point sampling can't use the
        ANN index, so it runs client-side over up to ``sample_size`` vectors (default
        200k, mirroring compute_visualization's fit). Pass ``sample_size=-1`` for ALL
        (O(n^2) time/memory — slow on large datasets). Deterministic.
        """
        import numpy as np

        if max_samples is not None:  # back-compat alias
            sample_size = max_samples
        limit = None if sample_size in (-1, 0) else sample_size
        ids, X = self._fetch_all_vectors(field_name, limit)
        n = len(ids)
        if count >= n:
            if count > n:
                print(f"  find_unique: requested {count} but only {n} samples available; returning all.")
            return list(ids)

        Xn = X / (np.linalg.norm(X, axis=1, keepdims=True) + 1e-12)
        chosen = [0]
        min_d = 1.0 - Xn @ Xn[0]  # cosine distance to the first pick
        min_d[0] = -1.0
        for _ in range(count - 1):
            nxt = int(np.argmax(min_d))
            chosen.append(nxt)
            min_d = np.minimum(min_d, 1.0 - Xn @ Xn[nxt])
            min_d[nxt] = -1.0
        return [ids[i] for i in chosen]

    def find_duplicates(
        self,
        field_name="clip",
        thresh=0.04,
        max_samples=None,
        k=20,
        ef_search=64,
        chunk=5000,
        group_field="emb_dup_group",
    ):
        """Find near-duplicate samples by embedding cosine distance.

        Groups samples whose cosine distance is <= ``thresh`` (connected components).
        Computed SERVER-SIDE via the per-field HNSW index (top-k radius pairs +
        union-find), writing the group id to ``metadata[group_field]``. Returns a
        list of groups, each a sorted list of sample IDs (size >= 2). Chunked +
        bounded memory; requires the field's HNSW index. Use ``duplicates_view``
        to browse. (``k`` bounds neighbours per sample; raise for very dense dups.)
        """
        import secrets

        if max_samples is not None:
            print("  Note: max_samples is ignored — duplicates are computed over ALL samples server-side.")
        c = self.client
        token = secrets.token_hex(16)
        self._drive_ann(
            lambda aid: c.quality_duplicates_compute(self._id, field_name, thresh, k, ef_search, aid, chunk, token),
            field_name,
            label="scanning",
        )
        fin = c.quality_duplicates_finalize(self._id, token, group_field)
        groups: list = []
        offset, limit = 0, 200
        while True:
            page = c.quality_duplicates_groups(self._id, group_field, limit, offset)
            batch = page.get("groups", [])
            groups.extend(sorted(g["sample_ids"]) for g in batch)
            if len(batch) < limit:
                break
            offset += limit
        print(
            f"Found {fin.get('total_groups', len(groups))} duplicate groups "
            f"({fin.get('total_duplicate_samples', 0)} samples) → metadata['{group_field}']",
        )
        return groups

    def compute_content_hashes(self, field_name="md5", image_loader=None, batch_size=100):
        """Compute an exact content hash (MD5 of decoded RGB pixels) per sample.

        Stored as a hash field so ``find_exact_duplicates()`` (and the hash
        duplicate tools) can find pixel-identical images. Being pixel-based, it
        ignores file-format / re-compression differences.
        """
        import hashlib

        return self._compute_hashes(
            image_loader,
            {field_name: lambda img: hashlib.md5(img.tobytes()).hexdigest()},
            batch_size,
            False,
        )

    def find_duplicate_hashes(self, field_name, limit=50, offset=0, filters=None):
        """Find groups of exact-duplicate images by a stored hash field."""
        return self.client.find_hash_duplicates(self._id, field_name, limit=limit, offset=offset, filters=filters)

    def find_exact_duplicates(self, field_name="md5", limit=50, offset=0):
        """Find pixel-identical duplicate groups (run ``compute_content_hashes`` first)."""
        return self.client.find_hash_duplicates(self._id, field_name, limit=limit, offset=offset)

    def sample_duplicates(self, sample_id, field_name="_any"):
        """Exact duplicates of one sample (samples sharing an identical hash).

        ``field_name``: a specific hash field, ``"_any"`` (match on >=1 of the
        sample's hash fields), or ``"_all"`` (match on every hash field it has).
        Returns ``{"sample_ids": [...], "field_name": ...}``; the sample itself
        is included.
        """
        return self.client.sample_duplicates(self._id, sample_id, field_name=field_name)

    def duplicates_view(self, groups, keep_first=True):
        """Build a view over the duplicate samples from ``find_duplicates`` groups.

        Args:
            groups: list of sample-ID groups (from ``find_duplicates``).
            keep_first: if True, exclude the first member of each group (the
                'representative'), so the view holds only the redundant copies.

        Returns a DatasetView you can iterate, tag, or delete from.
        """
        ids = []
        for g in groups:
            ids.extend(g[1:] if keep_first else g)
        view = DatasetView(self)
        view._filters = {"field": "id", "operator": "in", "value": ids}
        return view

    # =========================================================================
    # EMBEDDING CONCEPTS (declutter: exclude / rank by "images like this")
    # =========================================================================

    def create_concept(
        self,
        name,
        seed_ids=None,
        field="clip",
        kind="seed",
        neg_ids=None,
        prompt=None,
        weight=None,
        bias=0.0,
        threshold=None,
        *,
        wait=True,
        timeout=1800,
    ):
        """Create an embedding concept — a linear direction w → a calibrated [0,1] score per
        sample. ``kind='seed'`` averages ``seed_ids``; ``kind='probe'`` fits a logistic boundary
        on ``seed_ids`` (positives) vs ``neg_ids`` or a random background; ``prompt=`` makes a
        text concept from a natural-language description (kind is set automatically). Advanced:
        pass a raw ``weight``. Apply it with the ``concept_exclude`` filter operator.
        ``threshold=None`` lets the server derive the default from the score range.
        Scoring is a server job; ``wait=False`` returns its Job."""
        if prompt is not None and kind == "seed" and seed_ids is None:
            kind = "text"
        return self.client.create_concept(
            self._id,
            name,
            seed_ids=seed_ids,
            embedding_field=field,
            kind=kind,
            neg_ids=neg_ids,
            prompt=prompt,
            weight=weight,
            bias=bias,
            threshold=threshold,
            wait=wait,
            timeout=timeout,
        )

    def concepts(self):
        """List the concepts saved on this dataset."""
        return self.client.list_concepts(self._id)

    def concept_preview(self, concept_id, op=">", threshold=0.5):
        """How many samples pass ``score <op> threshold`` for a concept (slider tuning)."""
        return self.client.concept_preview(self._id, concept_id, op=op, threshold=threshold)

    def rescore_concept(self, concept_id, full=True, *, wait=True, timeout=1800):
        """Rescore a concept (full rebuild, or incremental for new/replaced/removed vectors)."""
        return self.client.rescore_concept(self._id, concept_id, full=full, wait=wait, timeout=timeout)

    def refine_concept(
        self,
        concept_id,
        keep_ids=None,
        exclude_ids=None,
        threshold=None,
        self_train=False,
        *,
        wait=True,
        timeout=1800,
    ):
        """Active-learning refine from boundary corrections: ``keep_ids`` become negatives
        ("don't exclude"), ``exclude_ids`` become positives ("exclude / more like this"). The
        concept becomes a weighted probe and is rescored. Pass the current ``threshold`` to
        preserve the cut level; ``self_train=True`` also weakly-labels the current split."""
        return self.client.refine_concept(
            self._id,
            concept_id,
            keep_ids=keep_ids,
            exclude_ids=exclude_ids,
            threshold=threshold,
            self_train=self_train,
            wait=wait,
            timeout=timeout,
        )

    def rebuild_concept(self, concept_id, seed_ids=None, weight=None, threshold=None, *, wait=True, timeout=1800):
        """Overwrite-in-place: regenerate w from new seeds/weight (full rescore), or retune
        the saved threshold only."""
        return self.client.rebuild_concept(
            self._id,
            concept_id,
            seed_ids=seed_ids,
            weight=weight,
            threshold=threshold,
            wait=wait,
            timeout=timeout,
        )

    def delete_concept(self, concept_id):
        """Delete a concept (its materialized scores cascade-delete)."""
        return self.client.delete_concept(self._id, concept_id)

    # =========================================================================
    # IMPORT / EXPORT (standard CV formats)
    # =========================================================================

    def import_coco(self, images_dir, annotations_json, label_field="ground_truth", on_duplicate="skip"):
        """Import COCO-format detections into this dataset (typed Detections)."""
        from .io import import_coco

        return import_coco(self, images_dir, annotations_json, label_field=label_field, on_duplicate=on_duplicate)

    def import_yolo(self, images_dir, labels_dir, classes, label_field="ground_truth", on_duplicate="skip"):
        """Import YOLO-format detections (``classes`` is the ordered class-name list)."""
        from .io import import_yolo

        return import_yolo(self, images_dir, labels_dir, classes, label_field=label_field, on_duplicate=on_duplicate)

    def import_voc(self, images_dir, annotations_dir, label_field="ground_truth", on_duplicate="skip"):
        """Import Pascal VOC detections (per-image XML)."""
        from .io import import_voc

        return import_voc(self, images_dir, annotations_dir, label_field=label_field, on_duplicate=on_duplicate)

    def import_classification_dir(self, root, label_field="ground_truth", on_duplicate="skip"):
        """Import an ImageNet-style ``root/<class>/*.jpg`` tree as Classifications."""
        from .io import import_classification_dir

        return import_classification_dir(self, root, label_field=label_field, on_duplicate=on_duplicate)

    def export(self, export_dir, format="coco", label_field="ground_truth"):
        """Export this dataset's detections to ``format`` ('coco' or 'yolo').

        Works on a filtered view too — call ``view.export(...)`` to export a subset.
        """
        from .io import export

        return export(self, export_dir, format=format, label_field=label_field)

    # =========================================================================
    # EVALUATION
    # =========================================================================

    def evaluate_classifications(self, pred_field, gt_field, eval_key="eval", classes=None):
        """Evaluate single-label classifications (pred vs ground-truth fields).

        Stores ``metadata[eval_key]`` = "correct"/"incorrect" per sample; returns
        a report with accuracy and a confusion matrix (rows=GT, cols=pred).
        """
        from .evaluation import evaluate_classifications

        return evaluate_classifications(self, pred_field, gt_field, eval_key=eval_key, classes=classes)

    def evaluate_detections(self, pred_field, gt_field, eval_key="eval", iou=0.5, classes=None):
        """Evaluate object detections at a given IoU threshold (greedy matching).

        Stores per-sample ``metadata[eval_key + "_tp"/"_fp"/"_fn"]`` counts;
        returns precision/recall, per-class AP, and mAP.
        """
        from .evaluation import evaluate_detections

        return evaluate_detections(self, pred_field, gt_field, eval_key=eval_key, iou=iou, classes=classes)

    def evaluate_segmentations(self, pred_field, gt_field, eval_key="eval", image_loader=None):
        """Evaluate semantic-segmentation index masks (pixel value = class id).

        Stores per-sample pixel accuracy + mIoU; returns dataset-wide means.
        """
        from .evaluation import evaluate_segmentations

        return evaluate_segmentations(self, pred_field, gt_field, eval_key=eval_key, image_loader=image_loader)

    # =========================================================================
    # PATCHES & LABEL ANALYSIS
    # =========================================================================

    def to_patches(self, label_field, dest_name, patches_dir, image_loader=None, on_duplicate="skip"):
        """Explode each detection in ``label_field`` into its own cropped-image sample.

        Crops every detection's bbox, writes crop PNGs under ``patches_dir``, and
        creates (or loads) a new dataset ``dest_name`` whose samples are the crops,
        each carrying ``{parent_id, parent_filepath, label, confidence, bbox}`` in
        metadata (and the class as a tag). Returns the new Dataset — browsable in
        the grid like any other.

        For the crops to be servable, put ``patches_dir`` under FRIDGE_DATA_ROOT
        (local serving) or upload them and rewrite filepaths afterward.
        """
        import os

        from .evaluation import _as_list

        if image_loader is None:
            image_loader = _default_s3_image_loader()
        os.makedirs(patches_dir, exist_ok=True)

        patch_samples = []
        made = 0
        for sample in self.iter_samples(progress=True):
            dets = _as_list(sample.metadata.get(label_field))
            if not dets:
                continue
            try:
                img = image_loader(sample.filepath)
            except Exception:
                continue
            W, H = img.size
            for i, det in enumerate(dets):
                bbox = det.get("bbox")
                if not bbox or len(bbox) != 4:
                    continue
                x, y, w, h = bbox
                # Match the frontend/labels convention: values <= 2 are normalized.
                if max(x, y, w, h) <= 2.0:  # normalized → pixels
                    x, y, w, h = x * W, y * H, w * W, h * H
                crop = img.crop((int(x), int(y), int(x + w), int(y + h)))
                if crop.width < 1 or crop.height < 1:
                    continue
                path = os.path.join(patches_dir, f"{sample.id}_{i}.png")
                crop.save(path)
                label = det.get("label")
                patch_samples.append(
                    Sample(
                        filepath=path,
                        metadata={
                            "parent_id": sample.id,
                            "parent_filepath": sample.filepath,
                            "label": label,
                            "confidence": det.get("confidence"),
                            "bbox": list(bbox),
                        },
                        tags=[label] if label else [],
                        location=sample.location,  # inherit the parent's geolocation
                    ),
                )
                made += 1

        # A new crop dataset is as visible as its source; an existing one keeps its own setting.
        dest = Dataset(dest_name, is_global=bool((self._info or {}).get("is_global")))
        if patch_samples:
            dest.add_samples(patch_samples, on_duplicate=on_duplicate)
        print(f"to_patches: cropped {made} detections into dataset '{dest_name}'")
        return dest

    def compute_mistakenness(self, pred_field, gt_field, metadata_field="mistakenness"):
        """Estimate likely label errors by comparing confident predictions to ground truth.

        For each sample, if the top prediction's label disagrees with the
        ground-truth label, mistakenness = the prediction's confidence (a
        confident disagreement suggests the ground truth may be wrong); agreement
        scores 0. Stored per-sample in ``metadata[metadata_field]`` — sort
        descending to surface the most likely annotation errors to review.
        """
        from .evaluation import _as_list, _top_label

        values = {}
        for sample in self.iter_samples(progress=True):
            preds = _as_list(sample.metadata.get(pred_field))
            gt = _top_label(sample.metadata.get(gt_field))
            if not preds or gt is None:
                continue
            best = max(preds, key=lambda d: d.get("confidence", 0.0))
            conf = float(best.get("confidence", 0.0))
            values[sample.id] = conf if best.get("label") != gt else 0.0

        if values:
            self.set_values_per_sample(metadata_field, values)
            self.refresh_stats()
        print(f"Mistakenness computed for {len(values)} samples → metadata['{metadata_field}']")
        return {"count": len(values), "field": metadata_field}

    def compute_leaky_splits(self, split_field, field_name="clip", thresh=0.04, max_samples=None):
        """Find likely train/test leaks: near-duplicate samples in DIFFERENT splits.

        Uses embedding near-duplicate groups (``find_duplicates``) and flags any
        group whose members span more than one distinct ``split_field`` value.
        Returns ``{"groups": [...], "leaked_ids": [...]}`` where each group is
        ``{"sample_ids": [...], "splits": {id: split_value}}``.
        """
        groups = self.find_duplicates(field_name=field_name, thresh=thresh, max_samples=max_samples)
        ids = [sid for g in groups for sid in g]
        if not ids:
            return {"groups": [], "leaked_ids": []}

        # One query to fetch the split value for every grouped sample.
        resp = self.client.query_samples(
            self._id,
            limit=len(ids),
            filters={"field": "id", "operator": "in", "value": ids},
        )
        split_by_id = {item["id"]: (item.get("metadata") or {}).get(split_field) for item in resp.get("items", [])}

        leaky_groups, leaked_ids = [], []
        for g in groups:
            splits = {sid: split_by_id.get(sid) for sid in g}
            if len({v for v in splits.values() if v is not None}) > 1:
                leaky_groups.append({"sample_ids": g, "splits": splits})
                leaked_ids.extend(g)

        print(f"Leaky splits: {len(leaky_groups)} cross-split duplicate groups ({len(leaked_ids)} samples)")
        return {"groups": leaky_groups, "leaked_ids": leaked_ids}
