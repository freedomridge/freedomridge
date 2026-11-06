# Changelog

Freedom Ridge has one version for the server, the web app and the Python SDK (the `VERSION`
file), following [Semantic Versioning](https://semver.org). A release lists **upgrade notes** when
it needs more than the standard steps in [Upgrading](docs/UPGRADING.md).

## Unreleased

### Added

- Compute workers, first part: a worker token (`"scope": "worker"` in `POST /api/tokens`,
  `client.create_token(name, scope="worker")`), which only the new worker endpoints accept, and
  `python -m fridge.worker`, which registers a machine with the server and keeps it online.
  **Settings → Compute Workers** and `GET /api/workers` list workers (an admin sees all of them), and
  System status counts them. **API Tokens** makes a worker token (**Used by: a compute worker**).

### Upgrade notes

- One database migration, `0003_compute_worker_tokens.sql`, which adds a column to `api_tokens` and
  two to `workers`. Apply it with `scripts/apply_migrations.sh`, as step 4 of
  [Upgrading](docs/UPGRADING.md) describes. It waits at most 2 seconds for its locks: if it stops with
  a lock timeout, run it again.

## 1.3.1 (2026-11-06)

### Fixed

- Restoring a version (**Restore sample data**, `POST /datasets/{id}/versions/{version_id}/revert`,
  `Dataset.revert()`) failed with 409 "Dataset changed during restore" whenever something else wrote
  to the dataset while it ran. That included the statistics refresh that the previous restore started,
  so two restores in a row could fail. The server now runs the restore again, up to three attempts in
  all, and returns the 409 only when every attempt is interrupted.

### Upgrade notes

- No database migration. Only the server changed; the web app and the Python SDK are the same as in
  1.3.0.

## 1.3.0 (2026-11-05)

### Added

- A dataset can have a default view: a public saved view the web app opens the dataset on, for
  example one whose concept filters hide black images or road photos. An administrator stars it in
  **Saved Views** (`default_view_id` in `PATCH /datasets/{id}`). Its filters show in the filter bar
  after a **Default view:** label with the view's name. **Clear All** keeps them, and **Show all
  images** removes them until the next Clear All. Only the app applies it: the API and the SDK still
  return every sample. The view must be public, and stays public while it is the default.
- CI checks the upgrade path: a database created by each 1.x release, upgraded with the current
  migrations, must match a fresh install. Contributors run the whole CI on their own machine with
  `scripts/ci_local.sh <commit>`.

### Upgrade notes

- One database migration, `0002_dataset_default_view.sql`, which adds a column to `datasets`. Apply
  it with `scripts/apply_migrations.sh`, as step 4 of [Upgrading](docs/UPGRADING.md) describes. It
  locks `datasets` and `views` for an instant and waits at most 2 seconds for those locks: if it
  stops with a lock timeout because something was using the tables, run it again.

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
