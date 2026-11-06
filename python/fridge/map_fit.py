"""The compute step of a scheduled map re-fit: fit UMAP on an exported vector file.

The job runner starts this inside its own memory-capped scope
(``systemd-run --user --scope -p MemoryMax=...``) so an out-of-memory kill ends only this process:

    python -m fridge.map_fit --input work/<job>/vectors.bin --output work/<job>/fit

It needs no database or API access, so the same step can later run on a separate compute worker.
The input is PostgreSQL's binary COPY of ``(sample_id int4, vector_version int8, vector)``. The fit
size comes from the same RAM- and dimension-scaled formula as ``Dataset.compute_visualization``
(``_auto_fit_sample_size``), measured inside the cap; every point is then projected through the
fitted model. The model itself is not kept: new samples are placed on a re-fitted map by their nearest
mapped neighbours. Outputs, each written completely before ``manifest.json`` (written last):

    ids.npy         int32 sample IDs, in input order
    versions.npy    int64 vector versions (the embedding row's xmin at export)
    coords.npy      float32 (N, 2) map coordinates
    manifest.json   counts, fit size and timings
"""

import argparse
import json
import os
import resource
import sys
import time
import warnings
from pathlib import Path

import numpy as np

_SIGNATURE = b"PGCOPY\n\xff\r\n\x00"


def read_export(path):
    """(ids, versions, vectors) from a binary COPY file with one fixed-size row per vector."""
    rows = _export_rows(path)
    if rows is None:
        return np.empty(0, np.int32), np.empty(0, np.int64), np.empty((0, 0), np.float32)
    return rows["id"].astype(np.int32), rows["version"].astype(np.int64), rows["vector"].astype(np.float32)


def read_export_ids(path):
    """(ids, versions) of a binary COPY export, without loading its vectors."""
    rows = _export_rows(path)
    if rows is None:
        return np.empty(0, np.int32), np.empty(0, np.int64)
    return rows["id"].astype(np.int32), rows["version"].astype(np.int64)


def _export_rows(path):
    """The export's rows, memory-mapped (None when it has none), after checking its layout."""
    size = os.path.getsize(path)
    head = np.fromfile(path, dtype=np.uint8, count=min(size, 64))
    if size < 21 or head[:11].tobytes() != _SIGNATURE:
        raise ValueError(f"{path} is not a PostgreSQL binary COPY file")
    extension = int.from_bytes(head[15:19].tobytes(), "big")
    offset = 19 + extension
    trailer = np.fromfile(path, dtype=">i2", count=1, offset=size - 2)
    if trailer.size != 1 or trailer[0] != -1:
        raise ValueError(f"{path} is truncated (no end-of-data marker)")
    if size - offset == 2:
        return None
    first = np.fromfile(path, dtype=">i4", count=1, offset=offset + 2 + 4 + 4 + 4 + 8)
    dim = (int(first[0]) - 4) // 4
    row = np.dtype(
        [
            ("fields", ">i2"),
            ("id_length", ">i4"),
            ("id", ">i4"),
            ("version_length", ">i4"),
            ("version", ">i8"),
            ("vector_length", ">i4"),
            ("dim", ">i2"),
            ("unused", ">i2"),
            ("vector", ">f4", (dim,)),
        ],
    )
    body = size - offset - 2
    if dim <= 0 or body % row.itemsize:
        raise ValueError(f"{path}: rows are not all {dim}-dimensional vectors")
    rows = np.memmap(path, dtype=row, mode="r", offset=offset, shape=(body // row.itemsize,))
    if not (
        (rows["fields"] == 3).all()
        and (rows["id_length"] == 4).all()
        and (rows["version_length"] == 8).all()
        and (rows["vector_length"] == 4 + 4 * dim).all()
        and (rows["dim"] == dim).all()
    ):
        raise ValueError(f"{path}: unexpected row layout or mixed vector dimensions")
    return rows


def _cgroup_anon_bytes():
    """Anonymous memory of this process's cgroup (its scope), or None outside cgroup v2."""
    try:
        with open("/proc/self/cgroup") as cgroup:
            path = next(line.split(":", 2)[2].strip() for line in cgroup if line.startswith("0::"))
        with open(os.path.join("/sys/fs/cgroup", path.lstrip("/"), "memory.stat")) as stat:
            return next(int(line.split()[1]) for line in stat if line.startswith("anon "))
    except (OSError, StopIteration, ValueError):
        return None


def _save(path: Path, array) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "wb") as target:
        np.save(target, array)
        target.flush()
        os.fsync(target.fileno())
    os.replace(temporary, path)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Fit a map (UMAP) on an exported vector file")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--bytes-per-point", type=int, default=None, help="measured memory per fit point")
    parser.add_argument("--max-fit-size", type=int, default=None, help="largest fit (growth beyond a measured fit)")
    args = parser.parse_args(argv)
    try:  # if the host itself runs short, the kernel kills this fit before the database
        with open("/proc/self/oom_score_adj", "w") as adjust:
            adjust.write("1000")
    except OSError:
        pass
    # umap-learn imports TensorFlow for its optional parametric model when it is installed: ~1 GB of
    # RSS and seconds of start-up inside the memory cap, for nothing a re-fit uses.
    sys.modules.setdefault("tensorflow", None)  # type: ignore[arg-type]
    from .core.dataset import (
        MINIMUM_FIT_POINTS,
        _auto_fit_sample_size,
        _available_ram_bytes,
        default_fit_bytes_per_point,
        fit_reducer,
    )

    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    for stale in ("manifest.json", "plan.json"):  # an earlier attempt's outputs never count
        (output / stale).unlink(missing_ok=True)
    started = time.monotonic()
    ids, versions, vectors = read_export(args.input)
    total, dimension = vectors.shape
    if total < 5:
        raise SystemExit(f"Not enough vectors to fit a map (found {total}, need at least 5)")
    print(f"Read {total:,} vectors of {dimension} dimensions in {time.monotonic() - started:.0f} s", flush=True)
    anon_before_fit = _cgroup_anon_bytes()
    bytes_per_point = args.bytes_per_point or default_fit_bytes_per_point(dimension)
    available = _available_ram_bytes()
    fit_size = _auto_fit_sample_size(total, dimension, bytes_per_point)
    if args.max_fit_size:
        # The bound (growth beyond the last measured fit, or 75% of a killed attempt) never goes below
        # _auto_fit_sample_size's usable minimum.
        bound = max(args.max_fit_size, min(total, MINIMUM_FIT_POINTS))
        if fit_size > bound:
            print(f"  Fit size limited to {bound:,} (growth step, or 75% of a killed attempt)", flush=True)
            fit_size = bound
    # What this attempt tried, for the runner to size a retry if the kernel kills it at the cap.
    plan = {
        "rows": total,
        "fit_size": min(fit_size, total),
        "bytes_per_point": bytes_per_point,
        "available_bytes": available,
        "anon_before_fit": anon_before_fit,
    }
    with open(output / "plan.json", "w") as target:
        json.dump(plan, target)
    fitting = time.monotonic()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        timings: dict = {}
        reducer, coords = fit_reducer(vectors, fit_size, timings=timings)
    fitted = time.monotonic()
    coords = np.asarray(coords, dtype=np.float32)
    if coords.shape != (total, 2) or not np.isfinite(coords).all():
        raise SystemExit("The fit produced missing or non-finite coordinates")
    del vectors, reducer
    _save(output / "ids.npy", ids)
    _save(output / "versions.npy", versions)
    _save(output / "coords.npy", coords)
    manifest = {
        "rows": total,
        "dimension": int(dimension),
        "fit_size": int(min(fit_size, total)) if fit_size != -1 else total,
        "read_seconds": round(fitting - started, 1),
        "fit_seconds": round(fitted - fitting, 1),  # fit plus projection of the other rows
        "umap_fit_seconds": timings.get("fit_seconds"),
        "transform_seconds": timings.get("transform_seconds"),
        "total_seconds": round(time.monotonic() - started, 1),
        "max_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
        "bytes_per_point": bytes_per_point,
        "anon_before_fit": anon_before_fit,
    }
    with open(output / "manifest.json.tmp", "w") as target:
        json.dump(manifest, target)
    os.replace(output / "manifest.json.tmp", output / "manifest.json")
    print(f"Map fitted: {json.dumps(manifest)}", flush=True)


if __name__ == "__main__":
    main()
