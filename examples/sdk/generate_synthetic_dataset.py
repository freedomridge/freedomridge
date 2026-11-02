"""Generate a synthetic Freedom Ridge dataset to try things out.

Draws real images (Pillow) with EVERY Freedom Ridge annotation type — object
detections, classifications, polygons, keypoints, a semantic-segmentation mask, and
a heatmap overlay — so you can explore annotation filtering, rendering, and
bounding-box patching in the UI without needing real data. Dependency-light: just
Pillow+numpy (Freedom Ridge base deps), unless you opt into CLIP patch embeddings.

Run (with a backend up at localhost:8000):

    python examples/sdk/generate_synthetic_dataset.py --name synth-demo --num 24

Then open the 'synth-demo' dataset in the app:
  - Filter by `detections.label` / `detections.confidence` / `polygons.label`
  - View bounding boxes, polygons, keypoints, the segmentation mask, the heatmap
  - Run `Dataset(name).to_patches('detections', ...)` to crop per-detection patches

Equivalent to `python -m fridge.synth`.
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'python'))

from fridge import Dataset, connect  # noqa: E402
from fridge.core.client import get_client  # noqa: E402
from fridge.synth import SyntheticSpec, generate  # noqa: E402

from examples._shared.importer import login  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", default="synth-demo")
    ap.add_argument("--api-url", "--url", default=os.getenv("FRIDGE_API_URL", "http://localhost:8000/api"))
    ap.add_argument("--username", default=os.getenv("FRIDGE_USERNAME", "admin"))
    ap.add_argument("--password", default=os.getenv("FRIDGE_PASSWORD"))
    ap.add_argument("--num", type=int, default=24)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    connect(args.api_url)
    client = get_client()
    login(client, args)
    gen = generate(SyntheticSpec(num_images=args.num, seed=args.seed))
    ds = Dataset(args.name)
    ds.add_samples(gen.fridge_samples())

    print(f"Created '{args.name}': {len(gen.samples)} images under {gen.out_dir}")
    print(f"  detection class counts: {gen.class_counts()}")
    print(f"Explore it in the grid/map/embeddings panels of the '{args.name}' dataset.")


if __name__ == "__main__":
    main()
