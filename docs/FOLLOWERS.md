# Followers

A **follower** is a second Freedom Ridge instance that mirrors chosen datasets of a **primary**, one way, and
adds what the primary doesn't hold. For example, a machine with the disk and memory for patch vectors and their
search indexes. It can also run `python -m fridge.worker` for the primary.

Both sides are in this release: the primary's change log, follower management and the endpoints a follower
reads, and the follower's sync, which keeps a read-only mirror of each dataset shared with it.

## On the primary

An admin manages followers under **Settings → Followers** (or the endpoints below):

1. **Add a follower**, giving it a name such as `tailscale-local`. The primary creates an account for it,
   `follower:<name>`, which has no password and so can't log in. It also creates a **follower token**, shown
   once: give it to the follower with the primary's URL. Only the follower endpoints accept that token (any
   other endpoint answers 403), and they accept no other credential.
2. **Choose datasets** for it to follow, each with three options. `tags` and `views` are each `primary`, where
   the primary's copy is the only one edited, or `both`, where edits made on the follower are sent to the primary:
   - **`tags`;**
   - **`views`:** with `primary`, the primary's shared views are copied down and views made on the follower
     stay there;
   - **`images`** (**Serve images**, off by default): this server serves the dataset's images, thumbnails, crops
     and overlays to the follower when the follower can't read them itself, from this server's disk or from a
     bucket the follower has no credentials for. Turning it on shares those images with the follower without
     sharing any credentials.

   A mirror (a dataset this instance follows from another) can't be chosen: follows don't chain, so an instance
   that wants it follows its primary.
3. **Watch** when each follower last checked in, and how many log rows the primary keeps for it until it
   catches up. Nothing is removed automatically.
4. **Remove a follower** to delete everything the primary keeps for it:
   - its token stops working;
   - its log rows are deleted;
   - a dataset nobody else follows stops logging.

   Its account stays, renamed `follower:<name>:removed:<id>` and inactive, for what it wrote. The follower
   keeps its own copy.

## The change log

While any follower follows a dataset, the primary logs what changes in it. Each row is stamped with the
transaction that made the change, whatever made it:

- **Samples by block** (`id // 4096`). The statistics trigger already on `samples` records each changed block
  once per transaction, so sample writes gain no work of their own. A follower re-reads a changed block and
  compares it with its copy, which also finds the samples deleted from it.
- **Vectors and hashes** per sample and field.
- **Map coordinates** by field and block, so a full re-fit of a map logs one row per block, not per sample. A full
  re-rank of a map logs a dataset change, so followers re-rank their copy when the primary did.
- **Dataset state:** the field registry, settings and shared views. Deleting a followed dataset deletes its log
  rows with its subscriptions; its followers find it gone (404) and stop their mirrors.

A dataset nobody follows logs nothing. The log is pruned once every follower of the dataset has acknowledged
it.

## The follower's protocol

Every call takes the follower token (`Authorization: Bearer <token>`) and records the follower as seen.

1. `GET /api/follower/config` returns its datasets, their options, the API's `protocol` version (2 for a primary that
   takes the follower's own patch scales; absent from older ones), the primary's `database_identity` (its
   cluster's system identifier and its database's oid; `identity` is the cluster's alone, as followers before
   2.6.1 read it), and whether each dataset is `ready`. A dataset becomes ready once every transaction that might have written to it without
   being logged has ended. Those are writes already in flight when it became followed.
2. **Copy a ready dataset:**
   - `GET .../blocks` lists every block and its sample count.
   - `POST .../blocks/samples {"blocks": [...]}` returns those blocks' samples, with every column, the array
     fields, metadata and hashes. It takes up to 16 blocks per call.
   - `POST .../vectors {"sample_ids": [...], "fields": [...]}` returns their vectors as base64
     little-endian float32. It takes up to 2000 samples per call; no fields means every field.
   - `GET .../state` returns the settings, the field registry and the shared views.
3. **Then read the feed** from `start_txid - 1` with
   `GET .../changes?after=<cursor>&identity=<identity>`. It returns the blocks, vector and hash fields, and
   dataset state that changed in whole transactions after the cursor, and the next cursor. Re-read what it
   names; a sample or vector no longer there was deleted.
   - The feed serves only transactions that have ended, so nothing can appear behind a cursor it gave out. A
     long transaction on the primary holds the feed back until it commits.
   - `more: true` means another page follows straight away.
   - `vector_limit=<n>` bounds a page by its vector changes: whole transactions, up to the first that would go
     over `n`, and at least one transaction. This instance's sync asks for 20,000, since it keeps the page in its
     job's checkpoint. Without it, a page is up to 1,000 transactions, whatever their size.
   - A re-read block's samples bring their own vectors only when they're new on the follower: changes to the
     others' vectors are in the feed's vector changes.
4. `POST .../ack {"cursor": ...}` after applying a page, so the primary can prune.

Errors:

- **410:** the primary was restored: into a new cluster, or into a database dropped and created again (the
  `identity` sent is the config's `database_identity`; a bare system identifier is compared with the cluster's
  alone). Copy again. The primary starts its
  subscriptions again from then, by itself.
- **409:** a cursor from before the dataset's copy started.
- **404:** a dataset this follower doesn't follow.

A restore into the same database without dropping it (`pg_restore --clean`) keeps the identity, so the primary
can't detect it: remove and re-add its followers then, so each copies again. The documented restore drops and
creates the database, which followers detect.

## On the follower

An admin adds the primary under **Settings → Primaries**, with its API address (`https://primary.example.com/api`)
and the follower token. The token is checked against the primary before it's saved, and stored encrypted with
`STORAGE_ENCRYPTION_KEY`. An instance follows a primary once: a second address for the same primary (its LAN
address and a Tailscale name, say) is refused, since both would share one subscription there. The primary, for its
part, binds a follower's token to the first instance that uses it, by that instance's installation id (made once,
in its database), and refuses the token from another (409). A follower restored from a dump or moved to another
server keeps its installation id. One reinstalled from scratch gets a new one: **Reset instance** on the primary's
Followers page lets the next installation that calls take the token over.

The job runner then syncs once a minute (`FOLLOWER_SYNC_SECONDS`, default 60). Each sync is one background job per
primary that resumes where it stopped:

1. It reads the config. A dataset newly shared and ready gets a local dataset, its **mirror**, with the primary's
   name, or `<name> (from <primary host>)` when a dataset here already has that name. The mirror is private: admins
   and the admin who added the primary see it, and access can be granted as for any dataset.
2. It copies the mirror block by block: samples, hashes, vectors and map coordinates, then the field registry,
   settings and shared views, and ranks each map the primary has.
3. Then it applies the feed, and acknowledges each page applied.

The Primaries page shows each mirror's state (copying, following, or no longer following), the copy's progress,
its last sync, and any error. The job's successful runs are pruned from the job history like other automatic jobs.

### What a mirror holds

- **The primary's, read-only here:** its samples (metadata, tags, array fields, location), the field registry, the
  settings, the hashes, every vector field the primary has and their maps, and its shared views. The database refuses any other
  change to them with "read-only mirror"; only the sync writes them. A dataset page shows a "Mirror of <host>"
  badge, and tagging is off.
- **This instance's own:** vector fields only it has (patch vectors imported here, say) and their maps,
  statistics, indexes (`is_promoted`), concepts, access grants, storage, and views made here. This instance's own
  vector fields can be deleted here (`POST /api/datasets/{id}/embeddings/{field}/delete`); the primary's can't. If the
  primary later sends a vector or hash field of the same name as one only this instance had, the primary's replaces
  it, all of it at once, with this instance's map of it: none of this instance's values stay beside the primary's.
- **Sample ids** differ between the two. The sync keeps the mapping, translates an object sample's `parent_id`
  (null until its parent arrives), and translates the sample ids in a shared view's filters (a brushed
  selection). A sample that isn't here matches nothing.
- **Maps:** the primary places new samples on its maps and re-fits them; the mirror takes its coordinates, so a
  view with a polygon drawn on a map selects the same samples on both. The mirror ranks a map when the primary
  has re-ranked it. Map placement here leaves the primary's fields alone.
- **Views that use a concept** stay on the primary, because concepts belong to each instance; the views panel
  lists them.
- **Images:** the mirror reads the same media paths. When this instance can read them (the same disk path, or a
  storage configuration with read access to the bucket), it serves them itself. When it can't, and the primary
  has the dataset's `images` option on, it asks the primary for the image, thumbnail, crop or overlay and keeps
  the answer in its media cache (`S3_LOCAL_CACHE_PATH`, pruned with it). With the option off, those images don't
  load. Patch vectors never need them.

### Patch scales of its own

A follower can hold patch vectors the primary doesn't, computed on its own machine (a GPU, and the disk for 8×8
patches and their indexes). With a primary on protocol 2, each mirror reports its own patch scales: the base field,
the scale, the vectors' size and whether every tile has its index. It reports with `POST
/api/follower/datasets/{id}/fields`, at most every 10 minutes and once the copy is following, and each report
replaces the last one. The primary keeps them while the dataset stays followed and shows them on Settings →
Followers ("Holds clip 8×8"). Patch scales that came from the primary aren't reported.

### Edits sent to the primary

With a dataset's **tags** option both ways, tagging its mirror (the tag dialog, or `POST .../samples/bulk-tag` by ids,
selection or filters) is made on the primary instead, for the same samples there, as the follower's account. A sync
is queued at once, and the tags show on the mirror when it has run, within a minute. With tags from the primary
only, tagging a mirror is refused. The option covers `tags` only: the primary's other array fields stay read-only
on a mirror, and the primary refuses them from a follower.

With the **views** option both ways:

- a view shared with everyone on the mirror (saved public, or shared later) is created on the primary as the
  follower's account, marked `made_on` with the follower's name, and copied back at once. Not one that uses a
  concept: concepts belong to each instance, so it stays on the mirror, shared there;
- deleting a copied view deletes it on the primary, which allows it only for views the follower made;
- private views, and views shared with some users only, stay on the mirror.

With views from the primary only, views made on the mirror stay there. Either way, a copied view's sample ids
are translated, so a saved search (patch search included, which saves its results) shows the same samples on
both. Every other edit of a mirror (bulk set, rename, clear or delete) is refused before it's queued.

### When it stops

- The primary removes this follower, or revokes its token: every mirror of that primary stops. Only the primary's
  own answer counts (it says which, with a code). Any other refusal, such as a proxy's 403 or a maintenance page,
  is shown as the primary's last error and retried on the next sync.
- The primary stops sharing a dataset: its mirror stops.
- The primary was restored into a new database (410), or a mirror's cursor is too old (409): that mirror stops,
  and a new copy starts if the dataset is still shared.
- Removing the primary here (Stop following) stops all its mirrors.

A mirror whose sync fails, a vector field clash say, doesn't hold up the other mirrors of its primary: its error
shows on the Primaries page, and it's tried again after a minute, waiting twice as long after each failure in a
row, up to an hour. A clash comes from vector indexes being per field
name over every dataset here: a field of this instance's indexed at another size than the primary's same-named
field. The error names the field.

A stopped mirror stays as a read-only copy, marked **no longer following**, to keep or delete. A mirror that's still
following can't be deleted here: stop sharing it on the primary first.

## Endpoints

The endpoints are listed in [Authentication](AUTHENTICATION.md#endpoints).
