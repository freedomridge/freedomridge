# SDK recipes

Run scripts from the repository root after `uv sync --extra cpu` (or `--extra cuda`). Use
`FRIDGE_API_URL` and `FRIDGE_API_TOKEN` (Settings → API Tokens), or `FRIDGE_USERNAME` and
`FRIDGE_PASSWORD`. See each script's `--help` or module docstring.

| Script | Purpose |
|---|---|
| `compute_hashes.py` | Backfill missing image hashes for duplicate detection; skips objects and complete images |
| `compute_embeddings.py` | Recompute CLIP vectors for any dataset through authenticated media; cached weights |
| `compute_patch_embeddings.py` | Fixed-grid patch similarity, worker partitions and optional UMAP; `--public-s3` retains anonymous public-bucket support |
| `compute_visualization.py` | Build a map from existing vectors; fits on as many points as 75% of free RAM allows (`--fit-sample-size N` for speed, `--all` for every point) and places every point |
| `generate_random_embeddings.py` | Synthetic vectors for visualization experiments |
| `generate_synthetic_dataset.py` | Small synthetic images with boxes, polygons, points, masks and heatmaps |
| `example_dataset_delete.py` | Explicit dataset deletion API recipe; read before running |
| `query_api_demo.py` | Query/filter examples |
| `sdk_api.py` | SDK operations and equivalent API usage |
| `test_sdk_unit.py` | Offline SDK label and query construction checks |
| `test_sdk_queries.py` | Opt-in live SDK/API equivalence script; `FRIDGE_TEST_DATASET` selects a prepared dataset |

```bash
python examples/sdk/compute_patch_embeddings.py --dataset open-images-v7-val --scales 4x4 8x8
python examples/sdk/compute_patch_embeddings.py --dataset my-s3-dataset --public-s3 --scales 4x4 8x8
PYTHONPATH=python:. python -m pytest examples/sdk/test_sdk_unit.py -q
```

In the second patch example, `--public-s3` reads the dataset's `s3://` images from
a public bucket anonymously over HTTPS. Patch fields use the existing
`clip_patch_<scale>` naming. `--partition` and `--total-partitions` distribute work;
finish with `--reduce-only`. That reduction uses the SDK's client-side
visualization path for large patch inventories. The script runs with the Hugging
Face hub offline, so download the patch model's weights once beforehand:

```bash
python -c "from transformers import CLIPModel, CLIPProcessor; m = 'openai/clip-vit-base-patch32'; CLIPModel.from_pretrained(m); CLIPProcessor.from_pretrained(m)"
```

Image download is only needed for remote media. The live query script reads and
edits an explicitly prepared dataset;
it is not part of the offline CI tests and is never run merely by pytest collection.

Demo imports already include perceptual hashes. For an older dataset, with local
access to its media and the same API credentials:

```bash
python examples/sdk/compute_hashes.py coco-val2017 --algorithms dhash_8 phash_8
```

This fills only missing fields; reruns skip already-hashed images and object crops.
The SDK's `compute_hashes(overwrite=True)` replaces existing values explicitly; it
takes standard hash names only, not custom functions.
