'use client';

import { useState, useMemo, useEffect, useCallback } from 'react';
import { useQuery } from '@tanstack/react-query';
import { getPatchScales, searchSimilarPatches, type PatchSearchResult } from '@/lib/api';
import { patchScaleLabel, patchScaleSource } from '@/lib/patch-scales';

export interface PatchInfo {
    row: number;
    col: number;
    scale: string;
}

interface UsePatchStateOptions {
    datasetId: number | null | undefined;
    onSearchStart?: () => void;
    onSearchEnd?: () => void;
    onSearchResults?: (sampleIds: number[], description: string) => void;
    onCloseModal?: () => void;
    viewStages?: Array<{ description?: string }>;
}

export function usePatchState({
    datasetId,
    onSearchStart,
    onSearchEnd,
    onSearchResults,
    onCloseModal,
    viewStages = [],
}: UsePatchStateOptions) {
    // Available scales from backend
    const { data: patchScales } = useQuery({
        queryKey: ['patchScales', datasetId],
        queryFn: () => getPatchScales(datasetId!),
        enabled: !!datasetId,
    });

    const availablePatchScales = useMemo(
        () => patchScales?.map(s => s.scale) || [],
        [patchScales]
    );
    // How each scale reads in the pickers: a follower's scale names the follower (lib/patch-scales.ts).
    const patchScaleLabels = useMemo(
        () => Object.fromEntries((patchScales ?? []).map(s => [s.scale, patchScaleLabel(s)])) as Record<string, string>,
        [patchScales]
    );

    // Search state
    const [searchResults, setSearchResults] = useState<PatchSearchResult[] | null>(null);
    const [searchScale, setSearchScale] = useState<string>('4x4');
    const [searchFieldName, setSearchFieldName] = useState<string>('clip');

    // UI state
    const [modalPatchScale, setModalPatchScale] = useState<string | null>(null);
    const [gridPatchMode, setGridPatchMode] = useState<string | null>(null);

    // Selection: per-sample array of selected patches
    const [selectedPatches, setSelectedPatches] = useState<Map<number, PatchInfo[]>>(new Map());

    // Clear search results AND their highlighted patches when the view no longer has a
    // patch-search stage (e.g. filters cleared). Don't clear on every searchResults change —
    // a new search populates selectedPatches with the matched patches (see handlePatchSearch).
    useEffect(() => {
        const hasPatchStage = viewStages.some(
            s => s.description?.startsWith('Patch Search') || s.description?.startsWith('Unlabeled')
        );
        if (!hasPatchStage) {
            // eslint-disable-next-line react-hooks/set-state-in-effect -- synchronize existing per-view UI state after a prop or client-mount change
            setSearchResults(null);
            setSelectedPatches(new Map());
        }
    }, [viewStages]);

    // Search handler (called from sample modal)
    const handlePatchSearch = useCallback(async (
        sampleId: number, fieldName: string, scale: string,
        row: number, col: number, limit: number = 50
    ) => {
        onSearchStart?.();
        try {
            const results = await searchSimilarPatches({
                datasetId: datasetId!,
                sampleId,
                fieldName,
                patchScale: scale,
                patchRow: row,
                patchCol: col,
                limit,
            });
            setSearchResults(results);
            setSearchScale(scale);
            setSearchFieldName(fieldName);
            // Highlight the matching patch on each result image in the grid + enter patch mode,
            // so the search shows WHICH patch matched per image (not just filters the samples).
            const patchMap = new Map<number, PatchInfo[]>();
            for (const r of results) patchMap.set(r.sample_id, [{ row: r.patch_row, col: r.patch_col, scale }]);
            setSelectedPatches(patchMap);
            setGridPatchMode(scale);
            const ids = results.map(r => r.sample_id);
            const source = patchScaleSource(patchScales?.find(s => s.scale === scale));
            onSearchResults?.(ids, `Patch Search (${scale} R${row}C${col})${source}`);
            onCloseModal?.();
        } catch (error) {
            const msg = error instanceof Error ? error.message : 'Unknown error';
            alert(`Patch search failed: ${msg}`);
        } finally {
            onSearchEnd?.();
        }
    }, [datasetId, patchScales, onSearchStart, onSearchEnd, onSearchResults, onCloseModal]);

    // Grid patch click handler (toggle individual patches)
    const handlePatchGridSelect = useCallback((sampleId: number, row: number, col: number, scale: string) => {
        setSelectedPatches(prev => {
            const next = new Map(prev);
            const existing = next.get(sampleId) || [];
            const idx = existing.findIndex(p => p.row === row && p.col === col && p.scale === scale);
            if (idx >= 0) {
                const updated = existing.filter((_, i) => i !== idx);
                if (updated.length === 0) next.delete(sampleId);
                else next.set(sampleId, updated);
            } else {
                next.set(sampleId, [...existing, { row, col, scale }]);
            }
            return next;
        });
    }, []);

    // Embeddings panel / find-unlabeled patch selection handler.
    const handleEmbeddingsPatchSelection = useCallback((patches: Map<number, PatchInfo[]>) => {
        setSelectedPatches(patches);
        // Collect the distinct scales across all selected patches. A SINGLE scale -> set
        // gridPatchMode to it (drives the interactive select overlay). MIXED scales (e.g.
        // find-unlabeled "All sizes" returns 4x4 + 8x8) -> leave null, so each cell highlights
        // its own matched tile at its own scale via the per-patch highlight overlay; a single
        // gridPatchMode would filter out (and mis-grid) every tile of the other scale.
        const scales = new Set<string>();
        for (const arr of patches.values()) for (const p of arr) scales.add(p.scale);
        setGridPatchMode(scales.size === 1 ? [...scales][0] : null);
    }, []);

    const clearSelectedPatches = useCallback(() => setSelectedPatches(new Map()), []);

    // Derived: embeddings panel highlight keys (from both search + grid selection)
    const embeddingHighlightKeys = useMemo(() => {
        const keys = new Set<string>();
        if (searchResults) {
            for (const r of searchResults) {
                keys.add(`${r.sample_id}_${searchFieldName}_patch_${searchScale}_r${r.patch_row}c${r.patch_col}`);
            }
        }
        for (const [sampleId, patches] of selectedPatches) {
            for (const p of patches) {
                keys.add(`${sampleId}_${searchFieldName}_patch_${p.scale}_r${p.row}c${p.col}`);
            }
        }
        return keys.size > 0 ? keys : undefined;
    }, [searchResults, searchScale, searchFieldName, selectedPatches]);

    // Total selected patch count
    const selectedPatchCount = useMemo(() => {
        let count = 0;
        for (const patches of selectedPatches.values()) count += patches.length;
        return count;
    }, [selectedPatches]);

    return {
        // Available scales, and how each reads
        availablePatchScales,
        patchScaleLabels,

        // UI state + setters
        modalPatchScale,
        setModalPatchScale,
        gridPatchMode,
        setGridPatchMode,

        // Selection
        selectedPatches,
        selectedPatchCount,
        clearSelectedPatches,
        handlePatchGridSelect,
        handleEmbeddingsPatchSelection,

        // Search
        handlePatchSearch,

        // Embeddings integration
        embeddingHighlightKeys,
    };
}
