# Changelog

Freedom Ridge has one version for the server, the web app and the Python SDK (the `VERSION`
file), following [Semantic Versioning](https://semver.org). A release lists **upgrade notes** when
it needs more than the standard steps in [Upgrading](docs/UPGRADING.md).

## 2.11.1 (2026-11-12)

2.11.1 fixes the slowdowns 2.11.0 brought to the public demo and one older one: the object grid after Find Similar or
a selection, datetime fields without an index, a dataset page's coverage counts, and the server pausing for the first
map reduction after a start. The app's database connections no longer compile queries (JIT), the production compose
file passes `FRIDGE_MEDIA_RENDERS`, and the SDK gains `dataset.count()` and `add_samples(..., replace_tags=True)`. No
migrations: the standard upgrade steps. The first searches after the restart are slower while caches warm; run one
search on each dataset after upgrading.

### Added

- **`dataset.count()` and `len(dataset)`** give the number of samples in a dataset, as `dataset.all().count()` did.
- **`add_samples(..., on_duplicate="update", replace_tags=True)`** makes an existing sample's tags exactly the ones
  sent, removing the others, tags added in the app included. Tags are still merged by default. The API takes it as
  `"replace_tags": true` next to `"on_duplicate": "update"`, and refuses it with any other mode. With a server older
  than this, the SDK raises after the write, saying the tags were merged.

### Changed

- **The server no longer compiles queries to machine code (JIT) before running them.** PostgreSQL compiled any query
  it estimated to be costly, again at every run, and the app's filters often look costly while running in
  milliseconds: in production, the object grid's count took 651 ms, 539 ms of it compiling, and 21 ms without. No
  query measured ran faster with JIT. The app's own connections (the API and the job runner) now turn it off;
  scripts and other clients of the database keep the server's setting.

### Fixed

- **The object grid shows a selection or a search's results quickly again.** Since 2.11.0, when the object grid
  showed the results of Find Similar, or any other list of samples, its count and its page read every object of the
  dataset, and PostgreSQL first compiled each of them to machine code (JIT) because it overestimated their cost. Both
  now find the listed objects and the objects of the listed images directly. Measured on copies, the database
  answers each of them in:
  - COCO (37,000 objects): 0.1 to 0.2 ms instead of 0.2 to 0.3 s;
  - Open Images (304,000 objects): 8 to 15 ms instead of 0.3 to 0.4 s.

  The rows are the same.

- **Datetime fields without an index are read once a sample again** by the sidebar's and `/stats`' earliest and
  latest values, a grid sorted by such a field, and `/aggregate`'s values. Since 2.11.0 each read the field's text
  for every one of the dozen times the instant rule names it, and for a sample with large metadata (stored
  compressed, such as an image with many detections) each read decompresses all of it. Measured on copies of COCO,
  KITTI and iNaturalist: the sidebar's bounds take 0.14 s instead of 1.3 s on COCO's 5,000 images (0.09 s instead
  of 0.5 s on KITTI, 0.22 s instead of 0.33 s on iNaturalist), and a newest-first page 0.29 s instead of 0.82 s on
  COCO. On a dataset with small metadata a sorted page can take a few milliseconds longer (iNaturalist: 0.12 s
  instead of 0.10 s). The answers are the same. Filters on such a field still read it a few times a sample.

- **The first Run UMAP reduction after the server starts no longer stops the app for about 5 seconds.** The server
  loaded the UMAP library on the loop that answers every request, so until it had loaded (about 5 s, measured), no
  page, filter or sign-in got an answer. It now loads in the background with the fit, and the longest wait measured
  for another request during a first reduction went from 5 to 10 s down to about 0.2 s.

- **A dataset page opens without waiting seconds for its coverage counts.** The counts of samples with each vector
  field (the "N of M embedded" signal and the grid's missing-vectors warning) folded every vector row's field name
  with a regular expression and joined each row to its sample: about 5 seconds on a million vectors, every time a
  dataset page opened, longer on the public demo while guests' requests queued. They now count each stored field
  once and only fold a patch field's tiles: about 0.6 s on the same dataset, with the same numbers.

- **`FRIDGE_MEDIA_RENDERS` works in the production compose file.** `.env.example` documents it (how many thumbnails,
  crops and overlays each backend process renders at once), but `docker-compose.prod.yml` didn't pass it to the
  backend, so setting it did nothing. Unset, each process still renders as many as it has CPUs.

- **The database image builds again.** `Dockerfile.db` pinned PostgreSQL 18.6 but took the newest PostGIS, and the
  newest (3.7.0+dfsg-1.pgdg12+2, built for PostgreSQL 18.7) needs a server function 18.6 lacks: a fresh build's
  database refused to load PostGIS ("undefined symbol: heap_datum_check"). PostGIS is now pinned with the base, to the
  build 2.11.0's released image has. An image built from an earlier release's `Dockerfile.db` since that PostGIS was
  published (2026-11-11) has the same fault: rebuild it from 2.11.1; the database's data is untouched.

## 2.11.0 (2026-11-11)

2.11.0 gives each object sample its image in a column of its own, `samples.parent_id`, so deleting an image
deletes its objects, the image and object grids link through it, and objects whose image is gone can be found and
deleted. Dates and times in metadata compare as the moments they name, however they are written, and indexed datetime
fields are rebuilt on the moment. A lasso drawn before its map was laid out again keeps filtering and warns instead of
being dropped, a bulk delete refuses it, and a saved view can be updated with the current filters. Index builds run one
at a time on the server. It also fixes the Edit User dialog dropping a typed password, and histogram, sort and SDK
datetime bugs. Six migrations; two of them need the app stopped (about a minute for a few hundred thousand objects).

### Upgrade notes

- **Stop the app before the migrations.** Six database migrations, applied with `scripts/apply_migrations.sh` (step 4
  of [Upgrading](docs/UPGRADING.md); see [Upgrading to 2.11.0](docs/UPGRADING.md#upgrading-to-2110)). `0029` and
  `0030` refuse to run while anything else is connected to the database, as `0021` did; the other four take a moment.
  - `0027_map_layouts.sql` adds a small table for each embeddings map's layout id, and `0028_view_updated_at.sql` a
    column saying when a saved view's filters last changed, with the trigger that stamps it; neither changes a row.
    Every map keeps the layout it has: a lasso saved before the upgrade counts as drawn on it.
  - `0029_object_parent_column.sql` gives every object sample its image in a new column. It checks the free disk first
    and prints its progress. It links about 13,000 objects a second on a large table (3.2 million in 4 minutes,
    measured) and indexes them again (7 s for 3.2 million); other samples cost only the new indexes, about 0.6 s a
    million. If it fails or is interrupted, nothing has changed and it can run again.
  - **`0029` deletes the object samples whose image no longer exists**: those whose `metadata.parent_id` is an id no
    sample has any more, in any dataset. Their vectors go with them, and followers delete them too. It prints, per
    dataset, how many it deleted, and how many objects it kept with no image because they name another object or a
    sample of another dataset, have a `parent_id` that isn't a sample id, or have none. On a follower, a mirror's
    objects aren't deleted: they go when the primary deletes them.
  - `0029` stops, naming it, if an array field is already called `parent_id`: copy that field to another name and
    delete it, then upgrade. A follower of a primary on 2.10.x or older that has an array field named `parent_id`
    refuses that dataset and says to rename the field on the primary.
  - **`0030_object_parent_key.sql` adds the key that deletes an image's objects with it**. It checks every object's
    image in one pass: 2.3 s for 3.2 million objects among 4 million samples, measured. An object whose image was
    deleted after `0029` linked it (a database that ran `0029` and then deleted images before `0030`) is deleted, and
    counted, as `0029` deletes its orphans; on a mirror, or in a dataset being deleted, it is kept with no image.
  - `0031_metadata_instant.sql` adds one function, `metadata_instant_v1`, the rule that reads a datetime field's text
    as an instant, and changes no rows. `0032_datetime_index_rebuilds.sql` builds and drops no index: it marks each
    indexed datetime field not indexed and queues its rebuild, so it takes seconds.
- **Once the app is up again, run `VACUUM (ANALYZE) samples;` in psql once**: every object `0029` linked leaves an old
  row version behind until then.
- **Indexed datetime fields are rebuilt in the background.** They now compare as moments, so their indexes, built on
  the stored text, are rebuilt on the moment. Until its new index is built, a field shows
  **Rebuilding** in Dataset Settings → Indexes, and filters, sorts and bounds on it give correct results without the
  index (slower on a large dataset). The rebuilds run smallest dataset first, and Background jobs shows each as a
  field index build ([Upgrading](docs/UPGRADING.md#upgrading-to-2110)).
- **One index build at a time on the server, field indexes before vector indexes.** A `CREATE INDEX CONCURRENTLY`
  waits for every other one's snapshot, even on another table, so a field index built during an hours-long vector
  index build waited it out and could fail on its one-hour budget. Now every index build (promote, the index advisor's
  Index, `build_ann_index`, the rebuilds, a restore of a removed index) is started only when no other one runs: one
  queued meanwhile waits unstarted, holding nothing, so its dataset's other jobs run, and Background jobs and the
  Indexes tab say what it waits for. The rebuilds go field indexes first (minutes) and vector indexes after (hours). A
  field rebuild on a dataset with an unfinished rename, clone or clear (a stopped one too) waits for it, shown as
  waiting with the job named, instead of failing every day. Creating an array field (and a follower adding its
  primary's) is refused with 409 and `Retry-After` while an index build runs, instead of waiting it out: the SDK
  retries it for up to two minutes, and a follower's sync waits and tries again.
- The API no longer answers 409 `map_refitted` to a filter holding a lasso drawn before its map was re-fitted: it
  filters by it. An SDK script that relied on that refusal gets the samples the polygon selects on the current map.
- **Followers:**
  - **Upgrade followers soon after their primary.** A shared view, and a relayed patch search, is evaluated by the
    server that runs it. Until a primary and its followers all run this release, the same filter on a datetime field
    whose values carry offsets or mixed spellings (`T` or a space, `Z` or `+00:00`) can count differently on each
    side, and one on `last_modified_at` matches nothing on a server still on 2.10.x. Nothing breaks and no protocol
    changes; the answers agree once both are upgraded. A follower rebuilds its own datetime indexes after its upgrade,
    as a primary does.
  - A new lasso still records the map's model (`model_id`) beside its layout, in 2.11 only, so followers on 2.10 or
    earlier keep answering relayed searches that hold one (they refuse a lasso without it on a re-fitted map). 2.12
    stops writing it: upgrade followers to 2.11 or later before their primary takes 2.12.

### Added

- **A lasso warns when its map was laid out again after it was drawn.** A lasso now records the layout of the map it
  was drawn on. When the map is laid out again, by a re-fit, Run UMAP reduction, or an upload of coordinates that
  moves its points, the lasso's chip turns amber ("This map was laid out again after this selection was drawn. It may
  select different samples now. Redraw it or remove it.") and a toast says so once. That covers applied chips, the
  draft, saved views, the default view and links. Lassos saved before this release warn once their map is first laid
  out again.
- **Update this view.** When the filters you opened from a saved view (in Saved Views, as the dataset's default view,
  or from its link) differ from it, its owner or an admin can replace the view's filters with them: Saved Views offers
  "Update this view", after a confirm naming the view. Its name, link and sharing stay, and a default view stays the
  default. A stale lasso's chip says so ("…then use Update this view to save it.") when you may. Anyone else, and a
  mirror's copy of a view on its primary, is offered only saving the filters as a new view. If someone changed the
  view's filters after you opened it, the update is refused ("This view changed since you opened it") rather than
  written over theirs, and Saved Views offers to open it again or save yours as a new view. The API is
  `PATCH /api/datasets/{id}/views/{view_id}/content` with the new `content` and the `updated_at` you read (409
  `view_changed` when it moved; without it the last writer wins). Views now return `updated_at`, which the database
  sets whenever a view's filters change, whoever changes them: an update, a version restore, a mirror's sync.
- **Finding and deleting object samples with no image.** An object whose `parent_id` names no image of its dataset
  is still accepted (its image may come in a later request, and links it then); what stays unlinked can now be seen
  and removed. `POST /api/datasets/{id}/objects/unlinked` counts them by kind (its image is gone, it names a sample
  of another dataset, it names another object, its `parent_id` isn't a sample id, it has none) and returns the
  filter that shows them; for someone who may edit the dataset it first links the objects whose image is there.
  `POST /api/datasets/{id}/objects/unlinked/delete` (admin) deletes the kinds named, by default only the objects whose
  image is gone, as a bulk delete job whose result counts them; never on its own, and refused on a mirror. The SDK
  has `dataset.unlinked_objects()`, `dataset.unlinked_objects_view(kinds)` and
  `dataset.delete_unlinked_objects(kinds)`. In the app, the Objects grid says "⚠ N objects have no image" when there
  are any; clicking it shows them, and an admin can delete those whose image is gone from there.
- **`last_modified_at` can be filtered**, like `created_at`: in the filter card, the query language, links and
  views, and by its histogram's bars. It was read as a metadata key, so a filter on it matched nothing. The sidebar
  shows its date slider on a dataset small enough to compute every field; on a larger one its bounds would mean
  reading every sample on each visit (no index covers it, and one would slow every edit).

### Changed

- **A bulk delete by a filter holding a lasso drawn on an earlier layout of its map is refused.** Deleting samples,
  or a field's vectors, by such a filter (the API, or the SDK's `delete_samples` and `delete_embeddings` on a view)
  answers 409 (code `stale_lasso`) naming the map, and is checked again as the job runs: if the map is laid out again
  after the delete was submitted, the job fails and deletes nothing more. Redraw the lasso, or remove it. Tagging and
  metadata edits by such a filter go ahead and name the maps in their answer (`stale_lassos`); the SDK warns
  (`fridge.StaleLassoWarning`), as it does when a query's filter holds one, and the app's tag dialog says so, with
  the count, before applying.
- **A re-fitted map no longer removes a lasso drawn on it before.** The lasso was refused and dropped, from the bar,
  from a saved view as it opened, and from the scatter's draft. It now stays and keeps filtering, on the new map, with
  the warning above, until you redraw or remove it.
- **The `parent_id` field can't be renamed or cleared, and no field can be renamed or copied over it, on a dataset
  with object samples** (409, saying why): every object would lose its image at once. Writing one sample's
  `parent_id` (a PATCH, a bulk set) re-points that object and stays allowed. A dataset without objects is unaffected.
- **The filter sidebar no longer lists `parent_id`** and computes nothing for it, on either grain. It is still a
  field: the query text, the stage editor's field search, links and the SDK filter on it, and the sample modal shows
  it with the other metadata.
- **An object sample's image is kept in a column of its own, `samples.parent_id`.** Triggers fill it from
  `metadata.parent_id` on every write when that names an image of the same dataset, whichever of the object and its
  image is written first, and leave it empty otherwise. `metadata.parent_id` is still what clients write and read.
  `parent_id` can't be the name of an array field any more.
- **The image and object grids are linked through that column.** When a filter, a selection or a lasso on one grain
  carries over to the other (the object grid showing the objects of the matching images, the image grid the images
  of the matching objects), and when the object grid is ranked by an image search, an object's image is read from
  `samples.parent_id`. The rows are the same; a count visits each row once instead of searching every partner for it:
  on 3.2 million objects, counts that ran past a minute take 1 to 2 s, and the grids' pages are as fast or faster. A
  filter for images with a matching object, sent without a grain, no longer returns an object that another object
  names as its parent: it isn't an image. A filter on the field `parent_id` still reads `metadata.parent_id`.
- The index advisor lists `idx_samples_parent_id` as needed, and never offers to remove it: the trigger that links
  objects to an image when it arrives reads it.
- **Deleting an image deletes its object samples**, whatever deletes it: one sample's delete, a bulk delete, a
  version restore that removes the samples added since, a dataset's deletion, and a delete in psql (a database key
  on `samples.parent_id`). Their vectors, map points and hashes go with them, and followers and statistics are told
  as for any delete. The counts name them: the single delete answers `objects_deleted`; a bulk delete's result has
  `objects_deleted` beside `deleted_count` (the samples it targeted), and job history says "1,200 samples deleted,
  and 9,840 objects with their images."; a restore's `samples_removed` includes them, and so does the count the
  app's revert asks with. A bulk delete deletes an image's objects even when its filter or `excluded_ids` left them
  out, and counts them in `objects_deleted`. Objects that name no image (a junk `parent_id`, an image of another
  dataset) are never deleted this way. An image with objects can't be moved to another dataset. An object written
  while its image is being deleted is refused with a 409 that the SDK retries; written again, it has no image. The
  key makes adding objects slower: through `add_samples`, 11% for 3,000 objects and 16% for 50,000 (measured on a
  small table); in plain SQL on 4 million samples, about 5,400 a second instead of 7,000. Deletes take about 13%
  longer. Adding images costs nothing more.
- A follower's mirror keeps the objects its primary keeps when the primary deletes their image (a primary on 2.10.x
  or older keeps them; a 2.11 one deletes them, or re-points them): its sync clears their `metadata.parent_id` before
  it deletes the image, so they keep their sample ids and `@local.` fields.
- **Find unlabeled regions** (object grain) reads the objects of the images it finds from `samples.parent_id`, through
  its index, to leave out the regions already labeled.
- **Dates and times in metadata compare as moments.** On a field declared `datetime` (and a mirror's own
  `@local.` datetime fields), filters, sorts, the histogram, the sidebar's bounds, `/stats`, the SDK and the query
  language compare the moment a value names, not its text: `2026-01-01T10:00:00+02:00` is the same moment as
  `2026-01-01T08:00:00Z`. A value written without an offset is UTC, and a date alone is midnight UTC. What you
  stored is shown as you stored it. Before and after, on the KITTI importer's Berlin-time values
  (`2011-09-26T13:02:45.123456+02:00`), `captured_at >= 2011-09-26T12:00:00Z` matched that sample (the text
  `13:02` sorts after `12:00`) and now doesn't (it is 11:02:45 UTC). Data written in UTC, or without offsets,
  gives the same answers as before, except:
  - **A date means a whole UTC day**, for `=`, `!=`, `<=`, `>` and now `in` too: `in (2026-01-01)` matched only
    the text `2026-01-01` and now matches that whole day. A value with an offset belongs to its UTC day:
    `2026-01-01T23:30:00-05:00` is in `= 2026-01-02`. To filter by local days, store local time as its own
    field ([Dataset management](docs/DATASET_MANAGEMENT.md#dates-and-times)).
  - **A range on a datetime field needs a date and time:** `captured_at > soon` is refused (400) instead of
    comparing text. Equality with text that isn't a date still compares text, so `captured_at = unknown` finds
    a stray `unknown`.
  - **Samples whose value isn't a date and time** (`2026-02-30`, `n/a`, a number) never match a range, sort
    after every datetime with the samples that have none, and count as missing in the histogram.
- **An indexed datetime field is indexed on the moment it names**, not its text, so range filters, sorts and
  bounds on it use the index. Dataset Settings → Indexes now lists datetime fields too.
- **Sorting by a datetime field puts the samples without one last, oldest or newest first.** An indexed field's
  pages still come from its index; an unindexed field's newest-first page reads the written dates first and
  computes moments only for the samples near the page.
- **The sidebar's date slider sends only the end you moved.** An end left at its limit adds no bound, so newer
  samples, or ones within a fraction of a millisecond of the limit, are no longer cut off, and a handle dragged
  back to its end reaches it. Its dates are UTC and say so. The filter card's date picker is labelled UTC (a
  value without an offset is UTC) and shows a value written with an offset as its UTC time. The histogram says
  "UTC" with a datetime bar's range, and its axis shows dates instead of epoch seconds.
- **The SDK sends a `datetime` without a time zone with the machine's offset.** In metadata and in filter values, a
  Python `datetime` with no time zone now goes with the offset of the machine running the script, so it names the
  instant the script meant (a date and time written without an offset means UTC). This changes what a script
  writes. On a machine in Chicago in January, `metadata={"captured_at": datetime(2026, 1, 15, 10, 30)}` was stored
  as `"2026-01-15T10:30:00"` and is now stored as `"2026-01-15T10:30:00-06:00"`. A `datetime` with a time zone, a
  `date` and text are sent as before, and values already stored don't change. To store clock time without an
  offset, pass it as text.
- **`created_at` and `last_modified_at` follow the rule every datetime follows.** A value means the UTC day, minute
  or second it was written to, whatever its offset, for `=`, `!=`, `in` and `not in` too:
  `created_at = 2026-01-01T10:00+02:00` and `last_modified_at in (2026-01-01)` matched nothing, as the stored moments
  carry fractions of a second. A value written with a fraction is still that exact moment.
- **A datetime filter's chip shows its time.** A bound with a time of day is shown in UTC, to the precision it
  was written (`captured_at ≥ 2026-01-01 08:00 UTC`); it showed the date alone, so `08:00` and `20:00` read the
  same. A bound written with an offset is shown at its UTC time.
- **A new metadata field is a datetime only when its values are written the one way the database reads them**
  (`YYYY-MM-DD`, then optionally `T` or a space, `hh:mm`, `:ss`, a fraction and `Z`, `±hh`, `±hhmm` or `±hh:mm`).
  For example, a new field whose values are written `2026-01-01T10` (an hour without minutes) was typed
  `datetime` and is now `string`; so are `2026-01-01T1000`, `2026-01-01T24:00:00` and a comma fraction, which
  Python's parser read but the database can't. Fields already registered keep their type.

### Fixed

- **An object whose `metadata.parent_id` is a number beyond 2,147,483,647 no longer makes the image or object grid
  fail** while a filter links the two grains.
- **A histogram bar of a time-named field typed text no longer stages a filter that can't match it.** Such a field
  (`seen_at` whose first value was `unknown`) keeps time bars, but its filters compare text, so a click staged the
  bar's edges as numbers. Its bars now say to declare the field a datetime (Dataset Settings) to filter by them.
- **Renaming or clearing a field whose index is being rebuilt removes its old index.** The index on its text,
  named for the old field, stayed behind; a renamed field is now rebuilt under its new name. The background
  (bulk) rename and clear refuse such a field until its rebuild is done.
- **Sorting by an indexed number field puts the samples without a value last from largest first too.** They
  came first, because the index holds them there. The page is still read from the index, in two parts: the
  samples with a value, then the rest by id.
- **A histogram bar's filter returns exactly that bar.** A datetime field's bars cover whole microseconds and
  are labelled with their first and last one, so the `>=` and `<=` a click stages match the bar's samples; float
  edges drifted a microsecond. `created_at = infinity` counts as missing instead of failing the histogram.
- **Datetime bounds are the earliest and latest moments**: the sidebar's (filtered or not), `/stats` and the
  bounds kept at ingest compared text, so a value with an offset could be the wrong end.
- **The histogram of a datetime field no longer fails on a value that isn't a date and time.** A field typed
  datetime holding `2026-02-30` or `2026-01-01 n/a` made its histogram fail with an error 500; such values now
  count as missing. `24:00` and a leap second (`:60`) are no longer binned as the next day or minute but count as
  missing too, and a value without an offset is binned as UTC whatever the database's time zone setting.
- **A `datetime` or `date` anywhere in an SDK request no longer fails with "Object of type datetime is not JSON
  serializable"**: a filter value (`dataset.filter(captured_at__gte=date(2026, 1, 1))`, a filter dict a script
  builds), values for `set_values`, `set_values_per_sample`, `lookup_samples`, `client.update_sample`,
  `client.bulk_set_metadata` or `client.add_samples`. It is sent as metadata is.
- **An admin's password reset in the Edit User dialog can no longer be lost.** The new password had its own Reset
  button, and **Save Changes** (or Enter in the field) closed the dialog without it, so the user kept their old
  password and nothing said so. The dialog now has one button, named for what it does: **Save Changes**, **Reset
  Password** when only a new password is typed, or **Save and Reset Password**. A typed password is reset first; if
  the server refuses it, nothing else is saved and the dialog says why.

## 2.10.0 (2026-11-11)

2.10.0 makes versions keep more of what a dataset was: who made each one, its public saved views and the view it
opened on, and, for history brought in from another system, the date and author it had there. Reverting in the app
puts the dataset back to exactly the version after saying what it will delete, and reverting or deleting a version
now takes an admin. Vector index rebuilds go one at a time, smallest first, with Retry in the jobs list. Media from
S3 is streamed and renders are bounded. Deactivating an account revokes its API tokens, and a password change makes
revoking them a choice. It also frees a deletion stuck on a default view and fixes selections across grids, views
whose count fails, the map across the antimeridian, field names that read as operators, and a follower's copy on an
out-of-range `parent_id`. Four migrations, each instant.

### Upgrade notes

- Four database migrations, applied with `scripts/apply_migrations.sh` (step 4 of [Upgrading](docs/UPGRADING.md)),
  each instant: `0023_stuck_deletion_default_view.sql` changes two database functions and no rows;
  `0024_version_saved_views.sql`, `0025_version_created_by.sql` and `0026_mirror_primary_versions.sql` add nullable
  columns, so they rewrite no table.
- **Reverting a dataset to a version, and deleting a version, now take an admin.** A power user's revert or
  delete, from the app, the API or an SDK script (`dataset.revert(...)`, `client.delete_version(...)`), gets HTTP
  403. Nothing else about versions changes: everyone with access to the dataset, guests included, can still list,
  browse and compare its versions, and making one still takes a power user.

### Added

- **Versions record the dataset's public saved views:** each view's name, description, filters, link and owner
  when the version is made. The snapshot page lists them under **Saved view** and shows the samples a view held at
  that version (`GET .../versions/{id}/samples?view=<name>`; in the SDK, `VersionView.list_views()` and
  `load_view(name)`). A public view added, removed or given other filters since a version is a change since it,
  and the diffs report `view_changes`. A revert puts the recorded views back, making a view deleted since again
  with its link when that is still free. Private views are never recorded or touched. A version made before this
  release recorded no views, and says so.
- **Versions record who made them:** the username of whoever creates one, or reverts for a revert's undo point,
  shown in the Versions tab and on the snapshot page. A version made before this release shows as made by unknown.
- **History brought in from another system keeps its dates and authors.** `create_version(created_at=...,
  created_by=...)` dates a version from when it was made there (with a UTC offset, not in the future) and records
  the author it had there. Without `created_by` the author is unknown, not whoever brings the history in. The SDK
  checks the server recorded both, so a server from before this raises instead of dating the version today.
  A stated date or author is a claim, so such a version also records who made the request (`imported_by`), and
  the app shows "by ada, imported by richard".
- **A revert can restore the dataset's settings** (`restore_settings`; a second checkbox in the app): its
  description, media fields, segmentation classes and colours, and the view it opens on, as the version recorded
  them; not its name. Off by default. A version that recorded no settings refuses it before anything changes.
- **A version records which view the dataset opened on.** The default view is recorded with the saved views, by
  name, so a settings restore finds it again after the view was deleted and made again.
- **A mirror's Versions tab says how many versions its primary keeps.** Versions aren't mirrored: they stay on
  the primary, and those a mirror lists are its own ([FOLLOWERS.md](docs/FOLLOWERS.md)).

### Fixed

- **A selection made in one grid no longer shows in a grid of the other grain.** With an Images grid beside an
  Objects grid, a Select All of more than 50,000 objects ticked every tile of the Images grid and showed its count
  there, an image unticked there was counted against the objects, and Tag there tagged the objects. A selection now
  belongs to the grain of the grid it was made in: only grids of that grain show it, and Tag and Export act on it
  there. The other grid says how many are selected in another panel, and a pick in it starts a new selection.
- **A view whose count fails can still be scrolled to its end.** When counting a view failed (on the public demo,
  a guest's 30-second limit on a large dataset), the grid stopped at its first 200 rows and said "Counting…" for
  good, with no way to count again. It now says the count is unavailable, keeps loading rows as you scroll, down
  to the last one, and offers Retry. The total above the filters says "Count unavailable" too.
- **A field whose name starts with `exclude_` or ends with `_min` or `_max` filters itself.** The sidebar read such
  a name as an operator on another field: ticking `blurry` under a field named `exclude_reason` filtered on
  "`reason` is not blurry", which matched every image while the chip said the filter was applied. Sidebar picks
  saved in the browser before this release are read as they were then.
- **Upgrading: step 4 stops every service but the database and nginx.** [Upgrading](docs/UPGRADING.md) stopped the
  backend, the job runner, the backup service and the frontend before the migrations, and left the `worker`
  service running. 2.9.0's vector migration refuses to run while anything else is connected, so the instruction is
  now the one that needs no judgement about each service: stop `backend job-runner worker backup frontend`, in the
  three guides that give the command.
- **A signed-in user can no longer tell which samples, views and jobs exist in a dataset they can't open.** An
  image request (`/api/samples/<id>/media`), deleting a view by its id, and reading, cancelling, resuming or
  discarding a job answered 404 for an id that doesn't exist but 403 for one in a dataset the caller can't open, so
  the ids of that dataset's rows could be probed. Both now answer the same 404. Nothing changes for a user who can
  open the dataset, and a dataset's own id still answers 403.
- **A dataset whose deletion is stuck on its default view is freed by resuming its deletion.** A dataset deleted
  before 2.9.0 while it had a default view stayed "being deleted": the deletion failed when it reached the view, at
  every resume, and the dataset's row refused every change, so 2.9.0 couldn't free it. **Resume deletion** (an
  admin, on the dataset's page or the dataset list) now finishes it, a stopped mirror's too. The deletion may make
  the one change that removing the view makes to the row, clearing its default view, and nothing else; outside the
  deletion the row still refuses it.
- **A selection that can't be kept across a reload says so.** An applied big Select All (the server keeps it for 2
  hours) and an applied selection of over 50,000 samples were dropped at a reload or Back, and the grid showed
  more samples with no word why. Their chip now stays, greyed and marked expired, and filters nothing: select the
  samples again, or remove it. It is never sent to the server or kept in a saved view, and Copy link copies
  nothing while one is shown and says why. A dataset that opens with one doesn't open on its default view, so the
  count is the whole dataset's. Until the page reloads or Back is used, the selection filters as before.
- **The map shows both sides of the antimeridian.** A view across longitude 180 (over Fiji, the Aleutians or the
  Pacific) left out every sample east of it, with no message, and after panning on to a repeated copy of the world
  the map was empty although the data was there. Both sides are shown now, at every zoom: a bubble on the
  antimeridian counts the samples on both sides of it, and a click on it zooms to them. A map region drawn across
  the antimeridian still selects only the samples west of it, and one drawn on a repeated copy of the world selects
  none.
- **Vector index rebuilds after an upgrade go one at a time, smallest first.** After 2.9.0's migration every rebuild
  was queued at once. Background jobs listed dozens of "Building ANN index" jobs, the waiting ones showed
  "QueueContended … retrying later" with their attempts counting up, and a large dataset's build could go before
  small ones. Now:
  - One build is queued at a time, the field with the fewest vectors first.
  - A dataset busy with another job (a map re-fit, a bulk edit) is skipped rather than holding up the rest.
  - A rebuild that fails is tried again a day later instead of being dropped.
  - A job that waits for another says how long it has waited, instead of looking failed.
  - A build no longer reads every vector to learn the field's dimension: that took over an hour on 3.7 million
    vectors under load, before the build began. Mixed dimensions are refused before the build starts.
- **Retry a failed vector index build from the jobs list.** A failed or cancelled "Building ANN index" job shows
  its field and a **Retry** for power users and admins, until a later build of that field is waiting, running or
  done.
- **A follower's copy no longer stops on a `parent_id` past the integer range.** Ingest accepts any `parent_id`,
  and the copy failed with "out of range for type integer" on a number such as `99999999999`, at every retry. Such a
  number names no sample, so it's copied as no parent, like a negative one. A follower stuck on this recovers at its
  next sync after the upgrade, since the batch is retried; there's nothing to do.
- **A version delete that the server refuses says so.** The Versions tab ignored the answer, so a guest's Delete
  ended silently with the version still listed. Making a version and deleting one are now disabled in the app
  for a role that can't do them, with the reason as the tooltip.

### Changed

- **Deactivating an account revokes its API tokens for good** ([AUTHENTICATION.md](docs/AUTHENTICATION.md#api-tokens)).
  Its tokens stopped working while it was inactive and worked again once it was reactivated. Deactivating now
  revokes them all in the same step, follower and worker tokens included, and reactivating the account doesn't
  bring them back. The Edit User dialog says so under **Active Account**, and after saving it says how many tokens
  were revoked and how many of them were follower or worker tokens; `PATCH /api/users/{id}` answers
  `revoked_tokens`. An account deactivated before this release keeps its tokens until it is saved inactive again
  or an admin revokes them (`DELETE /api/users/{id}/tokens`).
- **Revoking the API tokens is a choice when a password changes** ([AUTHENTICATION.md](docs/AUTHENTICATION.md#roles)).
  This changes 2.9.0's behaviour. An admin reset no longer always revokes: it asks, ticked by default (**Also revoke
  their API tokens** in the Edit User dialog). Unticked, the user is signed out and their tokens, follower and
  worker tokens included, still work, and the dialog says so before and after. `POST /api/users/{id}/reset-password`
  takes `"revoke_tokens": false` for that; without the field it revokes, so an older client resets as 2.9.0 did.
  Changing your own password can now revoke your tokens too: **Also revoke my API tokens**, unticked by default,
  also in the change asked for at the first login (`"revoke_tokens": true` in `POST /api/auth/change-password`).
  It then says how many tokens were revoked and how many of them were follower or worker tokens.
  `reset_admin.py --keep-tokens` keeps the admin's tokens; without it they are revoked, as before.
- **Images and videos from S3 are streamed.** A full-size image or a video from a bucket not yet in the server's
  cache was read whole into memory before the first byte went out, so a few large ones at once, or many at once
  on the public demo, could use up the server's memory. It now goes out a megabyte at a time as it arrives, with
  the same headers, and into the cache as before, the rest of it too when the viewer leaves early. Finding out
  whether an S3 PNG or TIFF is 16-bit reads its header, not the whole file. Local files were already streamed.
- **Thumbnails, crops and overlays render a few at a time.** Each needs the whole image in memory, so each backend
  process renders as many at once as the machine has CPUs (`FRIDGE_MEDIA_RENDERS` sets another number; it is per
  process, so the server as a whole renders the number of processes times it) and the others wait their turn: a
  cold grid fills in a little more slowly under load, and none is refused. Cached thumbnails don't wait. No size
  limit is added.
- **Reverting takes an admin; it took a power user.** An exact revert deletes the samples added since, which
  deleting samples already took an admin to do, and a revert can now also rewrite or remove public saved views
  and restore the dataset's settings. In the app the button is disabled for anyone else and says why.
- **Deleting a version takes an admin; it took a power user.** A deleted version can't be brought back, and it is
  the record of what the dataset was.
- **Reverting in the app puts the dataset back to exactly the version:** samples and public saved views added
  since are removed, and deleted ones restored. The confirmation counts them first and says how many samples and
  views it deletes and restores, that deleted samples lose their vectors and hashes, and when one of the deleted
  views is the one the dataset opens on. Without the counts it doesn't ask and nothing is reverted. Unticked, the
  revert is **Restore sample data**, as before. **The API's and the SDK's revert defaults are unchanged**
  (`exact_membership=False`), so a script's revert doesn't start deleting samples.
- The SDK's `add_samples(on_duplicate="update")` docstrings say what it keeps: an existing sample's tags are
  merged, not replaced, and only the vectors and hashes given are replaced.

## 2.9.0 (2026-11-10)

2.9.0 gives each dataset its own partition of the vector table, with its own vector indexes, so a search plans and
locks that dataset's vectors only, and near-duplicates and uniqueness stay in their dataset. Its migration copies
every vector with the app stopped (about a minute per million). It also adds links to a filter, a view or a
sample, with Copy link; a follower's fields of its own on a mirror; and fixes from a review: datasets with a default
view can be deleted, media and thumbnails check access and buckets first, on-demand workers, the launcher, password
resets ending API tokens, versions, and number filters. Two migrations.

### Upgrade notes

- Two database migrations, `0021_embeddings_partitioned.sql` and `0022_local_fields.sql`, applied with
  `scripts/apply_migrations.sh` (step 4 of [Upgrading](docs/UPGRADING.md)).
- **Stop the app before the migrations** ([Upgrading](docs/UPGRADING.md#upgrading-across-290)). `0021` copies every
  vector into a table with one partition per dataset, so it refuses to run while anything else is connected to the
  database. It takes about a minute per million vectors (9.4 minutes for 9.7 million, measured), and needs free
  disk about the size of the vectors and their plain indexes, plus `max_wal_size` and a quarter again; it checks
  first and prints its progress. If it fails or is interrupted (a dropped ssh session: run it in tmux), nothing has
  changed and it can run again. If the database server itself crashes during the copy, the partial copy's files stay
  on disk until that database is dropped ([Upgrading](docs/UPGRADING.md#upgrading-across-290) says how).
- The vector (HNSW) indexes aren't copied. When the app starts it builds them again, one per dataset and field,
  smallest dataset first, and a dataset's similarity searches are exact until its index is built: on 3.7 million
  1,024-dimension vectors, 8 s warm and up to 90 s cold, for 2 to 6 hours until both of its fields' indexes are
  built. A guest's search there can hit its 30 s limit meanwhile. Every dataset and field a shared index served gets
  its own, a follower's mirrors and small datasets included.
- `apply_migrations.sh` now also sets the database's `plan_cache_mode` on every run (`database_settings.sql`): a
  restored backup doesn't carry database settings.
- `0022` adds two columns to samples with constant defaults, so it rewrites no table. It first stops, naming them,
  if an array field is already called `local_metadata` or `local_tags`, or a field's name already starts with
  `@local.`.
- **A metadata key starting with `@local.` is refused on every dataset now** (422 when samples are added or set).
  The prefix names a mirror's own fields. Other keys that start with `@`, such as `@timestamp`, are unaffected.

### Added

- **Links that open a dataset on a filter, a saved view or a sample.** `/datasets/<id>?q=split = train` opens the
  dataset on that filter, written in the query language (docs/FILTER_QUERY.md), one chip per `AND` part.
  `?view=<slug>` opens a saved view, `?sample=<id>` opens a sample, and `?sort=<field>&dir=desc` sets the grid's
  sort. Filters the language can't write (map regions, lassos, picked samples, searches, concepts) go in `?f=`,
  encoded with a version number: a link made by a version that reads it differently says so and opens nothing.
  The link's filters replace the ones the browser kept for the dataset, which are one Back away, and the dataset's
  default view isn't put on top. The address loses the link once it has opened, so a reload keeps later changes.
  A view the reader can't open, a filter with a mistake or a sample of another dataset is reported, and the
  dataset opens as it would without the link.
- **Copy link.** A **Copy link** button beside **Saved Views** copies a link to the applied filters and the
  grid's sort, each saved view has one for the view, and the sample view's adds the sample. The filters go in as
  query text when every chip can be written so and reads back the same, else encoded. The unapplied draft isn't in
  the link, and the message says so. A big Select All (the server keeps it for 2 hours) and a link over 6,000
  characters (about a thousand picked samples) copy nothing and say to save a view and copy its link instead. A
  link opens only for people who can open the dataset, or the view. Where the browser doesn't let the page copy,
  the link is shown to copy by hand.
- **Signing in goes back to the page asked for.** Opening a link while signed out, or a session expiring, goes to
  the login page and then back to that page, filters included. Only a page of this server is followed; anything
  else goes to the dataset list.
- **A follower's fields of its own on a mirror** ([FOLLOWERS.md](docs/FOLLOWERS.md#fields-of-its-own)). A follower
  can add labels, predictions, notes and tags to a mirror beside the primary's, named `@local.<name>`
  (`@local.weeds`, and `@local.tags` for its own tags), and can never change or remove the primary's. They filter,
  sort, colour a map, show on the grid and in a sample's view, and export like any field. The sidebar lists them
  under **On this server**, and the tag dialog offers **This server's tags**. The primary never sees them, and a
  re-read or resumed copy keeps them; after a primary restore, the question before resuming says how many of the
  samples it would delete have them. They're written through bulk-set, bulk-tag and the sample edit, or the SDK
  (`client.bulk_set_metadata(dataset_id, sample_ids=[...], metadata={'@local.weeds': 3})`), and read with
  `sample['@local.weeds']`.

### Changed

- **Each dataset's vectors are their own partition** of the embeddings table, with their own vector indexes
  (`hnsw_emb_d<dataset>_<field>`). A search plans and locks one dataset's vectors and indexes only: with shared
  indexes, every statement paid for every dataset's (up to 330 ms of planning at 3,000 indexes, and concurrent
  searches failing with "out of shared memory"). `build_ann_index` builds the index of the dataset it's called on.
- **An embedding field's dimension is fixed per dataset**, not across every dataset on the server.
- **Near-duplicates and uniqueness compare a dataset's samples with each other only.** Their neighbours came from
  every dataset with the same field name before.
- Deleting a dataset drops its vectors' partition once its rows are gone.

### Fixed

- **A dataset that has a default view can be deleted.** The deletion job failed when it reached the view, at
  every resume, and the dataset stayed "being deleted". The default view is now cleared when the deletion starts.
  A dataset already stuck that way isn't freed by this release (its row refuses every change while it is being
  deleted); that needs a later one.
- **Image requests no longer tell a signed-out caller which samples exist.** `/api/samples/<id>/media` answered
  "not found" for a missing sample, "sign in" for an existing one, and another "not found" for an existing
  sample whose asked-for field was empty, so sample ids and the presence of a field could be probed without an
  account. No image or value was returned. It now asks who is calling first, and checks access to the dataset
  before saying anything about the field.
- **Deleting a dataset no longer fails while a backup runs.** The deletion's last step removes the dataset's field
  indexes, which waits for every transaction reading the samples table. A backup holds that table for the whole
  dump, so the wait ran out, and each time counted as a failure: after the fifth the deletion failed with every
  sample already gone, and the dataset stayed "deleting" until an administrator resumed it. The step now waits for
  a running backup first, and waits out other long reads (a map re-fit's export) without failing.
- **On-demand compute workers: a power user's own worker no longer holds back a machine.** The job runner started a
  machine only when no compute worker was free, and counted every idle worker as free. A power user's worker takes
  only jobs on datasets its owner can access, so a re-fit of another dataset waited with no machine until it was
  cancelled 12 hours later. A worker now counts only for the jobs it can take.
- **On-demand compute workers: machines that can't work no longer start one after another.** When every machine
  failed the same way (an image that doesn't start, a server it can't reach), each one powered off or was ended
  and the next minute started another, until the job's 12 hours or the month's machine-hours ran out, while System
  status showed no error. After two machines in a row never connect, no more start until an admin saves the
  settings again, and the error says why. No machine starts either while the admin who saved the settings is
  deactivated or no longer an admin: the machines' workers use that admin's token.
- **On-demand compute workers: changing the region no longer loses track of a running machine.** The job runner
  looked at every machine in the region saved last. AWS doesn't know a machine's id in another region, so a machine
  started before the change looked gone: its record ended while it ran on, outside the time limits and the month's
  machine-hours. Each machine is now looked at and ended in the region it started in, and the settings page refuses
  to change the region or the AWS keys while a machine runs.
- **`npx freedomridge` sizes PostgreSQL for a laptop.** `docker-compose.try.yml` loaded the database settings tuned
  for a 32 GB server, among them 8 GB of shared buffers: more memory than a small Docker Desktop has to give, where
  the database may not start at all. It now starts with 512 MB of shared buffers, 16 MB of work memory
  and 256 MB of maintenance memory; `POSTGRES_SHARED_BUFFERS`, `POSTGRES_EFFECTIVE_CACHE_SIZE`, `POSTGRES_WORK_MEM`
  and `POSTGRES_MAINTENANCE_WORK_MEM` in the folder's `.env` give it more. Docker needs at least 4 GB of memory for
  it ([launcher/README.md](launcher/README.md)).
- **`npx freedomridge --dir` starts a separate installation.** Every folder ran as the same Docker Compose project,
  so starting a second folder replaced the first one's containers with its own, on the first one's database, and
  `docker compose ... down -v` from either folder deleted that one database. A folder other than `~/freedomridge`
  now gets its own project, named in its `.env`, with its own containers and database. `~/freedomridge`, and a
  folder an earlier launcher already started, keep the project and database they have. The launcher refuses to
  start a folder whose project's containers belong to another folder, and says which.
- **Upgrading stops the app before the migrations.** [Upgrading](docs/UPGRADING.md) applied the migrations while the
  old version still ran, and recreated its containers only afterwards. In between, the old API went on serving
  requests and the old job runner went on running jobs against the changed schema: the API checks the schema only
  in `/api/health`, the job runner only when it starts. Step 4 now stops the backend, the job runner, the scheduled
  backup and the frontend first, as [Docker deployment](docs/DOCKER_DEPLOYMENT.md) and [Deploy to AWS](docs/DEPLOY_TO_AWS.md) now do too.
  `npx freedomridge` already stopped the app before migrating.
- **An admin's password reset ends the user's API tokens** ([AUTHENTICATION.md](docs/AUTHENTICATION.md#api-tokens)).
  A reset signed the user out but kept their tokens, so a token copied by whoever had the account worked again as
  soon as the user chose a new password. `reset_admin.py` with a password left an admin's old tokens working at
  once. Both now revoke the account's tokens with its sessions, and an admin can revoke all of another user's
  tokens with `DELETE /api/users/{id}/tokens`. That includes the follower and worker tokens the account issued:
  followers that connect with one stop syncing until they are given a new token (Settings → Followers), and
  on-demand machines started under it end. The Edit User dialog says so under **Reset Password**, and after a
  reset it says how many tokens were revoked and how many of them were follower or worker tokens. Deactivating an
  account and changing your own password work as before.
- **A version's answers no longer change with the live dataset.** Filtering or aggregating a version on a label
  path such as `animals.label` read which fields are lists from the dataset as it is now. Once `animals` was
  cleared in the dataset, the same query on the unchanged version matched nothing; for a box field such as
  `vehicles` it happened after the next statistics refresh. A version is now read with the fields it stored.
- **A compute worker stops working on a dataset its owner can no longer access.** When an admin removed a power
  user's access to a private dataset while that user's worker was fitting one of its maps, the worker could still
  download the job's data, upload a result and keep renewing its lease until the job ended. Those calls now check
  the access again, as claiming a job does. The worker drops that job and goes on with others, and the job goes
  back to the queue. Nothing else changes.
- **A filter like `score = "1"` on an indexed number field matches the same samples everywhere.** On a number field
  with an index, the grid compares a number written as text with the stored numbers, so it finds `1.0`. Duplicate
  groups, sparse sampling, bulk tag, delete and set by filter, a follower's tag edits and version queries compared
  the text `"1"` and missed it. They now match what the grid matches. A field without an index still compares
  such a value as text.
- **Thumbnails and overlays stop when a dataset loses a storage bucket.** After an admin restricted a storage
  configuration to other datasets, the original images of a dataset that lost it were refused, but thumbnails,
  crops and heatmap overlays already made from them were still served from the cache to anyone with access to
  that dataset. The bucket is now checked before the cache is read. Nothing else changes.

### Removed

- **`backend/init_db.py`.** A leftover script from before migrations: run against an installation it dropped the
  dataset, sample, vector and field tables (and, by CASCADE, everything that refers to them) and then failed
  partway through the schema, so the data was gone and the
  database unusable. Nothing in Freedom Ridge called it: a new database is created from `backend/db/schema.sql`
  when its container first starts, and an existing one is upgraded with `scripts/apply_migrations.sh`.

## 2.8.0 (2026-11-10)

2.8.0 lets a follower resume into its copy after its primary is restored from a backup, instead of copying every
dataset again. It also fixes what a review found: similarity searches that came back short under a narrow filter,
the sidebar scanning a large dataset for an overlay type on every load, a big Select All's chip, a lasso after a
reload, unanswered launches of on-demand workers, array fields named after a samples column, object parents when
copying between instances, and SDK filters that ignored `and`, `or` and arguments that aren't conditions. One
migration.

### Upgrade notes

- One database migration, `0020_mirror_restores.sql`, applied with `scripts/apply_migrations.sh` (step 4 of
  [Upgrading](docs/UPGRADING.md)).
- A follower asks before resuming only when the restored primary runs this release too (it answers `match-keys`).
  A primary on an older release gets a new copy on its followers, as before.
- A large dataset whose statistics last ran before an upgrade added an overlay type (class probabilities, 2.4.0)
  gets a full statistics refresh in the background the first time its sidebar opens: about 20 minutes for 3.7
  million samples. Meanwhile the sidebar shows no fields of that type.

### Added

- **After a primary is restored from a backup, a follower asks before resuming into its copy**
  ([FOLLOWERS.md](docs/FOLLOWERS.md#when-it-stops)). Until now each copy stopped and a new dataset was copied, so
  what the follower had added (patch vectors computed on its own GPU, indexes, concepts, views, access grants) had to
  be made again. Now the copy of each dataset the primary still shares is held, and the primary's other datasets keep
  syncing. The follower matches the primary's samples to the copy's by file path, and by image hash where both have
  one, without changing anything. The copy's row on Settings → Primaries then shows the counts and asks: **Resume
  into this copy** keeps what this follower added on the matched samples, deletes the ones the primary no longer
  has (after a second confirmation), and copies the rest fresh. **Make a new copy** does what happened before.

### Fixed

- **A big Select All's chip no longer offers Exclude.** Above 50,000 samples a Select All is applied as one
  chip. Clicking it relabelled it "Exclude: …" while the grid, the count and the export stayed on the selected
  samples. The chip has no toggle now; chips of picked samples and of searches still switch between include and
  exclude.
- **On-demand compute workers: a launch AWS never answered keeps its machine.** When the request to start a
  machine timed out, the launch was recorded as refused, so a machine AWS had in fact started ran on with no record
  and no one to end it. The launch now stays open: the next minute finds the machine by its tag, or ends the launch
  after 15 minutes if there is none.
- **An array field can't take the name of a column samples already has.** `lon`, `lat`, `created_by_user_id` and
  `modified_by_user_id` were missing from the reserved names. Creating an array field by one of them (or following
  a primary that has one) changed no column and registered the name as an array field anyway. It is refused now.
- **A similarity search no longer comes back short when more samples match.** A search of up to 1,000 results
  walks the vector index within a budget of 200,000 vectors. Under a narrow filter it could reach the budget with
  matches left and answer with fewer results than asked, which looked the same as "nothing else matches". A short
  answer is now checked: the matches are counted, and when more match than came back they are ranked exactly,
  without the vector index. The check gets 5 seconds: under a narrow filter on a field with no index it would read
  the whole dataset (over a minute on 3.7 million samples). Past that the search answers with what the index found
  and its chip says "more may match". Patch searches still have the budget.
- **A lasso on a large embeddings map comes back whole after a reload or Back.** On a map shown as a sample of
  its points, a lasso selects every point inside it, on screen or not. After a reload, or Back and Forward, only
  the points that had been on screen were still selected, so Apply, Tag and Export acted on a part of the
  selection with nothing saying so. The lasso's outline is now kept with them.
- **SDK: `Dataset.filter()` refuses an argument that isn't a condition.** It skipped one without a word, so
  `ds.filter(s.reviewed)` or a dict filter selected the whole dataset. It raises `TypeError` now.
- **SDK: `and`, `or`, `not` and chained comparisons on filter conditions raise.** `ds.match((s.a > 1) and (s.b < 2))`
  kept only the second condition, and `0 < s.a < 1` only `s.a < 1`, because Python asks a condition for its truth
  value and every object is true. A bulk tag or delete then hit more samples than the filter said. A condition now
  raises `TypeError` there, and the message names `&`, `|` and `~`. Scripts that relied on the old behaviour were
  filtering on one clause.
- **Copying a dataset to another instance gives objects their image's new id**
  ([BACKUP_AND_MIGRATION.md](docs/BACKUP_AND_MIGRATION.md#option-2-dataset-sync-via-sdk-selective-works-across-different-instances)).
  `python -m fridge.sync_between_instances` copied an object sample's `metadata.parent_id` unchanged, so on the target
  it named whichever sample had the source image's id, or none, and parent and object filters selected the wrong
  samples. Each object now gets its parent's id on the target, also when the parent is copied after it or the object
  was copied by an earlier run. An object whose image isn't in the dataset gets no parent, and the run says how many
  there were. Datasets copied with an earlier release can be put right by running the copy again.
- **The sidebar no longer scans a large dataset for an overlay type on every load.** A dataset last measured
  before an upgrade added an overlay type (class probabilities, 2.4.0) had no cache for it, and the sidebar's scan
  gave up after 10 s every time ("Failed to discover `__probability_fields__` … statement timeout"). A scan that
  runs out of time now queues one full statistics refresh, which measures the dataset in the background and publishes
  every overlay cache. Until it finishes, the sidebar shows no fields of that type and doesn't scan for them.

## 2.7.0 (2026-11-10)

2.7.0 completes followers. A primary can run patch searches on the patch vectors a follower computed itself, a new
copy of a dataset resumes into its stopped mirror and keeps what the follower added there, stopping tells the
primary, and edits sent up say who made them. It also fixes what a review of followers found: restored followers,
bound tokens, media from a primary, large change logs, `TRUNCATE`, mirrors of many-field samples and long copies
holding up other mirrors. Five migrations.

### Upgrade notes

- Apply the migrations with the app stopped. `0019_follower_truncate_guard.sql` adds triggers to the sample and vector
  tables under a 2 s lock timeout, so on a busy server it fails quickly and has to be run again.
- Primaries and followers can be upgraded in either order ([Upgrading](docs/UPGRADING.md)): a 2.7.0 follower works
  with a 2.6.x primary, and a 2.6.x follower with a 2.7.0 primary. After a primary is upgraded, a follower restored
  from an older backup copies its datasets again (409), and a follower taken back to a release before 2.6.1 is refused
  until **Reset instance** on the primary (both under Fixed).
- Four database migrations, `0015_follower_fields.sql`, `0016_follower_searches.sql`, `0017_mirror_resume.sql` and
  `0018_follower_leave.sql`, applied with `scripts/apply_migrations.sh` (step 4 of [Upgrading](docs/UPGRADING.md)).
- One more database migration, `0019_follower_truncate_guard.sql`. From it, a `TRUNCATE` in SQL of the sample,
  vector, hash, map coordinate, field or view tables is refused while any dataset is followed or is a mirror.
  Nothing in Freedom Ridge truncates them; a script of your own that does should delete the rows instead.

### Added

- **Followers report the patch scales they hold themselves** ([FOLLOWERS.md](docs/FOLLOWERS.md#patch-scales-of-its-own)).
  A follower with patch vectors the primary doesn't have (computed on its own GPU, say) tells the primary which
  scales, at most every 10 minutes, and Settings → Followers shows them. The follower API is now `protocol: 2`. This
  is the first step to patch searches on the primary that run on a follower's patch vectors.
- **Patch searches on a follower's patch scales** ([FOLLOWERS.md](docs/FOLLOWERS.md#patch-scales-of-its-own)). On the
  primary, the sample viewer offers the scales a follower holds, and a patch search on one runs on the follower,
  which polls for them over its own connection: a small hosted primary can offer 8×8 patch search without holding
  the vectors. The results are the primary's own samples, as a local search shows them.
- **A follower's new copy of a dataset resumes into its stopped mirror** ([FOLLOWERS.md](docs/FOLLOWERS.md#when-it-stops)),
  when the same primary database shares it again (re-shared, the primary added again, or after a 409), keeping
  what the follower added there: patch vectors computed on its own GPU no longer have to be computed again.
  Samples gone from the primary go. A stopped copy can be kept as a separate copy instead.
- **Stopping tells the primary** ([FOLLOWERS.md](docs/FOLLOWERS.md#when-it-stops)). Stop following on a follower
  tells its primary, which stops keeping its change log for it: before, a follower that stopped held back the
  primary's log pruning until its admin removed it. Adding the primary again brings back what it followed, with the
  same options, resumed into its mirrors. Settings → Followers shows a follower that stopped, and what comes back.
- **Edits sent up say who made them** ([FOLLOWERS.md](docs/FOLLOWERS.md#edits-sent-to-the-primary)): a tag or view
  from a follower shows its maker there ("alice on lab"), display only.

### Fixed

- **Followers: a shared view's parent ids are translated** ([FOLLOWERS.md](docs/FOLLOWERS.md#what-a-mirror-holds)).
  Copying a shared view, either way, translated only `id`, so a filter on an object's parent (`parent_id`,
  `metadata.parent_id`) kept the other instance's ids and selected nothing, or another image's objects. A view
  with a range on sample ids (`id > 15`) now stays on the instance that made it, as a view with a concept does:
  each instance numbers its samples in its own order.
- **Followers: two adds of one primary at the same time make one link.** Adding the same primary at two addresses
  at once (a LAN address and a DNS name, say) could make two links, which shared one subscription there:
  one's acknowledgements pruned changes the other hadn't read. The second add now waits and is refused (409).
- **A follower restored from an older backup copies again instead of missing changes.** The primary prunes its
  change log up to what each follower has acknowledged, and a restored follower read the feed from its older
  cursor without being told those rows were gone. The feed now refuses a cursor behind the acknowledged one (409):
  the follower stops that copy and takes a new one, which resumes into the same mirror.
- **A follower token bound to an installation is refused when a call names no installation.** Before, such a
  call passed, so a second install could share the subscription. A follower before 2.6.1 never binds its token and
  isn't affected; one taken back to such a release is refused until **Reset instance** on the primary.
- **An image a follower fetches from its primary can't run as a page.** The follower passed on whatever content
  type the primary answered with, from its own address and without the sandbox and download rules its other media
  gets, so a primary could have served a page with script that ran as the person viewing it. Those answers, and
  the cached copies, now follow the same rules: only an image or a video is shown, and anything else downloads.
- **Deleting a followed dataset no longer stalls on its change log.** The log a follower hadn't caught up on was
  removed in one statement at the end of the deletion, which could run out of time on every try once the log was
  large. The deletion now removes it in batches, like the dataset's other rows.
- **`TRUNCATE` can't bypass followers.** Run in SQL on the sample, vector, hash, map coordinate, field or view
  tables, it skipped the change log followers read and the protection that keeps a mirror read-only. It is now
  refused while any dataset is followed or is a mirror, with a message that says what to do instead.
- **A mirror no longer stops when a sample gains more than 64 vector fields at once.** 4×4 and 8×8 patch tiles on
  one image are 80 fields: the follower asked the primary for all of them in one request, which the primary
  refused, on every retry. It now asks for them 64 fields at a time.
- **Map coordinates of a vector added to an already mirrored sample arrive with it.** When the same sync also
  re-read that sample's block, the coordinates were written before the vector was there and then dropped, so the
  sample was missing from the mirror's map until that part of the primary's map changed again.
- **Samples that swap filepaths on the primary keep their vectors on the mirror.** The mirror deleted the sample
  that held the path and made a new one without its vectors, and without anything the follower had added to it.
  It now keeps both samples and only changes their paths.
- **A long copy no longer holds up a primary's other mirrors.** A follower synced one mirror at a time, lowest
  first, until it had caught up, so a first copy of a large dataset (hours) kept every other dataset of that
  primary from syncing. The mirrors now take turns, a step each.
- **Changing a follower's options on Settings → Followers no longer undoes the change made just before it.** Each
  change sends all three options; one made before the page had reloaded the list sent the previous option's old
  value. The options now wait while a change is saved.

## 2.6.1 (2026-11-09)

2.6.1 fixes followers, from a review of the feature: a restored primary is detected and its followers copy again,
one failing mirror no longer holds up the others and backs off, a follower can change only tags on the primary, a
token is bound to one follower installation, and re-read blocks and big embedding jobs no longer make the follower
re-download vectors. Primaries and followers can be upgraded in either order. Two migrations.

### Upgrade notes

- Two database migrations, `0013_follower_restarts.sql` and `0014_follower_instance.sql`, applied with
  `scripts/apply_migrations.sh` (step 4 of [Upgrading](docs/UPGRADING.md)). Existing follower subscriptions keep
  their place.
- A primary's identity now names its database too. Primaries and followers can be upgraded in either order, and
  nothing is copied again: a follower before this release keeps following an upgraded primary (it detects
  restores once it's upgraded too).

### Fixed

- **Followers: restores are detected, and their subscriptions start again.**
  - A restore that drops and creates the database (as the AWS guide does) kept the cluster's identity, so
    followers carried on from cursors into a database that no longer matched. Sample ids reused by the restore
    could then put a follower's own data on other images. The identity is now the cluster's and the database's,
    so they get a 410 and copy again.
  - After a restore into a new cluster, a dataset's subscription waited forever on a start from the old
    database. It now starts again on the primary's own, as does one whose start was never set.
- **Followers: one mirror that fails no longer holds up the other mirrors of its primary.** Its error shows on the
  Primaries page, and it's tried again after a minute, waiting twice as long after each failure in a row, up to an
  hour. A vector field clash with a same-named field indexed at another
  size here says which field.
- **Followers: the `tags` option covers tags only.** A follower could change any array field of the primary's.
- **Followers: a shared view that uses a concept stays on the instance that made it.** Sent to the primary, it
  pointed at another concept there, or none, and the next sync then deleted it on the mirror.
- **Followers: a primary field that takes the name of one of the mirror's own replaces all of it at once,** its map
  too, for vector and hash fields. Before, the mirror's own values stayed on the samples the feed hadn't named yet,
  one field mixing two models, and those were then read-only.
- **Followers: a mirror's own vector fields can be deleted** (patch vectors imported there, say). Every delete on a
  mirror was refused.
- **Followers: one subscription, one follower instance.**
  - An instance refuses a second link to a primary it follows under another address.
  - The primary binds a follower's token to the first instance that uses it (by an installation id that a dump
    carries, so a restored or moved follower is still the same instance), and refuses it from another. **Reset
    instance** on the Followers page hands the token to a follower reinstalled from scratch.
  - Two instances on one subscription shared its acknowledgements, so the faster one's pruned changes the other
    hadn't read.
- **Followers: tagging samples on the primary no longer re-downloads their vectors.** A changed block (a bulk tag,
  say) made the follower fetch every vector of every field of its samples again: tens of GB for a large dataset.
  Now a re-read block brings only the vectors of samples new to the mirror; the feed's vector changes carry the
  rest. Writing a vector also no longer uses up an `embeddings.id` value for a row that already exists.
- **Followers: feed pages are bounded by their vector changes.** A large embedding job on the primary made pages
  of up to about a million changes, which the follower rewrote in its checkpoint on every step. A follower now
  asks for pages of at most 20,000 (`vector_limit`, in whole transactions); an older primary ignores it.
- **The embeddings panel no longer warns of a crash that didn't happen.** Searching (Find similar, say) before
  the scatter had finished loading remounted the panel, which took its own load marker for a crash: it dropped to
  a small sample and said the previous load may have run out of memory. Only a marker from another page load
  counts now, since a real crash always means a new page load.
- **Followers: only the primary's own refusal stops the mirrors.** It now says, with a code, when the follower was
  removed or its token deleted. Before, any 401 or 403, from a proxy say, stopped every mirror, and a new copy of
  each started once the primary answered again.

## 2.6.0 (2026-11-09)

2.6.0 lets a primary serve a followed dataset's images to its follower, an option off by default, and shows 16-bit
and float greyscale images (multispectral bands, thermal frames) instead of a white picture. The AWS guide shows how
to move an x86_64 server to Arm. One migration.

### Upgrade notes

- One database migration, `0012_follower_images.sql`, applied with `scripts/apply_migrations.sh` (step 4 of
  [Upgrading](docs/UPGRADING.md)): the followers' images option, off.
- Only for datasets with 16-bit or float greyscale images (see Fixed). Their white thumbnails stay in the server's
  caches until removed: delete the dataset's folder, `<dataset id>/`, under `FRIDGE_CROP_CACHE_PATH` and, if it
  has overlays, under `FRIDGE_COMPOSITE_CACHE_PATH`. A browser that already showed one may keep it until a hard
  refresh. Embeddings, hashes and image metrics computed for those images described a white image: compute them
  again (`compute_embeddings`, `compute_hashes(overwrite=True)`, `compute_metrics(skip_existing=False)`).

### Added

- **A primary can serve a followed dataset's images to its follower** ([FOLLOWERS.md](docs/FOLLOWERS.md)). With
  the dataset's new **Serve images** option on (Settings → Followers, off by default), a follower that can't read
  an image itself, on the primary's disk or in a bucket it has no credentials for, gets the image, thumbnail,
  crop or overlay from the primary and caches it. The primary serves only that dataset's samples, through a
  follower-only endpoint (`GET /api/follower/datasets/{id}/samples/{sample}/media`) with the usual media checks.
  The Primaries page shows the option on each mirror.
- **Moving an x86_64 server to Arm** ([DEPLOY_TO_AWS.md](docs/DEPLOY_TO_AWS.md#moving-an-x86_64-instance-to-arm)),
  without a dump and restore: PostgreSQL's data directory moves as it is, checked with `amcheck` on the demo's
  database. Copy `/data` to new disks with rsync while the server runs, then again after stopping it. Not onto
  a disk restored from an EBS snapshot, which loads each block lazily and stalls queries. Arm is now stated as
  supported and recommended in the deployment guides.

### Fixed

- **16-bit and float greyscale images** (a multispectral band or a thermal frame, saved as a 16-bit PNG or TIFF or
  a float TIFF) showed as pure white in grid thumbnails, crops and overlays, and search by image, image metrics,
  hashes, embeddings and 3D-object crops saw a white image too: converting to 8-bit RGB clipped every value above
  255.
  - Each such image is now stretched: its 0.5th to 99.5th percentile spans 0..255, so a 10-, 12- or 14-bit
    sensor's images are neither dark nor white, and a few hot pixels don't set the range. Each image gets its own
    contrast.
  - The full view of such a sample image is converted the same way, since a browser shows a 16-bit PNG by its top
    8 bits and most can't show a TIFF. Masks and heatmaps keep their raw values.
  - 8-bit images are converted exactly as before.

## 2.5.0 (2026-11-09)

2.5.0 runs on Arm: the release images are built for `linux/arm64` as well, the AWS module defaults to a Graviton
instance, and on-demand workers start Graviton machines. `npx freedomridge` runs a release on one machine with
Docker, and upgrades it. Models' weights are downloaded once, and follows don't chain. One migration.

### Upgrade notes

- One database migration, `0011_compute_launcher_arm.sql`, applied with `scripts/apply_migrations.sh` (step 4 of
  [Upgrading](docs/UPGRADING.md)). It changes the on-demand workers' default instance type, and the saved one if
  they were never turned on or used.
- On-demand workers need one more AWS permission, `ec2:DescribeInstanceTypes`, to pick the machine image for the
  instance type's architecture ([ON_DEMAND_WORKERS.md](docs/ON_DEMAND_WORKERS.md)). The Terraform module adds it.

### Added

- **Arm images.** The release images on ghcr.io are built for `linux/arm64` as well as `linux/amd64`, under
  the same tags, so Freedom Ridge runs natively on Arm servers (AWS Graviton) and Apple-silicon Macs. The
  release workflow builds each on a runner of that architecture and starts the try-it stack on both.
- **The AWS module defaults to a Graviton instance**, `t4g.large`: the same 8 GiB as `t3.large` for about 19%
  less. The Ubuntu image, and the CloudWatch agent the instance installs, follow the instance type's
  architecture, so an x86_64 `instance_type` works as before. An existing deployment is unchanged: the module
  never replaces or resizes a running instance. Moving one to Arm means a new instance.

- **`npx freedomridge`** ([launcher/README.md](launcher/README.md)): runs this release on one machine with Docker
  and Node.js. It puts the release's try-it Compose file in `~/freedomridge`, pulls the images, brings the
  folder's database up to the release with the release's own migrations, starts it and opens the app. Run with
  a newer version, it upgrades; it refuses a folder a newer release runs. `stop`, `status` and `logs`, and
  `--port`/`--api-port`, kept in the folder's `.env`. `docker-compose.try.yml` takes `FRIDGE_PORT` and
  `FRIDGE_API_PORT` for the same. The launcher is published to npm by the release tag, after approval, like the SDK.

### Changed

- **On-demand workers start Arm (Graviton) machines by default**, `r7g.2xlarge`: the same 8 vCPUs and 64 GiB as
  `r7i.2xlarge`, for less. Any instance type works, Arm or x86_64: the machine's Amazon Linux image follows its
  architecture, as AWS describes it.

### Fixed

- **Follows don't chain.** A mirror can no longer be shared with a follower of its own (`409`), and the Followers
  page no longer lists mirrors: an instance that wants the dataset follows its primary.
- **A model's weights are downloaded once.** When a model revision has only `pytorch_model.bin`, as the pinned
  patch model and the server's CLIP checkpoint do, transformers loaded it and then downloaded a second,
  safetensors copy in the background that it never loads: 1.2 GB in the model cache instead of 605 MB for CLIP
  ViT-B/32. The SDK and the server now turn that download off (transformers' `DISABLE_SAFETENSORS_CONVERSION`,
  unless it's set already). A cache that already holds the extra copy keeps it; deleting it is safe.

## 2.4.0 (2026-11-09)

2.4.0 adds follower instances: a second Freedom Ridge keeps read-only mirrors of the datasets a primary shares
with it, kept up to date from the primary's change log, and can send tags and views back. It also adds on-demand
compute workers, AWS spot machines started for a waiting map re-fit, and class-probability overlays. A deployment
that uses none of it needs only the four migrations, none of which rewrites a table.

### Upgrade notes

- Four database migrations, applied with `scripts/apply_migrations.sh` (step 4 of [Upgrading](docs/UPGRADING.md)).
  None rewrites a table; each waits at most 2 seconds for its locks, so if one stops with a lock timeout, run it
  again.
  - `0007_follower_change_log.sql`: the `datasets.followed` column, and the change log's tables and triggers.
  - `0008_followers.sql`: the followers' tables, and `api_tokens` accepting the `follower` scope.
  - `0009_mirrors.sql`: the follower side's tables, the checks that keep a mirror read-only, and the map
    coordinates' change log.
  - `0010_compute_launcher.sql`: the on-demand workers' settings (off) and the machines they start.
- Nothing else changes for a deployment that uses none of this. On-demand workers also need
  `JOB_RUNNER_PLACEMENTS=server` and AWS permissions ([ON_DEMAND_WORKERS.md](docs/ON_DEMAND_WORKERS.md)).

### Added

- **A change log for follower instances.** Every change to a followed dataset is logged with the transaction
  that made it, by triggers, whatever path made it:
  - samples inserted, changed or deleted, by block of 4096 ids, from the statistics trigger already on samples;
  - vectors and hashes, per sample and field;
  - map coordinates, by field and block, and a map's full re-rank;
  - its field registry, settings and saved views.

  A dataset nobody follows logs nothing, and a deleted dataset's log rows go with it.
- **Followers, on the primary** ([FOLLOWERS.md](docs/FOLLOWERS.md)).
  - Under Settings → Followers, an admin adds a follower instance (`POST /api/followers`), which gets its own
    account and a token that only the follower endpoints accept. The page shows each follower's last check-in,
    highlighted after a week of silence, and the changes held for it.
  - The admin chooses the datasets it follows, each with a `tags` and a `views` option (primary only, or both
    ways).
  - The follower copies a dataset, then reads what changed in commit order (`/api/follower/...`), and
    acknowledges what it applied, so the log is pruned.
  - Removing a follower deletes everything kept for it.
- **Followers, on the follower.**
  - Under Settings → Primaries, an admin follows a primary with its address and follower token.
  - The job runner syncs every minute: each dataset shared with this follower is copied into a read-only
    **mirror**, then kept up to date from the primary's feed.
  - Everything the primary sent is read-only here, enforced by the database. Vector fields only this instance
    has and their maps, statistics and local views stay its own.
  - The primary's maps are copied, logged by field and block, so a map view selects the same samples on both;
    the mirror re-ranks a map when the primary did.
  - With tags both ways, tagging a mirror is made on the primary as the follower's account. With views both
    ways, views shared with everyone on a mirror are made on the primary, marked with the follower's name.
  - Sample ids are translated, in object samples' `parent_id` and in views' filters.
  - Views that use a concept stay on the primary.
  - A mirror stops when the primary stops sharing it, removes the follower or is restored into a new database;
    it then stays as a read-only copy to keep or delete.
- **`python -m fridge.worker --exit-when-idle SECONDS`:** a compute worker exits once it has had no job for that
  long, an unreachable server included, so a machine started for some jobs can power off afterwards. The SDK
  docs show running a worker from the server's `backend` image with Docker.
- **On-demand compute workers** ([ON_DEMAND_WORKERS.md](docs/ON_DEMAND_WORKERS.md)), off until an admin turns
  them on under Settings → Compute Workers.
  - When a compute job (a map re-fit) has waited with no worker free, the job runner starts an AWS spot machine,
    or on-demand when AWS has no spot capacity. The machine runs the worker from this version's backend image
    and powers off, and so terminates, once the worker is idle.
  - Limits are times: machines at once, hours per machine, machine-hours a month. A job held back by one says
    which.
  - Each machine has its own worker token, revoked when it ends. Machines that AWS ended, that are over their
    time, or whose worker never came or went quiet are ended once a minute.
  - **Check with AWS** dry-runs a launch with the settings. The Terraform module attaches the AWS permissions
    with `on_demand_workers = true`.
- **Class-probability overlays** (docs/SDK.md, "Class probabilities"). A field holding a segmentation model's
  class probabilities per pixel (`fridge.ClassProbabilities`, one 8-bit grayscale PNG with the classes side by
  side) is drawn as one class per pixel with a rule: argmax, or thresholds in order of precedence with a
  fallback, including thresholds on a group of classes' summed probability.
  - The sample view changes the rule live, or shows one class's probability as a heatmap. **Save as default**
    stores the rule for everyone (anyone who may edit the dataset; `PUT /api/datasets/{id}/class-rules/{field}`).
  - The grid draws the default rule.
  - The SDK sets classes and colours with `dataset.set_field_classes` and the default rule with
    `dataset.set_default_rule` (`fridge.argmax_rule`, `fridge.threshold_rule`).
  - The rule is kept in `segmentation_config.class_rules` and follows `rename_field` and `clone_field` like class
    names and colours.

## 2.3.0 (2026-11-08)

2.3.0 adds what a follower instance needs first: one image's patch vectors computed without a server, and a
job that imports such vectors from Parquet files in S3, so no image has to be downloaded to get its patches.
There is no migration; the upgrade note concerns the job runner's environment.

### Upgrade notes

- `docker-compose.prod.yml` now also gives the job runner `STORAGE_ENCRYPTION_KEY`, which it needs to read
  a storage configuration's credentials for the import. It's the same variable the backend already requires,
  so a deployment changes nothing. A deployment with its own compose file should add it to the job runner.

### Added

- **One image's patch vectors without a server:** `fridge.compute_image_patch_vectors(image, scale="4x4")`
  returns the vectors `compute_patch_embeddings` stores for that image (16 × 512 float32 for `4x4`), with no
  client or network, so a batch job next to the images can compute them for a server to import.
  `fridge.load_patch_model()` loads the model, offline from a warmed cache. `PATCH_MODEL_ID`,
  `PATCH_MODEL_REVISION` and `PATCH_CROP_VERSION` say which vectors are interchangeable.
- **Import patch vectors from S3:** `dataset.import_patch_vectors(prefix)` (admin;
  `POST /api/datasets/{id}/embeddings/patches/import`) loads vectors computed elsewhere from Parquet files
  under an S3 prefix, as a resumable server job. Rows match samples by filepath, unmatched rows and unreadable
  files are counted with examples, and each tile position's search index is queued afterwards (docs/SDK.md,
  "Importing patch vectors computed elsewhere"). The bucket must be served to the dataset by a storage
  configuration.

### Changed

- `compute_patch_embeddings` loads its default model, `openai/clip-vit-base-patch32`, at a pinned Hugging Face
  revision, the one that has been current since 2024-02, so its vectors stay the same as those computed
  elsewhere.

## 2.2.0 (2026-11-08)

2.2.0 is the first release with published Docker images: a server upgrades by pulling them instead of
building, and anyone can try the app with only Docker. The map's Mapbox token is now read when the
frontend starts. There is no migration; the upgrade notes cover the switch to pulling images.

### Upgrade notes

- Step 3 of [Upgrading](docs/UPGRADING.md) now pulls: after `git checkout v2.2.0`, run
  `docker compose -f docker-compose.prod.yml pull` instead of `build --pull`. 2.2.0's images are the first
  on ghcr.io, so this upgrade can already pull them.
- Nothing changes for the Mapbox token: `NEXT_PUBLIC_MAPBOX_TOKEN` in `.env` keeps working.
- Once the server runs the pulled images, the images it built for earlier releases and Docker's build cache
  are unused. `docker images` lists the old ones by your Compose project's name (`<project>-backend` and so
  on); remove them with `docker image rm`, and the build cache with `docker builder prune -a`.

### Added

- **Release images:** each release publishes its Docker images to
  `ghcr.io/freedomridge/{backend,worker,db,backup,frontend}:X.Y.Z`, and `docker-compose.prod.yml` names them.
  An upgrade pulls them (`docker compose -f docker-compose.prod.yml pull`) instead of building on the server,
  which took about an hour on a small host and filled its disk with build cache. `build:` stays, so a
  revision that isn't a release still builds from its checkout.
- **Try it with only Docker:** `docker-compose.try.yml` starts a release's published images on one machine, at
  http://localhost:3000, with no domain, TLS, Python or Node (README, "Try it locally"). After pushing a
  release's images, the workflow starts it from them without logging in to ghcr.io and logs in as admin.

### Changed

- The map's Mapbox token is read when the frontend starts, from `MAPBOX_TOKEN` (`docker-compose.prod.yml` sets
  it from `NEXT_PUBLIC_MAPBOX_TOKEN` in `.env`, as before), instead of being compiled in when the frontend is
  built. Changing the token needs `docker compose -f docker-compose.prod.yml up -d frontend`, not a rebuild, and
  one frontend image serves any deployment. In development, `NEXT_PUBLIC_MAPBOX_TOKEN` in `frontend/.env.local`
  still works.

## 2.1.2 (2026-11-08)

2.1.2 puts the Python SDK on PyPI and fixes saving a version while data is coming in, index review
waiting for good, and the single-image view's Tag dialog. It needs only the standard upgrade steps
(no migration).

### Added

- **The Python SDK is on PyPI:** `pip install freedomridge`, or `pip install "freedomridge==X.Y.Z"` for a
  given release, from 2.1.1 on. The version messages of the SDK and the server name that command instead
  of an install from GitHub.

### Fixed

- **Index review:** **Review now** could stay on "Not reviewed yet" for good. A review that finished just as
  the page read its state showed as neither running nor done, and the page stopped checking. The state is now
  read at one moment.
- **Saving a version** right after an import or edit, or while data was coming in, could fail with "Dataset
  changed during snapshot creation". Every statistics refresh records its run on the dataset, and the snapshot
  took that for a change; with data coming in, one finished during every attempt on a big dataset. A snapshot
  now conflicts only when the dataset's settings change while it copies, and is then taken again, up to twice,
  as a restore is. A field renamed or retyped during the copy no longer restarts it either: like other field
  and sample changes made meanwhile, it's left out of the version.
- **Tagging from the single-image view:** the Tag dialog read the image's tags again after every read, so the
  list flickered until the server refused with "too many requests" (#10). It reads them once.

## 2.1.1 (2026-11-07)

2.1.1 moves to transformers 5 for its security fixes. Embeddings are unchanged, so nothing needs
recomputing, and it needs only the standard upgrade steps (no migration).

### Security

- The server and the SDK's embedding extras use transformers 5 (5.21 or newer; it was 4.57), which fixes six
  advisories CI had accepted for transformers 4 (PYSEC-2025-217, PYSEC-2026-2288, -2289, -2290, -3929 and -4174).
  Embeddings are unchanged, so stored ones stay valid: text and image search, `compute_embeddings` (with
  sentence-transformers, or Hugging Face models such as DINOv3) and `compute_patch_embeddings` give vectors that
  match transformers 4.57's to a cosine of at least 0.99997. 5.21 is the oldest 5.x whose image processor takes
  the server's query images.

## 2.1.0 (2026-11-07)

2.1 lets a compute worker fit a server's maps, lays out the dataset page's top bar again, and keeps a
field's class names and colours through a rename. It needs only the standard upgrade steps (no migration),
and a 2.0 SDK works with it. Two changes return different images than 2.0: excluding a map region now keeps
the images with no location, and a search keeps the applied filters unless you choose **whole dataset**.

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
- [SDK.md](docs/SDK.md) has a section on showing a model's predictions as mask overlays: one mask field per
  model version, named so that it says which model it is, its class names and colours, and removing it.

### Fixed

- An embeddings lasso's chip opened the field form, which could show only the field's name (such as `clip`). It
  now says which map the lasso was drawn on and, like a map region, a click toggles it between including and
  excluding its area (the new `embedding_polygon_exclude` filter operator).
- Clicking a text or image search's chip to exclude its results changed only its label. It now excludes them.
- On a dataset with objects, a `created_at` filter matched through an image's objects (or an object's image)
  compared the outer row's date instead of the object's (or image's) in similarity search, duplicate and
  hash-stats, sparse-sample and embeddings requests. It now compares the right row's date, so such a filter can
  match more images there.
- A renamed mask or label field kept no class names or colours: its `segmentation_config` entries
  (`mask_targets` and `color_scheme.fields`) stayed under the old name. `rename_field` now moves them to the new
  name and `clone_field` copies them. `clear_field` still leaves them, so a field cleared and written again keeps
  its class names and colours.

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
