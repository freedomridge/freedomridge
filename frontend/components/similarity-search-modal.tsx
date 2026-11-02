'use client';

import * as Dialog from '@radix-ui/react-dialog';
import { useState } from 'react';
import { type Sample, type ComplexFilter } from '@/lib/api';

interface Props {
    sample: Sample;
    // > 1 when several selected samples seed the search: their embeddings are AVERAGED.
    seedCount?: number;
    embeddingFields: string[];
    currentFilters?: ComplexFilter;
    /** Samples in the current view (applied stages) — what a "Current View" search can draw from. */
    currentViewCount?: number;
    /** Unapplied draft filters / selections exist; showing results replaces them. */
    hasDraftFilters?: boolean;
    onSearch: (field: string, limit: number, useCurrentView: boolean) => void;
    onClose: () => void;
}

export function SimilaritySearchModal({ sample, seedCount = 1, embeddingFields, currentFilters, currentViewCount, hasDraftFilters, onSearch, onClose }: Props) {
    const [limit, setLimit] = useState(25);
    // An object seed only has the clip_obj embedding, so the search is locked to it (page.tsx
    // forces clip_obj for object seeds); lock the field here too rather than show an editable
    // dropdown whose choice — and even its default clip_img — would be silently ignored.
    const isObjectSeed = sample.media_type === 'object';
    const [embeddingField, setEmbeddingField] = useState(isObjectSeed ? 'clip_obj' : (embeddingFields[0] || ''));
    const [scope, setScope] = useState<'entire' | 'current'>('entire');

    const handleSubmit = () => {
        // A cleared/junk input makes `limit` NaN or 0; clamp to a sane range so we never send
        // NaN/0/oversized to the backend (worst case a 50k-row exact scan or an opaque 422).
        const safeLimit = Number.isFinite(limit) && limit > 0 ? Math.min(Math.round(limit), 50000) : 25;
        onSearch(embeddingField, safeLimit, scope === 'current');
        onClose();
    };

    return (
        <Dialog.Root open={true} onOpenChange={onClose}>
            <Dialog.Portal>
                <Dialog.Overlay className="fixed inset-0 bg-black/50 backdrop-blur-sm z-50" />
                <Dialog.Content className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 w-full max-w-md bg-zinc-900 border border-zinc-800 rounded-xl shadow-2xl z-50 p-6">
                    <Dialog.Title className="text-xl font-bold text-white mb-4">
                        Find Similar Samples
                    </Dialog.Title>

                    <div className="space-y-4">
                        {/* Reference sample */}
                        <div>
                            {/* With several samples selected the search averages ALL of them; the header
                                named only the first as "Reference Sample". */}
                            <label className="block text-sm font-medium text-zinc-400 mb-1">
                                {seedCount > 1 ? 'Reference Samples' : 'Reference Sample'}
                            </label>
                            <div className="text-sm text-zinc-300 truncate" data-testid="similarity-seed">
                                {seedCount > 1
                                    ? `Averaging ${seedCount.toLocaleString()} selected samples`
                                    : sample.metadata?.source_id || sample.metadata?.filename || `Sample ${sample.id}`}
                            </div>
                        </div>

                        {/* Embedding field selector (locked for object seeds — only clip_obj applies) */}
                        <div>
                            <label className="block text-sm font-medium text-zinc-400 mb-1">Embedding Field</label>
                            {isObjectSeed ? (
                                <div className="w-full bg-zinc-800/60 border border-zinc-700 rounded px-3 py-2 text-zinc-300 text-sm">
                                    clip_obj <span className="text-zinc-500">(object embedding)</span>
                                </div>
                            ) : (
                                <select
                                    value={embeddingField}
                                    onChange={(e) => setEmbeddingField(e.target.value)}
                                    className="w-full bg-zinc-800 border border-zinc-700 rounded px-3 py-2 text-white text-sm focus:outline-none focus:border-zinc-500"
                                >
                                    {embeddingFields.map(f => (
                                        <option key={f} value={f}>{f}</option>
                                    ))}
                                </select>
                            )}
                            <p className="text-xs text-zinc-500 mt-1">
                                {isObjectSeed
                                    ? 'Finds visually similar objects (the object crop embedding).'
                                    : 'Finds images with similar content (subject, scene, etc.)'}
                            </p>
                        </div>

                        {/* Result count */}
                        <div>
                            <label className="block text-sm font-medium text-zinc-400 mb-1">Number of Results</label>
                            <input
                                data-testid="similarity-limit"
                                type="number"
                                min="1"
                                max="50000"
                                value={limit}
                                onChange={(e) => setLimit(Number(e.target.value))}
                                className="w-full bg-zinc-800 border border-zinc-700 rounded px-3 py-2 text-white text-sm focus:outline-none focus:border-zinc-500"
                            />
                            <p className="text-xs text-zinc-500 mt-1">
                                Up to 1,000 is fast (indexed); larger runs an exact scan (slower, a few seconds).
                            </p>
                        </div>

                        {/* Scope */}
                        <div>
                            <label className="block text-sm font-medium text-zinc-400 mb-2">Search Scope</label>
                            <div className="space-y-2">
                                <label className="flex items-center gap-2 cursor-pointer">
                                    <input type="radio" name="scope" value="entire" checked={scope === 'entire'}
                                        onChange={() => setScope('entire')} className="text-cyan-500" />
                                    <span className="text-sm text-zinc-300">Entire Dataset</span>
                                </label>
                                <label className="flex items-center gap-2 cursor-pointer">
                                    <input type="radio" name="scope" value="current" checked={scope === 'current'}
                                        onChange={() => setScope('current')} className="text-cyan-500" />
                                    <span className="text-sm text-zinc-300">
                                        Current View
                                        {currentViewCount != null
                                            ? ` (${currentViewCount.toLocaleString()} samples${currentFilters?.filters?.length ? ', with filters' : ''})`
                                            : (currentFilters?.filters?.length ? ' (with filters)' : '')}
                                    </span>
                                </label>
                            </div>
                            {scope === 'current' && currentViewCount != null && limit > currentViewCount && (
                                <p data-testid="similarity-scope-warning" className="text-xs text-amber-500/90 mt-2">
                                    Only {currentViewCount.toLocaleString()} samples are in the current view, so at most {currentViewCount.toLocaleString()} results can come back.
                                </p>
                            )}
                            {hasDraftFilters && (
                                <p data-testid="similarity-draft-note" className="text-xs text-zinc-500 mt-2">
                                    Showing the results replaces the current filters, including unapplied drafts and selections.
                                </p>
                            )}
                        </div>
                    </div>

                    <div className="mt-6 flex items-center justify-end gap-3">
                        <button
                            onClick={onClose}
                            className="px-4 py-2 text-zinc-400 hover:text-white transition-colors"
                        >
                            Cancel
                        </button>
                        <button
                            onClick={handleSubmit}
                            className="px-4 py-2 bg-cyan-500 text-white rounded hover:bg-cyan-600 transition-colors"
                        >
                            Search
                        </button>
                    </div>
                </Dialog.Content>
            </Dialog.Portal>
        </Dialog.Root>
    );
}
