'use client';

import * as Dialog from '@radix-ui/react-dialog';
import { useState } from 'react';

interface Props {
    availablePatchScales: string[];  // e.g. ["4x4", "8x8"]
    onSearch: (patchScale: string | undefined, limit: number) => void;
    onClose: () => void;
}

// Dialog for "Find Unlabeled": from a selected object, surface similar UNlabeled patch
// regions across the dataset. Lets the user pick the patch size (one scale, or all) and how
// many look-alikes to surface, then runs the object->patch cross-search.
export function FindUnlabeledModal({ availablePatchScales, onSearch, onClose }: Props) {
    // '' = all scales (recall across sizes); a specific scale narrows to that tile size.
    const [patchScale, setPatchScale] = useState<string>('');
    const [limit, setLimit] = useState(60);

    const handleSubmit = () => {
        // A cleared/junk input makes `limit` NaN or 0; clamp so we never send NaN/0/oversized.
        const safeLimit = Number.isFinite(limit) && limit > 0 ? Math.min(Math.round(limit), 50000) : 60;
        onSearch(patchScale || undefined, safeLimit);
        onClose();
    };

    return (
        <Dialog.Root open={true} onOpenChange={onClose}>
            <Dialog.Portal>
                <Dialog.Overlay className="fixed inset-0 bg-black/50 backdrop-blur-sm z-50" />
                <Dialog.Content className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 w-full max-w-md bg-zinc-900 border border-zinc-800 rounded-xl shadow-2xl z-50 p-6">
                    <Dialog.Title className="text-xl font-bold text-white mb-1">
                        Find Unlabeled Look-alikes
                    </Dialog.Title>
                    <p className="text-xs text-zinc-500 mb-4">
                        Surfaces patch regions that look like the selected object but aren&apos;t
                        covered by an existing labeled box.
                    </p>

                    <div className="space-y-4">
                        {/* Patch size */}
                        <div>
                            <label className="block text-sm font-medium text-zinc-400 mb-1">Patch Size</label>
                            <select
                                data-testid="unlabeled-scale-select"
                                value={patchScale}
                                onChange={(e) => setPatchScale(e.target.value)}
                                className="w-full bg-zinc-800 border border-zinc-700 rounded px-3 py-2 text-white text-sm focus:outline-none focus:border-zinc-500"
                            >
                                <option value="">All sizes (best recall)</option>
                                {availablePatchScales.map(s => (
                                    <option key={s} value={s}>{s}</option>
                                ))}
                            </select>
                            <p className="text-xs text-zinc-500 mt-1">
                                Smaller tiles (e.g. 8x8) catch small objects; larger tiles catch big ones.
                            </p>
                        </div>

                        {/* Result count */}
                        <div>
                            <label className="block text-sm font-medium text-zinc-400 mb-1">Number of Look-alikes</label>
                            <input
                                data-testid="unlabeled-limit-input"
                                type="number"
                                min="1"
                                max="1000"
                                value={limit}
                                onChange={(e) => setLimit(Number(e.target.value))}
                                className="w-full bg-zinc-800 border border-zinc-700 rounded px-3 py-2 text-white text-sm focus:outline-none focus:border-zinc-500"
                            />
                            <p className="text-xs text-zinc-500 mt-1">
                                The top-ranked uncovered regions, one per parent image.
                            </p>
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
                            data-testid="unlabeled-search-btn"
                            onClick={handleSubmit}
                            className="px-4 py-2 bg-amber-500 text-white rounded hover:bg-amber-600 transition-colors"
                        >
                            Find
                        </button>
                    </div>
                </Dialog.Content>
            </Dialog.Portal>
        </Dialog.Root>
    );
}
