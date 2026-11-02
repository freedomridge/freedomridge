# Dataset scale experiments

These tools are separate from the real-data [demo kit](../../examples/README.md).
Run only against a disposable backend and a dataset you intend to use for load
experiments; they create large datasets, vectors or cloud objects.

| File | Purpose |
|---|---|
| `create_1m_test_dataset.py` | Generate a million-row dataset based on existing BDD100K images |
| `create_10k_patch_test.py` | Build a 10k-image patch-search load experiment |
| `upload_bdd100k_to_s3.py` | Upload to a user-supplied bucket and optionally register its storage config; supports `--dry-run` |
| `LARGE_DATASET_README.md` | Detailed million-row experiment notes and sizing |

The S3 uploader is retained because it is generic, with no private bucket or host
binding. Follow BDD100K's license before copying data. Its `--data-root` is the
parent of `bdd100k/`, matching `examples/bdd100k/ingest.py` so relative keys are
stable. Set `FRIDGE_API_URL` and `FRIDGE_API_TOKEN`, or explicit username/password
environment variables. No default password is provided.

CLIP computation for real demo data now uses the shared importer path. To repair
vectors on any existing dataset, use `examples/sdk/compute_embeddings.py`.
