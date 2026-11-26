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
The field each opens on for everyone (`grid_media_field`, `modal_media_field`) is set in Dataset Settings →
General, **Grid opens on** and **Sample view opens on**, or with `PATCH /datasets/{id}`. Switching in a grid or a
sample's view changes only your own view, which your browser remembers for the dataset.

## Add samples (edit)

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

## Update samples (edit; tag for tags alone)

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

## Tag samples (tag)

```python
requests.post(
    f"{API_URL}/datasets/{dataset_id}/samples/bulk-tag",
    headers=headers,
    json={"sample_ids": [sample_id], "add_tags": ["validated"], "remove_tags": ["daytime"]},
).raise_for_status()
```

Add `"field_name": "<array field>"` to tag a field other than `tags`; that needs edit, except a mirror's own
`@local.tags` on a follower, which is tag as `tags` is.

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

## Export samples as CSV

In the web app, the grid's **Export** button opens the Export dialog, which writes a CSV of the whole dataset, the
applied filters, the current view or the selected samples, with the fields you tick: a header row of field names, then
one row a sample. The browser builds it from `POST /api/datasets/{id}/samples/query` (`fields`, `slim: true`, a page
at a time by `after_id`). A list is written as its values joined with `;`, an object as JSON, a missing value as an
empty cell. Every text cell, the header's too, is in double quotes, and a number is written bare: a spreadsheet that
reads the file with `;` takes a line with no `;` for one cell, with the comma as its decimal mark, so a number followed
by an unquoted text could read as one formula.

A text that would start a formula when a spreadsheet opens the file (`=`, `+`, `-`, `@` or a look-alike such as `＝`,
after any leading spaces or double quotes, or a tab or a carriage return first) is written with an apostrophe in front,
which a spreadsheet shows as text: at the start of a cell, and also after a `;`, a tab or a line break inside one,
where a spreadsheet that reads the file with `;` (Excel in much of Europe) or tabs begins a new cell. A program reading
the file sees those apostrophes, and ordinary text is touched too: `range 3; -5 to 5` becomes `range 3;' -5 to 5`. A
text made only of numbers (`-5`, `-5;3`) gets none; one with anything else gets one before each number that has a sign
too (`a;-5` becomes `a;'-5`). The assistant's value list download keeps the same rule ([ASSISTANT.md](ASSISTANT.md)).

## Describe a field (edit)

A field's description and unit say what it means, in the app (Dataset Settings → Fields, and on the field's name in
the filters) and to the assistant:

```python
field = requests.patch(
    f"{API_URL}/datasets/{dataset_id}/fields/digital_gain/description",
    headers=headers,
    json={"description": "Sensor gain applied before readout; higher in dark scenes.", "unit": "dB"},
).json()
# {"field_name": "digital_gain", "description": "Sensor gain ...", "unit": "dB",
#  "values_private": false, "description_source": "set"}
```

- Send any of `description` (at most 1,000 characters), `unit` (at most 40) and `values_private` (true or false: the
  field's values aren't given to the [assistant](ASSISTANT.md#keeping-a-fields-values-back)). A key left out stays as it is; `null` or `""`
  clears a description or unit. Text is trimmed; a description may hold line breaks, and neither may hold other
  control characters. Too long or a control character: 422.
- A field the dataset's schema lists takes a description: its own fields, its built-in ones (`id`, `filepath`,
  `created_at`, `last_modified_at`, `tags`, `media_type`) and label fields such as `detections.label`. Anything else
  is a 404, a key that exists only in some samples' metadata and that nothing registered (neither the schema nor the
  field statistics list it) included. Below edit: 403 "This needs edit access to the dataset". A dataset being
  deleted: 409.
- The answer, and every field of `GET /datasets/{id}/schema` and of the field-stats answers, carries `description`,
  `unit`, `values_private` and `description_source`: `"set"` when someone wrote the description, `"default"` when it
  is the server's own, `null` when there is none. The server describes by name only the fields it makes itself, the
  built-in ones (with no unit); clearing what someone wrote on one brings the default back. Every other field has the
  description and unit someone wrote for it, or none: nobody describes a field by guessing from its name, so a field
  of your own called `brightness` has no description until you write one. The SDK's `compute_metadata()` and
  `compute_metrics()` describe the fields they compute when they compute them, as the user running them, where a
  field has none of its own ([details](SDK.md#field-descriptions)). `description_source` speaks of the description
  only.
- On a mirror, the primary's fields are described on the primary and arrive with its field settings: changing their
  description or unit here is refused (409), and such a request changes nothing, its `values_private` included.
  `values_private` belongs to each server: a mirror sets it on any field, the built-in ones included, and the sync
  never resets it (see [the SDK guide](SDK.md#field-descriptions) for a field the primary removes or renames). A
  version saves descriptions and units with the field types, and a revert restores them; the built-in and label
  fields' descriptions aren't in a version (a revert changes `width`'s, never `created_at`'s). `values_private` is
  never versioned, and outlives the field: a private field that is removed (cleared, emptied of its last sample, or
  removed by a revert) stays private if it comes back. While it is gone nothing lists it (a request about it is a
  404, as for a name the dataset never had), so its mark can only be taken off once the field exists again
  ([details](SDK.md#field-descriptions)).

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

## Default view (edit)

A dataset's default view is a public saved view that the web app opens the dataset on, for example
one that hides black images or road photos with concept filters. Its filters show in the filter bar
after a **Default view:** label with the view's name, each marked ★. **Clear All** removes the
viewer's own filters and keeps the default view, and **Show all images** removes it too, until the
next Clear All. New samples pass through its filters like any others; a concept filter hides a new
image once the image is scored.

Only the app applies it. The API and the SDK return every sample, and a script loads the view
like any other saved view when it wants those filters.

Someone with edit in the dataset sets it in **Saved Views** with the ★ next to a public view, or over REST:

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
redrawn, a filter added or removed), **Saved Views** says so, and the view's owner, an admin, or (for a
public view) anyone with edit in the dataset gets **Update this view**: after a confirm naming the view,
its filters are replaced with the applied
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

The address of a dataset's page is a link to what it shows (unless [switched off](#switching-the-live-address-off)):
the applied filters, the grid's sort and the sample whose view is open. It changes as they do, so a bookmark,
a reload or the address pasted elsewhere opens the same filters and sample. Back and Forward step through the
filters applied, each with its address,
and through opening a sample's view: Back closes it and Forward opens it again. Next and previous in the
view change the sample in the address without adding to the history. The web app also copies links:

- **Copy link**, beside **Saved Views**: the applied filters and the grid's sort.
- The link icon on a view in **Saved Views**: that view.
- **Copy link** in a sample's view: the applied filters and that sample. Next and previous walk the grid.

A link is `/datasets/<id>?<parameters>`, each optional:

| Parameter | Meaning |
|---|---|
| `q=<text>` | The filters in the [filter query language](FILTER_QUERY.md), one chip per top-level `AND` part. Empty (`q=`) means no filters, which is what Copy link writes when none are applied. |
| `f=<version>.<data>` | The filters encoded, when a chip has no text form: a map region, a lasso, picked samples, a search, a concept's sources, a duplicate collapse. |
| `view=<slug>` | A saved view. The address and Copy link write it while a public view's filters are applied as it was saved. |
| `w=<handle>` | A working view of the assistant, which it links to when the filters can't be `q=` text (below). Only read: the address and Copy link never write it. |
| `sort=<field>`, `dir=asc` or `desc` | The grid's sort. |
| `sample=<id>` | The sample whose view opens. |

Copy link writes `q=` when every chip can be written as text, so a person or a script can read, edit or
write one: `/datasets/12?q=split = train AND score > 0.5` (the browser encodes the spaces; write `+`, `&`
and `#` in a value as `%2B`, `%26` and `%23`). The link's
filters replace the ones the browser kept for the dataset; Back doesn't return to those. The address then
shows the page's own link to the same filters (the query as the app writes it). A link that can't be opened
says why, and the dataset opens, with its address, as it would without the link. A link whose request fails (the
network, the server) waits instead, saying so, with **Try again** and **Open the dataset without it**, and stays in
the address, so a reload opens it again.

An address carries what the filters name: field names, values, class names and map region coordinates (as
text in `q=`, or in `f=`, which anyone can decode). It is kept in the browser's history, and in whatever the
browser syncs it to; each load or reload of the page puts it in the access logs of the server and of any proxy
in between, and the page's own requests to the server carry it as their `Referer` (which the bundled nginx and
backend don't log, but another proxy may); it shows in screenshots; and it travels wherever it is pasted.
Other sites the page loads from or links to receive only the origin
(`Referrer-Policy: strict-origin-when-cross-origin`). Don't put secrets in filter values. To keep filter text
out of the address, switch the live address off (below).

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

While the applied filters hold a selection no link can carry (a big Select All, one too large for a link, or an
expired selection's chip), the address names no filters, only the sort and the sample whose view is open: opened
elsewhere it opens the dataset as it would without a link. A reload still shows the page as it was, the
selection's chip expired.

An `f=` made by a version of Freedom Ridge that encodes filters differently says so, and the dataset opens
without it.

A `w=<handle>` link is the assistant's, for a view it made in a conversation that can't be written as `q=` text (too
long for an address, or a chip with no text form). It opens for the person who had the conversation, for 30 days after
the view was last used, on the view's filters as chips, like a `view=` link; the address then shows the page's own
link to those filters. A filter of the view whose values can no longer be read is left out, and the page says so.
Opened by anyone else, or after it has expired, it says so and the dataset opens without it. To share such filters,
save a view and share that.

### Switching the live address off

`FRIDGE_LIVE_ADDRESS=off` for the frontend (also `false`, `0` or `no`; read when it starts, see `.env.example`)
keeps filters, searches, the sort and the open sample out of the page's address. The dataset page's address
stays `/datasets/<id>`, and a version's snapshot page keeps its path alone, so that text stays out of the
browser's history and whatever it syncs to, out of bookmarks and screenshots of the address bar, and out of
the access log line a reload makes. Back, Forward and a reload still step through and restore the filters and
the open sample, which each history entry keeps in the browser; after a reload the grid's sort is the one this
browser last chose. Copy link, a view's link and the links they make work as before, and a link opens as
above, its parameters then leaving the address. When the frontend can't be asked whether the setting is on,
the pages treat it as off.

It changes only the page's address, not the server's requests:

- A link that is opened is still requested from the server once with its parameters, so they are in that
  access log, as for any link, and the page's first requests, made while it opens the link, carry them as
  their `Referer`.
- The snapshot page sends its search and saved view in the address of the request for its samples
  (`…/samples?…&q=<search>&view=<name>`), so the server's access log has every search whether the setting is
  on or off.
- The dataset page sends filter values, regions and search text in request bodies, which the bundled nginx
  and backend don't log.

## Delete samples (edit)

`DELETE /api/datasets/{id}/samples/{sample_id}` deletes one sample; `POST /api/datasets/{id}/samples/bulk-delete`
deletes `sample_ids`, a `selection_token` or `filters` (less `excluded_ids`) as a server job (202; follow it with
`GET /api/jobs/{job_id}`). **Deleting an image deletes its object samples** ([Object view](OBJECT_PATCH_VIEW.md)): the single
delete answers `objects_deleted`, and a bulk delete's result has `deleted_count` (the samples it targeted) and
`objects_deleted` (the objects that went with their images). The SDK's `Dataset.delete_sample()` and
`Dataset.delete_samples()` return the same.

Object samples left with no image (their image never came, or was deleted on a release before deletes took the
objects) are found with `POST /api/datasets/{id}/objects/unlinked` and deleted, a kind at a time, with
`POST /api/datasets/{id}/objects/unlinked/delete` ([Object view](OBJECT_PATCH_VIEW.md)).

## Delete a dataset (manage)

```python
job = requests.delete(f"{API_URL}/datasets/{dataset_id}", headers=headers)
print(job.status_code)   # 202: deletion runs as a server job
```

The response describes the job; follow it with `GET /api/jobs/{job_id}` or the
Jobs panel. The SDK's `Dataset.delete()` waits for it.

## Endpoint summary

Who may call each one: the admin role, or a [level in the dataset](AUTHENTICATION.md#access-levels).

| Endpoint | Method | Who | Description |
|----------|--------|------|-------------|
| `/datasets` | POST | admin | Create a dataset; `"is_global": true` makes it global at `everyone_level` (view unless given) |
| `/datasets/{id}` | GET | view | Dataset details, with your `access_level` |
| `/datasets/{id}` | PATCH | edit; manage for `name`, `is_global`, `everyone_level` and `assistant_closed`; admin for `storage_config_id` | Update name, description, media fields, `segmentation_config`, `default_view_id`, visibility, whether the [assistant](ASSISTANT.md#closing-a-dataset) may read it |
| `/datasets/{id}` | DELETE | manage | Delete (202, server job) |
| `/datasets/{id}/samples` | POST | edit | Add samples |
| `/datasets/{id}/samples` | GET | view | List samples (`{"items", "total"}`) |
| `/datasets/{id}/samples/{sample_id}` | PATCH | edit; tag when only `tags` is sent (or a mirror's `@local.tags`) | Update one sample |
| `/datasets/{id}/samples/{sample_id}` | DELETE | edit | Delete one sample (an image's objects with it) |
| `/datasets/{id}/samples/bulk-delete` | POST | edit | Delete many samples (an image's objects with it) |
| `/datasets/{id}/objects/unlinked` | POST | view (with edit, it links what it can first) | The objects with no image, by kind |
| `/datasets/{id}/objects/unlinked/delete` | POST | edit | Delete the objects with no image of the kinds named |
| `/datasets/{id}/samples/bulk-set` | POST | edit | Merge metadata into many samples |
| `/datasets/{id}/samples/bulk-tag` | POST | tag for `tags` (and a mirror's `@local.tags`), edit for another field | Add or remove tags |
| `/datasets/{id}/embeddings` | POST | edit | Upload vectors |
| `/datasets/{id}/fields/{field}/description` | PATCH | edit | A field's description, unit and `values_private` |

## Troubleshooting

- **Images don't show:** the path must resolve inside the backend's
  `FRIDGE_DATA_ROOT` (or be `s3://...`) and be readable by the backend. Check the
  backend log: `docker compose -f docker-compose.prod.yml logs backend`.
- **Location not on the map:** coordinates are `[lon, lat]`, longitude first.
- **Labels don't render:** check the shapes in [Labels](#labels): detections need
  a `bbox` key (`bounding_box` is not recognised).
