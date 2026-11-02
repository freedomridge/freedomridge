"""CLI: ``python -m fridge.synth`` — spin up a synthetic Freedom Ridge dataset for demos/testing.

Draws real images with every Freedom Ridge annotation type (detections,
classifications, polygons, keypoints, segmentation masks, heatmaps) and uploads
them as a dataset you can explore in the grid/map/embeddings panels.

    python -m fridge.synth --name synth-demo --num 24
    python -m fridge.synth --name synth-demo --full   # needs torch+transformers
"""

import argparse
import os

from . import SyntheticSpec, generate


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--name", default="synth-demo", help="dataset name to create (default: synth-demo)")
    p.add_argument("--api-url", "--url", default=os.getenv("FRIDGE_API_URL", "http://localhost:8000/api"))
    p.add_argument("--username", default=os.getenv("FRIDGE_USERNAME", "admin"))
    p.add_argument("--password", default=os.getenv("FRIDGE_PASSWORD"))
    p.add_argument("--num", type=int, default=24, help="number of images (default: 24)")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument(
        "--full",
        action="store_true",
        help="full demo: also compute image hashes, a whole-image CLIP similarity index, "
        "grid patch embeddings, and a bounding-box patch dataset (needs torch+transformers+imagehash)",
    )
    args = p.parse_args()

    from ..core.client import connect, get_client
    from ..core.dataset import Dataset

    connect(args.api_url)
    if not os.getenv("FRIDGE_API_TOKEN"):
        if not args.password:
            p.error("Set FRIDGE_API_TOKEN or FRIDGE_PASSWORD")
        get_client().login(args.username, args.password)

    if args.full:
        from .demo import build_demo

        build_demo(args.name, num_images=args.num, seed=args.seed)
        return

    gen = generate(SyntheticSpec(num_images=args.num, seed=args.seed))
    ds = Dataset(args.name)
    ds.add_samples(gen.fridge_samples())
    print(f"Created dataset '{args.name}' with {len(gen.samples)} samples (images + annotations + lat/long).")
    print(f"  images written under: {gen.out_dir}")
    print(f"  detection class counts: {gen.class_counts()}")
    print(f"Open '{args.name}' to explore; re-run with --full to add hashes/embeddings/patches.")


if __name__ == "__main__":
    main()
