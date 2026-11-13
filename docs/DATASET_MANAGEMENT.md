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
| `"update"` | metadata, location and array fields replaced; tags merged, or replaced with `"replace_tags": true` | 200 |

Use `"skip"` for a script you may re-run. Send smaller batches if you get HTTP 413
(the shipped nginx allows 100 MB on this route).

### Sample fields

| Field | Notes |
|-------|-------|
| `filepath` | Required. A local path that resolves inside the backend's `FRIDGE_DATA_ROOT`, or `s3://bucket/key`. Paths outside the data root are not served. |
| `media_type` | Optional; detected from the extension (`image`, `video`, `audio`). |
| `tags` | List of strings. |
| `metadata` | Free-form JSON. Types are detected from the values; ISO-8601 dates and times become datetimes ([below](#dates-and-times)). |
| `location` | GeoJSON Point, `{"type": "Point", "coordinates": [lon, lat]}`. |
| `heading` | Compass heading in degrees. |
| `embeddings` | `{field_name: [floats]}`. A field name's dimension is fixed server-wide by its first vectors. |
| `hashes` | `{name: "hex string"}`, for example `{"phash_8": "c3a1..."}`. |
| `array_fields` | `{field_name: [strings]}` for array fields created on the dataset. |

### Dates and times

A metadata value is a date and time when it is written `YYYY-MM-DD`, then optionally `T` or a space, `hh:mm`,
`:ss`, a fraction of a second and an offset (`Z`, `+02`, `+0200` or `+02:00`). A new field whose values are all
written so is a `datetime` field. Freedom Ridge compares, sorts and bins them as moments: a value with an offset is
the same moment as its UTC spelling, and a value without an offset is read as UTC. What you stored is shown as you
stored it. Samples whose value isn't a date and time sort after the others, in both directions, and never match a
range.

Local time is its own field. To filter by the clock time where a picture was taken, store that time as a second
field, written without an offset: `captured_at: "2026-06-01T17:05:00-07:00"` and
`captured_local: "2026-06-01T17:05:00"`. Filter on `captured_local` for "after 5 PM on 1 June": a 5 PM picture
from California and one from Illinois both match, though they are two hours apart. Every datetime field behaves
the same way; this one simply holds clock readings. For "after 5 PM on any day", also store the hour as a
number field.

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
one that hides black images or road photos with concept filters. Its filters show in the filter bar
after a **Default view:** label with the view's name, each marked ★. **Clear All** removes the
viewer's own filters and keeps the default view, and **Show all images** removes it too, until the
next Clear All. New samples pass through its filters like any others; a concept filter hides a new
image once the image is scored.

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

## Updating a saved view

The web app remembers which saved view the applied filters came from: one chosen in **Saved Views**,
the default view, a view's link, or one just saved. Once the filters differ from it (a stale lasso
redrawn, a filter added or removed), **Saved Views** says so, and the view's owner or an admin gets
**Update this view**: after a confirm naming the view, its filters are replaced with the applied
ones. Its name, link and sharing stay, and a default view stays the default; everyone who opens it
then gets the new filters. A temporary selection is left out, as when saving a view. Anyone else
is offered only saving the filters as a new view, which is then theirs to update. On a mirror, a
copy of a view on its primary changes there: the update is refused with a message naming the
primary (409 `view_on_primary`).

If someone else changed the view's filters after you opened it, the update is refused rather than
written over theirs: "This view changed since you opened it." Saved Views then offers **Open it
again** (its filters as they are now) or **Save as a new view** (yours).

The remembered view is kept with the filters: after a reload, and in each Back/Forward entry, so
Back to filters from another view or from none doesn't offer to update this one. It is forgotten
when other filters replace them all (Show all images, a search's results, another link). Comparing
the filters ignores only what the app adds when it applies them (the default view's ★ mark) and the
order of keys; anything else, a chip's name included, counts as a change.

Over REST, the keys of `content` given replace the view's (`filters`, and `viewStages` for the app),
and the others stay. A view's `updated_at` says when its filters last changed. The database sets it
for every writer (an update, a version restore, a mirror's sync, a new view), only when the content
changes: a rename, a new description or sharing leaves it, and no request sets it. Send the
`updated_at` you read (`null` for a view not changed since 2.11) and an update over filters that
changed since is refused with 409 `view_changed`; leave it out and the last writer wins:

```python
view = requests.get(f"{API_URL}/views/{slug}", headers=headers).json()
requests.patch(
    f"{API_URL}/datasets/{dataset_id}/views/{view['id']}/content",
    headers=headers,
    json={"content": {"filters": filters, "viewStages": stages}, "updated_at": view["updated_at"]},
).raise_for_status()
```

## Links to a filter, a view or a sample

The web app copies a link that opens a dataset as you see it:

- **Copy link**, beside **Saved Views**: the applied filters and the grid's sort.
- The link icon on a view in **Saved Views**: that view.
- **Copy link** in a sample's view: the applied filters and that sample. Next and previous walk the grid.

A link is `/datasets/<id>?<parameters>`, each optional:

| Parameter | Meaning |
|---|---|
| `q=<text>` | The filters in the [filter query language](FILTER_QUERY.md), one chip per top-level `AND` part. Empty (`q=`) means no filters, which is what Copy link writes when none are applied. |
| `f=<version>.<data>` | The filters encoded, when a chip has no text form: a map region, a lasso, picked samples, a search, a concept, a duplicate collapse. |
| `view=<slug>` | A saved view. |
| `sort=<field>`, `dir=asc` or `desc` | The grid's sort. |
| `sample=<id>` | The sample whose view opens. |

Copy link writes `q=` when every chip can be written as text, so a person or a script can read, edit or
write one: `/datasets/12?q=split = train AND score > 0.5` (the browser encodes the spaces; write `+`, `&`
and `#` in a value as `%2B`, `%26` and `%23`). The link's
filters replace the ones the browser kept for the dataset, which are one Back away, and the address loses
the parameters once the page has opened them.

A link doesn't carry:

- **Access.** The reader signs in (a link opened signed out goes through the login page and back) and
  needs access to the dataset, and to the view for a view link.
- **The draft.** Only the applied filters go in: not the sidebar's draft, a brush or ticked samples.
- **A big Select All.** Its selection is kept on the server for 2 hours, so Copy link refuses it. Save a
  view and copy the view's link: the view keeps the filters, which is what a Select All shows.
- **A selection too large for a link.** A link holds at most 6,000 characters, about a thousand picked
  samples of a dataset of millions. Copy link refuses a longer one; save a view and copy its link.
- **Another server.** A link names this server's dataset, samples and views by their ids and slugs (the
  ids inside `f=` too). A follower's copies have their own.
- **A snapshot.** A link shows what matches its filters when it is opened.

An `f=` made by a version of Freedom Ridge that encodes filters differently says so, and the dataset opens
without it.

## Delete samples (admin)

`DELETE /api/datasets/{id}/samples/{sample_id}` deletes one sample; `POST /api/datasets/{id}/samples/bulk-delete`
deletes `sample_ids`, a `selection_token` or `filters` (less `excluded_ids`) as a server job (202; follow it with
`GET /api/jobs/{job_id}`). **Deleting an image deletes its object samples** ([Object view](OBJECT_PATCH_VIEW.md)): the single
delete answers `objects_deleted`, and a bulk delete's result has `deleted_count` (the samples it targeted) and
`objects_deleted` (the objects that went with their images). The SDK's `Dataset.delete_sample()` and
`Dataset.delete_samples()` return the same.

Object samples left with no image (their image never came, or was deleted on a release before deletes took the
objects) are found with `POST /api/datasets/{id}/objects/unlinked` and deleted, a kind at a time, with
`POST /api/datasets/{id}/objects/unlinked/delete` ([Object view](OBJECT_PATCH_VIEW.md)).

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
| `/datasets/{id}/samples/{sample_id}` | DELETE | admin | Delete one sample (an image's objects with it) |
| `/datasets/{id}/samples/bulk-delete` | POST | admin | Delete many samples (an image's objects with it) |
| `/datasets/{id}/objects/unlinked` | POST | any logged-in user with access | The objects with no image, by kind |
| `/datasets/{id}/objects/unlinked/delete` | POST | admin | Delete the objects with no image of the kinds named |
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
