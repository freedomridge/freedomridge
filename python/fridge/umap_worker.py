"""Manual UMAP fit for Freedom Ridge embeddings (SDK / authenticated).

`fit` fits UMAP on each embedding field (RAM-scaled by default), projects EVERY
existing point, uploads coords to separate layout storage, and SAVES the fitted model to disk so the projection
daemon can transform new points without refitting. Run this for the first fit, or
whenever you want a fresh re-fit of all points.

This is the SDK path — it authenticates like any client (an API token via
$FRIDGE_API_TOKEN, else username/password). The always-on projection of newly-ingested
points is a separate, co-located, direct-DB daemon that needs no API auth:
`scripts/umap_project_job.py` (deployed via the optional `umap` docker-compose profile).

    python -m fridge.umap_worker fit --dataset large-dataset                   # RAM-scaled high-fidelity fit
    python -m fridge.umap_worker fit --dataset large-dataset --fit-sample-size all
    python -m fridge.umap_worker fit --dataset my-data --fit-sample-size 200000

Models live under FRIDGE_UMAP_MODEL_DIR (default $FRIDGE_DATA_ROOT/umap_models).
"""

import argparse
import os

from .core.client import connect, get_client
from .core.dataset import Dataset

MODEL_DIR = os.environ.get(
    "FRIDGE_UMAP_MODEL_DIR",
    os.path.join(os.environ.get("FRIDGE_DATA_ROOT", os.path.expanduser("~/.fridge")), "umap_models"),
)


def model_filename(dataset_id, field) -> str:
    return f"{dataset_id}__{field}.joblib"


def parse_model_filename(name):
    """'7__field.joblib' -> (7, 'field'); None if it doesn't match the convention."""
    if not name.endswith(".joblib"):
        return None
    stem = name[: -len(".joblib")]
    if "__" not in stem:
        return None
    ds, field = stem.split("__", 1)
    try:
        return int(ds), field
    except ValueError:
        return None


def model_path(dataset_id, field) -> str:
    return os.path.join(MODEL_DIR, model_filename(dataset_id, field))


def _connect():
    url = os.environ.get("FRIDGE_API_URL", "http://localhost:8000/api")
    connect(url)
    client = get_client()
    # connect() already applies $FRIDGE_API_TOKEN (Bearer) if set; otherwise log in.
    if not os.environ.get("FRIDGE_API_TOKEN"):
        client.login(
            os.environ.get("FRIDGE_USERNAME", "admin"),
            os.environ.get("FRIDGE_PASSWORD", "admin123"),
        )
    client.session._default_timeout = (10, 1800)
    print(f"Connected to Freedom Ridge at {url}; model dir: {MODEL_DIR}")
    return client


def cmd_fit(args):
    fit = args.fit_sample_size
    if fit == "all":
        fit = -1
    elif fit != "auto":
        fit = int(fit)

    _connect()
    ds = Dataset(args.dataset)
    fields = ds.embedding_fields()
    if not fields:
        print(f"No embedding fields on '{args.dataset}' — nothing to fit.")
        return
    os.makedirs(MODEL_DIR, exist_ok=True)
    print(f"Embedding fields: {fields}")
    failed = []
    for field in fields:
        path = model_path(ds._id, field)
        print(f"\n=== fit {field} → {path} (fit={args.fit_sample_size}) ===")
        try:
            ds.compute_visualization(field_name=field, fit_sample_size=fit, save_model_path=path)
            print(f"  done: {field}")
        except Exception as e:
            print(f"  fit for {field} FAILED: {e}")
            failed.append(field)
    if failed:
        # Exit nonzero so a non-interactive fit job surfaces the failure instead of "succeeding".
        raise SystemExit(f"UMAP fit failed for {len(failed)} of {len(fields)} field(s): {', '.join(failed)}")


def main():
    p = argparse.ArgumentParser(description="Manual UMAP fit for Freedom Ridge embeddings")
    sub = p.add_subparsers(dest="cmd", required=True)
    pf = sub.add_parser("fit", help="Fit UMAP per field, project all points, and save the model")
    pf.add_argument("--dataset", required=True, help="Freedom Ridge dataset name (e.g. 'large-dataset')")
    pf.add_argument(
        "--fit-sample-size",
        default="auto",
        help="'auto' (RAM-scaled, default), 'all', or an integer count.",
    )
    pf.set_defaults(func=cmd_fit)
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
