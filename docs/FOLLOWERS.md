# Followers

A **follower** is a second Freedom Ridge instance that mirrors chosen datasets of a **primary**, one way, and
adds what the primary doesn't hold. For example, a machine with the disk and memory for patch vectors and their
search indexes. It can also run `python -m fridge.worker` for the primary.

Both sides are in this release: the primary's change log, follower management and the endpoints a follower
reads, and the follower's sync, which keeps a read-only mirror of each dataset shared with it.

## On the primary

An admin manages followers under **Settings → Followers** (or the endpoints below):

1. **Add a follower**, giving it a name such as `laptop`. The primary creates an account for it,
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
   catches up. A follower that stopped following on its side shows **Stopped following** with the datasets it
   gets back if it follows again (see "Stopping tells the primary" below). **Don't bring back** drops one.
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
address and a DNS name, say) is refused, since both would share one subscription there. The primary, for its
part, binds a follower's token to the first instance that uses it, by that instance's installation id (made once,
in its database), and refuses the token from another (409). Once the token is bound, a call that sends no
installation id is refused the same way. A follower restored from a dump or moved to another
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

### Reaching the primary

The follower starts every connection: it reads the config and the feed, fetches images the primary serves, and
polls for relayed patch searches. The primary never connects to a follower. So a follower needs no open port,
public address or certificate; only the primary has to be reachable by its followers, at an address they can open.

A launcher install (`npx freedomridge`) listens on 127.0.0.1 only, so as a primary for other machines it has to be
put behind an address they can reach, such as a VPN or a tunnel. It is made for one machine: it runs with
development settings, which use the default storage key and put internal detail in error messages. Before others
can reach one:

- Log in and replace the admin password (`admin123`). The first login asks for a new one, but until someone has
  logged in, whoever reaches the instance first gets that login.
- Share it only with people you'd trust with the instance itself.
- For a wider audience, run the primary with `docker-compose.prod.yml` instead ([Docker deployment](DOCKER_DEPLOYMENT.md)).

While a primary's machine is asleep or offline, its followers keep their mirrors, read-only as always, and catch
up from the change log when it's back. Images the primary serves (**Serve images**) don't load meanwhile.

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
- **Views that use a concept, or a range on sample ids** (`id > 15`, a `parent_id` range), stay on the primary:
  concepts belong to each instance, and each numbers its samples in its own order. The views panel lists them.
  Sample ids named outright (`id in [...]`, an object's `parent_id`) are translated.
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

**Patch searches on the primary then run on the follower.** On the primary, the sample viewer, the grid and Find
unlabeled offer a follower's scales next to its own, named for it ("8x8 · on desktop", with "(offline)" when no
follower holding it has checked in within the last minute), and a search's filter chip says where it ran. A patch search (or a search for unlabelled look-alikes of an object) on a scale only a
follower holds goes to that follower; the primary never connects to it:

1. The primary queues the search (`follower_searches`) for the follower that reported the scale first, or for
   the next one that is online if the first doesn't take it within 3 seconds.
2. The follower, whose job runner polls `GET /api/follower/searches?wait=25` over its own connection, claims it.
   It runs the same search the primary would, on its mirror, translating sample ids both ways, and answers with
   `POST /api/follower/searches/{id}` in the primary's sample ids. It runs up to 4 at a time.
3. The primary shows the samples from its own tables. Samples deleted there since the follower's copy drop out,
   and samples the follower hasn't copied yet (about a minute behind) are missing.

A follower checked in within the last minute counts as online. Without one online, the search answers 503 and
names the follower; with 8 searches already queued or running on it, 429; without an answer within 30 seconds,
504. Filters on a concept, or on a selection made on the primary, can't run on a follower: save the selection as a
view, or search on the follower itself. Neither can a range on sample ids (`id > 15`, a `parent_id` range): a
follower numbers its samples in another order, so only `=`, `!=`, `in` and `not in` on them are passed on, translated.

A search belongs to a follower only while it follows the dataset. When the primary's admin stops sharing the
dataset with it, or it stops following the primary, its queued and running searches of that dataset are cancelled:
each goes to the next follower holding the scale, or ends with 503 ("no longer follows this dataset").

### Edits sent to the primary

With a dataset's **tags** option both ways, tagging its mirror (the tag dialog, or `POST .../samples/bulk-tag` by ids,
selection or filters) is made on the primary instead, for the same samples there, as the follower's account. A sync
is queued at once, and the tags show on the mirror when it has run, within a minute. With tags from the primary
only, tagging a mirror is refused. The option covers `tags` only: the primary's other array fields stay read-only
on a mirror, and the primary refuses them from a follower.

**Who made it.** The follower sends the username of whoever made the edit (`made_by`), shown on the primary: a tag
it adds to the tag registry reads "created by alice on lab", and a view "made on lab by alice". It's display only:
the edit is still made as the follower's account. The follower's name comes from that account, never from what it
sent, and a user on the primary can't mark a view as made on a follower (`made_on` and `made_by` are dropped from
anyone else's view).

With the **views** option both ways:

- a view shared with everyone on the mirror (saved public, or shared later) is created on the primary as the
  follower's account, marked `made_on` with the follower's name and `made_by` with its maker, and copied back at
  once. Not one that uses a concept or a range on sample ids (instance_bound): each instance has its own concepts
  and numbers its own samples, so it stays on the mirror, shared there;
- deleting a copied view deletes it on the primary, which allows it only for views the follower made;
- private views, and views shared with some users only, stay on the mirror.

With views from the primary only, views made on the mirror stay there. Either way, a copied view's sample ids
are translated, so a saved search (patch search included, which saves its results) shows the same samples on
both. Every other edit of the primary's fields on a mirror (bulk set, rename, clear or delete) is refused before
it's queued.

### Fields of its own

A follower may add fields of its own to a mirror (labels, predictions, notes and tags) beside the primary's. It
can never change or remove the primary's. Its fields are named `@local.<name>` (a letter or `_`, then letters,
digits or `_`): `@local.weeds`, and `@local.tags` for its own tags. No field the primary has can start with `@`, so
a primary field named `weeds` or `local` sits beside `@local.weeds` and nothing is renamed or overwritten.

- **Writing them:** `POST .../samples/bulk-set` and `PATCH .../samples/{id}` with `@local.` keys in `metadata` and
  `remove_metadata_keys` (`array_fields` for `@local.tags` on one sample), and `POST .../samples/bulk-tag` with
  `field_name` `@local.tags`. In the app, the tag dialog on a mirror offers **This server's tags** beside the
  primary's, which it may edit only with the tags option both ways. A request that writes fields of the mirror's
  own and the primary's together is refused (409), and so is one on a mirror that no longer follows (409) or on a
  dataset that isn't a mirror (400).
- **What they are for the rest of the app:** the sidebar lists them first, under **On this server**, with the
  primary's under **From the primary**. They filter, sort, colour a map, show on the grid and in a sample's view
  (under **On this server**), and export under their full names, as any field. In the SDK, `sample['@local.weeds']`
  and `sample.local` read them, and `dataset.filter(F('@local.weeds') > 3)` filters on them.
- **What happens to them:** the primary never sees them, and a re-read or a resumed copy keeps them on the samples
  that remain. A sample the primary deletes goes with them. A view that filters on one stays on the mirror. A
  version of a mirror keeps no history of them, and filtering a version on one is refused. Copying a dataset to
  another instance (`fridge.sync_between_instances`) leaves them out and says so: the target isn't a mirror.
- **Removing one:** remove the key from every sample (bulk set with `remove_metadata_keys`). The field leaves the
  sidebar once no sample holds it: at once for a small mirror, and after the bulk job's last steps, which read the
  dataset in bounded windows, for a large one. Rename, clone and clear don't take them yet.

### When it stops

- The primary removes this follower, or revokes its token: every mirror of that primary stops. Only the primary's
  own answer counts (it says which, with a code). Any other refusal, such as a proxy's 403 or a maintenance page,
  is shown as the primary's last error and retried on the next sync.
- The primary stops sharing a dataset: its mirror stops.
- A mirror's cursor is too old (409): that mirror stops, and a new copy resumes into it if the dataset is still
  shared.
- The primary was restored into a new database (410): each mirror stops. One of a dataset it still shares is held,
  and asks whether to resume into it (below). A mirror of a dataset it no longer shares just stops.
- Removing the primary here (Stop following) stops all its mirrors, and tells the primary (below).

A mirror whose sync fails, a vector field clash say, doesn't hold up the other mirrors of its primary: its error
shows on the Primaries page, and it's tried again after a minute, waiting twice as long after each failure in a
row, up to an hour. A clash comes from vector indexes being per field
name over every dataset here: a field of this instance's indexed at another size than the primary's same-named
field. The error names the field.

A stopped mirror stays as a read-only copy, marked **no longer following**, to keep or delete. A mirror that's still
following can't be deleted here: stop sharing it on the primary first.

**A new copy resumes into the stopped one.** When the same primary database shares the dataset with this
instance again (re-shared, the primary added here again with a new token, or a copy started again after a 409),
the copy goes into the latest stopped mirror of that dataset instead of a new dataset. What this instance added
there stays: its own vector fields (patch vectors computed here, say), indexes, concepts, access grants and views.
Every block is read again, so the samples the primary no longer has go, with what this instance added to them. A
copy that had already stopped when migration 0017 was applied doesn't record its primary database, so it's never
resumed.

**When the primary was restored, the copy asks first.** A restore into a new database can hand sample ids out again,
so a copy can't simply carry on by them. Each copy of a dataset the restored primary still shares is held instead, and
the primary's other datasets keep syncing:

1. The follower compares the primary's samples with the copy's, changing nothing. It reads them a page at a time
   (`GET /api/follower/datasets/{id}/match-keys`) and matches them by file path. An object sample has a path of its
   own (`img.jpg#o0`), so objects match the same way. Where both sides have an image hash (the lowest field name both
   have), the hashes must agree too. These are perceptual hashes, so two different files can share one.
2. The copy's row on the Primaries page then asks, with the counts: how many of the primary's samples match by file
   path (and by image hash), how many don't and would be copied fresh, and how many here the primary no longer has.
   Its badge reads **On hold**, and it waits until an admin here answers
   (`POST /api/primaries/mirrors/{id}/restore-decision`).
3. **Resume into this copy:** first it says how many samples here will be deleted, and asks again. It's refused when
   nothing matches. Then the copy's samples the primary no longer has go, with what this instance added to
   them. The matched ones take the primary's new ids, keeping this instance's own vector fields, indexes, concepts and
   views on them. The copies of the primary's shared views are made again. Then every block is read again, as for a
   resumed copy. All of it runs in bounded steps of the sync job, so a large copy is resumed over several of them.
4. **Make a new copy:** a new dataset is copied, and the held one is kept as a separate copy.

If the primary is restored again before the answer, the copy is compared again. If it stops sharing the dataset, the
question goes, and the copy stays as it is. Once a resume has started, it finishes first, so the copy's sample ids
are never left half moved. A primary restored onto a release that can't compare copies (no `match-keys`) gets a new
copy, as before, and the held one is kept as it is. A held copy whose step fails is treated like a failing mirror:
its error shows on its row, it's tried again later, and the primary's other mirrors carry on.

On the Primaries page, the stopped copy a new copy would resume into says so: only the latest one of its primary
database, and none while a copy of that database still follows the dataset. **Keep as a separate copy** freezes
it: a new copy of its dataset then makes a new dataset (`POST /api/primaries/mirrors/{id}/keep-separate`).

**Stopping tells the primary.** Stop following stops the mirrors here first, then calls the primary
(`POST /api/follower/leave`, waiting up to 5 seconds), so it stops keeping its change log for this instance:

- its subscriptions move aside with their options, so a dataset nobody else follows stops logging, and each
  dataset's log is pruned without this instance;
- the patch scales it reported go, and relayed searches waiting for it are cancelled (a second follower holding
  the scale takes them);
- its token keeps working.

If the primary can't be told (offline, or a release before this one), the follower stops anyway and the Primaries
page says so: the primary's admin can remove the follower on its Followers page.

**Coming back.** Adding the same primary again (with the same follower token) brings back what this instance
followed when it stopped, with the same options (`POST /api/follower/return`), and each copy resumes into its
stopped mirror. A dataset shared with it meanwhile comes too. A dataset deleted meanwhile, being deleted, or one the
primary's admin chose not to bring back, doesn't. Adding the primary does this. So does a sync of a primary this
instance still has, when the primary says it stopped following: a Stop following from a link removed since that
reached the primary late, after the primary was added again. A sync still running here when it stopped doesn't
bring anything back, because Stop following removes the link before it tells the primary.

## Endpoints

The endpoints are listed in [Authentication](AUTHENTICATION.md#endpoints).
