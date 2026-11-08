# Freedom Ridge Python SDK

Complete API reference for the Freedom Ridge Python SDK.

## Installation

```bash
# Install from source (the SDK lives in python/)
cd /path/to/freedomridge
pip install -e python/

# Compute helpers need extras; install only what you use, or everything with [all]:
pip install -e "python/[all]"
```

| Extra | Needed for |
|-------|------------|
| `embeddings` | `compute_embeddings`, `compute_visualization`, `compute_all`, local text encoding |
| `huggingface-embeddings` | `compute_embeddings(model_source="huggingface")`: Transformers image models such as DINOv3 |
| `patch-embeddings` | `compute_patch_embeddings` |
| `hashes` | `compute_hashes` |
| `metrics` | `compute_metrics` |
| `quality` | scikit-learn, for `compute_visualization(method="pca"` or `"tsne")` (`embeddings` already pulls it in) |
| `all` | all of the above |

`boto3` is in no extra: install it yourself if a compute helper must read `s3://` images.

## Connecting & Authenticating

Before using `Dataset`/`Sample`, point the SDK at your Freedom Ridge server and
authenticate. There are two ways to authenticate:

**API token (recommended for scripts / automation)** — tokens don't expire on
idle and never embed a password. Create one in the web UI (Settings → API Tokens)
or with `client.create_token(...)`, then:

```python
import fridge

# Pass the token explicitly...
fridge.connect("http://localhost:8000/api", api_token="<your-token>")

# ...or via the FRIDGE_API_URL / FRIDGE_API_TOKEN environment variables:
#   export FRIDGE_API_URL=http://localhost:8000/api
#   export FRIDGE_API_TOKEN=<your-token>
fridge.connect()
```

Requests time out after `(10, 300)` seconds by default (connect, read). Pass `timeout=` to change
it, for example for large uploads over a slow link or long statistics refreshes:
`fridge.connect(url, api_token=..., timeout=(120, 1800))`. It takes a number or a
`(connect, read)` tuple.

**Username/password (interactive use)** — logs in with a session cookie:

```python
import fridge

fridge.connect("http://localhost:8000/api")
fridge.get_client().login("username", "password")
```

Managing tokens programmatically (requires a power_user/admin login or token):

```python
client = fridge.get_client()
tok = client.create_token("ingest-job", expires_days=90)
print(tok["token"])      # shown ONCE — store it securely
client.list_tokens()      # values are never returned again
client.revoke_token(tok["id"])
```

> If you don't call `connect()`, the SDK defaults to `FRIDGE_API_URL` or
> `http://localhost:8000/api` (and `FRIDGE_API_TOKEN` if set).

**The SDK and the server must share a major version.** Within one major version, any SDK works with any server.

- **The server's version:** every answer says which release made it (the `X-Fridge-Version` header). The SDK
  raises `fridge.IncompatibleServerError` on the first answer from a server of another major version.
- **The SDK's version:** every request says which release it is (`X-Fridge-SDK-Version`). The server refuses an
  SDK of another major version with HTTP 426.

Either way, the message names both versions and the SDK to install, from the server's release:
`pip install "freedomridge @ git+https://github.com/freedomridge/freedomridge.git@v2.0.0#subdirectory=python"`.
SDKs before 2.0 send no version, so a 2.x server can't refuse them. Upgrade the SDK when you upgrade the
server.

### Compute workers

A machine with the RAM or CPUs for heavy jobs can run them for a server as a compute worker. It
needs a worker token, which only the worker endpoints accept:

```python
token = client.create_token("my desktop", scope="worker")["token"]
```

```bash
FRIDGE_API_URL=https://YOUR_DOMAIN/api FRIDGE_API_TOKEN=<worker token> python -m fridge.worker
```

The worker registers with the server, then claims compute work again and again (each claim waits up to 25 s on
the server and keeps it online), so **Settings → Compute Workers** and `client.list_workers()` list it, and
System status counts it online. It keeps going while the server is down, restarting or slow to answer, and
registers again if the server dropped it. A refusal only a person can fix, such as a revoked token or an
unknown `--kinds`, stops it with the reason. `--name` sets how the server lists it (default: the host name).

The work it takes is a **map re-fit's fit**, on a server whose job runner leaves fits to workers
(`JOB_RUNNER_PLACEMENTS=server`; by default the server fits maps itself). The server exports the vectors; the
worker downloads them, runs `python -m fridge.map_fit` in a child process, uploads the new coordinates (never
a model) and the server checks them and swaps the map in. The worker needs the fit's packages: install the
SDK with the `embeddings` extra. The fit sizes itself to this machine's free memory, and an out-of-memory kill
stops only the child: it runs again on 75% of the points, down to the smallest useful fit. A thread renews the
job's lease meanwhile; if the lease is lost, or someone cancels the job, the fit stops at once. Ctrl-C (or
SIGTERM) gives the job back. Work files live in `FRIDGE_WORKER_DIR` (default `~/.fridge/worker`), one folder
per job, removed when it ends.

## Core Classes

### Dataset

The `Dataset` class is the main interface for creating and managing datasets.

#### Creating a Dataset

```python
from fridge import Dataset

# Create a new dataset (or load it if a dataset with this name already exists).
dataset = Dataset(
    "my_dataset",
    description="Optional description",
    media_fields=["filepath"],  # Optional, defaults to ["filepath"]
)
```

`Dataset(name)` is load-or-create: it returns the existing dataset if one with
that name exists, otherwise creates it. The `description`/`media_fields`
arguments are only applied when creating.

#### Loading an Existing Dataset

```python
# Load (or create) by name
dataset = Dataset("my_dataset")

# Load strictly by numeric id (does not create)
dataset = Dataset.from_id(1)
```

#### Dataset Properties

```python
dataset.id             # Dataset ID (int)
dataset.name           # Dataset name (str)
dataset.description    # Dataset description (str | None)
dataset.media_fields   # List of media field names (list[str])
dataset.created_at     # Creation timestamp (ISO-8601 string)
```

#### Dataset Methods

##### `add_samples(samples: list[Sample], on_duplicate="error") -> dict`

Add samples to the dataset. Always takes a **list** (there is no `add_sample`
singular — wrap a single sample in a list). Returns a dict including the created
sample `ids`. `on_duplicate` is one of `"error"` (default), `"skip"`, or
`"update"`. In `"update"` mode, `metadata`/`location`/array fields are **replaced**
(the uploader owns derived data) but `tags` are **merged** (union) — re-ingesting a
sample never erases tags added in the app. Use a sample `save()` or bulk-set to
deliberately clear tags.

```python
from fridge import Sample

# Add a single sample
sample = Sample(
    filepath="/path/to/image.jpg",
    metadata={"category": "cat"},
    tags=["training"],
)
result = dataset.add_samples([sample])
print(result["ids"])

# Bulk add (much more efficient than one call per sample)
samples = [
    Sample(filepath=f"/data/img_{i}.jpg", metadata={"idx": i})
    for i in range(1000)
]
dataset.add_samples(samples)
```

###### Retrying field ingest from a Step Function

Load the target with `Dataset.from_id(dataset_id)` or an explicit lookup of an
existing dataset. `Dataset(name)` creates it if absent, so a typo can redirect
ingest into a new dataset. Creating a dataset needs an `admin` token; adding samples
to an existing one needs `power_user` or `admin`.

Use `dataset.add_samples(batch, on_duplicate="skip")` on the **first attempt and
every retry**. Keep each sample's `filepath` stable (for S3, use the same
`s3://bucket/key`); uniqueness is per dataset and filepath. Put metadata, labels
(in metadata), tags, registered array fields, named embeddings, and hashes on
each `Sample` before calling add. The SDK sends the entire list in one request:
`POST /api/datasets/{id}/samples` with
`{"samples": [...], "on_duplicate": "skip"}`.

Start with batches of 1,000 samples; reduce this for large metadata or many
embeddings. The shipped nginx configuration accepts bodies up to 100 MB on this
add-samples route, on sample lookup and bulk-set, and on the embedding,
patch-embedding, coordinate and hash upload routes. Sample queries
(`/samples/query`) accept 10 MB, reverse-image search 30 MB, and every other
`/api/` route 1 MB. HTTP 413 requires smaller batches, not a retry of the
unchanged request.

One add request is one transaction, including inline artifacts, field registration,
statistics cache updates, and pending statistics work. Any validation or database
failure rejects the request; a write failure rolls back the entire batch. There
is no post-commit best-effort artifact upload. A response lost after commit is
ambiguous to the caller, so resend the same batch with `"skip"`: existing samples
and their artifacts remain unchanged, only new filepaths are inserted. A replay
containing only existing filepaths returns 200 with `ids: []` and `skipped` equal
to the batch size, without adding statistics or concept work. A partially
overlapping batch inserts only its new samples; returned `ids` cover those
inserted samples, not every input position. Skipping does not backfill or correct
an existing sample's metadata or artifacts. If a later step needs IDs after a
skip resend, use `dataset.client.lookup_samples(dataset_id, "filepath", filepaths)`;
skipped samples have no entries in the add response's `ids`.

A client read timeout or proxy timeout (including nginx's 300-second sample
request timeout and HTTP 504) is ambiguous: the server may still commit after
the caller times out. Resend with `"skip"` even if the first attempt appears to
have written nothing.

In the default `"error"` mode, nonduplicate
samples and their inline artifacts commit, and duplicates are reported with HTTP
409 containing `ids` for the new samples and an `existing` filepath-to-ID map.
The SDK **returns that conflict dictionary**, without raising. This includes
duplicate filepaths within the request; each new filepath is inserted once.
An all-duplicate resend returns `ids: []` and the existing matches. This duplicate
report is not a failed write: validation, database errors, server-side database timeouts, and capacity
refusal still reject the whole request without committing new samples.
Use `"skip"` for retried batches so duplicates return HTTP 200.
`"update"` intentionally rewrites existing data and is not the replay contract.

Let SDK exceptions fail the Step Function task; do not catch and return a success
payload. Invalid local values can raise before any HTTP request (for example,
a malformed location); fix these inputs instead of retrying them unchanged.
Retry connection failures, connection/read timeouts, transient 5xx errors, and a
429 that outlasts the SDK's own retries, with bounded exponential backoff and
jitter. Database lock/statement timeouts surface as errors too. For 409, retry
only a busy response carrying `Retry-After`; the default-mode duplicate report is
returned as described above.

The SDK itself resends a busy 409 or an nginx 429 (rate or connection limit) when
it carries `Retry-After`, for up to 120 seconds, and then raises the error. Each
wait is at least the `Retry-After` value (nginx sends 10 seconds) plus up to 50%
jitter; when the server asks for less, waits double from 1 second up to 8. A busy
409 that reports a map swap (`X-Map-Swap-Seconds`) extends the window once by that
many seconds. Nginx refuses these 429 requests before proxying them, so resending
is safe. Streamed uploads are never resent; responses without `Retry-After`
(including 413), transport failures, and 5xx responses are not automatically
retried. A 507 means the
database free-space gate refused the write: alert and wait for space to be freed
or capacity increased before retrying. Fix invalid input (400/422), authentication
(401), permissions (403), or a missing dataset (404) before retrying.

Avoid an explicit statistics refresh or a `with dataset:` block in this continuous
ingest task: background upkeep consumes transactional pending work incrementally.
Separate `upload_embeddings` / `upload_patch_embeddings` calls reject invalid
sample IDs with 422 and `detail.unknown_sample_ids`. The SDK warns, drops exactly
those IDs, and resends the rest of that chunk once, allowing jobs to finish when
samples are deleted during computation. Upload and compute results report `dropped`
(embedding/patch rows) and `dropped_sample_ids` (unique IDs). Other failures,
including a second rejection, still raise. SDK chunks commit independently,
as do separate add calls.
Use inline artifacts to make the sample-add step atomic on write failure. Pre-create
any native array fields outside the retrying add step.

##### Querying Samples (DatasetView API)

Freedom Ridge provides three intuitive ways to query and filter samples:

###### Method 1: Direct Attribute Access (Most Pythonic)

```python
s = dataset.samples

# Simple comparisons
view = s[s.brightness > 0.5]
view = s[s.scene == "highway"]

# AND - use &
view = s[(s.brightness > 0.5) & (s.scene == "highway")]

# OR - use |
view = s[(s.weather == "rainy") | (s.weather == "foggy")]

# NOT - use ~
view = s[~(s.scene == "parking")]

# IN - use is_in()
view = s[s.scene.is_in(["highway", "city", "residential"])]

# Complex queries
view = s[
    ((s.scene == "highway") | (s.scene == "city")) &
    (s.brightness > 0.5) &
    ~(s.weather == "sunny")
]
```

###### Method 2: Filter Query Strings (Most Readable)

The same filter query language the app's filter cards take ([FILTER_QUERY.md](FILTER_QUERY.md)). The
server parses it and checks every field, operator and value against the dataset, so a misspelt field
raises `ValueError` with the line, the column and a suggestion instead of matching nothing.

```python
# Simple
view = dataset.query("brightness > 0.5")

# AND/OR (AND binds tighter than OR, as in SQL)
view = dataset.query("scene = 'highway' AND weather = 'rainy'")
view = dataset.query("scene = 'highway' OR scene = 'city'")

# Lists: commas or any whitespace between values, so a pasted column works as is
view = dataset.query("scene IN ('highway', 'city', 'residential')")
view = dataset.query("""serial in (
    103000752  103001025  103004080
    103004120  103004128
)""")

# Ranges
view = dataset.query("brightness BETWEEN 0.3 AND 0.8")

# Case-insensitive substring: CONTAINS (or LIKE '%...%')
view = dataset.query("filepath CONTAINS 'img_00'")

# Missing or empty: IS NULL / IS NOT NULL, EXISTS / NOT EXISTS
view = dataset.query("reviewed IS NULL")

# NOT and <> (same as !=)
view = dataset.query("NOT scene = 'highway'")
view = dataset.query("scene <> 'highway'")

# Complex
view = dataset.query("""
    (scene = 'highway' OR scene = 'city')
    AND brightness > 0.5
    AND weather != 'sunny'
""")

# A saved view from the same text: each top-level AND part is one chip in the app
dataset.create_view("Highway serials", query="scene = 'highway' AND serial in (103000752 103001025)")
```

###### Method 3: Django-style Keyword Args (Simplest)

```python
# Simple equality
view = dataset.filter(scene="highway")

# Multiple conditions (AND)
view = dataset.filter(scene="highway", timeofday="daytime")

# Using operators with double underscore
view = dataset.filter(brightness__gt=0.5)        # > 0.5
view = dataset.filter(brightness__gte=0.5)       # >= 0.5
view = dataset.filter(brightness__lt=0.5)        # < 0.5
view = dataset.filter(brightness__lte=0.5)       # <= 0.5
view = dataset.filter(scene__in=["highway", "city"])  # IN list
view = dataset.filter(tags__contains="validated")     # contains
```

| Suffix | Meaning |
|--------|---------|
| (none) | equals |
| `__gt`, `__gte`, `__lt`, `__lte` | numeric or date comparison |
| `__ne` | not equal (samples without the field are included) |
| `__in`, `__nin` (or `__not_in`) | in / not in a list |
| `__contains` | case-insensitive substring on text; membership on `tags` and other array fields |
| `__exists=True/False` | the metadata key is present / absent |
| `__is_null=True/False` | the value is null or missing / has a value |

An unknown suffix (for example `brightness__between=[0.3, 0.8]`) raises
`ValueError` instead of being treated as a field name. Conditions can also be
passed positionally: `dataset.filter(dataset.samples.brightness > 0.5)`.

###### Chaining Operations

All views are chainable:

```python
view = (dataset
    .filter(scene="highway")
    .order_by("brightness", descending=True)
    .limit(100)
    .skip(50))

# Iterate over results
for sample in view:
    print(sample.filepath)

# Count every match: call count() (or len()) on the view before limit()/skip()
total = dataset.filter(scene="highway").count()
print(f"Found {total} samples")

# Get first match
first = view.first()

# Convert to list
all_samples = view.to_list()

# Get all IDs
ids = view.ids()
```

##### Iterating / paginating samples

Use the DatasetView API above, or `iter_samples()` to stream the whole dataset
efficiently (keyset pagination under the hood):

```python
# Stream every sample (optionally with a progress bar)
for sample in dataset.iter_samples(progress=True):
    process(sample)

# Page a view explicitly
page_1 = dataset.all().skip(0).limit(100).to_list()
page_2 = dataset.all().skip(100).limit(100).to_list()

# Low-level paginated fetch (raw dicts) is also available on the client:
data = dataset.client.get_samples(dataset.id, limit=100, offset=0)
```

##### `compute_embeddings(model_name="clip-ViT-B-32", field_name="clip", ...) -> dict`

Compute embeddings for all samples using the specified model (needs the
`embeddings` extra). Returns `{"total", "written", "dropped", "dropped_sample_ids"}`.

```python
# Compute CLIP embeddings
dataset.compute_embeddings(
    model_name="clip-ViT-B-32",
    field_name="clip"
)

# Compute with different model
dataset.compute_embeddings(
    model_name="clip-ViT-L-14",
    field_name="clip_large"
)
```

`model_name` is any sentence-transformers model id, for example:
- `clip-ViT-B-32` - CLIP ViT-B/32 (512-dim)
- `clip-ViT-B-16` - CLIP ViT-B/16 (512-dim)
- `clip-ViT-L-14` - CLIP ViT-L/14 (768-dim)

With `model_source="huggingface"` (needs the `huggingface-embeddings` extra), `model_name` is a
Transformers image model with a pooled output or `get_image_features`, run through its own processor:
DINOv2, DINOv3 (its CLS token), ViT, CLIP, SigLIP. These vectors are stored as the model outputs them,
not L2-normalized (Freedom Ridge compares by cosine either way), so they match what other tools running
the same checkpoint store.

```python
# DINOv3, only for samples that don't have it yet (1024-dim)
dataset.compute_embeddings(
    model_name="facebook/dinov3-vitl16-pretrain-sat493m",
    field_name="dinov3",
    model_source="huggingface",
    skip_existing=True,
)
```

An embedding field's dimension is fixed server-wide by its first vectors, across
every dataset, so name fields after the model (`clip` for ViT-B/32, `clip_large`
for ViT-L/14) and never reuse a name for a different model.

##### Deleting a dataset

```python
dataset.delete()   # admin only; waits for the server deletion job
```

Deletion runs as a durable server job (the API answers 202). `delete()` waits for
it (`timeout=1800`); `wait=False` returns the `Job`. If the job stops part-way,
`dataset.resume_deletion()` finishes it.

**⚠️ Warning:** This permanently deletes all samples, embeddings, and metadata.

##### Counting samples

`Dataset` has no `len()`. Count through a view:

```python
count = dataset.all().count()   # or len(dataset.all())
print(f"Dataset has {count} samples")
```

---

### Sample

The `Sample` class represents a single data point in your dataset.

#### Creating a Sample

```python
from fridge import Sample

sample = Sample(
    filepath="/path/to/image.jpg",
    metadata={
        "filename": "image.jpg",
        "category": "dog",
        "brightness": 0.75,
        "size": [1920, 1080]
    },
    tags=["training", "validated"],
    location={"type": "Point", "coordinates": [-122.4194, 37.7749]},
    embeddings={"clip": [0.1, 0.2, ...]},  # Optional pre-computed vectors, by field name
)
```

The positional order is `Sample(filepath, tags, metadata, location, heading,
embedding, embeddings, hashes, array_fields)`, so pass everything after
`filepath` by keyword. `embedding=[...]` is shorthand for `embeddings={"clip": [...]}`.

Local `filepath`s must resolve inside the backend's `FRIDGE_DATA_ROOT`; anything
else is not served. Use `s3://bucket/key` for objects in S3.

#### Sample Properties

```python
sample.id                # Sample ID (int, set after adding to dataset)
sample.filepath          # Path to media file (str)
sample.media_type        # Media type: "image", "video", etc. (str)
sample.metadata          # Custom metadata (dict)
sample.tags              # List of tags (list[str])
sample.location          # GeoJSON point (dict | None)
sample.heading           # Compass heading in degrees (float | None)
sample.embeddings        # {field_name: vector} to upload (dict); empty on samples read back
sample.hashes            # Hashes to upload (dict[str, str], e.g. {"dhash_8": "abc123"}); empty on samples read back
sample.created_at        # Creation timestamp (ISO-8601 string)
sample.last_modified_at  # Last update timestamp (ISO-8601 string)
```

#### Working with Sample Fields

Freedom Ridge supports dynamic field assignment. You can add custom fields to samples using either attribute assignment or dictionary access:

```python
# Method 1: Direct attribute assignment
sample.weather = "sunny"
sample.temperature = 75.5
sample.scene = "highway"

# Method 2: Via metadata dictionary (also works!)
sample.metadata["speed"] = 65
sample.metadata["num_vehicles"] = 3

# Both methods are equivalent!
print(sample.weather)  # "sunny"
print(sample.metadata["weather"])  # "sunny"
print(sample.speed)  # 65
print(sample.metadata["speed"])  # 65
```

**How it works:**
- Custom fields are automatically stored in the `metadata` dictionary
- Both `sample.field` and `sample.metadata["field"]` access the same data
- No need to pre-declare fields - add them dynamically as needed
- Fields automatically persist when you add the sample to a dataset

**Examples:**

```python
# Add any type of field
sample.quality_score = 8.5  # float
sample.annotator_name = "Alice"  # string
sample.contains_errors = False  # boolean
sample.object_count = 42  # integer
sample.categories = ["person", "car"]  # list
sample.extra_data = {"key": "value"}  # dict

# All fields accessible via both methods
assert sample.quality_score == sample.metadata["quality_score"]
assert sample.categories == sample.metadata["categories"]
```

#### Iterating Through Samples

Use `iter_samples()` on any view for efficient iteration with optional progress bars:

```python
# Basic iteration
for sample in dataset.all():
    print(sample.filepath)

# With progress bar
for sample in dataset.all().iter_samples(progress=True):
    process(sample)

# Filtered iteration with progress
for sample in dataset.filter(scene="highway").iter_samples(progress=True):
    process(sample)
```

##### Efficient Field Selection

Use `select_fields()` to fetch only some columns. It saves work only when every
selected field is one of `id`, `filepath`, `media_type`, `tags`, `created_at`,
`last_modified_at`, `metadata` or a registered array field:

```python
# Only fetch id and filepath (skips the metadata JSON for every row)
for sample in dataset.all().select_fields(["id", "filepath"]).iter_samples(progress=True):
    print(sample.filepath)
```

Selecting a metadata key such as `scene`, or `location`/`heading`, falls back to
full rows: the call still works, but is no faster.

##### Auto-save Modifications

Use `autosave=True` to automatically save changes after each iteration:

```python
# Automatically save all modifications
for sample in dataset.filter(scene="highway").iter_samples(autosave=True):
    sample.tags.append("processed")
    # No need to call sample.save() - it's automatic!
```

##### Batch Size Tuning

A view's `iter_samples()` fetches pages of the view's `limit()` if one is set,
otherwise 1000 rows (`dataset.iter_samples()` always defaults to 1000):

```python
# Custom batch size for memory-constrained environments
for sample in view.iter_samples(batch_size=100):
    process(sample)

# Larger batches for faster processing (if you have memory)
for sample in view.iter_samples(batch_size=5000):
    process(sample)
```


#### Metadata Schema

Metadata is a flexible JSON object. Common patterns:

```python
metadata = {
    # File info
    "filename": "cat_001.jpg",
    "file_size": 1024000,
    "file_date": "2024-03-15",
    
    # Image properties
    "width": 1920,
    "height": 1080,
    "brightness": 0.75,
    "aspect_ratio": 1.778,
    
    # ML annotations
    "detections": [
        {"bbox": [10, 20, 100, 200], "label": "cat", "confidence": 0.95}
    ],
    "classification": {"label": "cat", "confidence": 0.98},
    
    # Custom fields
    "quality_score": 8.5,
    "annotator": "alice",
    "notes": "High quality sample"
}
```

#### Location

Location is a GeoJSON Point or a `(longitude, latitude)` pair:

```python
location = {
    "type": "Point",
    "coordinates": [longitude, latitude]  # Note: longitude first!
}

# San Francisco, both forms
location = {"type": "Point", "coordinates": [-122.4194, 37.7749]}
location = (-122.4194, 37.7749)
```

Anything else raises `ValueError` before a request is sent. Samples read back
always carry the GeoJSON form.

---

## Sample Modification

Freedom Ridge supports modifying samples after they've been added to a dataset.

### Saving Individual Samples

```python
# Get a sample and modify it
sample = dataset.filter(scene="highway").first()

# Modify tags
sample.tags.append("reviewed")
sample.tags.remove("pending")

# Modify metadata
sample.metadata["quality_score"] = 9.5
sample.metadata["reviewed_by"] = "alice"

# Or use attribute access
sample.quality_score = 9.5

# Save changes
sample.save()
```

### Bulk Tagging

```python
# Get sample IDs from a view
view = dataset.filter(scene="highway").limit(100)
ids = view.ids()

# Add tags to multiple samples
dataset.tag_samples(ids, ["processed", "batch_1"])

# Remove tags from multiple samples
dataset.untag_samples(ids, ["pending"])
```

### Bulk Metadata Updates

```python
# Set ONE field to the SAME value on many samples — a single bulk request
view = dataset.filter(scene="highway").limit(100)
dataset.set_values(view.ids(), "reviewed", True)

# Set a DIFFERENT value per sample (e.g. scores)
dataset.set_values_per_sample("priority", {12: 0.9, 15: 0.3, 21: 0.7})
```

---

## Aggregations

Compute statistics on a view. All run **server-side** and respect the view's
filters, but not its `limit()`, `skip()` or `order_by()`: they always cover every
matching sample. `sum`, `mean` and `std` return `0.0` when nothing matches.
Multiple aggregations can be batched into one request with `aggregate()`.

### Count Values / Distinct (exact)

```python
# Exact, discrete per-value counts (uncapped by default; pass limit=N for top-N)
view = dataset.filter(weather="clear")
scene_counts = view.count_values("scene")
# {"highway": 1500, "city street": 800, "residential": 500}

# Distinct values (exact, uncapped)
weathers = dataset.all().distinct("weather")
# ["clear", "rainy", "foggy", "snowy", ...]
```

> `count_values`/`distinct` return **exact discrete** values (not bucketed or
> top-50-capped). For numeric binning use `histogram_values()`.

### Numeric statistics

```python
lo, hi = dataset.all().bounds("speed")           # exact min/max
avg    = dataset.filter(scene="highway").mean("speed")
total  = dataset.filter(scene="highway").sum("speed")
sd     = dataset.all().std("speed")
qs     = dataset.all().quantiles("speed", [0.1, 0.5, 0.9])
# {0.1: 12.0, 0.5: 47.5, 0.9: 88.0}
```

### Histogram

```python
hist = dataset.all().histogram_values("speed", bins=10)
# [{"key": 1, "count": 1000, "label": "0.00 - 5.00", "range": {"min": 0.0, "max": 5.0}}, ...]
```

### Batched aggregations (one request)

```python
results = dataset.filter(scene="highway").aggregate([
    {"field": "speed", "op": "mean"},
    {"field": "speed", "op": "std"},
    {"field": "speed", "op": "quantiles", "quantiles": [0.25, 0.5, 0.75]},
    {"field": "weather", "op": "count_values"},
])
# results is a list, one entry per aggregation (each {"op", "field", "value"})
```

Supported ops: `count`, `sum`, `mean`, `std`, `min`, `max`, `bounds`,
`quantiles` (numeric); `count_values`, `distinct`, `values` (categorical/any,
with optional `limit`). `view.schema()` returns the dataset field schema.

---

## Dataset Quality & Curation

Score and curate datasets from their embeddings (compute embeddings first). Each
`compute_*` writes a per-sample score into metadata you can then sort/filter on,
then refreshes field stats. `compute_uniqueness`, `compute_representativeness` and
`find_duplicates` run SERVER-SIDE over the per-field HNSW index (build it once with
`dataset.build_ann_index("clip")` or `scripts/build_ann_indexes.py`) — chunked,
bounded memory, resumable, and they score ALL samples (no cap). The index build is
a server job (Jobs panel: progress, cancel); `build_ann_index` waits for it
(`timeout=3700`) and returns `{"field", "dim", "index", "built", "seconds"}`, or
`{"built": False, "detail": "index already exists"}`; `wait=False` returns a `Job`. `find_unique` (farthest-point) stays client-side and is
approximate-by-sample (`sample_size=`, default 200k; `-1` for all). `max_samples=`
is accepted but ignored on the server-side methods.

```python
# How unique/redundant each sample is (cosine k-NN distance, normalized 0..1)
dataset.compute_uniqueness(field_name="clip")          # -> metadata["uniqueness"]
most_unique = dataset.all().order_by("uniqueness", descending=True).limit(50)

# How representative (close to the data centroid) each sample is
dataset.compute_representativeness(field_name="clip")  # -> metadata["representativeness"]

# Prediction difficulty from a stored logits/probabilities field (normalized
# softmax entropy). You must have stored model outputs per sample.
dataset.compute_hardness("pred_logits")                # -> metadata["hardness"]

# Pick a diverse coreset of N samples (farthest-point sampling) to label first
ids = dataset.find_unique(500, field_name="clip")
dataset.tag_samples(ids, ["to_label"])
```

## Duplicate Detection

Freedom Ridge finds duplicates three ways — exact pixel, exact perceptual-hash, and
embedding (near-duplicate):

```python
# 1) Exact, pixel-identical duplicates (ignores file format / re-compression)
dataset.compute_content_hashes()                 # MD5 of decoded pixels -> "md5" hash field
res = dataset.find_exact_duplicates()            # one page (limit=50) of groups
# {"groups": [{"sample_ids": [...], "size": N, ...}, ...], "total_groups": T, "field_name": "md5"}
next_page = dataset.find_exact_duplicates(offset=50)

# 2) Exact perceptual-hash duplicates (run compute_hashes() first); same paged dict
res = dataset.find_duplicate_hashes("phash_8")

# 3) Embedding near-duplicates (semantic; needs embeddings)
groups = dataset.find_duplicates(field_name="clip", thresh=0.04)
# groups is a list of sample-ID lists (size >= 2)

# Exact duplicates of ONE sample (powers the UI "Show Duplicates" button)
dups = dataset.sample_duplicates(sample_id)            # {"sample_ids": [...]}; "_any" field by default
dups = dataset.sample_duplicates(sample_id, "_all")    # identical on every hash field
dups = dataset.sample_duplicates(sample_id, "phash_8") # identical on one specific field

# Browse / clean up the redundant copies (keeps one representative per group)
dups = dataset.duplicates_view(groups, keep_first=True)
for sample in dups:
    print(sample.id, sample.filepath)
```

---

## Structured Labels

Attach typed labels to samples. They render in the grid/modal (boxes, polygons,
keypoints, masks, heatmaps) and are filterable per-object. The classes are thin
wrappers over the metadata conventions — they serialize to plain JSON, so they
upload exactly like dicts with `add_samples`, no conversion needed.

```python
from fridge import (Sample, Detection, Detections, Classification, Classifications,
                    Polyline, Polygon, Polylines, Keypoint, Keypoints,
                    Segmentation, Heatmap)

sample = Sample("/img.jpg")

# Object detections — bbox is [x, y, width, height], normalized (0-1) or pixels
sample.predictions = Detections([
    Detection("car", bbox=[0.1, 0.2, 0.3, 0.4], confidence=0.92),
    Detection("person", bbox=[0.5, 0.1, 0.1, 0.3], confidence=0.71, occluded=True),
])

# Classification (single) or Classifications (multi-label)
sample.ground_truth = Classification("highway", confidence=1.0)

# Polygons / polylines (points are [[x, y], ...]; Polygon = closed+filled)
sample.lanes = Polylines([Polyline("lane", points=[[0.1, 0.9], [0.4, 0.5]])])
sample.regions = [Polygon("field", points=[[0, 0], [1, 0], [1, 1], [0, 1]])]

# Keypoints (pose) and on-disk masks/heatmaps
sample.pose = Keypoints([Keypoint(points=[[0.5, 0.2], [0.5, 0.5]], label="spine")])
sample.seg = Segmentation("s3://bucket/mask.png", labels=["road", "car"])
sample.prob = Heatmap("s3://bucket/heat.png", range=(0, 255), colormap="viridis")

dataset.add_samples([sample])
```

Each object carries a stable-ish `id`, optional `confidence`, and arbitrary
`**attributes`. The same shapes can be hand-built as dicts if you prefer.

### Mask overlays and annotation colours

A mask is a metadata field whose value is `{"maskPath": "..."}`, which
`Segmentation` writes for you. A plain path string is not drawn as a mask. The mask
PNG follows the same rules as `filepath` (inside `FRIDGE_DATA_ROOT`, or `s3://`) and
may differ in size from the image; it is scaled to fit. Two kinds of mask are
drawn:

- **Index masks:** the red channel of each pixel is a class ID; 0 (or alpha 0)
  is background and is not drawn. Name the classes with integer-string keys.
- **RGB masks:** each pixel is painted in its own colour. Key the class names by
  `"#rrggbb"` colour to get a legend.

Class names and colours are dataset settings, stored in `segmentation_config`
(admin; the SDK call below is `PATCH /api/datasets/{id}`):

```python
sample = Sample("/data/images/img_001.jpg")
sample.segmentation = Segmentation("/data/masks/img_001.png")   # an index mask
dataset.add_samples([sample])

dataset.client.update_dataset(dataset.id, segmentation_config={
    # class names per mask field; default_mask_targets applies to every mask field
    "mask_targets": {"segmentation": {"1": "road", "2": "car", "3": "person"}},
    "default_mask_targets": {"1": "road", "2": "car"},
    "color_scheme": {
        "opacity": 0.5,                  # overlay opacity, 0-1
        "fields": {
            "segmentation": {            # mask colours by class ID
                "colorByAttribute": "label",
                "idColors": {"1": "#22c55e", "2": "#3b82f6", "3": "#ef4444"},
            },
            "predictions": {             # box or polygon colours by label
                "colorByAttribute": "label",
                "valueColors": {"car": "#3b82f6", "person": "#ef4444"},
            },
        },
    },
})
```

Each `fields` entry may also set its own `opacity`. Without colours, classes take
distinct colours from a built-in palette. Colours can also be edited in the
**Segmentation Settings** panel of the web app.

### Model predictions as mask overlays

Freedom Ridge never runs a model. Run it yourself, save its predictions as masks, and
add them as a mask field: each model version becomes an overlay you can switch on
next to the image's other masks.

- **Masks:** one index-mask PNG per image, at the model's output size; it is scaled
  to the image without blending classes. Put them where the server can read them:
  inside `FRIDGE_DATA_ROOT`, or in an S3 bucket that a storage configuration serving
  the dataset covers ([S3 permissions](S3_PERMISSIONS.md)). Give each model version
  a folder of its own and never overwrite a mask in place, since the server and
  browsers cache media by its path. Keep a record of the model (its source, commit,
  checksum, classes) in that folder too; Freedom Ridge does not store one.
- **The field name says which model it is.** The name is what the web app shows of
  a prediction, so make it complete, for example task, version and commit:
  `prediction_roads_v1_4_0_1a2b3c4`. Two models must never share a name. Field names
  are letters, digits and `_` (at most 128, starting with a letter or `_`). A `.`
  makes a nested field, so write a version's dots, and any `-`, as `_`.

```python
from pathlib import Path
from fridge import Segmentation

folder = "s3://my-predictions/roads/1a2b3c4"      # this model version's masks
field = "prediction_roads_v1_4_0_1a2b3c4"
dataset.set_values_per_sample_multi({          # the images the model ran on
    s.id: {field: Segmentation(f"{folder}/masks/{Path(s.filepath).stem}.png")}
    for s in view.iter_samples()
})

# Class names and colours: add this field's entries. update_dataset replaces the whole
# segmentation_config, so start from the current one.
config = dataset.client.get_dataset_by_id(dataset.id).get("segmentation_config") or {}
config.setdefault("mask_targets", {})[field] = {"1": "road", "2": "car"}
config.setdefault("color_scheme", {}).setdefault("fields", {})[field] = {
    "colorByAttribute": "label",
    "idColors": {"1": "#22c55e", "2": "#3b82f6"},
}
dataset.client.update_dataset(dataset.id, segmentation_config=config)
```

Each version is its own toggle under **Overlays** in the sample view, so versions are
compared by switching them on one at a time or together. To remove one, call
`dataset.clear_field(field)` and then delete its entries from `segmentation_config`
the same way: clearing a field leaves them, so a field that is cleared and written
again keeps its class names and colours. `rename_field` carries them to the new name
and `clone_field` copies them.

A prediction field is a value in each predicted sample's metadata. Predicting the
images you will review (a view of thousands) is cheap; writing a field for every
model version across millions of samples rewrites all those rows and grows the
metadata indexes each time.

### Per-object filtering

Filter on label fields — by label, and by multiple per-object fields together:

```python
from fridge import F, match_elements

# Single per-object field (dotted) via Django-style kwargs
view = dataset.filter(**{"detections.label": "car"})
view = dataset.filter(**{"detections.confidence__gt": 0.9})

# Multiple conditions on the SAME object (label AND confidence)
s = dataset.samples
view = s[match_elements("detections", (F("label") == "car") & (F("confidence") > 0.9))]
```

## Evaluation

Compare a predictions label field against a ground-truth field. Per-sample results
are written to metadata (so you can sort/filter — e.g. find the false negatives),
and a summary report is returned.

```python
# Detections: greedy IoU matching → precision/recall, per-class AP, mAP
report = dataset.evaluate_detections("predictions", "ground_truth", iou=0.5)
print(report["mAP"], report["precision"], report["recall"])
# per sample: metadata["eval_tp"], ["eval_fp"], ["eval_fn"]
fn_samples = dataset.filter(eval_fn__gt=0)        # samples with missed objects

# Classifications: accuracy + confusion matrix (rows=GT, cols=pred)
report = dataset.evaluate_classifications("pred", "gt")
print(report["accuracy"], report["confusion_matrix"])
mistakes = dataset.filter(eval="incorrect")

# Segmentations: dataset-wide pixel accuracy + global mean IoU (index masks)
report = dataset.evaluate_segmentations("pred_mask", "gt_mask")
```

### Label analysis & patches

```python
# Likely label errors: confident predictions that disagree with ground truth
dataset.compute_mistakenness("predictions", "ground_truth")   # -> metadata["mistakenness"]
review = dataset.all().order_by("mistakenness", descending=True).limit(100)

# Train/test leakage: near-duplicate samples in different splits
leaks = dataset.compute_leaky_splits("split", field_name="clip")

# Explode detections into a new dataset of cropped per-object patches
patches = dataset.to_patches("predictions", dest_name="car_patches",
                             patches_dir="/data/patches")   # browsable in the grid
```

---

## Import & Export

Import standard CV formats into typed labels, and export any dataset or **view**
(a filtered subset) back out.

```python
ds = Dataset("coco_val")
ds.import_coco("/data/val2017", "/data/annotations/instances_val2017.json",
               label_field="ground_truth")
ds.import_yolo("/data/images", "/data/labels", classes=["car", "person"], label_field="gt")
ds.import_voc("/data/JPEGImages", "/data/Annotations", label_field="gt")
ds.import_classification_dir("/data/imagenet/train", label_field="gt")  # root/<class>/*.jpg

# Export detections (whole dataset, or a filtered view)
ds.export("/out/coco", format="coco", label_field="predictions")
ds.filter(scene="highway").export("/out/highway_yolo", format="yolo", label_field="predictions")
```

COCO/VOC use pixel bboxes; YOLO uses normalized — conversions are automatic.
To copy a dataset between two Freedom Ridge servers use
`python -m fridge.sync_between_instances`; `fridge.backup` dumps and restores the
whole database, not single datasets.

## Field Management & Merge

`dataset.promote_field("score")` indexes a field for fast filtering. The index
builds in a server job (`CREATE INDEX CONCURRENTLY`, so ingest continues); the
method waits for it and returns `{"message": ...}` (`timeout=3600`), or the `Job`
with `wait=False`. Another index operation on the dataset still answers 409.

```python
# Bulk metadata-key ops across every sample (one query each). A mask or label field's class
# names and colours (segmentation_config) move with a rename and are copied by a clone; a clear
# leaves them.
dataset.rename_field("conf", "confidence")
dataset.clone_field("predictions", "predictions_backup")
dataset.clear_field("scratch_field")

# Merge labels/metadata from another source, matched by key
preds = [Sample("/img1.jpg", metadata={"predictions": Detections([...])}), ...]
dataset.merge_samples(preds, key_field="filepath")   # source values win
dataset.merge_samples(preds, key_field="source_id", fields=["predictions"], skip_existing=True)

# merge_samples never ADDS samples. To copy samples between datasets (metadata, tags, location,
# heading, array fields, embeddings, hashes), adding the ones the target lacks:
target.add_samples_from(source.filter(split="eval"))          # a view, or a whole Dataset
target.add_samples_from(source.filter(source_id__in=ids), embedding_fields=["dinov3"], hash_fields=[])
# Matches by filepath; samples already in the target are left untouched. Streams in batches.
```

## Geospatial filtering

Filter by location using the PostGIS backend:

```python
from fridge import geo_near, geo_within
s = dataset.samples

near = s[geo_near([-122.42, 37.77], max_distance=5000)]   # within 5 km (metres)
region = s[geo_within([[-122.5, 37.7], [-122.35, 37.7], [-122.35, 37.82], [-122.5, 37.82]])]
```

---

## Similarity Search

Find visually similar samples using embedding vectors:

### Text (Natural-Language) Search

Describe what you want; the prompt is CLIP-text-encoded (server-side when the
server has transformers, otherwise locally via sentence-transformers) and ranked
against the field's image embeddings:

```python
rivers = dataset.all().sort_by_similarity(text="a river through farmland", limit=50)
```

The field must be CLIP-family. Fields embedded by text-less models (DINO etc.)
are refused — register each field's checkpoint once so the guard knows:

```python
dataset.client.register_field_model(dataset.id, "clip", "openai/clip-vit-base-patch32")
# compute_embeddings() registers its own checkpoint automatically
```

### Basic Similarity Search

```python
# Get a reference sample
ref_sample = dataset.filter(scene="highway").first()

# Find similar samples across the dataset
similar = dataset.all().sort_by_similarity(ref_sample, limit=20)

for sample in similar:
    print(f"ID: {sample.id}, distance: {sample.distance:.4f}")
```

The result is a chainable view: you can further `filter`/`order_by`/`limit` it
and call aggregations — all evaluated in-memory over the results, preserving the
distance ordering:

```python
similar = (dataset.all()
    .sort_by_similarity(ref_sample, limit=200)
    .filter(dataset.samples.weather == "rainy")
    .limit(20))
rainy_counts = similar.count_values("scene")
```

### Scoped Similarity Search

Search for similar samples within a filtered view:

```python
# Find highway-like images within city street scenes
ref_sample = dataset.filter(scene="highway").first()
city_view = dataset.filter(scene="city street")

# Find city street images most similar to highway
similar_in_city = city_view.sort_by_similarity(ref_sample, limit=20)

for sample in similar_in_city:
    print(f"{sample.id}: {sample.distance:.4f} - {sample.scene}")
```

### Using Sample ID or Vector

```python
# By sample ID
similar = dataset.all().sort_by_similarity(sample_id=12345, limit=20)

# By raw vector from your own model (same dimension as the field)
similar = dataset.all().sort_by_similarity(vector=my_vector, limit=20)

# By text (see Text Search above)
similar = dataset.all().sort_by_similarity(text="a sunny highway", limit=20)
```

Without `limit`, a similarity search returns the view's `limit()` if set,
otherwise 50 results.

### Different Embedding Fields

```python
# Use a specific embedding field
similar = dataset.all().sort_by_similarity(
    ref_sample, 
    field_name="clip_large",  # Use ViT-L/14 embeddings instead of default
    limit=20
)
```

---

## Concepts (Learned Scores)

A concept is a direction in an embedding field's space that scores every sample
0–1 by how much it matches. Teach it with example clicks, a learned boundary, or
a text prompt — then exclude / show-only / rank by it in the UI (Concepts panel)
or filter by it in the SDK.

```python
# From examples: average the selected samples into a prototype ('seed')
water = [s.id for s in ds.samples[ds.samples.label == "SeaLake"].limit(5)]
concept = ds.create_concept("open-water", seed_ids=water)

# Sharper: fit a boundary against hard negatives ('probe')
forest = [s.id for s in ds.samples[ds.samples.label == "Forest"].limit(3)]
concept = ds.create_concept("open-water", seed_ids=water, neg_ids=forest, kind="probe")

# Or describe it — no clicks needed ('text', CLIP-encoded)
concept = ds.create_concept("river-like", prompt="a river crossing the landscape")

ds.concept_preview(concept["id"], op=">", threshold=0.7)  # -> {"matched": N}
ds.concepts()                        # list saved concepts
ds.rescore_concept(concept["id"])    # full rescore (e.g. after new data)
ds.refine_concept(...)               # ± corrections from the boundary
ds.delete_concept(concept["id"])
```

New samples are scored automatically by the hourly worker when a concept has
auto-rescore enabled (the default — toggle per concept in the Concepts panel).

Creating, rebuilding (new seeds or weight), refining and rescoring a concept score
every sample, which takes minutes on millions of samples. Each runs as a server
job: the HTTP request answers within five seconds, and these methods wait for the
job and return the same result as before (the concept dict, or
`{"concept_id", "scored"}` for `rescore_concept`). `timeout=` bounds the wait
(default 1800 s) and never cancels the job; `wait=False` returns a `Job` at once
(`job.wait()`, `job.status()`, `job.cancel()`). A threshold-only
`rebuild_concept(concept_id, threshold=...)` stays synchronous.

```python
job = ds.rescore_concept(concept["id"], wait=False)   # -> Job
job.wait(timeout=3600)                                # -> {"concept_id": ..., "scored": N}
```

While a definition change is being scored, the concept is not ready: filters
using it answer 409 instead of mixing scores of two definitions, and
`ds.concepts()` reports `scores_ready=False` with its `score_job`. A cancelled or
failed scoring job keeps its checkpoint; resume it with
`client.resume_bulk_job(job_id)` (the generic `POST /api/jobs/{id}/resume`) or the
Resume button on the concept card. One explicit operation owns a concept at a time:
a second one while it runs answers 409.

## Derived-Field Coverage

Freedom Ridge's uploader-owns policy means embeddings/hashes/locations exist only if
something computed them. `coverage()` reports per-field present/missing counts;
`missing()` pages the samples still lacking an artifact (a resumable backfill
contract); `compute_all()` runs the whole first-run chain with skip-if-complete.

```python
ds.coverage()
# {"total_samples": 27000,
#  "embeddings": [{"field": "clip", "present": 27000, "missing": 0}],
#  "hashes": [...], "location": {"present": 27000, "missing": 0}}

while True:
    page = ds.missing("embedding", field="clip", limit=1000)  # {"items": [...], "total": N}
    if not page["items"]:
        break
    backfill(page["items"])  # compute + upload; filled rows drop out of the next page

ds.compute_all()  # metadata -> embeddings -> UMAP -> hashes -> metrics, skipping complete steps

# Individually:
ds.compute_metadata()  # width / height / aspect_ratio / megapixels / file_size_bytes (base deps)
ds.compute_metrics()   # brightness / blurriness / contrast / entropy / salt_and_pepper /
                       # saturation / pct_underexposed / pct_overexposed
```

### Deleting a field's vectors

`delete_embeddings(field, sample_ids, *, wait=True, timeout=1800)` removes one embedding field's
vectors and nothing else (admin only). Use it to recompute a model's vectors for chosen samples: delete
them, list the cleared samples with `missing()`, upload new vectors.

```python
ds.delete_embeddings("dino", bad_ids)          # {"deleted": 1850000}; bad_ids is a list of sample ids
ds.filter(batch="b7").delete_embeddings("dino")  # a view, or ds.delete_embeddings("dino", view)
ds.delete_embedding_field("dino")              # every sample of the dataset (the whole field)

while True:                                    # fill-in: page the cleared samples by id
    page = ds.missing("embedding", field="dino", limit=1000, after_id=last_id)
    ...
    ds.client.upload_embeddings(ds.id, ...)    # same dimension; queued for map projection like new vectors
```

- It runs as a resumable runner job in chunks of 1,000 samples, with progress; cancelling keeps the
  chunks already deleted, and `client.resume_bulk_job(job_id)` runs the rest. It shares the dataset's
  bulk-job slot, so it waits for or is refused (409, retried by the SDK) behind another bulk job.
- A list of ids is sent 50,000 per request, each request a job that is waited for before the next;
  the result sums them (`{"deleted": n}`). `wait=False` returns the Job and is refused when the ids need
  more than one request. An empty list deletes nothing. `deleted` counts vectors, so ids that had none
  (or belong to another dataset) add nothing and deleting twice returns `{"deleted": 0}`.
- Sample metadata is never touched, including fields such as `uniqueness_<model>`; recompute those
  with `compute_uniqueness` after refilling.
- Deleting a vector removes its map position (`embedding_coordinates`), its queued projection and its
  scatter rank row, and the field's map point count follows. Vectors uploaded later are queued for projection
  like new ones and no stale position remains. The field's map state and model are kept, so with a scheduled
  re-fit the new vectors are placed with the field's current map model.
- The field's ANN index (`hnsw_emb_<field>`, hashed when the name is long) is maintained by the delete and the later uploads: similarity
  search and `compute_uniqueness` see only the current vectors, with no rebuild.
- Concept scores of the field are rescored by the background upkeep (the delete queues it): a sample
  without a vector loses its score there, not in the delete.
- `field` is the stored field name, as in `upload_embeddings`. A patch field's tiles are separate names
  (`<field>_patch_<scale>_r<row>c<col>`); a name that has no vectors in the dataset deletes nothing
  (`{"deleted": 0}`).

- Don't upload new vectors for the targeted samples until the delete job has succeeded or been
  discarded. A unit that hasn't run yet deletes whatever vector its samples have when it runs, a new one
  included; that applies to a stopped job resumed later too.
- Deleted vectors and index entries free their disk only after PostgreSQL vacuums the table and the
  index, so refilling a field needs room for it about twice over for a while.

**Whole field.** Only `delete_embedding_field(field, *, wait=True, timeout=1800)` deletes every sample's
vectors. Everything else that would select every sample is refused: `sample_ids=None` raises `TypeError`,
a view without a filter raises `ValueError` (so does a view of another dataset), and the server answers
400 to a filter whose conditions have no value (`score__lt=None`), which would otherwise match every
sample. A list of ids together with a filter is refused too.

Deleting every vector of a field removes the vectors only, exactly like a partial
delete: the dataset's map state, map point count, the field's model record (`embeddings/field-model`,
shared by every dataset using the field name), any scheduled re-fit and the ANN index all stay. If the
field is refilled by a different model under the same name, re-fit the map or use a new field name.

**Dimension.** A field's dimension is fixed per field name across every dataset on the server, and its
ANN index is built for one dimension. While any dataset still holds vectors of the field the dimension
is known from them. Once none remain anywhere, a new dimension is refused with 422 if the field's ANN
index exists, because the index would reject the vectors on insert; the message names the index. Remove
the index (Indexes panel, admin) first, or use a new field name. With no index, any dimension is accepted
again.

To compute the same metrics for one PIL image yourself (for example before upload), use
`fridge.compute_image_metrics(img)`. It returns the dict of eight floats that `compute_metrics()`
stores per sample, and needs the same optional dependencies (`pip install "freedomridge[all]"`).

## Array Fields (Dynamic Tag-Like Fields)

Freedom Ridge supports creating any number of tag-like array fields beyond the built-in `tags`. These are stored as native PostgreSQL `TEXT[]` columns with GIN indexes — same performance as tags.

### Creating an Array Field

```python
# Create a new filterable array field
dataset.create_array_field("inferred_tags")
dataset.create_array_field("categories")

# List all array fields
fields = dataset.list_array_fields()
# ['tags', 'inferred_tags', 'categories']
```

### Setting Array Field Values

```python
# At creation time
sample = Sample(
    filepath="/path/to/image.jpg",
    tags=["training"],
    array_fields={
        "inferred_tags": ["OBJECT:CAR", "OBJECT:SIGN"],
        "categories": ["outdoor", "coastal"],
    }
)
dataset.add_samples([sample])

# Or via attribute assignment (after array field is created)
sample.inferred_tags = ["OBJECT:CAR", "OBJECT:SIGN"]
```

### Bulk Updating Array Fields

The bulk-tag call works for any array field:

```python
# Add values to inferred_tags for specific samples
dataset.client.bulk_tag_samples(
    dataset.id,
    sample_ids=[1, 2, 3],
    add_tags=["NEW_TAG"],
    field_name="inferred_tags",  # defaults to "tags" if omitted
)
```

### Filtering

Array fields appear in the UI filter sidebar automatically with multi-select checkboxes. In the SDK:

```python
# Filter by array field values
view = dataset.filter(inferred_tags__contains="OBJECT:CAR")
```

---

## Image Hashes

Perceptual hashes enable exact-duplicate detection (samples that share an identical hash). Hashes are stored in a separate optimized table.

### Adding Hashes at Creation

```python
sample = Sample(
    filepath="/path/to/image.jpg",
    hashes={
        "dhash_8": "a1b2c3d4e5f6a7b8",
        "phash_8": "1234567890abcdef",
    }
)
dataset.add_samples([sample])  # hashes uploaded automatically
```

### Setting Hashes on Existing Samples

```python
# Set the same hashes on multiple samples
dataset.set_hashes([id1, id2, id3], {"dhash_8": "abc123"})

# Or from a view
view = dataset.filter(tags__contains="needs_hashing")
dataset.set_hashes(view.ids(), {"dhash_8": "abc123", "phash_8": "def456"})
```

### Auto-Computing Hashes from Images

Computes missing perceptual hashes (dhash_8 + phash_8 by default) for image rows. Object rows are skipped,
images that already have every requested hash are never opened, and partly hashed images compute only the
missing ones; `overwrite=True` recomputes. Returns counts of computed, skipped, object-skipped and failed
images. Requires `imagehash` and either S3 credentials or local file access. The demo importers attach the
same hashes during import (`--no-hashes` turns that off), so this is for datasets imported without them.

```python
# Default: dhash_8 + phash_8
dataset.compute_hashes()

# Other standard hashes: dhash_N, phash_N, ahash_N, whash_N (custom hash functions are not accepted)
dataset.compute_hashes(algorithms=["dhash_8", "phash_16"])

# Custom image loader
from PIL import Image
dataset.compute_hashes(image_loader=lambda fp: Image.open(fp))
```

---

## Dataset Versioning

Create snapshots explicitly on the datasets that need them. Ordinary datasets incur no
per-edit history writes. Full snapshots preserve sample membership, annotation metadata,
tags, filepaths, media types, geography, dynamic arrays, and sample timestamps. They also
capture dataset media settings, segmentation class mappings, and registered field types
and display settings. Image hashes are compared but their values and embeddings are not
restored.

Snapshots made by older versions remain readable, but fields they never recorded
are unknown; current values are never substituted into historical reads.

Snapshots reference external media and masks. Keep those objects immutable and retained;
replacing the bytes at an existing path changes what any version can display.

### Create a Version

```python
dataset.create_version("v1.0", description="Initial annotated dataset")
```

Creation captures one consistent database state. A conflicting concurrent dataset
update returns HTTP 409 without saving a partial version; retry after the writer finishes.

### List Versions

```python
versions = dataset.list_versions()
for v in versions:
    print(f"{v['name']} - {v['sample_count']} samples - {v['created_at']}")
```

### Compare a Version Against Current State

`diff()` compares one version against the **current** dataset state:

```python
d = dataset.diff(version_name="v1.0")   # or diff(version_id=...)
print(f"Added: {d['added']}, Removed: {d['removed']}, Modified: {d['modified']}")
```

To diff two versions against each other, use the client:

```python
d = dataset.client.get_version_diff(dataset.id, version_a_id, version_b_id)
```

### Inspect a Version's Samples (checkout)

`checkout()` is a generator that yields each sample **as it was** at a given
version. It does not modify the dataset. Samples returned by checkout are read-only:
`sample.save()` raises `ValueError`. Load the live sample explicitly to edit it.
Iteration uses a sample-ID cursor and includes samples deleted from the live dataset.
Hash-only versions cannot be checked out or browsed.

```python
for sample in dataset.checkout(version_name="v1.0"):
    print(sample.filepath, sample.metadata.get("reviewed"))
```

### Query a Version (`dataset.version()`)

`dataset.version(name)` (no argument: the newest version) returns a **read-only**
`VersionView` that is queried like a `DatasetView`, but always against the version's
stored data. Every query runs in the database, so a multi-million-sample version is
never pulled to the client.

```python
from fridge import F

v = dataset.version("v1.8")
v.count()                       # samples in the version
v.distinct("source_id")         # distinct metadata values
v.count_values("split")         # {"train": ..., "val": ...}
split = v.filter(F("split").is_in(["train", "val"]))   # same filter DSL; chainable
for sample in split.select_fields(["source_id", "category"]):
    print(sample.id, sample.metadata)  # only those fields are read
```

Supported: `filter`/`match`/`exclude`, `select_fields`, `limit`, `iter_samples`, `first`,
`count`, `distinct`, `count_values`, `aggregate` (same specs as `DatasetView.aggregate`).
Samples come back in id order. Writes (`tag_samples`, `set_values`, ...) raise
`VersionReadOnlyError`; sorting, `skip`, histograms and similarity are not supported on a version
(`NotImplementedError`). Filters that read live data a version does not store (embedding or hash
presence, concept scores, duplicate collapsing, parent/object links) are rejected with a 400.

In Dataset Settings → Versions, choose **Browse snapshot** to inspect saved samples,
search their paths/tags/metadata, and view their annotations. Images, masks, heatmaps,
and display settings come from the selected version. Diff links open the saved state
for removed/modified samples, including samples that no longer exist in the live dataset.

To inspect the saved dataset schema and display configuration through the SDK:

```python
info = dataset.client.get_version(dataset.id, version_id)
settings = info["dataset_snapshot"]
```

Diff responses include `dataset_changed` separately from sample counts; `None` means
an older or hash-only version did not record dataset settings.

### Revert to a Version

`revert()` restores saved sample fields, atomically saving an undo snapshot first.
By default it preserves current sample membership. It leaves dataset settings,
embeddings, and image-hash values unchanged. Use checkout or the snapshot browser
for the exact historical dataset view.

```python
result = dataset.revert(version_name="v1.0")   # or revert(version_id=...)

result = dataset.revert(version_name="v1.0", restore_deleted=True)

result = dataset.revert(version_name="v1.0", exact_membership=True)
```

`exact_membership=True` also restores deleted samples and removes later additions;
it is an explicit destructive operation. It supports empty snapshots. Restores run
in a serializable transaction and return HTTP 409 on a concurrent-write or filepath
conflict, with all changes rolled back. Retry a concurrency conflict after the other
writer finishes. The returned `auto_snapshot_id` can be restored with
`exact_membership=True` to undo membership changes; deleted embeddings/hashes still
require backfilling.

### Check Current Status

```python
# See if the current state has uncommitted changes vs the latest version
status = dataset.version_status()
if status.get("has_changes"):
    print("Uncommitted changes detected")
```

---

## Embeddings Visualization

Compute UMAP reductions for the embeddings panel in the UI.

### Compute Visualization

```python
# Reduce embeddings to 2D coordinates for the scatter plot
dataset.compute_visualization(field_name="clip")

# The default fit_sample_size="auto" fits UMAP on as many samples as 75% of this
# machine's free RAM allows (all of them when they fit; at least 50,000), then places
# every sample. Pass an int to fit on fewer for speed, or -1 to require all.
dataset.compute_visualization(
    field_name="clip",
    fit_sample_size=500000,   # fit on 500k, place every sample
)

# Method: "umap" (default), "pca" (cheap/deterministic), or "tsne"
dataset.compute_visualization(field_name="clip", method="pca")
```

Managed maps change layout only through a re-fit. `compute_visualization` checks map state before
fetching vectors or fitting and skips managed fields with a clear message; `compute_all` continues
with hashes and metrics. Server-side `reduce_embeddings` and manual coordinate uploads refuse managed
replacements with HTTP 409. The server places new samples on every map, managed or not, from their nearest
mapped neighbours (the job runner's `map_place` jobs, within seconds of ingest), so no model is kept and no
projection step is needed after an ingest. A field with no map yet (`dataset.map_state(field)["current_points"]`
is 0) needs a first `compute_visualization` or re-fit. While `compute_visualization` lays out a map that
already has points, it holds the server's placement on it (renewed while it works, released when it is done),
so samples ingested meanwhile are placed on the new layout, not the old one.
Incremental writers pause only while a re-fit is active; full replacements also refuse queued re-fits.
Managed and stale-model refusals have distinct error codes and no retry headers. Existing unmanaged maps, such
as those written by external importers, retain their full-fit workflow.

### Refresh Stats

After modifying metadata or adding new fields, refresh stats so they appear in the UI filters:

```python
dataset.refresh_stats()
```

---

## Advanced Usage

### Batch Ingestion

For large datasets, use batch processing:

```python
from fridge import Dataset, Sample
from pathlib import Path

dataset = Dataset("large_dataset")

# Process in batches
batch_size = 1000
samples = []

for image_path in Path("/data/images").glob("*.jpg"):
    sample = Sample(
        filepath=str(image_path),
        metadata={"filename": image_path.name}
    )
    samples.append(sample)
    
    if len(samples) >= batch_size:
        dataset.add_samples(samples)
        print(f"Added {len(samples)} samples")
        samples = []

# Add remaining samples
if samples:
    dataset.add_samples(samples)

print(f"Total samples: {dataset.all().count()}")
```

### Complex Filtering

The filter system supports nested logic. Name metadata fields **without** a
`metadata.` prefix (`"brightness"`, not `"metadata.brightness"`): a prefixed name
is looked up as a key literally called `metadata.brightness`, so it matches
nothing (or, with `!=`, everything) and no error is raised.

```python
# AND: All conditions must match
filters = {
    "logic": "AND",
    "filters": [
        {"field": "brightness", "operator": ">=", "value": 0.3},
        {"field": "brightness", "operator": "<=", "value": 0.7},
        {"field": "tags", "operator": "in", "value": ["validated"]}
    ]
}

# OR: Any condition can match
filters = {
    "logic": "OR",
    "filters": [
        {"field": "category", "operator": "=", "value": "cat"},
        {"field": "category", "operator": "=", "value": "dog"}
    ]
}

# Nested: Combine AND/OR
filters = {
    "logic": "AND",
    "filters": [
        {
            "logic": "OR",
            "filters": [
                {"field": "tags", "operator": "in", "value": ["cat"]},
                {"field": "tags", "operator": "in", "value": ["dog"]}
            ]
        },
        {"field": "brightness", "operator": ">", "value": 0.5}
    ]
}

result = dataset.client.query_samples(dataset.id, filters=filters)
samples = result["items"]
```

The same dicts can be built for you: `dataset.filter(...)`, `dataset.query(...)`
and `dataset.samples[...]` all produce them.

#### Supported Operators

| Operator | Description | Example |
|----------|-------------|---------|
| `=` | Equals | `{"field": "category", "operator": "=", "value": "cat"}` |
| `!=` | Not equals (samples without the field are included) | `{"field": "category", "operator": "!=", "value": "dog"}` |
| `>` | Greater than | `{"field": "brightness", "operator": ">", "value": 0.5}` |
| `<` | Less than | `{"field": "brightness", "operator": "<", "value": 0.8}` |
| `>=` | Greater or equal | `{"field": "score", "operator": ">=", "value": 7.0}` |
| `<=` | Less or equal | `{"field": "score", "operator": "<=", "value": 9.0}` |
| `in` | Value in list | `{"field": "tags", "operator": "in", "value": ["validated"]}` |
| `not in` | Value not in list | `{"field": "tags", "operator": "not in", "value": ["rejected"]}` |
| `contains` | Case-insensitive substring on text; membership on array fields | `{"field": "filename", "operator": "contains", "value": "cat"}` |
| `exists` / `not_exists` | The metadata key is present / absent | `{"field": "reviewed", "operator": "exists"}` |
| `is_null` / `is_not_null` | The value is null or missing / has a value | `{"field": "score", "operator": "is_null"}` |
| `geo_polygon` | Location inside a polygon given as a list of `[lon, lat]` points (not a GeoJSON object) | `{"field": "location", "operator": "geo_polygon", "value": [[-122.5, 37.7], [-122.35, 37.7], [-122.35, 37.82], [-122.5, 37.82]]}` |
| `geo_near` | Location within `max_distance` metres of a point | `{"field": "location", "operator": "geo_near", "value": {"point": [-122.42, 37.77], "max_distance": 5000}}` |

### Custom Embeddings

Add pre-computed embeddings:

```python
import numpy as np
from fridge import Sample

# Pre-computed embedding (e.g., from your own model)
embedding = np.random.rand(512).tolist()

sample = Sample(
    filepath="/path/to/image.jpg",
    embeddings={"my_model": embedding},   # {field_name: vector}
)

dataset.add_samples([sample])
```

The first vectors stored under a field name fix its dimension for the whole
server; a later vector of another length is rejected with HTTP 422.

### Multiple Media Fields

Freedom Ridge supports multiple media sources per sample (e.g., full resolution + thumbnail).

**How it works:**
- `filepath` is a dedicated database column (always present)
- Additional media fields are stored in `metadata` but registered as `media_fields`
- The UI can switch between different media views

```python
# Create dataset with multiple media fields
dataset = Dataset(
    "my_dataset",
    media_fields=["filepath", "thumbnail", "preview"],  # Register which fields contain media
)

# Add sample with multiple media sources
# Note: Additional media fields go in metadata!
sample = Sample(
    filepath="/path/to/full_res.jpg",  # Main field (database column)
    metadata={
        "thumbnail": "/path/to/thumb.jpg",    # Stored in metadata
        "preview": "/path/to/preview.jpg"      # Stored in metadata
    }
)

dataset.add_samples([sample])
```

**In the UI:**
- Use the View selector to switch between `filepath`, `thumbnail`, and `preview`
- Each displays the corresponding image source
- Grid and modal automatically adapt to the selected media field

**Storage:**
- `filepath` → database column
- `thumbnail`, `preview` → `metadata.thumbnail`, `metadata.preview` (JSON)
- `media_fields` → tells UI which metadata fields contain displayable media


---

## Complete Examples

### Example 1: Image Classification Dataset

```python
from fridge import Dataset, Sample
from pathlib import Path
import json

# Create dataset
dataset = Dataset(
    "imagenet_subset",
    description="ImageNet subset for training",
)

# Load class mappings
with open("imagenet_classes.json") as f:
    classes = json.load(f)

# Ingest images, one batched request per class
for class_dir in Path("/data/imagenet").iterdir():
    class_name = class_dir.name
    samples = [
        Sample(
            filepath=str(img_path),
            metadata={
                "class_id": classes[class_name],
                "class_name": class_name,
                "split": "train"
            },
            tags=["imagenet", class_name]
        )
        for img_path in class_dir.glob("*.JPEG")
    ]
    dataset.add_samples(samples, on_duplicate="skip")

print(f"Dataset has {dataset.all().count()} images across {len(classes)} classes")

# Compute embeddings
dataset.compute_embeddings(model_name="clip-ViT-B-32")
```

### Example 2: Object Detection Dataset

```python
from fridge import Dataset

dataset = Dataset("coco_detections")

# One call: reads the COCO JSON and stores each image's boxes as Detections
# (category names as labels, the file's pixel [x, y, w, h] boxes) in
# metadata["ground_truth"]. Images are batched; re-running skips existing files.
dataset.import_coco(
    "/data/coco/val2017",
    "/data/coco/annotations/instances_val2017.json",
    label_field="ground_truth",
)

cars = dataset.filter(**{"ground_truth.label": "car"})
print(f"{cars.count()} images contain a car")
```

### Example 3: Geospatial Dataset

```python
from fridge import Dataset, Sample, geo_within

dataset = Dataset("geo_tagged_images")

# (filepath, longitude, latitude) from your own GPS source: EXIF, a CSV, a log...
records = [
    ("/data/geotagged/img_001.jpg", -122.4194, 37.7749),  # San Francisco
    ("/data/geotagged/img_002.jpg", -73.9857, 40.7484),   # New York
]
dataset.add_samples(
    [Sample(fp, location=(lon, lat), tags=["geotagged"]) for fp, lon, lat in records],
    on_duplicate="skip",
)

# Samples inside a polygon: a list of [lon, lat] points (the ring is closed for you)
san_francisco = [[-122.52, 37.70], [-122.35, 37.70], [-122.35, 37.82], [-122.52, 37.82]]
in_sf = dataset.samples[geo_within(san_francisco)]
print(f"Found {in_sf.count()} samples in San Francisco")

# The same filter as a raw dict. The value is the point list, NOT a GeoJSON
# Polygon object: a GeoJSON dict is read as "fewer than 3 points" and silently
# matches every sample.
result = dataset.client.query_samples(dataset.id, filters={
    "logic": "AND",
    "filters": [{"field": "location", "operator": "geo_polygon", "value": san_francisco}],
})
print(f"Found {result['total']} samples in San Francisco")
```

---

## API Reference

### Dataset Class

```python
class Dataset:
    # Creation and loading
    def __init__(name: str, description: str = None, media_fields: list[str] = None,
                 storage_config_id: int = None, is_global: bool = False)  # load-or-create by name; new datasets are private
    @classmethod
    def from_id(dataset_id: int) -> Dataset  # load existing by id (no create)

    # Properties
    id: int | None
    name: str
    description: str | None
    media_fields: list[str] | None
    created_at: str | None   # ISO-8601

    # Sample operations
    def add_samples(samples: list[Sample], on_duplicate="error") -> dict  # {"ids": [...]}
    def iter_samples(selected_fields=None, progress=False, batch_size=1000, filters=None) -> Iterator[Sample]
    def delete_sample(sample_id: int) -> dict
    def get_or_create(filepath: str, **kwargs) -> tuple[Sample, bool]
    def __getitem__(filepath: str) -> Sample            # dataset["/path/img.jpg"]
    # No __len__: use dataset.all().count()
    def delete(*, wait=True, timeout=1800) -> dict | Job          # admin only; a server job
    def resume_deletion(*, wait=True, timeout=1800) -> dict | Job

    # Querying (returns DatasetView)
    def all() -> DatasetView
    def filter(*conditions, **kwargs) -> DatasetView
    def query(query_string: str) -> DatasetView
    @property
    def samples -> SampleQuery        # for s[s.field == value] syntax

    # Bulk modification
    # sample_ids may be a DatasetView. Large targets run as resumable jobs; wait=False returns the Job.
    def tag_samples(sample_ids: list[int], tags: list[str], *, wait=True, timeout=1800) -> dict | Job
    def untag_samples(sample_ids: list[int], tags: list[str], *, wait=True, timeout=1800) -> dict | Job
    def set_values(sample_ids: list[int], field: str, value: Any, *, wait=True, timeout=1800) -> int | Job
    def delete_samples(sample_ids, *, wait=True, timeout=1800) -> dict | Job   # admin only
    # One embedding field's vectors only; see "Deleting a field's vectors". sample_ids: a list or a view
    def delete_embeddings(field, sample_ids, *, wait=True, timeout=1800) -> dict | Job   # admin only
    def delete_embedding_field(field, *, wait=True, timeout=1800) -> dict | Job   # every sample; admin only
    # Requests of at most 5,000 patches, each waited in turn; wait=False returns one Job per batch
    # (a single Job for one batch). Completed batches stay applied if a later one fails.
    def set_values_per_sample(field: str, values_by_id: dict[int, Any], batch_size=5000, *, wait=True,
                              timeout=1800) -> int | Job | list[Job]
    # A stopped (cancelled/failed) bulk job: client.resume_bulk_job(job_id) runs the rest;
    # client.discard_bulk_job(job_id) keeps completed chunks, never runs the rest and stops blocking revert.

    # Hashes & dedup
    def set_hashes(sample_ids: list[int], hashes: dict[str, str]) -> int
    def compute_hashes(image_loader=None, algorithms=None, batch_size=100, overwrite=False) -> dict
    def compute_content_hashes(field_name="md5", image_loader=None, batch_size=100) -> dict   # counts
    def find_duplicate_hashes(field_name, limit=50, offset=0, filters=None) -> dict
    def find_exact_duplicates(field_name="md5", limit=50, offset=0) -> dict
    def sample_duplicates(sample_id, field_name="_any") -> dict
    def find_duplicates(field_name="clip", thresh=0.04, max_samples=None, k=20, ef_search=64, chunk=5000,
                        group_field="emb_dup_group") -> list[list[int]]
    def duplicates_view(groups, keep_first=True) -> DatasetView

    # Quality / curation (from embeddings; write a per-sample score)
    # uniqueness/representativeness/find_duplicates run server-side over the HNSW index
    def compute_uniqueness(field_name="clip", k=3, metadata_field="uniqueness", max_samples=None, ef_search=64,
                           chunk=5000) -> dict
    def compute_representativeness(field_name="clip", metadata_field="representativeness", max_samples=None,
                                   chunk=20000) -> dict
    def compute_hardness(logits_field, metadata_field="hardness") -> dict
    def find_unique(count, field_name="clip", sample_size=200000, max_samples=None) -> list[int]

    # Structured labels & evaluation
    def evaluate_detections(pred_field, gt_field, eval_key="eval", iou=0.5, classes=None) -> dict
    def evaluate_classifications(pred_field, gt_field, eval_key="eval", classes=None) -> dict
    def evaluate_segmentations(pred_field, gt_field, eval_key="eval", image_loader=None) -> dict
    def compute_mistakenness(pred_field, gt_field, metadata_field="mistakenness") -> dict
    def compute_leaky_splits(split_field, field_name="clip", thresh=0.04, max_samples=None) -> dict
    def to_patches(label_field, dest_name, patches_dir, image_loader=None, on_duplicate="skip") -> Dataset
    def set_values_per_sample_multi(updates_by_id, batch_size=5000, *, wait=True, timeout=1800) -> int | Job | list[Job]

    # Import / export (standard CV formats)
    def import_coco(images_dir, annotations_json, label_field="ground_truth", on_duplicate="skip")
    def import_yolo(images_dir, labels_dir, classes, label_field="ground_truth", on_duplicate="skip")
    def import_voc(images_dir, annotations_dir, label_field="ground_truth", on_duplicate="skip")
    def import_classification_dir(root, label_field="ground_truth", on_duplicate="skip")
    def export(export_dir, format="coco", label_field="ground_truth")   # also on DatasetView

    # Field management & merge
    # Each waits for its server job; wait=False returns the Job
    def rename_field(field_name, new_name, *, wait=True, timeout=1800) -> dict | Job
    def clone_field(field_name, new_name, *, wait=True, timeout=1800) -> dict | Job
    def clear_field(field_name, *, wait=True, timeout=1800) -> dict | Job
    def merge_samples(source, key_field="filepath", fields=None, skip_existing=False) -> int
    def add_samples_from(source, *, embedding_fields=None, hash_fields=None, batch_size=500) -> dict  # {"seen","added","skipped"}

    # Embeddings & visualization
    # Both return {"total", "written", "dropped", "dropped_sample_ids"}
    def compute_embeddings(model_name="clip-ViT-B-32", field_name="clip", image_loader=None, batch_size=50,
                           force_register=False, skip_existing=False,
                           model_source="sentence-transformers") -> dict
    def compute_patch_embeddings(model_name="openai/clip-vit-base-patch32", field_name="clip", scales=None,
                                 image_loader=None, batch_size=50, reduce=True, partition=None,
                                 total_partitions=None, num_threads=4) -> dict
    def compute_visualization(field_name="clip", fit_sample_size="auto", method="umap") -> dict
    def refresh_stats(*, wait=True, timeout=1800, force=False) -> dict | Job

    # Array fields
    def create_array_field(name: str) -> dict
    def list_array_fields() -> list[str]

    # Versioning
    def create_version(name: str, description: str = None, include_metadata: bool = True) -> dict
    def list_versions() -> list[dict]
    def version_status() -> dict
    def diff(version_name: str = None, version_id: int = None) -> dict   # version vs current
    def checkout(version_name: str = None, version_id: int = None) -> Iterator[Sample]  # snapshot samples
    def version(version_name: str = None, version_id: int = None) -> VersionView  # queryable, read-only
    def revert(version_name: str = None, version_id: int = None, restore_deleted: bool = False,
               exact_membership: bool = False) -> dict
```

### DatasetView Class

```python
class DatasetView:
    # Filtering (returns new DatasetView)
    def filter(condition: Condition) -> DatasetView
    def match(condition: Condition) -> DatasetView  # Alias for filter
    def exclude(condition: Condition) -> DatasetView
    
    # Ordering and pagination
    def order_by(field: str, descending: bool = False) -> DatasetView
    def sort_by(field: str, reverse: bool = False) -> DatasetView  # Alias
    def limit(n: int) -> DatasetView
    def skip(n: int) -> DatasetView
    def offset(n: int) -> DatasetView  # Alias for skip
    
    # Field selection
    def select_fields(fields: list[str]) -> DatasetView
    
    # Iteration
    def iter_samples(progress: bool = False, autosave: bool = False, batch_size: int = None) -> Iterator[Sample]
    def __iter__() -> Iterator[Sample]
    def __len__() -> int        # matches of the filters; see count()
    
    # Accessors
    def first() -> Sample | None
    def to_list() -> list[Sample]
    def ids() -> list[int]
    def count() -> int          # matches of the filters; call it before limit()/skip()
    
    # Aggregations (exact, server-side, filter-scoped)
    def aggregate(aggregations: list[dict]) -> list[dict]   # batched, one request
    def count_values(field: str, limit: int = None) -> dict[Any, int]   # exact discrete counts
    def distinct(field: str, limit: int = None) -> list[Any]            # exact distinct values
    def bounds(field: str) -> tuple[float, float]                       # exact min/max
    def histogram_values(field: str, bins: int = 10) -> list[dict]
    def values(field: str) -> list[Any]              # aligned with view length (None if missing)
    def sum(field: str) -> float
    def mean(field: str) -> float
    def std(field: str) -> float
    def quantiles(field: str, quantiles: list[float] = None) -> dict[float, float]
    def schema() -> list[dict]
    
    # Similarity search
    def sort_by_similarity(
        sample: Sample = None,
        sample_id: int = None,
        vector: list[float] = None,
        text: str = None,
        field_name: str = "clip",
        limit: int = None     # default: the view's limit, else 50
    ) -> SimilarityResultView
```

### Sample Class

```python
class Sample:
    def __init__(
        self,
        filepath: str,
        tags: list[str] = None,
        metadata: dict = None,
        location: dict | tuple = None,     # GeoJSON Point or (lon, lat)
        heading: float = None,
        embeddings: dict[str, list[float]] = None,
        hashes: dict[str, str] = None,
        array_fields: dict[str, list[str]] = None,
    )
    
    # Properties
    id: int                    # Set after adding to dataset
    filepath: str
    media_type: str            # "image", "video", etc.
    metadata: dict
    tags: list[str]
    location: dict | None      # GeoJSON Point
    heading: float | None      # Compass heading in degrees
    embeddings: dict[str, list[float]]  # to upload; empty on samples read back
    hashes: dict[str, str]     # {field_name: hash_hex}; to upload, empty on samples read back
    array_fields: dict[str, list[str]]  # Dynamic array fields (e.g., inferred_tags)
    distance: float | None     # Set for similarity search results
    created_at: str            # ISO-8601
    last_modified_at: str      # ISO-8601
    
    # Methods
    def save() -> None         # Persist changes to backend
```

---

## Best Practices

### Performance

1. **Use batch operations** - one `add_samples(list)` call is much faster than many `add_samples([s])` calls
2. **Index metadata fields** - For frequently filtered fields, call `dataset.promote_field("field")`
3. **Limit result sets** - Use `limit` and `offset` for pagination
4. **Send embeddings with the samples** - for continuous ingest, put vectors on each `Sample` (`embeddings=`) so they arrive in the same request; use `compute_embeddings()` to backfill

### Data Organization

1. **Use consistent metadata schemas** - Define a schema for your metadata structure
2. **Tag strategically** - Use tags for categorical grouping (train/val/test, quality levels)
3. **Store file paths as absolute** - Avoid relative paths that break when moving data
4. **Include provenance** - Track where data came from in metadata

### Error Handling

```python
from fridge import Dataset, Sample
import logging
import os

logger = logging.getLogger(__name__)

# Dataset(name) loads the dataset if it exists, otherwise creates it.
dataset = Dataset("my_dataset")
samples = [Sample("/data/images/a.jpg"), Sample("/data/images/b.jpg")]

# Validate samples before adding
valid_samples = []
for sample in samples:
    if os.path.exists(sample.filepath):
        valid_samples.append(sample)
    else:
        logger.warning(f"File not found: {sample.filepath}")

dataset.add_samples(valid_samples)
```

---

## Troubleshooting

### Common Issues

**Problem:** `ConnectionError` when calling the SDK

**Solution:** The SDK talks to the Freedom Ridge HTTP API, not a database directly.
Check the API is reachable and that you pointed the SDK at the right URL:
```bash
curl http://localhost:8000/api/config
```
```python
import fridge
fridge.connect("http://localhost:8000/api")  # or set FRIDGE_API_URL
```

**Problem:** `401 Unauthorized` / `403 Forbidden`

**Solution:** Authenticate first — `fridge.connect(url, api_token=...)` (or
`$FRIDGE_API_TOKEN`), or `fridge.get_client().login(user, pass)`. A `403` on a
specific dataset means your user/token lacks access to it. See
[Connecting & Authenticating](#connecting--authenticating).

**Problem:** `ImportError: No module named 'sentence_transformers'` (embeddings)

**Solution:** Install the `embeddings` extra (see [Installation](#installation)):
```bash
pip install -e "python/[embeddings]"
```

**Problem:** Slow sample insertion

**Solution:** Use one batched call instead of many small ones:
```python
# Slow: one HTTP request per sample
for sample in samples:
    dataset.add_samples([sample])

# Fast: a single batched request
dataset.add_samples(samples)
```

---

## Patch-Based Similarity Search

Freedom Ridge supports dividing images into grid patches, computing embeddings per patch, and searching for similar regions across a dataset. This is useful for finding specific objects, patterns, or textures that appear in only part of an image.

### Computing Patch Embeddings

```python
from fridge import Dataset

dataset = Dataset("my_dataset")

# Compute patch embeddings at two scales
# 2x2 = 4 patches (1/4 of image each) — good for large objects
# 4x4 = 16 patches (1/16 of image each) — good for medium objects like signs
dataset.compute_patch_embeddings(
    model_name="openai/clip-vit-base-patch32",
    scales=["2x2", "4x4"],
    batch_size=50,       # Images per GPU batch
    reduce=True,         # Run UMAP reduction for embeddings panel visualization
)
```

This will:
1. Divide each image into grid patches at each scale
2. Run CLIP on each patch
3. Upload patch embeddings (stored as `clip_patch_2x2_r0c0`, `clip_patch_4x4_r3c2`, etc.)
4. Run UMAP reduction so patches appear in the embeddings panel

Patch search compares the selected patch with every patch at that scale. To make it an index
search, give each tile position its own HNSW index, once per scale (16 for `4x4`). Patch search
and find-unlabeled use them once every position of the scale has one; until then they rank every
patch exactly, which takes seconds on a large dataset. Each index takes about as much disk as its
vectors, and covers every dataset with that field.

```python
for r in range(4):
    for c in range(4):
        dataset.build_ann_index(f"clip_patch_4x4_r{r}c{c}")
```

### Patch Similarity Search (SDK)

```python
from fridge import Dataset

dataset = Dataset.from_id(15)
results = dataset.client.patch_similarity_search(
    dataset_id=dataset.id,
    sample_id=12345,       # Source image
    field_name="clip",
    patch_scale="4x4",
    patch_row=2,           # Row of selected patch
    patch_col=3,           # Column of selected patch
    limit=50,
)

# Each result includes the best-matching patch location
for r in results:
    print(f"Sample {r['sample_id']}: patch r{r['patch_row']}c{r['patch_col']} (distance: {r['distance']:.4f})")
```

### Patch Search in the UI

1. Open a sample in the detail modal
2. Select a patch scale from the "Patches" dropdown (e.g., 4x4)
3. Click on a grid cell to select a patch
4. Click "Search Similar Patches"
5. The grid view shows matching images with the matching region highlighted

### Distributed Patch Computation

For large datasets, split the work across multiple machines. Each worker
processes a partition of the dataset independently. No coordination needed —
workers write to the same Freedom Ridge API.

**Using the CLI script:**
```bash
# On worker 1 of 10:
python examples/sdk/compute_patch_embeddings.py \
    --dataset MyDataset --scales 2x2 4x4 \
    --partition 1 --total-partitions 10 --no-reduce

# On worker 2 of 10:
python examples/sdk/compute_patch_embeddings.py \
    --dataset MyDataset --scales 2x2 4x4 \
    --partition 2 --total-partitions 10 --no-reduce

# ... workers 3-10 ...

# After all workers finish, run UMAP reduction once:
python examples/sdk/compute_patch_embeddings.py \
    --dataset MyDataset --reduce-only
```

**Using the SDK directly:**
```python
from fridge import Dataset

dataset = Dataset("my_dataset")
dataset.compute_patch_embeddings(
    scales=["4x4"],
    partition=1,             # This worker's partition (1-indexed)
    total_partitions=10,     # Total workers
)
```

**Custom compute pipelines** (e.g., AWS Lambda, Spark, Kubernetes):

Upload patch embeddings via the API — the field_name encoding is handled
server-side:
```python
client = dataset.client
client.upload_patch_embeddings(dataset.id, [
    {
        "sample_id": 12345,
        "vector": [0.1, 0.2, ...],  # Your CLIP vector
        "field_name": "clip",
        "patch_scale": "4x4",
        "patch_row": 2,
        "patch_col": 3,
    },
    # ... more patches
])
```

### UMAP Reduction for Patches

After computing patch embeddings (especially after distributed runs), run UMAP to enable the embeddings panel:

```python
dataset.compute_visualization(field_name="clip_patch_4x4")
```

This fits on the client, sized to the machine's RAM, and places every patch.
Don't use `client.reduce_embeddings` for patch fields: it fits on the server, and
by default (`max_embeddings=100000`) it reduces only the first 100,000 vectors and
leaves the rest without coordinates, with no error. Compare `reduced` with
`total_embeddings` in the result; older backends report the capped count in both
(`{"total_embeddings": 100000, "reduced": 100000}`), hiding the vectors left out.
`max_embeddings=-1` fits every vector, but the server refuses any fit of
more than 200,000 vectors with HTTP 413. A 4x4 grid is 16 vectors per image, so
more than 6,250 images already exceed the default, and 12,500 exceed the limit.

Or via CLI:
```bash
python examples/sdk/compute_patch_embeddings.py --dataset MyDataset --reduce-only
```

After reduction, select `clip_patch_4x4` or `clip_patch_2x2` in the embeddings panel dropdown to visualize all patches in 2D space.

---

## 3D cuboids and calibration

`Cuboid` / `Cuboids` are JSON-compatible labels. Store a list under any metadata
field name and a `Calibration` under `metadata.calibration`:

```python
from PIL import Image
from fridge import Calibration, Cuboid, Cuboids, Sample, objects_from_cuboids

calibration = Calibration(
    P=[700, 0, 621, 0, 0, 700, 187.5, 0, 0, 0, 1, 0],  # row-major 3×4
    image_size=[1242, 375],  # original width and height, required
)
boxes = Cuboids([
    Cuboid("Car", center=[1, 0.5, 14], size=[4, 1.8, 1.5], yaw=-0.7,
           id="frame-1-car-1", confidence=0.95, occluded=1, difficulty="moderate"),
])
sample = Sample("/data/frame-1.png", location=(-122.42, 37.77))
sample.calibration = calibration
sample.ground_truth_3d = boxes
parent_id = dataset.add_samples([sample])["ids"][0]

with Image.open(sample.filepath) as image:
    objects = objects_from_cuboids(
        parent_id, sample.filepath, image, boxes, calibration, "/data/crops",
        location=sample.location, field="ground_truth_3d",
    )
dataset.add_samples(objects, on_duplicate="update")
```

Paths above are examples: use a producer-writable directory under your backend's
`FRIDGE_DATA_ROOT`. The helper writes a separate JPEG for each visible box. Stable
parent paths, field names and cuboid IDs give stable crop paths. Pass the source
field name (`field=`, required); it is stored as `cuboid_field`
and keeps ground-truth and prediction crops and modal selection independent. `DontCare` labels and boxes
with no projected image extent are skipped. The PIL image must match `image_size`.

The required `_type: "cuboid"` marker is added automatically. `center` is the
geometric centre in metres; `size` is `[length, width, height]`. With `frame="camera"`
(default), x is right, y down, z forward; yaw rotates about y. With `frame="lidar"`,
x is forward, y left, z up; yaw rotates about z. Zero yaw heads along +x in either
frame. LiDAR boxes require a row-major 4×4 affine matrix in
`calibration.transforms["lidar->camera"]`. Optional `confidence`, `truncated`,
`occluded`, and `alpha` are top-level label fields; extra keyword arguments are
stored under `attributes`.

`box.corners()` returns eight corners in its own frame (0–3 are the front face).
`calibration.project(box)` returns `(edges, extent)`: up to twelve pixel edge
segments and an image-clamped `[x, y, width, height]`, or `None` for an invisible
extent. Edges are clipped at homogeneous depth 0.1 before division, so a box partly
behind the camera stays visible. P must use a unit optical axis in its third row
(KITTI convention), so this threshold is in metres. Pixels refer to the original image; the frontend
scales them for thumbnails. `calibration.distance(box)` measures centre distance
in the camera frame.

Object rows carry `parent_id`, `parent_filepath`, `cuboid_id`, `cuboid_field`, normalized projected
`crop_bbox`, `label`, optional confidence/KITTI fields, `distance_m`, `length_m`,
`width_m`, `height_m`, and `yaw`, plus extra attributes such as `difficulty` and
`num_lidar_points`. Canonical links and geometry take precedence over attributes.
The supplied parent location is inherited. These scalar fields use the existing
sidebar, histogram and image/object cross-grain filters. For categorical occlusion,
call `dataset.client.set_field_display_type(dataset.id, "occluded", "enum")`.

`Cuboid.from_kitti(line, image_size)` converts bottom-face centre to geometric
centre, preserves the normalized annotated `bbox_2d`, and computes benchmark
difficulty (`easy`, `moderate`, `hard`, `unrated`). `Calibration.from_kitti(text,
image_size)` reads P2 and composes R0_rect × Tr_velo_to_cam once. It ignores
non-numeric lines, keeps R0_rect, Tr_velo_to_cam and Tr_imu_to_velo under `kitti` so an export is lossless, and drops the unused projections. These are conversion helpers; importing
datasets and serving point clouds are separate features.

Cuboid lists are toggled under **Overlays**, and individual boxes can be hidden
or selected in the modal. Clicking an object opens its parent and selects its
`cuboid_id`. Editing parent labels does not rewrite object rows: producers must
regenerate both. As with other SDK labels, values read back from the server are
plain dictionaries/lists; reconstruct `Calibration` from its P, image_size and
transforms when calling its methods.

---

## Support

- **Documentation:** [docs/](../docs/)

Use the Issues or Discussions tab of
https://github.com/freedomridge/freedomridge.
