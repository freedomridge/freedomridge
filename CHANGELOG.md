# Changelog

Freedom Ridge has one version for the server, the web app and the Python SDK (the `VERSION`
file), following [Semantic Versioning](https://semver.org). A release lists **upgrade notes** when
it needs more than the standard steps in [Upgrading](docs/UPGRADING.md).

## Unreleased

### Added

- `python -m fridge.worker` fits maps: on a server whose job runner leaves fits to compute workers
  (`JOB_RUNNER_PLACEMENTS=server`), a worker claims a map re-fit's fit, downloads the exported vectors, runs
  the fit on its own machine and uploads the new coordinates, and the server swaps the map in. A server too
  small to fit its maps (such as a t3.large) can have them fitted on a desktop instead.
  - The fit sizes itself to the worker's free memory. An out-of-memory kill stops only the fit, which runs
    again on 75% of the points, down to the smallest useful fit.
  - A lost lease or a cancel stops the fit at once, Ctrl-C or SIGTERM gives the job back, and a transfer that
    fails for a moment is tried again. Install the SDK with the `embeddings` extra on the worker.

### Changed

- The dataset page's top bar is laid out again.
  - **Add Filter** and **Find similar** start the filter bar, before the chips they make, and the image count
    ends it. The dataset's name, its draft filters, Saved Views, Jobs, settings and fast mode share the row above.
  - **Each filter's card switches between Builder and Query.** Click a chip, or Add Filter, and edit the filter
    as a form or as text, such as a pasted list (docs/FILTER_QUERY.md). The bar always shows chips; the
    **Query** button is gone.
  - **Find similar** opens a panel: describe what you want, or upload or paste an example image, and choose how
    many of the closest images to show. With filters applied, it searches **within your filters** (they stay, as
    a chip beside the search's) or the **whole dataset** (the results replace them, as every search did before).
    A new search replaces the previous search. The search's chip offers **top N**, which runs it again. Find
    similar from images selected in the grid is unchanged.
  - **Draft filters** show one chip per sidebar field, saying what each one is, and each can be removed on its
    own.
  - The header's image-field dropdown is gone. The grid's **View** picker now also saves the field every grid
    opens on.
- **Excluding a map region or an embeddings lasso removes what is inside it and keeps everything else**, images
  with no location (or no point on that map) included. Excluding a map region used to drop the images with no
  location too.

### Fixed

- An embeddings lasso's chip opened the field form, which could show only the field's name (such as `clip`). It
  now says which map the lasso was drawn on and, like a map region, a click toggles it between including and
  excluding its area (the new `embedding_polygon_exclude` filter operator).
- Clicking a text or image search's chip to exclude its results changed only its label. It now excludes them.

## 2.0.0 (2026-11-06)

2.0 removes old forms of the API that 1.x still accepted, and new images now appear on every map by
themselves. Read the upgrade notes: an SDK works only with a server of its own major version.

### Added

- The SDK and the server check each other's version.
  - Every answer from the server carries its release in an `X-Fridge-Version` header. The SDK raises
    `fridge.IncompatibleServerError` when the server runs another major version, naming both versions and
    how to install the SDK of the server's release.
  - Every SDK request carries `X-Fridge-SDK-Version`, and the server refuses an SDK of another major version
    with HTTP 426 and the same advice.
- Compute workers, second part: the server side of a compute worker's job.
  - A worker claims compute work, long-polling up to 25 s, and only for datasets its owner can access. It
    renews the job's lease with heartbeats, and completes, fails or releases the job. Each claim also counts
    as the worker's heartbeat.
  - A map re-fit runs as steps that each say where they run. The export, the swap and the ranks run on the
    server; the fit is compute work. A runner that doesn't take a step's placement hands the job back.
  - A worker can download a re-fit's exported vectors, with Range requests to resume, and upload its
    coordinates under its lease. The server checks them against their sha256, the exported rows and the
    export's order before swapping the map in. No model crosses: a model is a pickle, which runs code when
    loaded.
  - `python -m fridge.worker` doesn't run fits yet. Until it does, keep `JOB_RUNNER_PLACEMENTS` at its
    default (`server,compute`). With `server` alone, fits wait for a worker and are cancelled after 12
    hours, leaving the old map.

### Changed

- Map: new images on every map are placed by the server within seconds of arriving, from the 15 mapped
  images most like them (the densest group of them on the map), by a new background job, **Placing new
  points on the map**. Before, they waited for the optional projection worker or a
  `Dataset.project_pending()` run with a saved UMAP model, and appeared only where one of those ran.
  - It covers every whole-image map, re-fitted or made with `compute_visualization`. A field with no map
    yet gets nothing placed until its first fit (`dataset.map_state(field)["current_points"]` says how
    many points a map has). Patch maps aren't placed, as before.
  - While `compute_visualization` lays out a map again, placement on that map waits, so images ingested
    meanwhile land on the new layout. A script that dies holds placement up for at most ten minutes. Another
    writer of a full layout can do the same with `PUT`/`DELETE /api/datasets/{id}/embeddings/placement-hold`.
  - No map keeps a model: a re-fit no longer saves one, which took about as much disk as the exported
    vectors (tens of GB for millions of images). A model kept from an earlier re-fit is deleted when its
    map is next re-fitted.
  - Measured on 3.7 million images, 9 in 10 new images land within 1.3% of the map's width of where
    UMAP's own fit put them ([JOBS.md](docs/architecture/JOBS.md), "Placing new samples"). Placed images
    drift as a map keeps growing, so re-fit a map that has grown well past its fit.
- `Dataset.create_view()` makes a private view unless `is_public=True`, like `client.create_view()` and
  the app's Save View.

### Fixed

- Histograms of a view whose filter is long (a selection of more than about 8,000 samples, or a long `in`
  list) failed with `400 Invalid HTTP request received.`: the filter went in the URL, past uvicorn's 64 KB
  request line. The app and `view.histogram_values()` now send it in the body of
  `POST /api/datasets/{id}/histogram` and `.../histogram/preview`; the GET forms remain.

### Removed

- The projection worker (`scripts/umap_project_job.py`, the `umap-worker` service and its `umap` Compose
  profile), `Dataset.project_pending()`, and `compute_visualization(save_model_path=...)`: the server
  places new points itself. `python -m fridge.umap_worker fit` still fits every embedding field of a
  dataset, and no longer saves a model.
- The legacy S3 proxy `GET /media/s3/<key>`: it was admin-only, read an environment-variable bucket and
  couldn't check dataset access. Every image is served by `/api/samples/{id}/media`.
- The SDK's image loader no longer rewrites the old proxy forms `/media/s3/<key>` (with `S3_BUCKET`) or
  `/media/<path>` (under `FRIDGE_DATA_ROOT`). A filepath is `s3://bucket/key` or a local path: absolute as
  it is (an external drive at `/media/<user>/...` included), relative under `FRIDGE_DATA_ROOT`.
- `GET /api/datasets/{id}/map_points`, and `POST .../map_points` without `zoom` or `"points": true`. Both
  returned every located sample of the dataset at once, with no bound. The map sends its zoom; send
  `"zoom"` (clusters below 12, points from 12) or `"points": true` (every point, up to the cap).
- The single `embedding` of a sample (`Sample(embedding=[...])` and `"embedding": [...]` in
  `POST /datasets/{id}/samples`). It was stored as the `clip` field whatever model made the vector. Use
  `embeddings={"clip": [...]}` (any field name).
- The flat filter form `{"field": value}` (with lists and `{"min", "max"}` ranges) in the `filters` of
  `GET /datasets/{id}/samples` and the other filtered endpoints. Filters are a filter tree:
  `{"field", "operator", "value"}` or `{"logic", "filters"}`, as the app, the SDK and the query language send.
- The `file_date`, `date_min`, `date_max`, `brightness_min` and `brightness_max` query parameters of
  `GET /datasets/{id}/samples`, which filtered fixed metadata keys.

### Upgrade notes

- **Upgrade the Python SDK together with the server.** An SDK works only with a server of its own major
  version. SDKs before 2.0 neither send their version nor check the server's, so a 2.x server can't refuse
  a 1.x SDK: it fails wherever the API changed instead. Install the SDK from the server's release:
  `pip install "freedomridge @ git+https://github.com/freedomridge/freedomridge.git@v2.0.0#subdirectory=python"`.
- Three database migrations, `0004` to `0006`. Apply them with `scripts/apply_migrations.sh`, as step 4 of
  [Upgrading](docs/UPGRADING.md) describes. Each waits at most 2 seconds for its locks: if one stops with a
  lock timeout, run it again. 2.0 keeps 1.x's database baseline, so a database of any 1.x release upgrades
  straight to 2.0.0 (CI checks every one).
- Migration `0004_map_state_without_model.sql` lets a re-fitted map have no model file (a map fitted on a
  compute worker, which never brings one back). It only changes a check on `embedding_map_state`, and is
  applied like the others.
- Migration `0005_map_placement_history.sql` rebuilds two small indexes of the job history, so the new
  placement jobs stay out of a dataset's job history like statistics and are deleted after a day. While it
  builds them, job updates wait; the indexes cover the jobs table, which stays small because automatic
  runs are pruned daily (about 1,000 rows and 15 MB on a deployment three days in).
- Migration `0006_map_placement_hold.sql` adds a column to `embedding_viz_meta`, and is applied like the
  others.
- **If you ran the projection worker** (`--profile umap`), stop and remove its container before upgrading:
  `docker compose --profile umap rm -sf umap-worker` (or `docker compose up -d --remove-orphans` afterwards).
  The new Compose file no longer defines it, so an upgrade would leave the old container running from its old
  image, still projecting with saved models.
- Saved UMAP models (`<dataset>__<field>.joblib` in `FRIDGE_UMAP_MODEL_DIR`) are no longer read; delete
  them to free their disk. A script that called `project_pending()` after an ingest needs no step now; one
  that made a field's first map with `compute_visualization()` keeps doing so.
- No stored filepath needs rewriting unless it uses the old proxy forms, which the server stopped writing
  long ago. Check with `SELECT count(*) FROM samples WHERE filepath LIKE '/media/s3/%';`: it should be 0. A
  `/media/...` path that is a real file on disk (a mounted drive) keeps working. A `/media/<path>` in the old
  local proxy form (relative to `FRIDGE_DATA_ROOT`, not a real file) no longer resolves in the SDK's image
  loader (the app never served it): store the relative path instead.
- A script that fetched every location from `map_points` gets a 422: send the zoom, or `"points": true`, or
  page through `samples/query` for the `location` field.
- `Sample(embedding=...)` is a `TypeError`, and the API refuses `"embedding"` with a 422 that names the
  replacement, so an older client's vectors are never silently dropped.
- A flat `filters` object is now a 422 naming the tree form. `{}` still means no filter. The five removed
  query parameters are a 422 that gives the filter tree to send instead (for example
  `{"field": "brightness", "operator": ">=", "value": 0.3}`), so a script using them fails rather than
  getting the whole dataset.
- A script that relied on `Dataset.create_view()` sharing the view with everyone must pass `is_public=True`
  (a dataset's default view must be public).

## 1.4.0 (2026-11-06)

### Added

- A filter query language: filters written as text, checked against the dataset's own fields
  ([FILTER_QUERY.md](docs/FILTER_QUERY.md)), for example
  `serial in (103000752 103001025) AND split = train AND NOT (score < 0.2 OR note exists)`.
  - **Query** in the filter bar shows the current filters as text to read, copy, paste or edit, and
    Apply turns the text back into chips.
  - A list takes commas or any whitespace between values, so a pasted column of serial numbers works
    as is.
  - A mistake names its line and column, with a suggestion ("unknown field 'splt' (did you mean
    'split'?)"), and changes nothing.
  - Concept scores, map regions, lassos and selections have no text form and stay as chips.
  - The same text works in the SDK (`dataset.query(...)` and the new
    `dataset.create_view(name, query=...)`) and over HTTP
    (`POST /api/datasets/{id}/filter_query/parse` and `.../format`).
- Map: a click on a cluster bubble now zooms in until the bubble breaks apart. It fits every image
  the bubble counts, and always goes at least one zoom level deeper.
- Compute workers, first part: a worker token (`"scope": "worker"` in `POST /api/tokens`,
  `client.create_token(name, scope="worker")`), which only the new worker endpoints accept, and
  `python -m fridge.worker`, which registers a machine with the server and keeps it online.
  **Settings → Compute Workers** and `GET /api/workers` list workers (an admin sees all of them), and
  System status counts them. **API Tokens** makes a worker token (**Used by: a compute worker**).

### Changed

- Map: a cluster bubble takes in the individual points it is drawn over and counts them, instead of
  drawing them on top of it. Bubbles grow and shrink with the map between data loads, so a circle
  always covers exactly the points it counts.
- Map: the map fills its panel when the panel is resized (dragging a separator, splitting a panel).
  Before, it kept its old size, with a black strip beside it.

### Fixed

- Map: the clustered view slowed down after its first few uses on each database connection. From
  the sixth run, PostgreSQL could switch to a generic plan, about 9 times slower: 14 s instead of
  1.5 s on 3.75M samples at zoom 4.5.

### Upgrade notes

- **`dataset.query()` now uses the server's parser**, the same one as the filter bar. Existing
  queries keep working, `BETWEEN` and `LIKE '%x%'` included. Two things differ:
  - **A field the dataset doesn't have raises `ValueError`**, naming its line, its column and the
    closest field. Before, a misspelt field silently matched nothing (or, with `IS NULL`, everything).
  - **`AND` binds tighter than `OR`, as in SQL.** `a = 1 OR b = 2 AND c = 3` now means
    `a = 1 OR (b = 2 AND c = 3)`; the SDK used to read it as `(a = 1 OR b = 2) AND c = 3`. Add
    brackets to a query that relied on the old order.
- One database migration, `0003_compute_worker_tokens.sql`, which adds a column to `api_tokens` and
  two to `workers`. Apply it with `scripts/apply_migrations.sh`, as step 4 of
  [Upgrading](docs/UPGRADING.md) describes. It waits at most 2 seconds for its locks: if it stops with
  a lock timeout, run it again.
- **Docker:** `docker-compose.prod.yml` mounts the `umap_models` volume on the job-runner at
  `/models` (`FRIDGE_UMAP_MODEL_DIR`), and takes an optional `JOB_RUNNER_MEMORY_LIMIT` (its
  `mem_limit` and `memswap_limit`).
  - **Unset (the default):** there is no limit, and server-side map re-fits stay skipped, as before.
    No action is needed to keep today's behaviour.
  - **Set, for example to `24g`:** re-fits run inside that limit, and the fit is the kernel's first
    choice for an out-of-memory kill, so the runner survives one.
  - On a t3.large, leave it unset ([Deploy to AWS](docs/DEPLOY_TO_AWS.md)).

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
