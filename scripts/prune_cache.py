#!/usr/bin/env python3
"""LRU-prune Freedom Ridge's on-disk caches to a byte budget.

The backend's S3 image mirror (S3_LOCAL_CACHE_PATH) and heatmap-composite cache
(FRIDGE_COMPOSITE_CACHE_PATH) are derived data that grow without bound. On a
single-EC2 deploy the cache shares a disk with Postgres, so an unbounded cache
eventually ENOSPCs the box and can take the database down with it. The worker
runs this hourly to keep total cache size under FRIDGE_CACHE_MAX_BYTES, evicting
the least-recently-ACCESSED files first.

Safe to run concurrently with the backend: deleting a complete cache file just
forces a cache miss + refetch; an in-flight read keeps the unlinked inode open
until it closes; half-written files use a unique `.tmp` name and are skipped.

Config (env):
  FRIDGE_CACHE_PRUNE_DIRS  colon-separated dirs   (default /cache/s3:/cache/composite:/cache/crop)
  FRIDGE_CACHE_MAX_BYTES   total budget in bytes  (default 10 GiB)
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.cache_pruning import prune as prune  # noqa: E402


def main() -> None:
    dirs = [d for d in os.getenv("FRIDGE_CACHE_PRUNE_DIRS", "/cache/s3:/cache/composite:/cache/crop").split(":") if d]
    budget = int(os.getenv("FRIDGE_CACHE_MAX_BYTES", str(10 * 1024**3)))
    prune(dirs, budget)


if __name__ == "__main__":
    main()
