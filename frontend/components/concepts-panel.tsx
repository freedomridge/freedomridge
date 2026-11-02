'use client';

import { useState, useEffect, useMemo, useRef } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
    listConcepts,
    createConcept,
    conceptPreview,
    conceptBoundary,
    conceptStep,
    refineConcept,
    rescoreConcept,
    resumeConceptJob,
    setConceptAutoRescore,
    deleteConcept,
    getEmbeddingFields,
    getDatasetCoverage,
    getSamples,
    JOB_SUBMITTED_EVENT,
    type Concept,
    type Sample,
    type BoundarySample,
    type ViewStage,
} from '@/lib/api';
import { getSampleMediaUrl } from '@/lib/media-utils';
import { useFilters } from '@/contexts/FilterContext';

interface Props {
    datasetId: number;
    onSampleClick?: (sample: Sample) => void;
}

// "Concepts": exclude / rank by "images like this". A concept is a saved direction in an
// embedding field → a calibrated [0,1] score per sample. You point at examples (a selection),
// the slider chooses the cut, and Apply adds a reversible concept_exclude stage to the view.
export function ConceptsPanel({ datasetId, onSampleClick }: Props) {
    const qc = useQueryClient();
    const { viewStages, addStage, removeStage, updateStage, selectedIds, brushedIds } = useFilters();
    const [name, setName] = useState('');
    const [prompt, setPrompt] = useState('');
    const [kind, setKind] = useState<'seed' | 'probe'>('seed');
    const [field, setField] = useState('');
    const [fieldTouched, setFieldTouched] = useState(false);
    const [creating, setCreating] = useState(false);
    const [error, setError] = useState<string | null>(null);

    // Available embedding fields for this dataset (a concept lives in one vector space).
    const { data: fields = [] } = useQuery({
        queryKey: ['embedding-fields', datasetId],
        queryFn: () => getEmbeddingFields(datasetId),
        enabled: !!datasetId,
        staleTime: 60_000,
        refetchOnWindowFocus: false,
    });

    // Seed from the current selection (grid checkboxes first, else an embeddings lasso).
    const seedIds = useMemo(() => (selectedIds.length ? selectedIds : brushedIds), [selectedIds, brushedIds]);

    // The grain of the selection (objects vs images). A concept must be built in a space the seeds
    // actually have, so an OBJECT selection should default to clip_obj — not the first (image)
    // field, which would 400 with "no clip_img embedding". One slim media_type lookup of a seed.
    const { data: seedGrain } = useQuery({
        queryKey: ['concept-seed-grain', datasetId, seedIds[0]],
        queryFn: async () => {
            const r = await getSamples(
                datasetId, 1, 0,
                { logic: 'AND', filters: [{ field: 'id', operator: 'in', value: [seedIds[0]] }] },
                undefined, undefined, true, true, 'media_type',
            );
            return r.items[0]?.media_type ?? null;
        },
        enabled: !!datasetId && seedIds.length > 0,
        staleTime: 30_000,
        refetchOnWindowFocus: false,
    });

    // Default the field to match the seed grain (objects → clip_obj, images → first non-object
    // field), unless the user has picked one. Avoids seeding a concept in a space the seeds lack.
    useEffect(() => {
        if (fieldTouched || !fields.length) return;
        if (seedGrain === 'object') {
            // eslint-disable-next-line react-hooks/set-state-in-effect -- synchronize existing per-view UI state after a prop or client-mount change
            if (fields.includes('clip_obj')) { setField('clip_obj'); return; }
        } else if (seedGrain) {
            const imgField = fields.find((f) => f !== 'clip_obj');
            if (imgField) { setField(imgField); return; }
        }
        if (!field) setField(fields[0]);
    }, [fields, field, fieldTouched, seedGrain]);

    const { data: concepts = [], isLoading } = useQuery({
        queryKey: ['concepts', datasetId],
        queryFn: () => listConcepts(datasetId),
        enabled: !!datasetId,
        staleTime: 30_000,
        refetchOnWindowFocus: false,
    });

    // A long concept action answered 202: show its card as "Scoring…" now, not after the wait.
    useEffect(() => {
        const submitted = (event: Event) => {
            if ((event as CustomEvent<{ datasetId: number }>).detail?.datasetId !== datasetId) return;
            qc.invalidateQueries({ queryKey: ['concepts', datasetId] });
        };
        window.addEventListener(JOB_SUBMITTED_EVENT, submitted);
        return () => window.removeEventListener(JOB_SUBMITTED_EVENT, submitted);
    }, [datasetId, qc]);

    const create = async () => {
        if (!seedIds.length || !name.trim() || !field) return;
        setCreating(true);
        setError(null);
        try {
            await createConcept(datasetId, { name: name.trim(), seed_ids: seedIds, kind, embedding_field: field });
            setName('');
            qc.invalidateQueries({ queryKey: ['concepts', datasetId] });
        } catch (e) {
            setError((e as Error).message);
        } finally {
            setCreating(false);
        }
    };

    // Text concept: no examples needed — the server CLIP-text-encodes the prompt into w.
    // A 501 (encoder not installed server-side) / 400 (non-CLIP field) surfaces verbatim.
    const createFromText = async () => {
        if (!prompt.trim() || !name.trim() || !field) return;
        setCreating(true);
        setError(null);
        try {
            await createConcept(datasetId, { name: name.trim(), prompt: prompt.trim(), kind: 'text', embedding_field: field });
            setName('');
            setPrompt('');
            qc.invalidateQueries({ queryKey: ['concepts', datasetId] });
        } catch (e) {
            setError((e as Error).message);
        } finally {
            setCreating(false);
        }
    };

    return (
        <div className="h-full flex flex-col bg-zinc-900">
            <div className="p-4 border-b border-zinc-800 space-y-3 shrink-0">
                <h2 className="text-sm font-bold text-white">Concepts</h2>
                <p className="text-xs text-zinc-500 leading-relaxed">
                    Select example images (or objects) and name a concept — it scores everything by how
                    much it looks like them. Per concept you can exclude or show only the matches at a cut
                    you tune, browse its sources, or mark ＋/− to refine it. New samples get scored too.
                </p>
                <div className="flex items-center gap-2">
                    <input
                        data-testid="concept-name-input"
                        value={name}
                        onChange={(e) => setName(e.target.value)}
                        placeholder="New concept name…"
                        className="flex-1 bg-zinc-800 border border-zinc-700 text-xs text-white rounded px-2 py-1.5 focus:outline-none focus:ring-1 focus:ring-cyan-500"
                    />
                    <button
                        data-testid="concept-create"
                        onClick={create}
                        disabled={creating || !seedIds.length || !name.trim() || !field}
                        title={seedIds.length ? `${kind === 'probe' ? 'Train probe from' : 'Seed from'} ${seedIds.length} selected sample(s)` : 'Select samples first (grid or embeddings lasso)'}
                        className="text-xs px-3 py-1.5 rounded bg-cyan-600 text-white disabled:bg-zinc-700 disabled:text-zinc-500 hover:bg-cyan-500 whitespace-nowrap"
                    >
                        {creating ? 'Creating…' : `From selection (${seedIds.length})`}
                    </button>
                </div>
                <div className="flex items-center gap-2">
                    <input
                        data-testid="concept-prompt-input"
                        value={prompt}
                        onChange={(e) => setPrompt(e.target.value)}
                        onKeyDown={(e) => { if (e.key === 'Enter' && !creating) createFromText(); }}
                        placeholder='…or describe it: "an open body of water"'
                        title="Text concept: CLIP-encodes your description — no example clicks needed"
                        className="flex-1 bg-zinc-800 border border-zinc-700 text-xs text-white rounded px-2 py-1.5 focus:outline-none focus:ring-1 focus:ring-cyan-500"
                    />
                    <button
                        data-testid="concept-create-text"
                        onClick={createFromText}
                        disabled={creating || !prompt.trim() || !name.trim() || !field}
                        title={name.trim() ? 'Create a concept from the description' : 'Name the concept first'}
                        className="text-xs px-3 py-1.5 rounded bg-cyan-600 text-white disabled:bg-zinc-700 disabled:text-zinc-500 hover:bg-cyan-500 whitespace-nowrap"
                    >
                        From text
                    </button>
                </div>
                <div className="flex items-center gap-2 text-xs">
                    <select
                        data-testid="concept-field"
                        value={field}
                        onChange={(e) => { setFieldTouched(true); setField(e.target.value); }}
                        title="Which embedding space to build the concept in"
                        className="flex-1 min-w-0 bg-zinc-800 border border-zinc-700 text-white rounded px-2 py-1.5"
                    >
                        {fields.length === 0 && <option value="">No embeddings on this dataset</option>}
                        {fields.map((f) => (
                            <option key={f} value={f}>{f}</option>
                        ))}
                    </select>
                    <select
                        data-testid="concept-kind"
                        value={kind}
                        onChange={(e) => setKind(e.target.value as 'seed' | 'probe')}
                        title="Seed = average the examples (fast). Probe = learn a boundary vs the rest (sharper)."
                        className="bg-zinc-800 border border-zinc-700 text-white rounded px-2 py-1.5"
                    >
                        <option value="seed">Seed</option>
                        <option value="probe">Probe</option>
                    </select>
                </div>
                {error && <div className="text-xs text-red-400">{error}</div>}
            </div>

            <div className="flex-1 overflow-y-auto p-4 space-y-3">
                {isLoading && <div className="text-center text-zinc-500 text-sm py-8">Loading concepts…</div>}
                {!isLoading && concepts.length === 0 && (
                    <div className="text-center text-zinc-500 text-sm py-8">
                        No concepts yet. Select some samples and create one above.
                    </div>
                )}
                {concepts.map((c) => (
                    <ConceptCard
                        key={`${c.id}:${c.updated_at ?? ''}`}
                        datasetId={datasetId}
                        concept={c}
                        viewStages={viewStages}
                        addStage={addStage}
                        removeStage={removeStage}
                        updateStage={updateStage}
                        onSampleClick={onSampleClick}
                        onChanged={() => qc.invalidateQueries({ queryKey: ['concepts', datasetId] })}
                        selectionIds={seedIds}
                    />
                ))}
            </div>
        </div>
    );
}

type Mode = 'off' | 'exclude' | 'select' | 'sources';

function ConceptCard({
    datasetId,
    concept,
    viewStages,
    addStage,
    removeStage,
    updateStage,
    onSampleClick,
    onChanged,
    selectionIds,
}: {
    datasetId: number;
    concept: Concept;
    viewStages: ViewStage[];
    addStage: (s: ViewStage) => void;
    removeStage: (i: number) => void;
    updateStage: (i: number, s: ViewStage) => void;
    onSampleClick?: (sample: Sample) => void;
    onChanged: () => void;
    selectionIds: number[];
}) {
    const stageIdx = viewStages.findIndex((s) => s.metadata?.concept && s.metadata?.conceptId === concept.id);
    const applied = stageIdx >= 0;
    const mode: Mode = applied ? ((viewStages[stageIdx].metadata?.mode as Mode) ?? 'exclude') : 'off';
    const cutMode = mode === 'off' || mode === 'exclude' || mode === 'select'; // slider/band relevant

    // Slider ranges to the concept's actual [min,max] band (calibrated scores cluster narrow).
    // Start from the applied cut if any, else a saved in-band threshold, else near the top.
    const lo = concept.score_min ?? 0;
    const hi = concept.score_max ?? 1;
    const inRange = (t: number) => t >= lo && t <= hi;
    const appliedThr = applied ? (viewStages[stageIdx].metadata?.threshold as number | undefined) : undefined;
    const initial =
        typeof appliedThr === 'number' && inRange(appliedThr)
            ? appliedThr
            : inRange(concept.threshold)
              ? concept.threshold
              : Number((lo + 0.85 * (hi - lo)).toFixed(4));
    const [threshold, setThreshold] = useState(initial);
    const [debounced, setDebounced] = useState(initial);
    useEffect(() => {
        const t = setTimeout(() => setDebounced(threshold), 200);
        return () => clearTimeout(t);
    }, [threshold]);

    const [marks, setMarks] = useState<Record<number, 'keep' | 'exclude'>>({});
    const [recomputing, setRecomputing] = useState(false);
    const [selfTrain, setSelfTrain] = useState(false);
    const [stepMode, setStepMode] = useState<'fixed' | 'samples'>('fixed');
    const [collapsed, setCollapsed] = useState(false);
    const [confirmDel, setConfirmDel] = useState(false);
    const steppingRef = useRef(false);
    const cardRef = useRef<HTMLDivElement>(null);
    const markCount = Object.keys(marks).length;

    // The images that DEFINE the concept (seeds + your added positives) — the "sources" view.
    const sourceIds = useMemo(
        () => [
            ...((concept.spec?.seed_ids as number[]) || (concept.spec?.pos_ids as number[]) || []),
            ...((concept.spec?.pos_added as number[]) || []),
        ],
        [concept.spec],
    );
    const removedCount = ((concept.spec?.neg_ids as number[]) || []).length;
    const addedCount = ((concept.spec?.pos_added as number[]) || []).length;

    // ＋ = positive ("is the concept", stored as 'exclude'); − = negative ("is not", 'keep').
    const setMark = (sid: number, m: 'keep' | 'exclude') => {
        setMarks((prev) => {
            const next = { ...prev };
            if (next[sid] === m) delete next[sid];
            else next[sid] = m;
            return next;
        });
        // Return focus to the (stable) card so arrow stepping keeps working while you mark.
        cardRef.current?.focus({ preventScroll: true });
    };
    const markSelection = (k: 'add' | 'remove') => {
        if (!selectionIds.length) return;
        const m = k === 'add' ? 'exclude' : 'keep';
        setMarks((prev) => {
            const next = { ...prev };
            for (const id of selectionIds) next[id] = m;
            return next;
        });
    };

    // Between a definition change and the end of its scoring job the scores mix two definitions:
    // the server refuses filters and previews of the concept, so the card waits (or offers Resume).
    const scoring = concept.scores_ready === false;
    const stopped = scoring && (concept.score_job?.state === 'failed' || concept.score_job?.state === 'cancelled');
    const [resuming, setResuming] = useState(false);
    const resume = async () => {
        if (!concept.score_job) return;
        setResuming(true);
        try {
            await resumeConceptJob(datasetId, concept.score_job.id);
            onChanged();
        } catch (e) {
            alert(`Resume failed: ${(e as Error).message}`);
        } finally {
            setResuming(false);
        }
    };

    const { data: preview } = useQuery({
        queryKey: ['concept-preview', datasetId, concept.id, debounced],
        queryFn: () => conceptPreview(datasetId, concept.id, '>', debounced),
        enabled: !!datasetId && cutMode && !collapsed && !scoring,
        staleTime: 10_000,
        refetchOnWindowFocus: false,
    });

    // Borderline band straddling the cut (closest scores both sides) — shifts as you drag.
    const { data: boundary } = useQuery({
        queryKey: ['concept-boundary', datasetId, concept.id, debounced],
        queryFn: async () => {
            const band = await conceptBoundary(datasetId, concept.id, '>', debounced, 8);
            if (!band.length) return [] as Array<BoundarySample & { sample: Sample }>;
            const ids = band.map((b) => b.sample_id);
            const items = (
                await getSamples(
                    datasetId,
                    ids.length,
                    0,
                    { logic: 'AND', filters: [{ field: 'id', operator: 'in', value: ids }] },
                    undefined,
                    undefined,
                    true,
                    false,
                    undefined,
                    ids,
                )
            ).items;
            const byId = new Map(items.map((s) => [s.id, s]));
            return band
                .map((b) => ({ ...b, sample: byId.get(b.sample_id) }))
                .filter((b): b is BoundarySample & { sample: Sample } => !!b.sample);
        },
        enabled: !!datasetId && cutMode && !collapsed && !scoring,
        staleTime: 10_000,
        refetchOnWindowFocus: false,
    });

    // How many embedded samples this concept hasn't scored yet (new/streamed images) — gates the
    // "score N new" button. Shared query key → one fetch across all cards (React Query dedups).
    const { data: coverage } = useQuery({
        queryKey: ['dataset-coverage', datasetId],
        queryFn: () => getDatasetCoverage(datasetId),
        enabled: !!datasetId,
        staleTime: 300_000,
        refetchOnWindowFocus: false,
    });
    const embedded = coverage?.embeddings.find((e) => e.field === concept.embedding_field)?.present ?? 0;
    const unscored = Math.max(0, embedded - concept.scored_count);

    // The single view switch: off / exclude / show-only / sources.
    const setMode = (m: Mode) => {
        if (m === 'off') {
            if (applied) removeStage(stageIdx);
            return;
        }
        let stage: ViewStage;
        if (m === 'sources') {
            stage = {
                type: 'filter',
                description: `Sources: ${concept.name}`,
                filter: { field: 'id', operator: 'in', value: sourceIds },
                metadata: { concept: true, conceptId: concept.id, conceptName: concept.name, mode: 'sources' },
            };
        } else {
            stage = {
                type: 'filter',
                description: m === 'select' ? `Only: ${concept.name}` : `Exclude: ${concept.name}`,
                filter: {
                    field: '_concept',
                    operator: m === 'select' ? 'concept_select' : 'concept_exclude',
                    value: { concept_id: concept.id, op: '>', threshold },
                },
                metadata: { concept: true, conceptId: concept.id, conceptName: concept.name, op: '>', threshold, mode: m },
            };
        }
        if (applied) updateStage(stageIdx, stage);
        else addStage(stage);
    };

    // Explicit "apply cut": push the current slider threshold to the live view. The slider only
    // previews (count + band) until you do — tuning never silently changes what's applied.
    const applyCut = () => {
        if (stageIdx < 0) return;
        const st = viewStages[stageIdx];
        const m = st.metadata?.mode;
        if (m !== 'exclude' && m !== 'select') return;
        updateStage(stageIdx, {
            ...st,
            filter: {
                field: '_concept',
                operator: m === 'select' ? 'concept_select' : 'concept_exclude',
                value: { concept_id: concept.id, op: '>', threshold },
            },
            metadata: { ...st.metadata, threshold },
        });
    };

    // Keep the "sources" grid view in sync with the concept's defining images after a refit.
    useEffect(() => {
        if (stageIdx < 0) return;
        const st = viewStages[stageIdx];
        if (st.metadata?.mode !== 'sources') return;
        const cur = (st.filter?.value as number[]) || [];
        if (cur.length === sourceIds.length && cur.every((v, i) => v === sourceIds[i])) return;
        updateStage(stageIdx, { ...st, filter: { field: 'id', operator: 'in', value: sourceIds } });
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [sourceIds.join(',')]);

    const runRefine = async (body: { keep_ids?: number[]; exclude_ids?: number[] }) => {
        setRecomputing(true);
        try {
            const updated = await refineConcept(datasetId, concept.id, { ...body, threshold, self_train: selfTrain });
            setMarks({});
            // Keep the applied cut at the refit's preserved threshold (new score scale).
            if (applied && (mode === 'exclude' || mode === 'select')) {
                const st = viewStages[stageIdx];
                updateStage(stageIdx, {
                    ...st,
                    filter: {
                        field: '_concept',
                        operator: mode === 'select' ? 'concept_select' : 'concept_exclude',
                        value: { concept_id: concept.id, op: '>', threshold: updated.threshold },
                    },
                    metadata: { ...st.metadata, threshold: updated.threshold },
                });
            }
            onChanged(); // remount → re-ranged to the new scores; sources view re-syncs
        } catch (e) {
            alert(`Recompute failed: ${(e as Error).message}`);
        } finally {
            setRecomputing(false);
        }
    };
    const recompute = () => {
        const keep_ids = Object.entries(marks).filter(([, v]) => v === 'keep').map(([k]) => Number(k));
        const exclude_ids = Object.entries(marks).filter(([, v]) => v === 'exclude').map(([k]) => Number(k));
        if (!keep_ids.length && !exclude_ids.length) return;
        return runRefine({ keep_ids, exclude_ids });
    };

    const rescore = async () => {
        try {
            await rescoreConcept(datasetId, concept.id, false);
            onChanged();
        } catch (e) {
            alert(`Rescore failed: ${(e as Error).message}`);
        }
    };
    const toggleAutoRescore = async () => {
        try {
            await setConceptAutoRescore(datasetId, concept.id, !concept.auto_rescore);
            onChanged();
        } catch (e) {
            alert(`Failed to update auto-scoring: ${(e as Error).message}`);
        }
    };
    const del = async () => {
        try {
            // Delete FIRST; only drop the applied view stage once the delete actually succeeds —
            // otherwise a failed delete would strip the stage while the concept still exists.
            await deleteConcept(datasetId, concept.id);
            if (applied) removeStage(stageIdx);
            onChanged();
        } catch (e) {
            alert(`Delete failed: ${(e as Error).message}`);
        }
    };

    const stepBy = (hi - lo) / 200 || 0.001;
    const nudge = async (dir: number) => {
        if (stepMode === 'fixed') {
            setThreshold((t) => Number(Math.min(hi, Math.max(lo, t + dir * stepBy)).toFixed(6)));
            return;
        }
        if (steppingRef.current) return;
        steppingRef.current = true;
        try {
            const { threshold: nt } = await conceptStep(datasetId, concept.id, threshold, dir > 0 ? 'up' : 'down', 10);
            if (typeof nt === 'number') setThreshold(nt);
        } catch {
            /* ignore transient step errors */
        } finally {
            steppingRef.current = false;
        }
    };

    const badge = mode === 'exclude' ? '⊘ excl' : mode === 'select' ? '◉ only' : mode === 'sources' ? '⊡ sources' : '';
    const dirtyCut = applied && (mode === 'exclude' || mode === 'select') && typeof appliedThr === 'number' && Math.abs(appliedThr - threshold) > 1e-6;
    const canReset = Math.abs(threshold - initial) > 1e-6; // slider moved off the last applied/saved cut
    const modeBtns = [
        { m: 'off', label: 'off', testid: 'concept-mode-off', title: 'Not applied to the view' },
        { m: 'exclude', label: 'exclude', testid: 'concept-apply', title: 'Hide the matching images' },
        { m: 'select', label: 'show only', testid: 'concept-show-only', title: 'Show ONLY the matching images' },
        { m: 'sources', label: 'sources', testid: 'concept-mode-sources', title: "Show this concept's defining images in the grid" },
    ] as const;

    return (
        <div
            ref={cardRef}
            data-testid="concept-card"
            data-conceptid={concept.id}
            tabIndex={0}
            onKeyDown={(e) => {
                if (e.key === 'ArrowRight' || e.key === 'ArrowUp') {
                    e.preventDefault();
                    nudge(1);
                } else if (e.key === 'ArrowLeft' || e.key === 'ArrowDown') {
                    e.preventDefault();
                    nudge(-1);
                }
            }}
            className={`border rounded-lg p-3 outline-none focus-within:ring-1 focus-within:ring-cyan-600/40 ${applied ? 'border-cyan-700 bg-cyan-950/20' : 'border-zinc-800'}`}
        >
            <div className="flex items-center justify-between mb-2">
                <button
                    onClick={() => setCollapsed((v) => !v)}
                    title={collapsed ? 'Expand' : 'Collapse'}
                    data-testid="concept-collapse-toggle"
                    className="text-zinc-500 hover:text-zinc-200 mr-1 shrink-0 w-4"
                >
                    {collapsed ? '▸' : '▾'}
                </button>
                <div className="flex flex-col min-w-0 flex-1">
                    <div className="flex items-center gap-2 min-w-0">
                        <span className="text-sm text-white truncate">{concept.name}</span>
                        <span className="text-[10px] uppercase text-zinc-500 border border-zinc-700 rounded px-1 shrink-0">{concept.kind}</span>
                        {badge && <span className="text-[10px] text-cyan-400 shrink-0">{badge}</span>}
                    </div>
                    {!collapsed && (
                        <span className="text-[10px] text-zinc-500 truncate" title={concept.embedding_field}>
                            {sourceIds.length} sources
                            {removedCount > 0 ? ` · ${removedCount} removed` : ''}
                            {addedCount > 0 ? ` · ${addedCount} added` : ''}
                        </span>
                    )}
                </div>
                {!collapsed &&
                    (confirmDel ? (
                        <span className="flex items-center gap-1 shrink-0 ml-2 text-xs">
                            <span className="text-zinc-400">delete?</span>
                            <button data-testid="concept-delete-confirm" onClick={del} title="Confirm delete" className="text-red-400 hover:text-red-300">
                                ✓
                            </button>
                            <button data-testid="concept-delete-cancel" onClick={() => setConfirmDel(false)} title="Cancel" className="text-zinc-500 hover:text-zinc-300">
                                ✗
                            </button>
                        </span>
                    ) : (
                        <button
                            data-testid="concept-delete"
                            onClick={() => setConfirmDel(true)}
                            title="Delete concept"
                            className="text-xs text-zinc-500 hover:text-red-400 shrink-0 ml-2"
                        >
                            ✕
                        </button>
                    ))}
            </div>

            {!collapsed && scoring && (
                <div data-testid="concept-scoring" className="text-[11px] text-amber-300/90 mb-2 flex items-center gap-2">
                    {stopped ? (
                        <>
                            <span>Scoring stopped before it finished; the concept can&apos;t filter until it does.</span>
                            <button
                                data-testid="concept-resume"
                                onClick={resume}
                                disabled={resuming}
                                className="underline hover:text-amber-200 disabled:opacity-50"
                            >
                                {resuming ? 'Resuming…' : 'Resume'}
                            </button>
                        </>
                    ) : (
                        <span>Scoring… follow it in Jobs. The concept filters again when it finishes.</span>
                    )}
                </div>
            )}

            {!collapsed && (
                <>
                    <div className="flex items-center gap-2 mb-2 text-[11px]">
                        <span className="text-zinc-500">view:</span>
                        <div className="flex rounded overflow-hidden border border-zinc-700">
                            {modeBtns.map(({ m, label, testid, title }) => {
                                const disabled = (m === 'sources' && sourceIds.length === 0) || (scoring && m !== 'off');
                                return (
                                    <button
                                        key={m}
                                        data-testid={testid}
                                        onClick={() => setMode(m)}
                                        disabled={disabled}
                                        title={scoring && m !== 'off' ? 'Available when scoring finishes' : disabled ? 'No defining images to show (built from a raw weight)' : title}
                                        className={`px-2 py-0.5 ${mode === m ? (m === 'off' ? 'bg-zinc-700 text-white' : 'bg-cyan-600 text-white') : disabled ? 'text-zinc-600 cursor-not-allowed' : 'text-zinc-400 hover:text-white'}`}
                                    >
                                        {label}
                                    </button>
                                );
                            })}
                        </div>
                    </div>

                    {mode === 'sources' && (
                        <div className="text-[10px] text-zinc-500 mb-2">
                            Grid shows the {sourceIds.length} defining image(s) — select any and hit{' '}
                            <span className="text-emerald-400">− remove</span> below, then Recompute.
                        </div>
                    )}

                    {cutMode && (
                        <>
                            <div className="flex items-center gap-2 mb-2">
                                <button
                                    data-testid="concept-threshold-down"
                                    onClick={() => nudge(-1)}
                                    title="Step down — hides more (← / ↓)"
                                    className="px-1.5 rounded bg-zinc-800 text-zinc-300 hover:bg-zinc-700 text-sm leading-none"
                                >
                                    −
                                </button>
                                <input
                                    data-testid="concept-threshold"
                                    type="range"
                                    min={lo}
                                    max={hi}
                                    step={(hi - lo) / 200 || 0.001}
                                    value={threshold}
                                    onChange={(e) => setThreshold(parseFloat(e.target.value))}
                                    className="flex-1 accent-cyan-500"
                                />
                                <button
                                    data-testid="concept-threshold-up"
                                    onClick={() => nudge(1)}
                                    title="Step up — hides fewer (→ / ↑)"
                                    className="px-1.5 rounded bg-zinc-800 text-zinc-300 hover:bg-zinc-700 text-sm leading-none"
                                >
                                    +
                                </button>
                                <span className="text-xs text-zinc-400 tabular-nums w-12 text-right">{threshold.toFixed(3)}</span>
                            </div>
                            <div className="flex items-center gap-2 text-[10px] text-zinc-500 mb-2">
                                <span>step:</span>
                                <button
                                    data-testid="concept-step-mode-fixed"
                                    onClick={() => setStepMode('fixed')}
                                    className={stepMode === 'fixed' ? 'text-cyan-400' : 'hover:text-zinc-300'}
                                    title="Step by a small fixed amount (instant)"
                                >
                                    fixed
                                </button>
                                <span>·</span>
                                <button
                                    data-testid="concept-step-mode-samples"
                                    onClick={() => setStepMode('samples')}
                                    className={stepMode === 'samples' ? 'text-cyan-400' : 'hover:text-zinc-300'}
                                    title="Step to the next ~10 images — even batches"
                                >
                                    by&nbsp;samples
                                </button>
                                <span data-testid="concept-hidden-count" className="ml-auto text-cyan-400">
                                    {preview ? `${preview.matched.toLocaleString()} would be ${mode === 'select' ? 'shown' : 'hidden'}` : '…'}
                                </span>
                                {canReset && (
                                    <button
                                        data-testid="concept-reset-cut"
                                        onClick={() => setThreshold(initial)}
                                        title="Reset to the last applied / saved cut"
                                        className="px-1.5 py-0.5 rounded bg-zinc-800 text-zinc-300 hover:bg-zinc-700"
                                    >
                                        reset
                                    </button>
                                )}
                                {dirtyCut && (
                                    <button
                                        data-testid="concept-apply-cut"
                                        onClick={applyCut}
                                        title="Apply this cut to the view"
                                        className="px-1.5 py-0.5 rounded bg-cyan-600 text-white hover:bg-cyan-500"
                                    >
                                        apply cut
                                    </button>
                                )}
                            </div>
                        </>
                    )}

                    {selectionIds.length > 0 && (
                        <div className="flex items-center gap-2 text-[10px] mb-2">
                            <span className="text-zinc-500">selection ({selectionIds.length}):</span>
                            <button
                                data-testid="concept-selection-add"
                                onClick={() => markSelection('add')}
                                title="These ARE the concept (＋) — stage, then Recompute"
                                className="text-rose-400 hover:text-rose-300"
                            >
                                ＋ add to concept
                            </button>
                            <button
                                data-testid="concept-selection-remove"
                                onClick={() => markSelection('remove')}
                                title="These are NOT the concept (−) — stage, then Recompute"
                                className="text-emerald-400 hover:text-emerald-300"
                            >
                                − remove
                            </button>
                        </div>
                    )}

                    {(markCount > 0 || unscored > 0) && (
                        <div className="flex items-center gap-2 text-xs mb-2">
                            {markCount > 0 && (
                                <>
                                    <button
                                        data-testid="concept-recompute"
                                        onClick={recompute}
                                        disabled={recomputing || scoring}
                                        title="Refit the concept from your ＋/− marks (keeps your cut level)"
                                        className="px-2 py-0.5 rounded bg-amber-600 text-white hover:bg-amber-500 disabled:opacity-50"
                                    >
                                        {recomputing ? 'Recomputing…' : `Recompute (${markCount})`}
                                    </button>
                                    <label
                                        className="flex items-center gap-1 text-zinc-400 cursor-pointer"
                                        title="Also learn from the current hidden/kept split (your marks still dominate)"
                                    >
                                        <input type="checkbox" checked={selfTrain} onChange={(e) => setSelfTrain(e.target.checked)} className="accent-amber-500" />
                                        self-train
                                    </label>
                                </>
                            )}
                            {unscored > 0 && !scoring && (
                                <button
                                    data-testid="concept-rescore"
                                    onClick={rescore}
                                    title={`Score ${unscored.toLocaleString()} newly-added image(s) with this concept`}
                                    className="text-amber-400/80 hover:text-amber-300 ml-auto"
                                >
                                    score {unscored.toLocaleString()} new
                                </button>
                            )}
                        </div>
                    )}

                    {/* Auto-scoring: make the recurring hourly cost visible and optional. */}
                    <label
                        className="flex items-start gap-1.5 text-[11px] text-zinc-400 mb-2 cursor-pointer"
                        title="When on, a background job scores newly-streamed images against this concept each hour, so a saved Exclude/Only view keeps catching new data automatically. Turn it off to avoid the recurring cost — you can still score new images on demand with 'score N new'."
                    >
                        <input
                            type="checkbox"
                            data-testid="concept-auto-rescore"
                            checked={concept.auto_rescore}
                            onChange={toggleAutoRescore}
                            className="accent-cyan-500 mt-0.5"
                        />
                        <span>
                            Auto-score new images hourly
                            <span className="block text-zinc-500">
                                {concept.auto_rescore
                                    ? 'New images are scored automatically each hour.'
                                    : 'Off — score new images manually with “score N new”.'}
                            </span>
                        </span>
                    </label>

                    {cutMode && (boundary || []).length > 0 && (
                        <div className="text-[10px] text-zinc-500 mb-1">
                            At the cut — mark <span className="text-rose-400">＋ is</span> / <span className="text-emerald-400">− not</span>, then Recompute
                        </div>
                    )}
                    {cutMode && (
                        <div className="grid grid-cols-4 gap-1.5">
                            {(boundary || []).map((b) => {
                                const mark = marks[b.sample_id];
                                const frame =
                                    mark === 'exclude'
                                        ? 'border-rose-500'
                                        : mark === 'keep'
                                          ? 'border-emerald-500'
                                          : b.cut
                                            ? 'border-rose-900/60'
                                            : 'border-emerald-900/60';
                                return (
                                    <div key={b.sample_id} data-testid="concept-boundary-item" className="flex flex-col gap-0.5">
                                        <div
                                            className={`aspect-square bg-zinc-800 rounded overflow-hidden cursor-pointer border-2 ${frame}`}
                                            onClick={() => onSampleClick?.(b.sample)}
                                            title={`score ${b.score.toFixed(3)} — ${b.cut ? 'currently hidden' : 'currently kept'}`}
                                        >
                                            {/* eslint-disable-next-line @next/next/no-img-element -- authenticated media proxy, next/image not applicable */}
                                            <img
                                                src={getSampleMediaUrl(b.sample.id, b.sample.metadata?.thumbnail_path ? 'thumbnail_path' : 'filepath')}
                                                alt=""
                                                className="w-full h-full object-cover"
                                                loading="lazy"
                                            />
                                        </div>
                                        <div className="flex gap-0.5">
                                            <button
                                                data-testid="concept-mark-pos"
                                                onClick={() => setMark(b.sample_id, 'exclude')}
                                                title="＋ this IS the concept"
                                                className={`flex-1 text-[10px] font-semibold rounded py-0.5 ${mark === 'exclude' ? 'bg-rose-600 text-white' : 'bg-rose-500/15 text-rose-300 hover:bg-rose-500/30'}`}
                                            >
                                                ＋
                                            </button>
                                            <button
                                                data-testid="concept-mark-neg"
                                                onClick={() => setMark(b.sample_id, 'keep')}
                                                title="− this is NOT the concept"
                                                className={`flex-1 text-[10px] font-semibold rounded py-0.5 ${mark === 'keep' ? 'bg-emerald-600 text-white' : 'bg-emerald-500/15 text-emerald-300 hover:bg-emerald-500/30'}`}
                                            >
                                                −
                                            </button>
                                        </div>
                                    </div>
                                );
                            })}
                        </div>
                    )}
                </>
            )}
        </div>
    );
}
