"""
Migrate the legacy flat media cache to dataset-scoped paths.

Background
----------
Before the per-dataset storage config work, the S3 proxy cached files at
  S3_LOCAL_PATH/{key}
without any bucket or dataset scoping. That was fine when only one bucket
existed globally. With per-dataset storage configs, the cache is now scoped:
  S3_LOCAL_PATH/{dataset_id}/{key}
which prevents cross-bucket collisions when different datasets point at
different buckets with overlapping key namespaces.

The runtime has a `HIT-LEGACY` fallback that reads the old location directly,
but that adds an os.path.exists check to every cached request and splits the
cache over time as new writes go to the scoped path. This script performs a
one-shot migration that HARD LINKS each legacy-cached file into the scoped
path for every dataset whose samples reference it.

Why hard links? Same inode on disk, so:
  * Zero extra disk space: the cache takes the same space afterwards
  * If the legacy path is ever cleaned up, scoped paths still work
  * Trivially reversible — delete scoped path, legacy path still there

Usage
-----
From the repository root:
    PYTHONPATH=python python scripts/migrate_media_cache.py          # dry-run
    PYTHONPATH=python python scripts/migrate_media_cache.py --apply  # actually link

Uses the SDK (admin/admin123 by default; override via FRIDGE_USERNAME /
FRIDGE_PASSWORD) so it runs through the same code path as any other SDK caller
— no direct DB access.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Any, Optional

from fridge.core.client import connect, get_client
from fridge.core.dataset import Dataset

S3_LOCAL_PATH = os.getenv("S3_LOCAL_CACHE_PATH", os.path.join(os.getenv("TMPDIR", "/tmp"), "freedomridge-s3-cache"))


def _parse_s3_key(value: Any) -> Optional[str]:
    """Return the S3 key from an s3://bucket/key value, or None if not s3."""
    if not isinstance(value, str) or not value.startswith("s3://"):
        return None
    rest = value[5:]
    if "/" not in rest:
        return None
    _, key = rest.split("/", 1)
    return key


def migrate(apply: bool) -> None:
    url = os.getenv("FRIDGE_API_URL", "http://localhost:8000/api")
    username = os.getenv("FRIDGE_USERNAME", "admin")
    password = os.getenv("FRIDGE_PASSWORD", "admin123")

    # Set the global SDK client (what Dataset uses internally), then log it in
    # so the session cookie is present for access-checked endpoints.
    connect(url)
    client = get_client()
    client.login(username, password)

    datasets = client.get_datasets()
    print(f"Datasets: {len(datasets)}")
    for ds in datasets:
        print(f"  #{ds['id']} {ds['name']}: media_fields={ds.get('media_fields') or ['filepath']}")

    stats = {
        "samples_scanned": 0,
        "paths_checked": 0,
        "legacy_hits": 0,
        "already_scoped": 0,
        "linked": 0,
        "errors": 0,
    }
    # Per-(key, dataset_id) dedup so we don't re-link when two samples reference
    # the same S3 object.
    seen: set[tuple[str, int]] = set()
    t_start = time.monotonic()

    for ds_info in datasets:
        dataset_id = ds_info["id"]
        name = ds_info["name"]
        media_fields = list(ds_info.get("media_fields") or ["filepath"])

        # Request just what we need for this dataset. `filepath` is a top-level
        # column; everything else lives under metadata.<field>. The slim query
        # path in the backend only pulls the named columns, so the iteration
        # stays fast even on million-sample datasets.
        selected = []
        for f in media_fields:
            selected.append(f if f == "filepath" else f"metadata.{f}")

        dataset = Dataset(name)
        print(f"\n-> dataset #{dataset_id} {name!r}: iterating {len(media_fields)} media field(s)")

        for sample in dataset.iter_samples(selected_fields=selected, batch_size=5000):
            stats["samples_scanned"] += 1

            for field in media_fields:
                if field == "filepath":
                    value = sample.filepath
                else:
                    value = (sample.metadata or {}).get(field)

                key = _parse_s3_key(value)
                if key is None:
                    continue

                stats["paths_checked"] += 1

                legacy_path = os.path.join(S3_LOCAL_PATH, key)
                scoped_path = os.path.join(S3_LOCAL_PATH, str(dataset_id), key)

                if os.path.exists(scoped_path):
                    stats["already_scoped"] += 1
                    continue

                if not os.path.exists(legacy_path):
                    # Never cached; will be fetched on demand.
                    continue

                stats["legacy_hits"] += 1

                dedup_key = (key, dataset_id)
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)

                if apply:
                    try:
                        os.makedirs(os.path.dirname(scoped_path), exist_ok=True)
                        os.link(legacy_path, scoped_path)
                        stats["linked"] += 1
                    except FileExistsError:
                        stats["already_scoped"] += 1
                    except OSError as e:
                        # Cross-device link (EXDEV) is the main failure mode;
                        # means the cache spans filesystems. Report and keep going.
                        print(f"  link failed {legacy_path} -> {scoped_path}: {e}")
                        stats["errors"] += 1
                else:
                    stats["linked"] += 1  # would-link in dry-run

            if stats["samples_scanned"] % 50000 == 0:
                elapsed = time.monotonic() - t_start
                rate = stats["samples_scanned"] / elapsed if elapsed > 0 else 0
                print(
                    f"  [{elapsed:6.1f}s] scanned={stats['samples_scanned']:,} "
                    f"legacy_hits={stats['legacy_hits']:,} "
                    f"{'would link' if not apply else 'linked'}={stats['linked']:,} "
                    f"({rate:,.0f}/s)",
                )

    print()
    print("=" * 60)
    print(f"{'APPLIED' if apply else 'DRY-RUN'} migration summary")
    print("=" * 60)
    for k, v in stats.items():
        print(f"  {k}: {v:,}")
    if not apply and stats["linked"] > 0:
        print()
        print(f"To actually link {stats['linked']:,} files, re-run with --apply")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "--apply",
        action="store_true",
        help="Actually create the hardlinks (default is dry-run).",
    )
    args = p.parse_args()
    migrate(apply=args.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
