# Changelog

Freedom Ridge has one version for the server, the web app and the Python SDK (the `VERSION`
file), following [Semantic Versioning](https://semver.org). A release lists **upgrade notes** when
it needs more than the standard steps in [Upgrading](docs/UPGRADING.md).

## 1.2.1 (2026-11-05)

### Fixed

- `fridge.compute_image_metrics()` and `compute_metrics()` were slower in 1.2.0 than in 1.1.0 when
  several processes compute them at once, as an ingest does: 16 processes computed saturation for about
  38 images a second, against 78 with 1.1.0. Saturation is now computed a strip of rows at a time, which
  makes it about 520 a second, and an ingest with 16 worker processes went from 7.2 images/s to 8.9
  (8.0 with 1.1.0). The values are identical.

### Upgrade notes

- No database migration. Only the Python SDK changed; the server and the web app are the same as in
  1.2.0.

## 1.2.0 (2026-11-04)

### Added

- SDK: `dataset.delete_embeddings(field, sample_ids)` and `view.delete_embeddings(field)` delete one
  embedding field's vectors for chosen samples, and `dataset.delete_embedding_field(field)` deletes
  them for every sample, as resumable background jobs (`POST /datasets/{id}/embeddings/{field}/delete`,
  administrators only). Samples, metadata, the field's map, model record and search index stay. The
  cleared samples are listed by `missing("embedding", field=...)` for a refill. A view without a filter
  and a filter whose conditions have no value are refused rather than taken as every sample.
- SDK: `compute_embeddings(model_source="huggingface")` computes embeddings with a Hugging Face image
  model such as DINOv3 (`facebook/dinov3-vitl16-pretrain-sat493m`), using the model's own preprocessing
  and its pooled output. Install the `huggingface-embeddings` extra, which adds torchvision.
- The Docker deployment guide has a **Sizing** section: disk per sample, vector and search-index entry,
  RAM for search and for the job runner, and where heavy computation runs.
- `CLAUDE.md`, the known-issues list and three design records are now part of the published source.

### Changed

- Patch search and find-unlabeled use one search index per tile position once every position of the
  scale has one (`build_ann_index("clip_patch_4x4_r0c0")` and so on, 16 for `4x4`). On a 41,620-image
  dataset a search took 40–120 ms instead of 1–1.5 s; on a small server it was 15–30 s before. Results
  are approximate, like similarity search (top-50 recall about 0.99). Without the indexes, every patch
  is ranked exactly, as before.
- `fridge.compute_image_metrics()` and `compute_metrics()` are about 2.4× faster. The values are
  identical.
- Uploading vectors of another dimension to a field that has no vectors left but still has its search
  index is refused with 422 naming the index, instead of failing with a server error.
- Backup retention prunes only `freedomridge_*.sql.gz` dumps.
- Upgrading: how to clear Docker's old build cache after an upgrade.

### Fixed

- A new user's home page stayed blank after the required password change until the page was reloaded.

### Upgrade notes

- No database migration.
- For fast patch search, build the per-position indexes once per patch scale after upgrading, for
  example for `4x4`:
  `python -c 'from fridge import Dataset, connect; connect(); ds = Dataset.from_id(DATASET_ID); [print(ds.build_ann_index(f"clip_patch_4x4_r{r}c{c}")) for r in range(4) for c in range(4)]'`
  (`Dataset.from_id`, because `Dataset("name")` creates a dataset whose name is mistyped).
  Each index takes about as much disk as its vectors and covers every dataset with that field; on a
  workstation each took about 18 s for 55,000 vectors.

## 1.1.0 (2026-11-04)

### Added

- Storage configurations can be limited to chosen datasets (**Settings → Storage
  Configuration → Datasets → Only these datasets**); other datasets are refused their
  buckets. Configurations still serve all datasets unless limited. Migration
  `0001_storage_config_dataset_scope.sql`.
- SDK: `dataset.version(name)` returns a read-only view of a saved version that is queried like
  a `DatasetView` (`count`, `distinct`, `count_values`, `aggregate`, `filter`, `select_fields`,
  iteration), computed in the database against the version's stored samples.
- SDK: `dataset.add_samples_from(source)` copies samples from a dataset or view, with their
  metadata, tags, location, array fields, embeddings and hashes, adding only the ones the target
  lacks.
- SDK: `dataset.load_view(name)` turns a saved view into a `DatasetView`, combining its stages as
  the app does.
- SDK: `compute_embeddings(skip_existing=True)` embeds only samples that lack the field;
  `/datasets/{id}/missing` takes an `after_id` cursor.
- SDK: `fridge.compute_image_metrics(img)` computes the image-quality metrics
  `compute_metrics()` stores, for one image.
- SDK: `connect(timeout=...)` sets the default request timeout.
- The home page's New Dataset and Create Dataset buttons open a form to create a dataset
  (admins only; public #1).

### Changed

Two API responses change (the Bearer token and slim query entries below); scripts that relied on
the old behavior may need updating.

- An invalid or expired API token (`Authorization: Bearer ...`) is rejected with 401. Before, the
  request was served as anonymous, so a script with a bad token saw empty results instead of an
  error.
- A storage configuration that is added, changed or deleted applies to every backend worker on
  its next request. Before, other workers could keep refusing a newly added bucket ("Bucket not
  authorized for this dataset", **Unavailable** grid tiles), or keep using old credentials, for up
  to 15 minutes.
- When several configurations cover a bucket, a dataset's own configuration
  (`storage_config_id`) now comes first, ahead of another configuration whose bucket name matches
  exactly.
- Slim sample queries (`slim: true` with `fields`) return metadata values with their JSON types
  (numbers, booleans, lists) instead of text, and accept any key, including keys with `:`, which
  were dropped before. CSV export of such columns is no longer blank.

### Fixed

- Text search, prompt concepts and reverse image search no longer fail with a server error on
  Python 3.14: the CLIP text and image towers now load with their own configs (public #6).
- SDK: `Sample.heading` was always `None` for samples read from the server.

## 1.0.0 (2026-11-02)

The first public release.
