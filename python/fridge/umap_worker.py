"""Manual UMAP fit for Freedom Ridge embeddings (SDK / authenticated).

`fit` fits UMAP on each embedding field (RAM-scaled by default), projects EVERY
existing point and uploads the coordinates. Run this for a field's first map, or whenever you want a
fresh fit of all points of a map without scheduled re-fits. No model is kept: the server places
newly-ingested points on every map from their nearest mapped neighbours.

This is the SDK path — it authenticates like any client (an API token via
$FRIDGE_API_TOKEN, else username/password).

    python -m fridge.umap_worker fit --dataset large-dataset                   # RAM-scaled high-fidelity fit
    python -m fridge.umap_worker fit --dataset large-dataset --fit-sample-size all
    python -m fridge.umap_worker fit --dataset my-data --fit-sample-size 200000
"""

import argparse
import os

from .core.client import connect, get_client
from .core.dataset import Dataset


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
    print(f"Connected to Freedom Ridge at {url}")
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
    print(f"Embedding fields: {fields}")
    failed = []
    for field in fields:
        print(f"\n=== fit {field} (fit={args.fit_sample_size}) ===")
        try:
            ds.compute_visualization(field_name=field, fit_sample_size=fit)
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
    pf = sub.add_parser("fit", help="Fit UMAP per field and project all points")
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
