"""Embedding "concepts".

A concept is a linear direction `w` in an embedding field's space → a calibrated [0,1]
per-sample score. Producers differ only in how `w` is made:
  - seed  : unit-normalized mean of the seed samples' embeddings (model-free)
  - probe : logistic-regression fit on tagged ± embeddings (Phase 2, model-free)
  - text  : CLIP text-encode a prompt (Phase 3, deferred — needs a CPU sidecar)
Everything downstream is shared and already exists: rank by a concept = similarity_search
with vector=w; exclude/keep = the concept_select / concept_exclude filter operators against
the narrow embedding_concept_scores table. Manual scoring and bounded maintenance share
the concept_score_value() SQL function; pending work tracks committed vector changes.

Create, rebuild (new direction), refine and rescore run as one explicit user-lane job each
(backend.concept_jobs, kind concept_rescore). The request answers within five seconds: the
final JSON when the job finished, otherwise 202 {job_id, state}. Threshold-only changes stay
synchronous.
"""

import asyncio
import math
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import concept_jobs, job_wait
from ..access import require_dataset_access
from ..concept_jobs import refine_marks, spec_dict, vector_literal
from ..database import db
from ..encoders import encode_text, has_text_tower
from .auth import require_role

router = APIRouter(
    prefix="/datasets/{id}/concepts",
    tags=["concepts"],
    dependencies=[Depends(require_dataset_access)],
)

# Soft cap on active concepts per dataset (scores cascade on delete; the cap is soft). Each
# concept materializes ~one score row per sample; keep the count sane. Not a hard limit —
# delete unused concepts (their scores cascade) to make room.
MAX_CONCEPTS_PER_DATASET = concept_jobs.MAX_CONCEPTS_PER_DATASET


class ConceptCreate(BaseModel):
    name: str
    embedding_field: str = "clip"
    kind: str = "seed"  # 'seed' (mean prototype) | 'probe' (logistic fit) | 'text' (CLIP prompt)
    seed_ids: Optional[List[int]] = None  # seed: samples to average; probe: positive examples
    neg_ids: Optional[List[int]] = None  # probe: explicit negatives (else random background)
    prompt: Optional[str] = None  # text: natural-language prompt, CLIP-text-encoded server-side
    weight: Optional[List[float]] = None  # advanced: bring-your-own w (overrides the producer)
    bias: float = 0.0
    threshold: Optional[float] = None  # None → derive a sane default from the score range
    spec: Optional[Dict[str, Any]] = None


class ConceptRebuild(BaseModel):
    # Overwrite-in-place: new seeds/weight regenerate w (then full rescore); a threshold-only
    # body just retunes the saved default cut without rescoring.
    seed_ids: Optional[List[int]] = None
    weight: Optional[List[float]] = None
    threshold: Optional[float] = None


class ConceptRefine(BaseModel):
    # Active-learning corrections from the boundary: keep_ids → negatives ("don't exclude this"),
    # exclude_ids → positives ("exclude this / more like it"). Refits as a weighted probe.
    keep_ids: Optional[List[int]] = None
    exclude_ids: Optional[List[int]] = None
    threshold: Optional[float] = None  # current slider → preserve the cut level (hidden count)
    self_train: bool = False  # also weakly-label the current hidden/kept split as ±


_VALID_OPS = (">", ">=", "<", "<=")


def _vec_literal(values: List[float]) -> str:
    """Format a Python float list as a pgvector literal."""
    return vector_literal(values)


_spec_dict = spec_dict


async def _build_seed_weight(dataset_id: int, field: str, seed_ids: List[int]) -> str:
    """w = unit-normalized mean of the seed samples' embeddings, as a pgvector literal.

    Cosine scoring is magnitude-invariant, so normalization is cosmetic here, but we keep w
    unit-length to match the documented contract (and in case w is ever used with a dot).
    """
    rows = await db.fetch(
        "SELECT e.vector::text AS v FROM embeddings e "
        "JOIN samples s ON s.dataset_id = e.dataset_id AND s.id = e.sample_id "
        "WHERE s.dataset_id = $1 AND e.field_name = $2 AND e.sample_id = ANY($3::int[]) "
        "AND e.vector IS NOT NULL",
        dataset_id,
        field,
        seed_ids,
    )
    if not rows:
        raise HTTPException(
            status_code=400,
            detail=f"No '{field}' embeddings found for the given seed samples",
        )

    def mean_direction():
        vecs = [[float(x) for x in r["v"].strip("[]").split(",")] for r in rows]
        dim = len(vecs[0])
        mean = [sum(v[i] for v in vecs) / len(vecs) for i in range(dim)]
        norm = math.sqrt(sum(c * c for c in mean))
        if norm > 0:
            mean = [c / norm for c in mean]
        return _vec_literal(mean)

    # Bounded by the seed count, but still CPU: keep it off the shared event loop.
    return await asyncio.to_thread(mean_direction)


async def _require_vectors(dataset_id: int, field: str, ids: List[int], detail: str) -> None:
    """Bounded existence check, so missing examples still answer 400 synchronously."""
    found = await db.fetchval(
        "SELECT EXISTS(SELECT 1 FROM embeddings e JOIN samples s ON s.dataset_id = e.dataset_id AND s.id = e.sample_id "
        "WHERE s.dataset_id = $1 AND e.field_name = $2 AND e.sample_id = ANY($3::int[]) "
        "AND e.vector IS NOT NULL)",
        dataset_id,
        field,
        ids,
    )
    if not found:
        raise HTTPException(status_code=400, detail=detail)


async def _require_weight_dims(dataset_id: int, field: str, dims: int) -> None:
    """A raw weight must match the field's vectors; checked before anything is submitted (400)."""
    field_dim = await db.fetchval(
        "SELECT vector_dims(vector) FROM embeddings e "
        "JOIN samples s ON s.dataset_id = e.dataset_id AND s.id = e.sample_id "
        "WHERE s.dataset_id = $1 AND e.field_name = $2 AND e.vector IS NOT NULL LIMIT 1",
        dataset_id,
        field,
    )
    if field_dim is not None and field_dim != dims:
        raise HTTPException(
            status_code=400,
            detail=f"Weight has {dims} dims but field '{field}' stores {field_dim}",
        )


# Score reads of a concept whose definition changed would mix two meanings until its job ends. The
# read's own statement refuses them (require_concept_scores; the app answers 409), so
# a definition change between a separate check and the read cannot slip through. $1 is the concept.
_READY = "(SELECT require_concept_scores(ARRAY[$1]::int[])) AND "


async def _get_concept(dataset_id: int, concept_id: int) -> Dict[str, Any]:
    row = await db.fetchrow(
        "SELECT id, dataset_id, name, embedding_field, kind, spec, bias, threshold, "
        "scored_count, created_at, updated_at "
        "FROM embedding_concepts WHERE id = $1 AND dataset_id = $2",
        concept_id,
        dataset_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Concept not found")
    out = dict(row)
    out["spec"] = _spec_dict(out.get("spec"))
    # A preview at the saved default threshold (exclude direction: score > t) so the UI has
    # an initial "N would be hidden" without a second call.
    out["matched"] = (
        await db.fetchval(
            "SELECT count(*) FROM embedding_concept_scores WHERE concept_id = $1 AND score > $2",
            concept_id,
            row["threshold"],
        )
        or 0
    )
    # Actual score range — the UI ranges the threshold slider to [min,max] (calibrated scores
    # cluster in a narrow band, e.g. CLIP cosine in ~[0.7,1.0], so a fixed 0..1 slider is useless).
    # Exclude NaN (a zero-magnitude embedding gives a NaN seed score, and Postgres sorts NaN as
    # the largest value, so max(score) would otherwise poison the slider's upper bound).
    rng = await db.fetchrow(
        "SELECT min(score) AS lo, max(score) AS hi FROM embedding_concept_scores "
        "WHERE concept_id = $1 AND score <> 'NaN'::real",
        concept_id,
    )
    out["score_min"] = rng["lo"] if rng else None
    out["score_max"] = rng["hi"] if rng else None
    return out


async def _owned(dataset_id: int, concept_id: int) -> None:
    owned = await db.fetchval(
        "SELECT 1 FROM embedding_concepts WHERE id = $1 AND dataset_id = $2",
        concept_id,
        dataset_id,
    )
    if not owned:
        raise HTTPException(status_code=404, detail="Concept not found")


@router.get("")
async def list_concepts(id: int):
    """All concepts for this dataset (the library)."""
    # scores_ready / score_job are additive: the explicit job owning the concept (if any) and its
    # state, so the UI can show "Scoring…" or offer Resume for a stopped job.
    rows = await db.fetch(
        "SELECT c.id, c.name, c.embedding_field, c.kind, c.spec, c.bias, c.threshold, c.scored_count, "
        "c.auto_rescore, c.created_at, c.updated_at, r.score_min, r.score_max, c.scores_ready, "
        "c.score_job_id, j.state AS score_job_state "
        "FROM embedding_concepts c "
        "LEFT JOIN LATERAL ("
        "  SELECT min(score) AS score_min, max(score) AS score_max "
        "  FROM embedding_concept_scores WHERE concept_id = c.id AND score <> 'NaN'::real"
        ") r ON TRUE "
        "LEFT JOIN jobs j ON j.id = c.score_job_id "
        "WHERE c.dataset_id = $1 ORDER BY c.id",
        id,
    )
    out = []
    for r in rows:
        d = dict(r)
        d["spec"] = _spec_dict(d.get("spec"))
        job_id, state = d.pop("score_job_id"), d.pop("score_job_state")
        d["score_job"] = {"id": str(job_id), "state": state} if job_id is not None and state is not None else None
        out.append(d)
    return out


@router.post("")
async def create_concept(
    id: int,
    body: ConceptCreate,
    user: dict = Depends(require_role("power_user")),
    wait: bool = True,
):
    """Create a concept (seed = average seed embeddings; probe = fit; text = CLIP prompt; or
    bring-your-own weight), then score it in a job. Answers within five seconds with the concept +
    a preview count at the default threshold, or 202 {job_id, state} while the job continues."""
    started = time.monotonic()
    n = await db.fetchval("SELECT count(*) FROM embedding_concepts WHERE dataset_id = $1", id) or 0
    if n >= MAX_CONCEPTS_PER_DATASET:
        raise HTTPException(
            status_code=400,
            detail=f"Concept limit reached ({MAX_CONCEPTS_PER_DATASET}); delete unused concepts first",
        )
    dup = await db.fetchval(
        "SELECT 1 FROM embedding_concepts WHERE dataset_id = $1 AND name = $2",
        id,
        body.name,
    )
    if dup:
        raise HTTPException(status_code=409, detail=f"A concept named {body.name!r} already exists")

    spec = dict(body.spec or {})
    args: Dict[str, Any] = {
        "operation": "create",
        "name": body.name,
        "embedding_field": body.embedding_field,
        "kind": body.kind,
        "bias": body.bias,
        "threshold": body.threshold,
    }
    if body.weight is not None:
        await _require_weight_dims(id, body.embedding_field, len(body.weight))
        args["weight"] = _vec_literal(body.weight)
    elif body.kind == "seed":
        if not body.seed_ids:
            raise HTTPException(status_code=400, detail="seed_ids required for a seed concept")
        args["weight"] = await _build_seed_weight(id, body.embedding_field, body.seed_ids)
        spec.setdefault("seed_ids", body.seed_ids)
    elif body.kind == "probe":
        if not body.seed_ids:
            raise HTTPException(status_code=400, detail="seed_ids (positive examples) required for a probe concept")
        await _require_vectors(
            id,
            body.embedding_field,
            body.seed_ids,
            f"No '{body.embedding_field}' embeddings found for the positive samples",
        )
        # The random background scans the field: the job fits the probe in its first unit.
        args["train"] = {"probe": {"pos_ids": body.seed_ids, "neg_ids": body.neg_ids or None}}
    elif body.kind == "text":
        # Phase-3 text concepts, minus the sidecar: encode the prompt in-process (501 with a
        # pip hint when transformers is absent — SDK users can encode client-side instead).
        # Scoring treats kind='text' exactly like 'seed' (cosine to w).
        if not body.prompt:
            raise HTTPException(status_code=400, detail="prompt required for a text concept")
        registered = await db.fetchval(
            "SELECT model FROM embedding_field_models WHERE field_name = $1",
            body.embedding_field,
        )
        if not has_text_tower(registered):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Field '{body.embedding_field}' was embedded with {registered!r}, which has "
                    f"no text tower — text concepts need a CLIP-family embedding field."
                ),
            )
        encoded = await encode_text(body.prompt, registered)
        # Dim-check BEFORE submitting: a mismatched vector would create the concept and
        # then fail scoring — leaving a permanently broken concept whose name 409s on retry.
        field_dim = await db.fetchval(
            "SELECT vector_dims(vector) FROM embeddings e "
            "JOIN samples s ON s.dataset_id = e.dataset_id AND s.id = e.sample_id "
            "WHERE s.dataset_id = $1 AND e.field_name = $2 AND e.vector IS NOT NULL LIMIT 1",
            id,
            body.embedding_field,
        )
        if field_dim is not None and field_dim != len(encoded):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Text encoder produced {len(encoded)} dims but field '{body.embedding_field}' "
                    f"stores {field_dim} — register the field's checkpoint (embedding_field_models) "
                    f"so the matching text tower is used."
                ),
            )
        args["weight"] = _vec_literal(encoded)
        spec.setdefault("prompt", body.prompt)
    else:
        raise HTTPException(status_code=400, detail=f"Unknown concept kind: {body.kind!r}")
    args["spec"] = spec
    job = await concept_jobs.submit_explicit(db.pool, id, user["id"], args)
    return await job_wait.answer(db.pool, job, started, user=user, wait=wait)


@router.get("/{concept_id}/preview")
async def preview_concept(id: int, concept_id: int, op: str = ">", threshold: float = 0.5):
    """Count of samples passing `score <op> threshold` — index-fast, for live slider tuning."""
    if op not in _VALID_OPS:
        raise HTTPException(status_code=400, detail=f"Invalid op: {op!r}")
    await _owned(id, concept_id)
    matched = (
        await db.fetchval(
            f"SELECT count(*) FROM embedding_concept_scores WHERE {_READY}concept_id = $1 AND score {op} $2",
            concept_id,
            threshold,
        )
        or 0
    )
    total = await db.fetchval("SELECT scored_count FROM embedding_concepts WHERE id = $1", concept_id) or 0
    return {"concept_id": concept_id, "op": op, "threshold": threshold, "matched": matched, "total": total}


def _is_cut(score: float, op: str, threshold: float) -> bool:
    """Whether a sample is in the matched (excluded) set for this op/threshold."""
    if op == ">":
        return score > threshold
    if op == ">=":
        return score >= threshold
    if op == "<":
        return score < threshold
    return score <= threshold


@router.get("/{concept_id}/boundary")
async def concept_boundary(id: int, concept_id: int, op: str = ">", threshold: float = 0.5, limit: int = 8):
    """Samples straddling the cut — those whose score is closest to the threshold on BOTH sides
    — so the preview shows the genuinely marginal cases the user can correct either way (mark a
    cut one 'keep', or a kept one 'exclude'). Returns [{sample_id, score, cut}]. Index scans."""
    if op not in _VALID_OPS:
        raise HTTPException(status_code=400, detail=f"Invalid op: {op!r}")
    await _owned(id, concept_id)
    half = max(1, limit // 2)
    above = await db.fetch(
        "SELECT sample_id, score FROM embedding_concept_scores "
        f"WHERE {_READY}concept_id = $1 AND score > $2 ORDER BY score ASC LIMIT $3",
        concept_id,
        threshold,
        half,
    )
    below = await db.fetch(
        "SELECT sample_id, score FROM embedding_concept_scores "
        f"WHERE {_READY}concept_id = $1 AND score <= $2 ORDER BY score DESC LIMIT $3",
        concept_id,
        threshold,
        half,
    )
    rows = [dict(r) for r in above] + [dict(r) for r in below]
    rows.sort(key=lambda r: r["score"], reverse=True)
    return [{"sample_id": r["sample_id"], "score": r["score"], "cut": _is_cut(r["score"], op, threshold)} for r in rows]


@router.get("/{concept_id}/step")
async def concept_step(id: int, concept_id: int, threshold: float, direction: str = "up", n: int = 8):
    """Threshold of the score ~n samples past the current cut (a tiny indexed OFFSET, ms) — lets
    the UI step by a fixed NUMBER of samples instead of a fixed value, so each step surfaces a
    fresh batch of marginal images however the scores cluster. direction='up' raises the cut
    (hides fewer); 'down' lowers it (hides more)."""
    if direction not in ("up", "down"):
        raise HTTPException(status_code=400, detail="direction must be 'up' or 'down'")
    await _owned(id, concept_id)
    n = max(1, min(int(n), 5000))
    if direction == "up":
        new_t = await db.fetchval(
            f"SELECT score FROM embedding_concept_scores WHERE {_READY}concept_id = $1 AND score > $2 "
            "ORDER BY score ASC OFFSET $3 LIMIT 1",
            concept_id,
            threshold,
            n,
        )
        if new_t is None:
            new_t = await db.fetchval(
                f"SELECT max(score) FROM embedding_concept_scores WHERE {_READY}concept_id = $1 "
                "AND score <> 'NaN'::real",
                concept_id,
            )
    else:
        new_t = await db.fetchval(
            f"SELECT score FROM embedding_concept_scores WHERE {_READY}concept_id = $1 AND score <= $2 "
            "ORDER BY score DESC OFFSET $3 LIMIT 1",
            concept_id,
            threshold,
            n,
        )
        if new_t is None:
            new_t = await db.fetchval(
                f"SELECT min(score) FROM embedding_concept_scores WHERE {_READY}concept_id = $1",
                concept_id,
            )
    return {"threshold": new_t}


@router.post("/{concept_id}/rescore")
async def rescore_concept(
    id: int,
    concept_id: int,
    full: bool = True,
    user: dict = Depends(require_role("power_user")),
    wait: bool = True,
):
    """Full rebuild, or pending changes for auto concepts / new embeddings for manual concepts.
    Returns {concept_id, scored} within five seconds, or 202 {job_id, state}."""
    started = time.monotonic()
    auto = await db.fetchval(
        "SELECT auto_rescore FROM embedding_concepts WHERE id = $1 AND dataset_id = $2",
        concept_id,
        id,
    )
    if auto is None:
        raise HTTPException(status_code=404, detail="Concept not found")
    mode = "full" if full else "pending" if auto else "appended"
    job = await concept_jobs.submit_explicit(
        db.pool,
        id,
        user["id"],
        {"operation": "rescore", "concept_id": concept_id, "full": full, "mode": mode},
    )
    return await job_wait.answer(db.pool, job, started, user=user, wait=wait)


@router.post("/{concept_id}/rebuild")
async def rebuild_concept(
    id: int,
    concept_id: int,
    body: ConceptRebuild,
    user: dict = Depends(require_role("power_user")),
    wait: bool = True,
):
    """Overwrite-in-place: regenerate w from new seeds/weight then full rescore (a job), or (if only
    threshold is given) just retune the saved default cut with no rescore (synchronous)."""
    started = time.monotonic()
    row = await db.fetchrow(
        "SELECT embedding_field FROM embedding_concepts WHERE id = $1 AND dataset_id = $2",
        concept_id,
        id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Concept not found")

    # Threshold-only retune (no new direction → no rescore needed).
    if body.seed_ids is None and body.weight is None:
        if body.threshold is None:
            raise HTTPException(status_code=400, detail="Provide seed_ids, weight, or threshold")
        await db.execute(
            "UPDATE embedding_concepts SET threshold = $1, updated_at = now() WHERE id = $2",
            body.threshold,
            concept_id,
        )
        return await _get_concept(id, concept_id)

    if body.weight is not None:
        await _require_weight_dims(id, row["embedding_field"], len(body.weight))
        weight = _vec_literal(body.weight)
        spec_patch: Dict[str, Any] = {"weight_override": True}
    elif body.seed_ids is not None:
        weight = await _build_seed_weight(id, row["embedding_field"], body.seed_ids)
        spec_patch = {"seed_ids": body.seed_ids}
    else:  # unreachable given the threshold-only guard above; keeps the types honest
        raise HTTPException(status_code=400, detail="Provide seed_ids, weight, or threshold")

    job = await concept_jobs.submit_explicit(
        db.pool,
        id,
        user["id"],
        {
            "operation": "rebuild",
            "concept_id": concept_id,
            "weight": weight,
            "spec": spec_patch,
            "threshold": body.threshold,
        },
    )
    return await job_wait.answer(db.pool, job, started, user=user, wait=wait)


@router.post("/{concept_id}/refine")
async def refine_concept(
    id: int,
    concept_id: int,
    body: ConceptRefine,
    user: dict = Depends(require_role("power_user")),
    wait: bool = True,
):
    """Active-learning refine: fold boundary corrections into the concept and refit. keep_ids
    become negatives ("don't exclude"), exclude_ids become positives ("exclude / more like
    this"); both are weighted heavily so they move the line. The concept becomes a learned
    probe; then full rescore. Threshold is reset into the new score band. The refit and the
    rescore run in a job; the answer comes within five seconds, or 202 {job_id, state}."""
    started = time.monotonic()
    keep = list(dict.fromkeys(body.keep_ids or []))
    excl = list(dict.fromkeys(body.exclude_ids or []))
    if not keep and not excl:
        raise HTTPException(status_code=400, detail="Provide keep_ids and/or exclude_ids")
    row = await db.fetchrow(
        "SELECT embedding_field, spec FROM embedding_concepts WHERE id = $1 AND dataset_id = $2",
        concept_id,
        id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Concept not found")
    base_pos, added_pos, _ = refine_marks(_spec_dict(row["spec"]), keep, excl)
    if not base_pos and not added_pos:
        raise HTTPException(status_code=400, detail="No positive examples with embeddings to refit")
    await _require_vectors(
        id,
        row["embedding_field"],
        list(dict.fromkeys(base_pos + added_pos)),
        "No positive examples with embeddings to refit",
    )
    job = await concept_jobs.submit_explicit(
        db.pool,
        id,
        user["id"],
        {
            "operation": "refine",
            "concept_id": concept_id,
            "train": {
                "refine": {
                    "keep_ids": keep,
                    "exclude_ids": excl,
                    "threshold": body.threshold,
                    "self_train": body.self_train,
                },
            },
        },
    )
    return await job_wait.answer(db.pool, job, started, user=user, wait=wait)


class AutoRescoreUpdate(BaseModel):
    enabled: bool


@router.post("/{concept_id}/auto-rescore")
async def set_auto_rescore(
    id: int,
    concept_id: int,
    body: AutoRescoreUpdate,
    user: dict = Depends(require_role("power_user")),
):
    """Toggle whether the hourly maintenance job auto-scores newly-streamed samples for
    this concept. On (default) keeps a saved concept-exclude/select current as data streams
    in — cheap now that scoring is incremental past the concept's watermark. Off stops the
    recurring cost; the user rescoring manually (rescore/rebuild) still works."""
    # Toggling changes the score revision, which would void a running explicit job's fence.
    updated = await db.fetchrow(
        "UPDATE embedding_concepts concept SET auto_rescore = $1, updated_at = now() "
        "WHERE id = $2 AND dataset_id = $3 AND NOT (" + concept_jobs.ACTIVE_OWNER + ") RETURNING id",
        body.enabled,
        concept_id,
        id,
    )
    if not updated:
        await _owned(id, concept_id)
        raise HTTPException(status_code=409, detail="Concept is being rescored; change auto-scoring when its job ends")
    return {"id": concept_id, "auto_rescore": body.enabled}


@router.delete("/{concept_id}")
async def delete_concept(id: int, concept_id: int, user: dict = Depends(require_role("power_user"))):
    """Delete a concept; its materialized scores cascade-delete."""
    deleted = await db.fetchval(
        "DELETE FROM embedding_concepts WHERE id = $1 AND dataset_id = $2 RETURNING id",
        concept_id,
        id,
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="Concept not found")
    return {"deleted": concept_id}
