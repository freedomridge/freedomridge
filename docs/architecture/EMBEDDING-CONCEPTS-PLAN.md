# Plan: embedding "concepts" — seed / tag-probe / text → score → search & exclude

Status: **IMPLEMENTED (Phases 0–2, 2026-06-14)** on branch `feat/embedding-concepts`.
Authored 2026-06-12. Scope: **Phase 0 (foundation) + Phase 1 (seed) + Phase 2 (logistic
probe) — all model-free.** Phase 3 (text) deferred; when built it uses a **CPU CLIP-text
sidecar** + a field→model registry.

**What shipped (all verified):**
- Migration `032_embedding_concepts.sql` (+ schema.sql mirror, parity test green): tables
  `embedding_concepts` (registry holds `w`) + `embedding_concept_scores` (narrow), and
  `score_concept(concept_id, full)` — calibrated [0,1] (seed = cosine `1-(x<=>w)/2`; probe =
  `sigmoid(w·x+b)` via `<#>`), full + incremental anti-join.
- `concept_select` / `concept_exclude` filter operators in `parse_filter_node` (id-safe via
  field_mapping, op allow-listed). Frontend operator union updated.
- `backend/routers/concepts.py` (registered in main.py): create (seed/probe/raw-w) / list /
  preview / rescore / rebuild(overwrite-in-place) / delete; soft-cap 50/dataset; power_user.
  Probe = numpy logistic GD (no sklearn dep — only numpy, already a backend dep).
- Maintenance: `backend.job_service` schedules incremental concept scoring from committed embedding work.
- SDK: `Client`/`Dataset` create_concept(kind, seed_ids, neg_ids, …)/concepts/preview/
  rescore/rebuild/delete.
- Frontend: a **Concepts panel** (library, create-from-selection with Seed/Probe toggle,
  threshold slider + live "N would be hidden" + "what gets cut" preview strip, Apply →
  removable `⊘` concept_exclude chip), wired in page.tsx + panel-content.tsx.
- Tests: backend `test_concept_filter.py` + `test_concepts_api.py`; SDK suite +10 (103/0
  live); Playwright `concepts.spec.ts` (seed flow + probe), full e2e 16/16. Fixture embeddings
  made separable (two groups) in global-setup. next.config gained env-overridable
  `FRIDGE_BACKEND_URL` + `NEXT_DIST_DIR` (default unchanged) to allow an isolated e2e stack.

**Design decisions locked 2026-06-14 (the 5 open questions):**
- Q2 **Score scale → calibrated 0–1** (sigmoid for probe, cosine→[0,1] for seed) so a
  threshold means the same thing across kinds and the slider is intuitive.
- Q3 **Scope → per-dataset only** (`dataset_id NOT NULL`).
- Q4 **New-data scoring → maintenance cycle** (anti-join in the existing refresh job; no
  ingest hook for now).
- Q5 **Growth → CASCADE (already) + soft cap/warn on active concept count**; revisit
  partitioning only if it explodes.
- Q1 **Probe negatives → DEFERRED to Phase 2** (default: one-class = positive tags + random
  background, with optional explicit negative tags). Reconfirm at Phase 2.

## Goal

Let a user say "I don't care about images like *this*" (a bad/irrelevant example, a tagged
set, or — later — a text prompt) and have the view **exclude them, including newly-streamed
ones**, without a bespoke "dynamic view." Also enables the positive direction (find/rank by a
concept) for free.

## The unifying insight (why this isn't three features)

All three are the same thing: **a linear direction `w` in an embedding field's space → a
per-sample score `score_i = w · x_i (+ b)`.** They differ ONLY in how `w` is produced:

| Producer | How `w` is made | Needs a model? |
|---|---|---|
| **Seed image(s)** | normalized mean of the seed embeddings (a prototype) | no |
| **Tag probe** | logistic regression on tagged ± embeddings → weight vector | no (tiny sklearn fit) |
| **Text prompt** (Phase 3) | CLIP text-encode the prompt | yes — once, to make `w` |

Everything downstream is shared and **already exists**:
- **Search / rank by a concept** → `similarity_search(vector=w)` already accepts a raw vector
  and is HNSW-backed (`backend/routers/search.py`). Nothing new.
- **Exclude / filter by a concept** → materialize `score_i` and filter `score > t`. Because
  new samples get scored at ingest, a *saved* exclude filter auto-catches new data — the live
  behavior we want, with NO new query machinery.

So a "concept" is just `{name, embedding_field, w, bias, threshold}` + its materialized scores.

## Mess-avoidance principles (the point of this doc)

1. **One scoring engine.** `score = w·x`. Producers only make `w`. No per-producer query paths.
2. **Scores are data, exclusion is a normal filter.** No "dynamic view" subsystem — a concept
   exclude is a filter node (like the existing `geo_polygon_exclude`), composable / reversible /
   saveable, that auto-applies to new data once they're scored.
3. **Reuse search.** Ranking by a concept = `similarity_search(vector=w)` (exists).
4. **No model in the web tier.** Seed/probe need none. Text's encoder is an isolated CPU
   sidecar (Phase 3).
5. **Narrow table, not `metadata`.** Scores live in a dedicated narrow table — NOT
   `samples.metadata` (a JSONB update on 2.4M rows churns the GIN index; same lesson as the
   `viz_cell_rank` wide-UPDATE → use the narrow-table pattern of
   [EMBEDDINGS-SAMPLE-PRIORITY-PLAN.md](EMBEDDINGS-SAMPLE-PRIORITY-PLAN.md)).
6. **View-everything.** Exclusion is explicit, reversible, and counted ("3,400 hidden — show").

## Architecture

```
                 produce w (varies)                    reuse (exists)
seed ─┐                                          ┌── search/rank: similarity_search(vector=w)
probe ─┼─► concept {w, bias} ─► score_i = w·x_i ─┤
text* ─┘        (registry)        (narrow table) └── exclude/filter: concept_score operator
                                                       └─ auto-applies to new data via ingest scoring
```

### Data model

```sql
-- One row per concept (the linear functional + how it was built).
CREATE TABLE embedding_concepts (
    id              SERIAL PRIMARY KEY,
    dataset_id      INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    embedding_field TEXT NOT NULL,           -- which vector space w lives in
    kind            TEXT NOT NULL,           -- 'seed' | 'probe' | 'text'
    spec            JSONB NOT NULL,          -- provenance: {seed_ids|pos_tag/neg_tag|prompt}
    weight          TEXT NOT NULL,           -- w as a pgvector literal (same dim as the field)
    bias            DOUBLE PRECISION DEFAULT 0,
    threshold       DOUBLE PRECISION,        -- default exclude/select cut (tunable per view)
    scored_count    INTEGER DEFAULT 0,
    created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (dataset_id, name)
);

-- Narrow materialized scores (rebuilt/extended by the scoring engine). NOT metadata.
CREATE TABLE embedding_concept_scores (
    concept_id  INTEGER NOT NULL REFERENCES embedding_concepts(id) ON DELETE CASCADE,
    sample_id   INTEGER NOT NULL REFERENCES samples(id) ON DELETE CASCADE,
    score       REAL NOT NULL,
    PRIMARY KEY (concept_id, sample_id)
);
-- Range scan for `concept_id = ? AND score > t` (preview counts + the filter operator).
CREATE INDEX idx_concept_scores_score ON embedding_concept_scores (concept_id, score);
```

### The scoring engine (the only shared compute)

`score_i = w · x_i + bias`, computed in SQL with pgvector's inner product. Cast to the field's
fixed dim (variable-dim column, same trick as search/HNSW):

```sql
-- build / rescore-all for one concept (one scan, ~seconds on 2.4M — like the quality compute):
INSERT INTO embedding_concept_scores (concept_id, sample_id, score)
SELECT $cid, e.sample_id, (-(e.vector::vector($d) <#> $w::vector($d)) + $bias)::real
FROM embeddings e JOIN samples s ON e.sample_id = s.id
WHERE s.dataset_id = $ds AND e.field_name = $field AND e.vector IS NOT NULL
ON CONFLICT (concept_id, sample_id) DO UPDATE SET score = EXCLUDED.score;
```
(`a <#> b` is negative inner product, so `-(x <#> w)` = `w·x`.) Incremental scoring (new
samples) is the same SELECT with `AND NOT EXISTS (score row)` — the viz_rank pattern of
[EMBEDDINGS-SAMPLE-PRIORITY-PLAN.md](EMBEDDINGS-SAMPLE-PRIORITY-PLAN.md) exactly.

**Ownership note (deliberate boundary):** concepts are **app-owned** derived data, scored
**app-side** by a maintenance job — NOT the uploader. The uploader owns *raw* artifacts
(embeddings/hashes); concepts are defined in the app *after* data exists and scoring needs only
the stored embeddings + the stored `w` (no model). This is a reasoned deviation from
the rule that the uploader owns derived fields, not a contradiction.

### Producers

- **Seed (Phase 1):** `w = normalize(mean(embeddings of seed_ids))`, `bias = 0`. Score = cosine
  alignment. "Exclude things like this image(s)."
- **Probe (Phase 2):** positives = a tagged set (or a selection); negatives = an explicit
  tagged set **or a random background sample** (one-class default, so the user only tags
  positives). Fit logistic regression on the positive/negative embeddings → `w, bias`. Tiny
  (hundreds of 512-d vectors, ms) — runs in the backend (fallback: SDK posts `w`). More
  accurate than a single-seed radius; uses the existing tag system.
- **Text (Phase 3, deferred):** `w = normalize(CLIP_text_encode(prompt))` via a **CPU CLIP-text
  sidecar** the backend calls; needs a **field→model registry** (which CLIP variant produced the
  field, and whether it has a text tower — DINOv3 doesn't). Otherwise identical downstream.

### Filtering / exclusion

Add a filter operator (sibling of `geo_polygon_exclude` / the planned `collapse_duplicates`):

- `concept_select {concept_id, op: '>'|'<', threshold}` → `EXISTS (score row AND score <op> t)`
- `concept_exclude {concept_id, op, threshold}` → `NOT EXISTS (...)`

Implemented in `parse_filter_node` as an anti/semi-join to `embedding_concept_scores` (indexed
on `(concept_id, score)`). Composable, reversible, saved in the view, and — because ingest
scores new samples — **auto-applies to newly-streamed data**. This is the "always exclude sky,
even new ones" behavior, with zero dynamic-view code.

### Search / rank (reuse, no new code)

Ad-hoc "find like this" (seed/probe/text) = `similarity_search(vector=w)` — already exists,
HNSW-fast for top-K ≤ 1000, exact scan above. Materialization is only needed when you **save**
the concept for persistent exclude/filter (so ad-hoc is instant; persisting triggers the
one-time scan).

## Concept lifecycle — reopen / rescore / rebuild (+ stacking)

A saved concept is reopenable and re-analyzable. "Re-analyze" is three distinct operations:
- **Reopen / re-tune** — `w` and scores already exist; just move the threshold. Instant, no compute.
- **Re-score new data** — the maintenance cycle scores newly-streamed samples with the SAME `w`
  (anti-join). Reopening later just covers more images; a "rescore now" action forces it on demand.
- **Rebuild** — the user added seed images / tags and wants a sharper direction: regenerate `w`
  (seed mean / probe fit), then full re-score. **Overwrite-in-place (locked 2026-06-14):** UPDATE
  the same concept row's `weight`/`bias`/`spec`, bump `updated_at`, re-score all rows. NO
  versioning/history.

Overwrite-in-place tradeoff (accepted): a Saved View referencing the concept shifts to the new
behavior — same semantics as editing a saved filter. Calibrated 0..1 scores soften this: a saved
threshold still means "the same cut" after a rebuild, so views sharpen rather than break.

**Stacking:** each applied concept is a removable chip / view-stage, so multiple compose
(exclude `sky` AND `blurry` AND …) and the whole stack saves into a Saved View that keeps
auto-applying to new data. A concept can also be used positively (keep-only / rank) while others
exclude.

## API surface (additions)

- `POST /datasets/{id}/concepts` — create: `{name, embedding_field, kind, spec, threshold?}`.
  Backend builds `w` (seed=mean, probe=fit; text=sidecar in Phase 3), stores the concept, kicks
  the score build (async/job). Returns the concept + a preview count at the default threshold.
- `GET /datasets/{id}/concepts` / `DELETE .../concepts/{cid}` — manage.
- `POST .../concepts/{cid}/preview` — count of samples passing `score <op> t` (live threshold
  tuning), index-fast.
- (reuse) `POST /search` with `vector=w` for ad-hoc ranking; `concept_select`/`concept_exclude`
  filter operators for views.

## SDK surface
`dataset.create_concept(name, field, kind, ...)`, `dataset.concepts()`, `concept.preview(t)`,
and `concept.weight` exposed so power users can build probes offline and POST `w` if preferred.

## Frontend UX
- From an image / selection: "Find similar" (ad-hoc search) and "Make a concept from this"
  (seed). From tags: "Train a concept" (probe). (Phase 3: a text box.)
- A concept = a saved object; a view can toggle **Exclude <concept>** (or Keep-only) with a
  **threshold slider + live preview count** ("excludes ~3,400"). Control beats magic.
- Always show "N hidden by <concept> — show" (reversible, counted).

## Incremental / streaming
A maintenance step (alongside refresh_stats / viz_rank / the planned dup-collapse refresh):
for each active concept, score samples that have the embedding but no score row (anti-join),
`<#>` dot-product, insert. New streamed images thus fall under saved concept excludes within
one maintenance cycle (or an ingest hook for immediacy).

## Performance
- Build/rescore per concept: one `<#>` scan of the field (~seconds on 2.4M, like quality compute).
- Incremental: cheap anti-join + small insert.
- Filter/preview: index range scan on `(concept_id, score)` — ms.
- Search: existing HNSW path.
- Narrow scores table (≈ `8 bytes × samples × concepts`); keep concept count sane; partition or
  cap if it ever explodes.

## Phasing
- **Phase 0 — foundation:** migrations (both tables), the `<#>` score engine (build +
  incremental), `concept_select`/`concept_exclude` operators, preview count, the create/list/
  delete API, maintenance hook. No producers yet beyond a raw-`w` create.
- **Phase 1 — seed:** mean-prototype producer + "make concept from image(s)" UX. Smallest add.
- **Phase 2 — probe:** logistic-regression fit (pos tags + background negatives) + "train from
  tags" UX. The accuracy win.
- **Phase 3 — text (deferred):** CPU CLIP-text sidecar + field→model registry → prompt producer
  + text search/box. Isolated, last.

## Open questions — RESOLVED 2026-06-14
1. **Negatives for probes:** DEFERRED to Phase 2. Default = one-class (positives + random
   background) with optional explicit negative tags. Reconfirm when building the probe.
2. **Score sign/normalization:** ✅ **calibrated 0..1** — sigmoid(`w·x + b`) for probe, cosine
   mapped `(s+1)/2` for seed (seed `w` is unit-normalized so `w·x ∈ [-1,1]`). Store the
   calibrated value in `embedding_concept_scores.score`; thresholds/sliders are then portable.
3. **Concept scope:** ✅ **per-dataset only** (`dataset_id NOT NULL`, as in the schema below).
4. **Ingest immediacy:** ✅ **maintenance-cycle** scoring (anti-join in the refresh job). No
   ingest hook for now.
5. **Score-table growth policy:** ✅ rely on `ON DELETE CASCADE` (already) + a **soft cap/warn**
   on active concept count per dataset; revisit partitioning only if it explodes.

## Files to touch (Phase 0–2)
- `backend/db/migrations/03X_embedding_concepts.sql` + `schema.sql` (two tables + indexes; parity-guarded)
- `backend/routers/concepts.py` (new router: create/list/delete/preview + build/score)
- `backend/routers/datasets.py` (`parse_filter_node`: `concept_select` / `concept_exclude` operators)
- a maintenance step (incremental scoring) alongside the existing jobs
- `python/fridge/core/{client,dataset}.py` (concept SDK) + frontend (concept UI, exclude toggle, preview)
- Phase 3 only: a CPU text-encode sidecar + field→model registry
