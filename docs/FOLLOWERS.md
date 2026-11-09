# Followers

A **follower** is a second Freedom Ridge instance that mirrors chosen datasets of a **primary**, one way, and
adds what the primary doesn't hold. For example, a machine with the disk and memory for patch vectors and their
search indexes. It can also run `python -m fridge.worker` for the primary.

This release has the primary's side: the change log, follower management, and the endpoints a follower reads.
The follower's own sync job is not built yet.

## On the primary

An admin manages followers (endpoints below; a Settings → Followers page is coming):

1. **Add a follower**, giving it a name such as `tailscale-local`. The primary creates an account for it,
   `follower:<name>`, which has no password and so can't log in. It also creates a **follower token**, shown
   once: give it to the follower with the primary's URL. Only the follower endpoints accept that token (any
   other endpoint answers 403), and they accept no other credential.
2. **Choose datasets** for it to follow, each with two options. Each is `primary`, where the primary's copy is
   the only one edited, or `both`, where edits made on the follower are sent to the primary:
   - **`tags`;**
   - **`views`:** with `primary`, the primary's shared views are copied down and views made on the follower
     stay there.
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
- **Dataset state:** the field registry, settings and shared views, and the dataset's deletion.

A dataset nobody follows logs nothing. The log is pruned once every follower of the dataset has acknowledged
it.

## The follower's protocol

Every call takes the follower token (`Authorization: Bearer <token>`) and records the follower as seen.

1. `GET /api/follower/config` returns its datasets, their options, the primary's `identity`, and whether each
   dataset is `ready`. A dataset becomes ready once every transaction that might have written to it without
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
4. `POST .../ack {"cursor": ...}` after applying a page, so the primary can prune.

Errors:

- **410:** the primary was restored into a new database. Copy again.
- **409:** a cursor from before the dataset's copy started.
- **404:** a dataset this follower doesn't follow.

After restoring the primary from an older backup into the same database, which the primary can't detect,
remove and re-add its followers, so each copies again.

The endpoints are listed in [Authentication](AUTHENTICATION.md#endpoints).
