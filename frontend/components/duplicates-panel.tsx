'use client';

import { useState, useRef, useEffect, useMemo } from 'react';
import { useQuery, useInfiniteQuery, useQueryClient } from '@tanstack/react-query';
import { getHashStats, findDuplicates, getSamples, getDuplicateCount, refreshDuplicates, notifySamplesMutated, type Sample, type ComplexFilter, type ViewStage } from '@/lib/api';
import { getSampleMediaUrl } from '@/lib/media-utils';
import { useFilters, withGrain } from '@/contexts/FilterContext';

interface Props {
    datasetId: number;
    onSelection: (ids: number[]) => void;
    onSampleClick: (sample: Sample) => void;
     onToggleSelection?: (id: number) => void;
    selectedIds?: number[];
    hasObjects?: boolean;
}

const PAGE_SIZE = 20;

export function DuplicatesPanel({ datasetId, onSelection, onSampleClick, onToggleSelection, selectedIds = [], hasObjects = false }: Props) {
    const [hashField, setHashField] = useState('');
    const queryClient = useQueryClient();
    const [rebuilding, setRebuilding] = useState(false);
    const [rebuildError, setRebuildError] = useState<string | null>(null);
    const loadMoreRef = useRef<HTMLDivElement>(null);
    const { viewStages, addStage, removeStage, updateStage } = useFilters();

    // Browse filter = applied stages MINUS our collapse stages, so collapsing a group
    // doesn't make it vanish from this panel (you can still toggle it back off). Grain-wrap
    // to the image grain (like every other panel) so on object datasets we dedupe images
    // only — object rows (and any hashes they carry) never leak into the groups or counts.
    const browseFilters: ComplexFilter = useMemo(() => withGrain({
        logic: 'AND',
        filters: viewStages
            .filter(s => !s.metadata?.collapse && !s.metadata?.dupGroupCollapse)
            .map(s => s.filter),
    }, 'image', null, hasObjects), [viewStages, hasObjects]);

    // Collapse all — one collapse_duplicates view stage (applies to every panel).
    const collapseIdx = viewStages.findIndex(s => s.metadata?.collapse);
    const collapseConfig: string | null = collapseIdx >= 0 ? viewStages[collapseIdx].metadata?.config : null;
    const setCollapseAll = (config: string | null) => {
        if (!config) { if (collapseIdx >= 0) removeStage(collapseIdx); return; }
        const label = config === '_all' ? 'all hashes' : config === '_any' ? 'any hash' : config;
        const stage: ViewStage = {
            type: 'filter',
            description: `Collapse duplicates (${label})`,
            filter: ({ field: '_collapse_duplicates', operator: 'collapse_duplicates', value: config } as unknown as ComplexFilter),
            metadata: { collapse: true, config },
        };
        if (collapseIdx >= 0) updateStage(collapseIdx, stage); else addStage(stage);
    };

    // Per-group collapse — each group is its own independent id-exclusion stage, so you
    // can collapse this group or that group and see exactly what's on.
    const isGroupCollapsed = (rep: number) =>
        viewStages.some(s => s.metadata?.dupGroupCollapse && s.metadata?.groupRep === rep);
    const toggleGroupCollapse = (sampleIds: number[]) => {
        const rep = Math.min(...sampleIds);
        const idx = viewStages.findIndex(s => s.metadata?.dupGroupCollapse && s.metadata?.groupRep === rep);
        if (idx >= 0) { removeStage(idx); return; }
        const nonRep = sampleIds.filter(id => id !== rep);
        if (nonRep.length === 0) return;
        addStage({
            type: 'filter',
            description: `Collapsed group (${nonRep.length} hidden)`,
            filter: { field: 'id', operator: 'not in', value: nonRep },
            metadata: { dupGroupCollapse: true, groupRep: rep },
        });
    };

    const { data: collapseHidden } = useQuery({
        queryKey: ['dup-collapse-count', datasetId, collapseConfig],
        queryFn: () => getDuplicateCount(datasetId, collapseConfig!),
        enabled: !!datasetId && !!collapseConfig,
        staleTime: 30_000,
        refetchOnWindowFocus: false,
    });

    const { data: stats, isLoading: statsLoading, isError: statsError } = useQuery({
        queryKey: ['hash-stats', datasetId, browseFilters],
        queryFn: () => getHashStats(datasetId, browseFilters),
        enabled: !!datasetId,
        staleTime: 120_000,
        refetchOnWindowFocus: false,
    });

    // Default to the best-covered hash field: the first one alphabetically could cover 2 of 24
    // samples and say "No duplicates found" while another field had groups.
    const fields = stats?.fields ? Object.keys(stats.fields) : [];
    const bestField = fields.length
        ? fields.reduce((a, b) => ((stats!.fields[b].samples_with_hashes ?? 0) > (stats!.fields[a].samples_with_hashes ?? 0) ? b : a))
        : '';
    const activeField = hashField || bestField;
    // Collapse configs: with >1 hash, offer any/all + each field; with 1, just that field.
    const collapseConfigs = fields.length > 1 ? ['_any', '_all', ...fields] : fields;
    const configLabel = (c: string) => c === '_any' ? 'Any hash' : c === '_all' ? 'All hashes' : c;

    const {
        data: dupData,
        isLoading: dupsLoading,
        isError: dupsError,
        fetchNextPage,
        hasNextPage,
        isFetchingNextPage,
    } = useInfiniteQuery({
        queryKey: ['duplicates', datasetId, activeField, browseFilters],
        queryFn: ({ pageParam = 0 }) => findDuplicates(datasetId, PAGE_SIZE, pageParam * PAGE_SIZE, activeField, browseFilters),
        initialPageParam: 0,
        getNextPageParam: (lastPage, allPages) => {
            const loaded = allPages.reduce((sum, p) => sum + p.groups.length, 0);
            if (loaded >= (lastPage.total_groups || 0)) return undefined;
            return allPages.length;
        },
        enabled: !!datasetId && activeField.length > 0 && !statsLoading,
        staleTime: 60_000,
        refetchOnWindowFocus: false,
    });

    const allGroups = useMemo(() => {
        if (!dupData?.pages) return [];
        return dupData.pages.flatMap(p => p.groups);
    }, [dupData]);

    const totalGroups = dupData?.pages?.[0]?.total_groups || 0;

    // Intersection observer for infinite scroll
    useEffect(() => {
        const el = loadMoreRef.current;
        if (!el) return;
        const observer = new IntersectionObserver(
            (entries) => { if (entries[0].isIntersecting && hasNextPage && !isFetchingNextPage) fetchNextPage(); },
            { threshold: 0.1 }
        );
        observer.observe(el);
        return () => observer.disconnect();
    }, [hasNextPage, isFetchingNextPage, fetchNextPage]);

    // Loading stats
    if (statsLoading) {
        return (
            <div className="h-full flex items-center justify-center text-zinc-500">
                <div className="text-center">
                    <div className="w-8 h-8 border-2 border-cyan-500 border-t-transparent rounded-full animate-spin mx-auto mb-3"></div>
                    <div className="text-sm">Loading hash data...</div>
                </div>
            </div>
        );
    }

    // A failed load must NOT masquerade as "no hashes" — that wrongly tells the operator the
    // dataset has none (they may recompute or ship believing there are no duplicates).
    if (statsError) {
        return (
            <div className="h-full flex items-center justify-center text-red-300 p-8 text-center text-sm">
                Failed to load hash data — retry.
            </div>
        );
    }

    // No hashes available
    if (!stats?.fields || Object.keys(stats.fields).length === 0) {
        return (
            <div className="h-full flex items-center justify-center text-zinc-500 p-8 text-center">
                <div>
                    <div className="text-sm mb-2">No image hashes found</div>
                    <div className="text-xs">Use the SDK to compute hashes:<br/>
                        <code className="text-cyan-400">dataset.compute_hashes()</code>
                    </div>
                </div>
            </div>
        );
    }

    const fieldStats = stats?.fields?.[activeField];
    const totalDupSamples = dupData?.pages?.[0]?.total_duplicate_samples ?? 0;

    return (
        <div className="h-full flex flex-col bg-zinc-900">
            {/* Header */}
            <div className="p-4 border-b border-zinc-800 space-y-3 shrink-0">
                <div className="flex items-center gap-4">
                    <h2 className="text-sm font-bold text-white">Duplicates</h2>

                    {fields.length > 0 && (
                        <select
                            value={activeField}
                            onChange={(e) => setHashField(e.target.value)}
                            className="bg-zinc-800 border border-zinc-700 text-xs text-white rounded px-2 py-1"
                        >
                            {fields.length > 1 && <option value="_all">All Fields</option>}
                            {fields.map(f => <option key={f} value={f}>{f}</option>)}
                        </select>
                    )}
                </div>

                {/* Collapse duplicates across the whole view (one representative per group) */}
                {fields.length > 0 && (
                    <div className="flex items-center gap-2 text-xs">
                        <span className="text-zinc-500">Collapse all:</span>
                        <select
                            value={collapseConfig ?? ''}
                            onChange={(e) => setCollapseAll(e.target.value || null)}
                            className="bg-zinc-800 border border-zinc-700 text-xs text-white rounded px-2 py-1"
                            title="Hide duplicates everywhere (grid, map, scatter); keeps one representative per hash group"
                        >
                            <option value="">Off</option>
                            {collapseConfigs.map(c => <option key={c} value={c}>{configLabel(c)}</option>)}
                        </select>
                        {collapseConfig && (
                            <span className="text-cyan-400">
                                {collapseHidden ? `${collapseHidden.hidden.toLocaleString()} hidden` : '…'}
                            </span>
                        )}
                        {collapseConfig && (
                            <button
                                data-testid="dup-collapse-rebuild"
                                disabled={rebuilding}
                                title="Rebuild this collapse set; background jobs maintain it as hashes change"
                                className="text-zinc-400 hover:text-white border border-zinc-700 rounded px-2 py-0.5 disabled:opacity-50"
                                onClick={async () => {
                                    setRebuilding(true);
                                    setRebuildError(null);
                                    try {
                                        await refreshDuplicates(datasetId, collapseConfig);
                                        await queryClient.invalidateQueries({ queryKey: ['dup-collapse-count', datasetId, collapseConfig] });
                                        notifySamplesMutated(datasetId);  // the collapsed grid/map/scatter re-read the set
                                    } catch (e) {
                                        setRebuildError(e instanceof Error ? e.message : 'Rebuild failed');
                                    } finally {
                                        setRebuilding(false);
                                    }
                                }}
                            >
                                {rebuilding ? 'Rebuilding…' : 'Rebuild'}
                            </button>
                        )}
                        {rebuildError && <span className="text-red-400">{rebuildError}</span>}
                    </div>
                )}

                {(totalGroups > 0 || fieldStats) && (
                    <div className="flex gap-4 text-xs text-zinc-400">
                        {fieldStats && <span>{fieldStats.samples_with_hashes.toLocaleString()} hashed</span>}
                        {!fieldStats && stats?.total_samples && <span>{stats.total_samples.toLocaleString()} total</span>}
                        <span className="text-yellow-400">{totalGroups.toLocaleString()} duplicate groups</span>
                        <span className="text-red-400">{totalDupSamples.toLocaleString()} duplicate samples</span>
                    </div>
                )}
            </div>

            {/* Groups */}
            <div className="flex-1 overflow-y-auto p-4 space-y-4">
                {dupsLoading && (
                    <div className="flex items-center justify-center py-8 text-zinc-500 text-sm">
                        Finding duplicates...
                    </div>
                )}

                {allGroups.map((group, gi) => (
                    <DuplicateGroup
                        key={gi}
                        datasetId={datasetId}
                        sampleIds={group.sample_ids}
                        size={group.size}
                        onSelection={onSelection}
                        onSampleClick={onSampleClick}
                        onToggleSelection={onToggleSelection}
                        selectedIds={selectedIds}
                        collapsed={group.sample_ids.length > 0 && isGroupCollapsed(Math.min(...group.sample_ids))}
                        onToggleCollapse={toggleGroupCollapse}
                    />
                ))}

                {dupsError && (
                    <div className="text-center text-red-300 text-sm py-8">
                        Failed to load duplicates — retry.
                    </div>
                )}
                {!dupsLoading && !dupsError && allGroups.length === 0 && (
                    <div className="text-center text-zinc-500 text-sm py-8">
                        No duplicates found
                    </div>
                )}

                {/* Load more trigger */}
                <div ref={loadMoreRef} className="h-10 flex items-center justify-center">
                    {isFetchingNextPage && (
                        <div className="text-zinc-500 text-xs">Loading more...</div>
                    )}
                </div>

                {allGroups.length > 0 && (
                    <div className="text-center text-xs text-zinc-600 pb-2">
                        {allGroups.length} / {totalGroups} groups
                    </div>
                )}
            </div>
        </div>
    );
}

function DuplicateGroup({ datasetId, sampleIds, size, onSelection, onSampleClick, onToggleSelection, selectedIds = [], collapsed = false, onToggleCollapse }: {
    datasetId: number;
    sampleIds: number[];
    size: number;
    onSelection: (ids: number[]) => void;
    onSampleClick: (sample: Sample) => void;
    onToggleSelection?: (id: number) => void;
    selectedIds?: number[];
    collapsed?: boolean;
    onToggleCollapse?: (sampleIds: number[]) => void;
}) {
    const selectedSet = new Set(selectedIds);
    const previewIds = sampleIds.slice(0, 6);

    const { data } = useQuery({
        queryKey: ['dup-group-samples', datasetId, previewIds.join(',')],
        queryFn: async () => {
            const res = await getSamples(datasetId, previewIds.length, 0, {
                logic: 'AND',
                filters: [{ field: 'id', operator: 'in', value: previewIds }],
            });
            return res.items;
        },
    });

    return (
        <div className="border border-zinc-800 rounded-lg p-3 hover:border-zinc-700 transition-colors">
            <div className="flex items-center justify-between mb-2">
                <span className="text-xs text-zinc-400">{size} images</span>
                <div className="flex items-center gap-3">
                    {onToggleCollapse && (
                        <button
                            onClick={() => onToggleCollapse(sampleIds)}
                            className={`text-xs ${collapsed ? 'text-cyan-400 font-medium' : 'text-zinc-400 hover:text-zinc-200'}`}
                            title={collapsed ? 'Showing only the representative — click to show all again' : 'Hide all but one representative of this group'}
                        >
                            {collapsed ? '✓ Collapsed' : 'Collapse'}
                        </button>
                    )}
                    <button
                        onClick={() => onSelection(sampleIds)}
                        className="text-xs text-cyan-400 hover:text-cyan-300"
                    >
                        Select all
                    </button>
                </div>
            </div>
            <div className="grid grid-cols-6 gap-1">
                {(data || []).map(sample => {
                    const isSelected = selectedSet.has(sample.id);
                    return (
                        <div
                            key={sample.id}
                            className={`aspect-square bg-zinc-800 rounded overflow-hidden cursor-pointer relative group ${
                                isSelected ? 'ring-2 ring-cyan-500' : 'hover:ring-1 hover:ring-cyan-500'
                            }`}
                            onClick={() => onSampleClick(sample)}
                        >
                            {/* eslint-disable-next-line @next/next/no-img-element -- authenticated media proxy, next/image not applicable */}
                            <img
                                src={getSampleMediaUrl(
                                    sample.id,
                                    sample.metadata?.thumbnail_path ? 'thumbnail_path' : 'filepath',
                                )}
                                alt=""
                                className="w-full h-full object-cover"
                                loading="lazy"
                            />
                            {onToggleSelection && (
                                <div
                                    className={`absolute top-1 right-1 w-5 h-5 rounded-full border-2 flex items-center justify-center transition-all z-10 ${
                                        isSelected
                                            ? 'bg-cyan-500 border-cyan-500 opacity-100'
                                            : 'bg-black/40 border-white/50 opacity-0 group-hover:opacity-100'
                                    }`}
                                    onClick={(e) => { e.stopPropagation(); onToggleSelection(sample.id); }}
                                >
                                    {isSelected && (
                                        <svg className="w-3 h-3 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" />
                                        </svg>
                                    )}
                                </div>
                            )}
                        </div>
                    );
                })}
                {size > 6 && (
                    <div className="aspect-square bg-zinc-800 rounded flex items-center justify-center text-zinc-500 text-xs">
                        +{size - 6}
                    </div>
                )}
            </div>
        </div>
    );
}
