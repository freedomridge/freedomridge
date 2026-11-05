# Dataset Management over REST

This page shows the raw HTTP calls for creating and filling a dataset. The
[Python SDK](SDK.md) wraps every one of them and is the easier path. The complete,
generated API reference is the backend's OpenAPI page at `/docs` (for example
`http://localhost:8000/docs` in development).

The snippets below run in order as one script.

## Authentication

Every call needs a bearer token. Create one in the web app under
**Settings → API Tokens** (power_user or admin), or with `POST /api/tokens` from a
logged-in session. `POST /api/auth/login` only sets a session cookie; it does not
return a token.

```python
import requests

API_URL = "http://localhost:8000/api"
headers = {"Authorization": "Bearer <your-token>"}
```

## Create a dataset (admin)

```python
response = requests.post(
    f"{API_URL}/datasets",
    headers=headers,
    json={
        "name": "street_scenes",
        "description": "My first dataset",
        "media_fields": ["filepath", "thumbnail"],  # optional; default ["filepath"]
    },
)
response.raise_for_status()
dataset_id = response.json()["id"]
```

`media_fields` lists the fields that hold displayable media: `filepath` plus any
metadata keys (here `thumbnail`) that hold another path for the same sample. The
grid and modal can switch between them.

## Add samples (power_user or admin)

Send up to about 1,000 samples per request, wrapped with an `on_duplicate` mode:

```python
samples = [
    {
        "filepath": "/data/images/IMG_001.jpg",
        "tags": ["outdoor", "daytime"],
        "metadata": {
            "scene": "urban",
            "brightness": 0.75,
            "thumbnail": "/data/images/thumb/IMG_001.jpg",
            "detections": [
                {"label": "car", "bbox": [0.10, 0.20, 0.30, 0.25], "confidence": 0.92},
            ],
        },
        "location": {"type": "Point", "coordinates": [-122.4194, 37.7749]},  # [lon, lat]
        "heading": 90.0,
    },
]

response = requests.post(
    f"{API_URL}/datasets/{dataset_id}/samples",
    headers=headers,
    json={"samples": samples, "on_duplicate": "skip"},
    timeout=300,
)
response.raise_for_status()
print(response.json())  # {"message": ..., "ids": [new sample ids], ...}
```

`on_duplicate` decides what happens to a `filepath` the dataset already has:

| Mode | Existing filepath | Response |
|------|-------------------|----------|
| `"error"` (default) | left unchanged | **409**, but new samples in the same request **are committed**; the body has `ids` for them and an `existing` filepath-to-id map |
| `"skip"` | left unchanged | 200; `ids` lists only the new samples, `skipped` counts the rest |
| `"update"` | metadata, location and array fields replaced; tags merged | 200 |

Use `"skip"` for a script you may re-run. Send smaller batches if you get HTTP 413
(the shipped nginx allows 100 MB on this route).

### Sample fields

| Field | Notes |
|-------|-------|
| `filepath` | Required. A local path that resolves inside the backend's `FRIDGE_DATA_ROOT`, or `s3://bucket/key`. Paths outside the data root are not served. |
| `media_type` | Optional; detected from the extension (`image`, `video`, `audio`). |
| `tags` | List of strings. |
| `metadata` | Free-form JSON. Types are detected from the values; ISO-8601 strings become datetimes. |
| `location` | GeoJSON Point, `{"type": "Point", "coordinates": [lon, lat]}`. |
| `heading` | Compass heading in degrees. |
| `embeddings` | `{field_name: [floats]}`. A field name's dimension is fixed server-wide by its first vectors. (`embedding: [...]` is shorthand for the `clip` field.) |
| `hashes` | `{name: "hex string"}`, for example `{"phash_8": "c3a1..."}`. |
| `array_fields` | `{field_name: [strings]}` for array fields created on the dataset. |

### Labels

Labels live in `metadata` under any field name; the viewer recognises them by
shape, not by name:

| Shape | Renders as |
|-------|------------|
| list of `{"label", "bbox": [x, y, w, h], "confidence"?}` | boxes (`bbox` is normalized 0-1, or pixels) |
| list of `{"label", "points": [[x, y], ...], "closed"?}` | polygons / polylines |
| `{"label", "confidence"?}`, or a list of them | classification chips |
| `{"maskPath": "...", "labels"?}` | a segmentation mask |
| `{"type": "heatmap", "map_path": "...", "range": [lo, hi]}` | a heatmap |

The [SDK label classes](SDK.md#structured-labels) build these dicts for you, and
[mask colours and class names](SDK.md#mask-overlays-and-annotation-colours)
are set per dataset with `segmentation_config`.

## Update samples (power_user or admin)

Update one sample in place; it keeps its id, embeddings and hashes:

```python
sample_id = response.json()["ids"][0]
requests.patch(
    f"{API_URL}/datasets/{dataset_id}/samples/{sample_id}",
    headers=headers,
    json={
        "metadata": {"reviewed": True},       # merged into the existing metadata
        "remove_metadata_keys": ["brightness"],
        "tags": ["outdoor", "reviewed"],      # replaces the tag list
    },
).raise_for_status()
```

For many samples, `POST /api/datasets/{id}/samples/bulk-set` merges the same
`metadata` into every listed sample (`{"sample_ids": [...], "metadata": {...}}`),
or a different patch per sample (`{"updates": [{"sample_id": 1, "metadata": {...}}, ...]}`).
Re-sending samples with `"on_duplicate": "update"` also works.

## Tag samples (power_user or admin)

```python
requests.post(
    f"{API_URL}/datasets/{dataset_id}/samples/bulk-tag",
    headers=headers,
    json={"sample_ids": [sample_id], "add_tags": ["validated"], "remove_tags": ["daytime"]},
).raise_for_status()
```

Add `"field_name": "<array field>"` to tag a field other than `tags`.

## List samples

```python
page = requests.get(
    f"{API_URL}/datasets/{dataset_id}/samples",
    headers=headers,
    params={"limit": 100, "offset": 0},
).json()
for sample in page["items"]:   # page["total"] is the dataset size
    print(sample["id"], sample["filepath"])
```

Increase `offset` by `limit` until it reaches `total`.

## Embeddings and the map

Upload vectors you computed yourself (a list of `{sample_id, vector, field_name}`):

```python
requests.post(
    f"{API_URL}/datasets/{dataset_id}/embeddings",
    headers=headers,
    json=[{"sample_id": sample_id, "vector": [0.1] * 512, "field_name": "my_model"}],
    timeout=300,
).raise_for_status()
```

To place samples on the embeddings map, use the SDK's
`Dataset.from_id(dataset_id).compute_visualization(field_name="my_model")`: it fits
on the client, sized to the machine's RAM, and uploads coordinates for every
sample. The server-side `POST /api/datasets/{id}/embeddings/reduce` endpoint fits
in the API process and refuses fields with more than 200,000 vectors (HTTP 413).

## Default view (admin)

A dataset's default view is a public saved view that the web app opens the dataset on, for example
one that hides black images or road photos with concept filters. Its filters show in the filter bar as
**Default view: <name>**, each marked ★. **Clear All** removes the viewer's own filters and keeps
the default view, and **Show all images** removes it too, until the next Clear All. New samples pass
through its filters like any others; a concept filter hides a new image once the image is scored.

Only the app applies it. The API and the SDK return every sample, and a script loads the view
like any other saved view when it wants those filters.

An administrator sets it in **Saved Views** with the ★ next to a public view, or over REST:

```python
requests.patch(
    f"{API_URL}/datasets/{dataset_id}",
    headers=headers,
    json={"default_view_id": view_id},   # None removes it
).raise_for_status()
```

The view must be one of the dataset's saved views and public (400 otherwise), and it can't be made
private while it is the default (409). Deleting the view, which its owner can do, leaves the dataset
without a default view.

## Delete a dataset (admin)

```python
job = requests.delete(f"{API_URL}/datasets/{dataset_id}", headers=headers)
print(job.status_code)   # 202: deletion runs as a server job
```

The response describes the job; follow it with `GET /api/jobs/{job_id}` or the
Jobs panel. The SDK's `Dataset.delete()` waits for it.

## Endpoint summary

| Endpoint | Method | Role | Description |
|----------|--------|------|-------------|
| `/datasets` | POST | admin | Create a dataset |
| `/datasets/{id}` | GET | any logged-in user with access | Dataset details |
| `/datasets/{id}` | PATCH | admin | Update name, description, media fields, `segmentation_config`, `default_view_id` |
| `/datasets/{id}` | DELETE | admin | Delete (202, server job) |
| `/datasets/{id}/samples` | POST | power_user | Add samples |
| `/datasets/{id}/samples` | GET | any logged-in user with access | List samples (`{"items", "total"}`) |
| `/datasets/{id}/samples/{sample_id}` | PATCH | power_user | Update one sample |
| `/datasets/{id}/samples/bulk-set` | POST | power_user | Merge metadata into many samples |
| `/datasets/{id}/samples/bulk-tag` | POST | power_user | Add or remove tags |
| `/datasets/{id}/embeddings` | POST | power_user | Upload vectors |

## Troubleshooting

- **Images don't show:** the path must resolve inside the backend's
  `FRIDGE_DATA_ROOT` (or be `s3://...`) and be readable by the backend. Check the
  backend log: `docker compose -f docker-compose.prod.yml logs backend`.
- **Location not on the map:** coordinates are `[lon, lat]`, longitude first.
- **Labels don't render:** check the shapes in [Labels](#labels): detections need
  a `bbox` key (`bounding_box` is not recognised).
