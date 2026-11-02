import React, { useState, useEffect, useRef } from 'react';
import { View, ViewStage, ComplexFilter, getDatasetViews, createView, deleteView } from '../lib/api';
import { useUser } from '@/contexts/UserContext';
import { filterHasInSelection } from '@/contexts/FilterContext';
import { ShareViewModal } from './ShareViewModal';
import { Share1Icon, TrashIcon, EyeOpenIcon, LockClosedIcon, GlobeIcon, PersonIcon } from '@radix-ui/react-icons';

interface ViewManagerProps {
    datasetId: number;
    currentFilters: ComplexFilter;
    currentViewStages?: ViewStage[];
    onLoadView: (view: View) => void;
}

export default function ViewManager({ datasetId, currentFilters, currentViewStages, onLoadView }: ViewManagerProps) {
    const { user } = useUser();
    const [views, setViews] = useState<View[]>([]);
    const [loadError, setLoadError] = useState<string | null>(null);
    const [newViewName, setNewViewName] = useState('');
    const [isOpen, setIsOpen] = useState(false);
    const [loading, setLoading] = useState(false);
    const [sharingView, setSharingView] = useState<View | null>(null);
    const [filterType, setFilterType] = useState<'all' | 'mine' | 'shared' | 'public'>('all');

    // Ref for detecting outside clicks
    const containerRef = useRef<HTMLDivElement>(null);

    useEffect(() => {
        if (isOpen) {
            loadViews();

            // Close on outside interaction
            const handleOutsideClick = (event: MouseEvent) => {
                // If sharing modal is open, interactions there attempting to "close" based on clicks shouldn't close the parent view manager
                if (sharingView) return;

                if (containerRef.current && !containerRef.current.contains(event.target as Node)) {
                    setIsOpen(false);
                }
            };

            // Only attach listener if we aren't currently sharing a view (which opens a stacked modal)
            if (!sharingView) {
                document.addEventListener('mousedown', handleOutsideClick);
            }

            return () => {
                document.removeEventListener('mousedown', handleOutsideClick);
            };
        }
        // eslint-disable-next-line react-hooks/exhaustive-deps -- load views when the panel opens / dataset changes; loadViews is recreated every render and adding it would refetch on every render
    }, [isOpen, datasetId, sharingView]);

    async function loadViews() {
        try {
            const data = await getDatasetViews(datasetId);
            setViews(data);
            setLoadError(null);
        } catch (e) {
            // Don't let a failed load look like "No views found" — the user may think they were lost.
            console.error(e);
            setLoadError((e as Error).message || 'Failed to load views');
        }
    }

    // A temporary selection (a Select All over 50,000 samples, or a version comparison's samples) is a
    // server token that expires after 2 hours: a view saved with one reopened as 0 samples. It is never
    // saved — the view is saved with its filters, which is what a Select All already shows.
    const stages = currentViewStages ?? [];
    const temporaryStages = stages.filter(s => filterHasInSelection(s.filter));
    const savedStages = stages.filter(s => !filterHasInSelection(s.filter));
    const canSave = temporaryStages.length === 0 || savedStages.length > 0;

    async function handleSaveView() {
        if (!newViewName || !user || !canSave) return;
        setLoading(true);
        try {
            await createView(datasetId, {
                name: newViewName,
                content: {
                    // The compiled filters are the AND of the stages (FilterContext.globalFilters);
                    // rebuilt from the stages kept when a temporary selection is left out.
                    filters: temporaryStages.length ? { logic: 'AND', filters: savedStages.map(s => s.filter) } : currentFilters,
                    viewStages: savedStages,
                }
            });
            setNewViewName('');
            await loadViews();
        } catch {
            alert('Failed to save view');
        } finally {
            setLoading(false);
        }
    }

    async function handleDeleteView(id: number) {
        if (!confirm('Are you sure?')) return;
        try {
            await deleteView(id);
            await loadViews();
        } catch {
            alert('Failed to delete view');
        }
    }

    const filteredViews = views.filter(v => {
        if (filterType === 'all') return true;
        if (filterType === 'mine') return v.user_id === user?.id;
        if (filterType === 'shared') return v.user_id !== user?.id && !v.is_public;
        if (filterType === 'public') return v.is_public;
        return true;
    });

    return (
        <div className="relative" ref={containerRef}>
            <button
                onClick={() => setIsOpen(!isOpen)}
                className={`px-3 py-1 border rounded text-sm transition-colors flex items-center gap-2 ${isOpen
                        ? 'bg-zinc-700 text-white border-zinc-600'
                        : 'bg-zinc-800 text-zinc-300 border-zinc-700 hover:bg-zinc-700 hover:text-white'
                    }`}
            >
                <EyeOpenIcon />
                {isOpen ? 'Close Views' : 'Saved Views'}
            </button>

            {isOpen && (
                <div className="absolute right-0 mt-2 w-[420px] bg-zinc-900 shadow-2xl rounded-lg p-4 z-50 border border-zinc-800 animate-in fade-in zoom-in-95 duration-100 flex flex-col gap-4">
                    <div className="flex items-center justify-between">
                        <h3 className="font-bold text-white text-base">Views</h3>
                        <select
                            value={filterType}
                            onChange={(e) => setFilterType(e.target.value as 'all' | 'mine' | 'shared' | 'public')}
                            className="bg-zinc-800 text-zinc-300 text-xs border border-zinc-700 rounded px-2 py-1 focus:outline-none focus:border-cyan-500"
                        >
                            <option value="all">All Views</option>
                            <option value="mine">My Views</option>
                            <option value="shared">Shared</option>
                            <option value="public">Public</option>
                        </select>
                    </div>

                    {/* Save New View - Improved Layout */}
                    <div className="p-3 bg-zinc-800/50 rounded border border-zinc-800">
                        <h4 className="text-xs font-semibold uppercase text-zinc-500 mb-2">Save Current View</h4>
                        <div className="flex gap-2">
                            <input
                                type="text"
                                value={newViewName}
                                onChange={e => setNewViewName(e.target.value)}
                                placeholder="Enter view name..."
                                className="flex-1 min-w-0 p-2 text-sm bg-zinc-900 border border-zinc-700 rounded text-white focus:outline-none focus:border-cyan-500 placeholder:text-zinc-600"
                                onKeyDown={(e) => e.key === 'Enter' && handleSaveView()}
                            />
                            <button
                                onClick={handleSaveView}
                                disabled={loading || !newViewName || !canSave}
                                className="px-4 py-2 bg-cyan-600 text-white rounded text-sm font-medium disabled:opacity-50 hover:bg-cyan-500 transition-colors whitespace-nowrap"
                            >
                                {loading ? 'Saving...' : temporaryStages.length ? 'Save filters only' : 'Save View'}
                            </button>
                        </div>
                        {temporaryStages.length > 0 && (
                            <p data-testid="view-temporary-selection-note" className="mt-2 text-xs text-amber-400/90">
                                {temporaryStages.map(s => `“${s.description}”`).join(', ')}{' '}is a temporary selection: selections over
                                50,000 samples are kept for 2 hours only, so a saved view can&apos;t include it.{' '}
                                {savedStages.length > 0
                                    ? 'Save the filters instead — a Select All is exactly what they show.'
                                    : 'Add the filters that describe these samples and save those.'}{' '}
                                To keep an exact set, tag it and filter by the tag.
                            </p>
                        )}
                    </div>

                    {/* List Views */}
                    <div className="max-h-[400px] overflow-y-auto space-y-1 pr-1 custom-scrollbar">
                        {loadError ? (
                            <div className="text-sm text-red-300 text-center py-8 bg-zinc-900/50 rounded border border-red-800/50 border-dashed">
                                Failed to load views — reopen the panel to retry.
                            </div>
                        ) : filteredViews.length === 0 && (
                            <div className="text-sm text-zinc-500 text-center py-8 bg-zinc-900/50 rounded border border-zinc-800/50 border-dashed">
                                No views found.
                            </div>
                        )}
                        {filteredViews.map(view => {
                            const isMine = view.user_id === user?.id;
                            const isPublic = view.is_public;
                            // A view is explicitly "Shared" if it is not public AND has been shared with specific users (if mine) OR it's not mine (shared with me)
                            // Note: shared_with_user_ids might be null/empty
                            const isSharedExplicitly = !isPublic && ((view.shared_with_user_ids?.length || 0) > 0 || (!isMine));
                            const isPrivate = isMine && !isPublic && !isSharedExplicitly;

                            return (
                                <div key={view.id} className="flex items-center justify-between p-2.5 hover:bg-zinc-800 rounded-md group transition-all border border-transparent hover:border-zinc-700/50">
                                    <button
                                        onClick={() => onLoadView(view)}
                                        className="text-left flex-1 min-w-0 mr-3"
                                    >
                                        <div className="flex items-center gap-2 mb-1">
                                            <span className="font-medium text-sm text-zinc-200 truncate">{view.name}</span>
                                        </div>

                                        <div className="flex items-center gap-2">
                                            {/* Badges */}
                                            {isPublic && (
                                                <span className="inline-flex items-center gap-1 px-1.5 py-0.5 bg-green-500/10 text-green-400 text-[10px] rounded border border-green-500/20 uppercase font-medium tracking-wide">
                                                    <GlobeIcon className="w-3 h-3" /> Public
                                                </span>
                                            )}
                                            {isSharedExplicitly && (
                                                <span className="inline-flex items-center gap-1 px-1.5 py-0.5 bg-blue-500/10 text-blue-400 text-[10px] rounded border border-blue-500/20 uppercase font-medium tracking-wide">
                                                    <PersonIcon className="w-3 h-3" /> Shared
                                                </span>
                                            )}
                                            {isPrivate && (
                                                <span className="inline-flex items-center gap-1 px-1.5 py-0.5 bg-zinc-500/10 text-zinc-400 text-[10px] rounded border border-zinc-500/20 uppercase font-medium tracking-wide">
                                                    <LockClosedIcon className="w-3 h-3" /> Private
                                                </span>
                                            )}

                                            {!isMine && (
                                                <span className="text-[10px] text-zinc-600 truncate ml-auto">
                                                    by {view.user_id}
                                                </span>
                                            )}
                                        </div>
                                    </button>

                                    <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                                        {isMine && user?.can_share_views !== false && (
                                            <button
                                                onClick={(e) => {
                                                    e.stopPropagation();
                                                    setSharingView(view);
                                                }}
                                                className="p-1.5 text-zinc-400 hover:text-cyan-400 hover:bg-cyan-400/10 rounded transition-colors"
                                                title="Share View"
                                            >
                                                <Share1Icon className="w-3.5 h-3.5" />
                                            </button>
                                        )}
                                        {(isMine || user?.role === 'admin') && (
                                            <button
                                                onClick={(e) => {
                                                    e.stopPropagation();
                                                    handleDeleteView(view.id);
                                                }}
                                                className="p-1.5 text-zinc-400 hover:text-red-400 hover:bg-red-400/10 rounded transition-colors"
                                                title="Delete View"
                                            >
                                                <TrashIcon className="w-3.5 h-3.5" />
                                            </button>
                                        )}
                                    </div>
                                </div>
                            );
                        })}
                    </div>
                </div>
            )}

            {user?.can_share_views !== false && (
                <ShareViewModal
                    view={sharingView}
                    datasetId={datasetId}
                    onClose={() => {
                        setSharingView(null);
                        loadViews();
                    }}
                />
            )}
        </div>
    );
}
