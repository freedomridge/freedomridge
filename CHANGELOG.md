# Changelog

Freedom Ridge has one version for the server, the web app and the Python SDK (the `VERSION`
file), following [Semantic Versioning](https://semver.org). A release lists **upgrade notes** when
it needs more than the standard steps in [Upgrading](docs/UPGRADING.md).

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
