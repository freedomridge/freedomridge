#!/usr/bin/env python3
"""Incremental UMAP projection daemon (co-located worker, direct DB — no API auth).

Deployed alongside the app via the optional `umap` docker-compose profile. For every
saved UMAP model in FRIDGE_UMAP_MODEL_DIR (written by `python -m fridge.umap_worker fit`),
it loops: find samples that have that embedding but queued projections, project
them through the model via transform(), and write x/y. Newly-ingested points get a
position within one interval — no refitting, and existing points don't move.

A field with scheduled map re-fits is never projected here, with a legacy file or otherwise: the job
runner places its new samples from their nearest mapped neighbours (backend.map_placement). Every write
is fenced in the same transaction, so a legacy model can't write onto a re-fitted map.

Access is INTRINSIC: it uses DATABASE_URL (exactly like backend.job_service),
so a deployment that provides the DB connection needs no token or login.

    DATABASE_URL=... FRIDGE_UMAP_MODEL_DIR=... python3 scripts/umap_project_job.py
"""

import asyncio
import glob
import os
import sys

import asyncpg
import joblib
import numpy as np
from fridge.umap_worker import MODEL_DIR, parse_model_filename

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # the repo root, for backend/
from backend.coordinates import (  # noqa: E402
    SAVE_PROJECTED_COORDINATES,
    MapModelChanged,
    MapRefitActive,
    check_map_fence,
)
from backend.diskspace import COORDINATE_ROW_BYTES, require_free_space  # noqa: E402

DATABASE_URL = os.getenv("DATABASE_URL", "postgres://freedomridge:password@localhost:5432/freedomridge")
INTERVAL = float(os.getenv("FRIDGE_UMAP_INTERVAL", "15"))
BATCH = int(os.getenv("FRIDGE_UMAP_BATCH", "5000"))


def _parse_vector(vector_str):
    """pgvector '[1.0,2.0,3.0]' -> [1.0, 2.0, 3.0] (matches the backend's parser)."""
    return [float(x) for x in vector_str.strip("[]").split(",")]


def _discover_models():
    """{(dataset_id, field): path} for every model file in MODEL_DIR."""
    out = {}
    for path in glob.glob(os.path.join(MODEL_DIR, "*.joblib")):
        parsed = parse_model_filename(os.path.basename(path))
        if parsed:
            out[parsed] = path
    return out


async def _map_states(conn):
    """{(dataset_id, field): row} for fields with scheduled re-fits, with `active` while one runs."""
    rows = await conn.fetch(
        """SELECT m.dataset_id, m.field_name, m.model_id::text AS model_id,
                  (j.state IN ('queued','running')) IS TRUE AS active
           FROM embedding_map_state m LEFT JOIN jobs j ON j.id = m.active_job_id""",
    )
    return {(row["dataset_id"], row["field_name"]): row for row in rows}


async def _project(conn, dataset_id, field, model, batch):
    """Project all queued samples for one (dataset, field). Returns count."""
    moved = 0
    last_first = -1
    while True:
        rows = await conn.fetch(
            """SELECT e.sample_id, e.vector, e.xmin::text AS vector_version
               FROM embedding_projection_pending p JOIN embeddings e
                 ON e.sample_id = p.sample_id AND e.field_name = p.field_name
               WHERE p.field_name = $1 AND p.dataset_id = $2
               ORDER BY p.sample_id LIMIT $3""",
            field,
            dataset_id,
            batch,
        )
        if not rows:
            break
        first = rows[0]["sample_id"]
        if first <= last_first:  # the previous batch didn't drop out of pending — stop
            break
        last_first = first
        X = np.asarray([_parse_vector(r["vector"]) for r in rows], dtype=np.float32)
        expected_dim = getattr(model, "n_features_in_", None)
        if expected_dim is not None and X.shape[1] != expected_dim:
            raise ValueError(
                f"vector dim {X.shape[1]} != model's fitted dim {expected_dim} for "
                f"field '{field}' — the saved model doesn't match this field's vectors.",
            )
        coords = model.transform(X)
        await require_free_space(conn, len(rows) * COORDINATE_ROW_BYTES, f"projecting {len(rows)} UMAP points")
        try:
            async with conn.transaction():
                await conn.execute("SET LOCAL lock_timeout = '5s'")
                await check_map_fence(conn, dataset_id, field)  # refuses a re-fitted map
                await conn.execute(
                    SAVE_PROJECTED_COORDINATES,
                    [r["sample_id"] for r in rows],
                    [float(c[0]) for c in coords],
                    [float(c[1]) for c in coords],
                    dataset_id,
                    field,
                    [r["vector_version"] for r in rows],
                )
        except (MapRefitActive, MapModelChanged) as e:
            print(f"[umap-project] ds {dataset_id} field '{field}': {e}")
            break
        moved += len(rows)

    # Compatibility refresh: coordinate triggers already maintain ranks for this batch.
    if moved:
        try:
            await conn.execute("SELECT recompute_viz_rank($1, $2, FALSE)", dataset_id, field)
        except Exception as e:
            print(f"[umap-project] viz_cell_rank recompute failed for {field}: {e}")
    return moved


async def main():
    print(f"[umap-project] model dir: {MODEL_DIR}; interval {INTERVAL}s; batch {BATCH}")
    models = {}  # (ds_id, field) -> (model, mtime) for legacy saved models
    while True:
        try:
            conn = await asyncpg.connect(DATABASE_URL)  # fresh conn each cycle (robust to drops)
            try:
                states = await _map_states(conn)
                # (Re)load any legacy model that is new or changed — a re-`fit` swaps it in live. A
                # re-fitted field never uses its legacy file, and none is used while a re-fit runs.
                for key, path in _discover_models().items():
                    if key in states and (states[key]["model_id"] is not None or states[key]["active"]):
                        models.pop(key, None)
                        continue
                    mtime = os.path.getmtime(path)
                    if key not in models or models[key][1] != mtime:
                        try:
                            models[key] = (joblib.load(path), mtime)
                            print(f"[umap-project] loaded model {path}")
                        except Exception as e:
                            print(f"[umap-project] could not load {path} (skipping): {e}")
                total = 0
                for (ds_id, field), (model, _) in models.items():
                    try:
                        total += await _project(conn, ds_id, field, model, BATCH)
                    except Exception as e:
                        print(f"[umap-project] projecting ds {ds_id} field '{field}' failed: {e}")
            finally:
                await conn.close()
            if total:
                print(f"[umap-project] projected {total} new point(s)")
        except Exception as e:
            print(f"[umap-project] cycle failed: {e}")

        await asyncio.sleep(INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
