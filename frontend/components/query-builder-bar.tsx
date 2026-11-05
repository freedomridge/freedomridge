import React, { useRef, useState } from 'react';
import { ViewStage, SchemaField } from '@/lib/api';
import { Cross2Icon, PlusIcon, MagnifyingGlassIcon, ImageIcon } from '@radix-ui/react-icons';
import { Rocket } from 'lucide-react';
import { FilterStageEditor } from './filter-stage-editor';
import { describeFilter, isAutoFilterLabel } from '@/lib/filter-utils';
import { type DefaultView, defaultViewStages, isDefaultViewStage } from '@/lib/default-view';
// import * as Tooltip from '@radix-ui/react-tooltip';
import * as Popover from '@radix-ui/react-popover';

interface QueryBuilderBarProps {
    stages: ViewStage[];
    onRemoveStage: (index: number) => void;
    onUpdateStage: (index: number, stage: ViewStage) => void;
    onAddStage: (stage: ViewStage) => void;
    // Back to the dataset's default view (no filters when it has none).
    onClearAll: () => void;
    // The dataset's default view: its filters are labelled, and Show all images removes them too.
    defaultView?: DefaultView | null;
    onShowAll?: () => void;
    datasetId: number;
    schema: SchemaField[];
    lightningMode: boolean;
    onToggleLightningMode: () => void;
    // Natural-language (CLIP) search over the dataset's embeddings; absent when the
    // dataset has no image-grain embedding field to search.
    onTextSearch?: (prompt: string, limit: number) => void;
    // Reverse image search: upload an image or paste an image URL, ranked by CLIP
    // similarity. Shares the text-search gate (present only for CLIP-family fields).
    onImageSearch?: (input: { imageBase64?: string; imageUrl?: string; label: string }, limit: number) => void;
    // A text / image / similarity search is in flight: show it, and hold the search inputs.
    // Nothing used to render this, so a reverse image search gave no sign it was working.
    isSearching?: boolean;
    allowImageUrl?: boolean;
}

const DEFAULT_SEARCH_LIMIT = 100;
const MAX_SEARCH_LIMIT = 50000;

export function QueryBuilderBar({
    stages,
    onRemoveStage,
    onUpdateStage,
    onAddStage,
    onClearAll,
    defaultView,
    onShowAll,
    datasetId,
    schema,
    lightningMode,
    onToggleLightningMode,
    onTextSearch,
    onImageSearch,
    isSearching = false,
    allowImageUrl = true,
}: QueryBuilderBarProps) {
    const [textQuery, setTextQuery] = useState('');
    // How many results a text / image search asks for (was a hidden, fixed 100).
    const [resultLimit, setResultLimit] = useState(DEFAULT_SEARCH_LIMIT);
    const safeLimit = Number.isFinite(resultLimit) && resultLimit > 0 ? Math.min(Math.round(resultLimit), MAX_SEARCH_LIMIT) : DEFAULT_SEARCH_LIMIT;
    const [editingIndex, setEditingIndex] = useState<number | null>(null);
    const [isAdding, setIsAdding] = useState(false);
    const [searchTerm, setSearchTerm] = useState('');
    const [selectedFieldForAdd, setSelectedFieldForAdd] = useState<string | null>(null);
    const [imageSearchOpen, setImageSearchOpen] = useState(false);
    const [imageUrl, setImageUrl] = useState('');
    const fileInputRef = useRef<HTMLInputElement | null>(null);

    const handleImageFile = (file: File) => {
        const reader = new FileReader();
        reader.onload = () => {
            // readAsDataURL gives a `data:image/...;base64,...` string the server accepts directly.
            onImageSearch?.({ imageBase64: String(reader.result), label: file.name }, safeLimit);
            setImageSearchOpen(false);
        };
        reader.readAsDataURL(file);
    };

    // Filter fields for the add menu
    const filteredFields = schema.filter(f =>
        f.name.toLowerCase().includes(searchTerm.toLowerCase())
    );

    const defaultCount = stages.filter(s => isDefaultViewStage(s, defaultView)).length;
    const hasDefaultFilters = defaultCount > 0;
    const hasOwnFilters = stages.some(s => !isDefaultViewStage(s, defaultView));
    // Some of the default view's filters were removed and none of the viewer's own added: one click back.
    const offersDefaultView = !!defaultView && !hasOwnFilters && defaultCount < defaultViewStages(defaultView).length;

    const handleAddSave = (stage: ViewStage) => {
        onAddStage({ ...stage, description: describeFilter(stage.filter) });
        setIsAdding(false);
        setSelectedFieldForAdd(null);
        setSearchTerm('');
    };

    return (
        <div className="flex items-center w-full bg-zinc-950 border border-zinc-800 rounded-lg p-1 shadow-sm">

            {/* --- Existing Filters (Pipeline) --- */}
            <div className="flex items-center flex-1 overflow-x-auto no-scrollbar gap-1">
                {hasDefaultFilters && defaultView && (
                    <span
                        data-testid="default-view-label"
                        className="px-2 text-xs text-amber-300/90 whitespace-nowrap"
                        title="The dataset's default view: it opens on these filters (★), and Clear All keeps them"
                    >
                        Default view: {defaultView.name}
                    </span>
                )}
                {stages.map((stage, index) => {
                    const isReadOnly = stage.type === 'map_region' || stage.type === 'similarity' || stage.type === 'selection' || stage.metadata?.collapse || stage.metadata?.dupGroupCollapse || stage.metadata?.concept;

                    const ChipContent = (
                        <div
                            data-testid="query-stage-chip"
                            className={`
                                flex items-center gap-2 px-3 py-2 text-xs font-medium transition-all border-r border-zinc-900 min-w-fit group
                                ${index === 0 ? 'rounded-l-md' : ''}
                                ${index === stages.length - 1 ? 'rounded-r-md' : ''}
                                ${editingIndex === index
                                    ? 'bg-zinc-800 text-white shadow-inner ring-1 ring-inset ring-zinc-700'
                                    : 'bg-zinc-900 text-zinc-400 hover:bg-zinc-800 hover:text-zinc-200'
                                }
                                ${!isReadOnly ? 'cursor-pointer' : 'cursor-default opacity-90'}
                            `}
                        >
                            <span className="whitespace-nowrap max-w-[200px] truncate" title={stage.description}>
                                {stage.type === 'map_region' && <span className="mr-1">🌍</span>}
                                {stage.type === 'similarity' && <span className="mr-1">🔍</span>}
                                {(stage.metadata?.collapse || stage.metadata?.dupGroupCollapse) && <span className="mr-1">⧉</span>}
                                {stage.metadata?.concept && <span className="mr-1">⊘</span>}
                                {isDefaultViewStage(stage, defaultView) && <span data-testid="default-view-star" className="mr-1 text-amber-300">★</span>}
                                {stage.description}
                            </span>
                            <button
                                onClick={(e) => {
                                    e.stopPropagation();
                                    onRemoveStage(index);
                                }}
                                className="opacity-0 group-hover:opacity-100 text-zinc-500 hover:text-red-400 p-0.5 rounded-full hover:bg-zinc-950 transition-all"
                            >
                                <Cross2Icon className="w-3 h-3" />
                            </button>
                        </div>
                    );

                    if (stage.type === 'map_region') {
                        // Toggle between geo_polygon (include) and geo_polygon_exclude
                        const isExclude = stage.filter?.operator === 'geo_polygon_exclude';
                        return (
                            <div key={index} className="cursor-pointer" onClick={() => {
                                const newOp = isExclude ? 'geo_polygon' : 'geo_polygon_exclude';
                                onUpdateStage(index, {
                                    ...stage,
                                    description: isExclude ? 'Map Region' : 'Exclude Map Region',
                                    filter: { ...stage.filter, operator: newOp },
                                });
                            }} title="Click to toggle include/exclude">
                                {ChipContent}
                            </div>
                        );
                    }

                    if (stage.type === 'selection' || stage.type === 'similarity') {
                        // Toggle between 'in' (include) and 'not in' (exclude)
                        const isExclude = stage.filter?.operator === 'not in';
                        return (
                            <div key={index} className="cursor-pointer" onClick={() => {
                                const newOp = isExclude ? 'in' : 'not in';
                                // Use metadata flag to preserve original description cleanly
                                const origDesc = stage.metadata?.originalDescription ?? stage.description;
                                onUpdateStage(index, {
                                    ...stage,
                                    description: isExclude ? origDesc : `Exclude: ${origDesc}`,
                                    filter: { ...stage.filter, operator: newOp },
                                    metadata: { ...stage.metadata, originalDescription: origDesc },
                                });
                            }} title="Click to toggle include/exclude">
                                {ChipContent}
                            </div>
                        );
                    }

                    if (isReadOnly) {
                        return <div key={index}>{ChipContent}</div>;
                    }

                    return (
                        <Popover.Root key={index} open={editingIndex === index} onOpenChange={(open) => setEditingIndex(open ? index : null)}>
                            <Popover.Trigger asChild>
                                {ChipContent}
                            </Popover.Trigger>
                            <Popover.Portal>
                                <Popover.Content
                                    className="z-[100] bg-zinc-950 border border-zinc-800 rounded-xl shadow-2xl p-0 w-[800px] animate-in fade-in zoom-in-95 data-[side=bottom]:slide-in-from-top-2"
                                    sideOffset={5}
                                    align="start"
                                    avoidCollisions={true}
                                    onInteractOutside={(e) => {
                                        // The value picker's dropdown is portaled to <body>; don't
                                        // treat clicking it as an outside click that closes the editor.
                                        if ((e.target as HTMLElement | null)?.closest('[data-searchable-dropdown]')) e.preventDefault();
                                    }}
                                >
                                    <FilterStageEditor
                                        stage={stage}
                                        onSave={(newStage) => {
                                            // An edited value must show in the chip; a named stage keeps its name.
                                            onUpdateStage(index, newStage.type === 'filter' && isAutoFilterLabel(stage.description, stage.filter)
                                                ? { ...newStage, description: describeFilter(newStage.filter) }
                                                : newStage);
                                            setEditingIndex(null);
                                        }}
                                        onCancel={() => setEditingIndex(null)}
                                        onDelete={() => {
                                            onRemoveStage(index);
                                            setEditingIndex(null);
                                        }}
                                        datasetId={datasetId}
                                        schema={schema}
                                    />
                                </Popover.Content>
                            </Popover.Portal>
                        </Popover.Root>
                    );
                })}

                {/* End of stages map */}
            </div>

            {/* --- Right Actions (Add, Clear, Lightning) --- */}
            <div className="flex items-center gap-2 border-l border-zinc-800 pl-3">

                {
                    hasOwnFilters && (
                        <button
                            onClick={onClearAll}
                            title={defaultView ? `Remove your filters; the default view “${defaultView.name}” stays` : undefined}
                            className="text-xs font-medium text-zinc-500 hover:text-red-400 transition-colors whitespace-nowrap px-2"
                        >
                            Clear All
                        </button>
                    )
                }
                {hasDefaultFilters && onShowAll && (
                    <button
                        onClick={onShowAll}
                        title="Remove every filter, the default view's too. Clear All brings the default view back."
                        className="text-xs font-medium text-zinc-500 hover:text-amber-300 transition-colors whitespace-nowrap px-2"
                    >
                        Show all images
                    </button>
                )}
                {defaultView && offersDefaultView && (
                    <button
                        onClick={onClearAll}
                        title={`Back to the dataset's default view “${defaultView.name}”`}
                        className="text-xs font-medium text-zinc-500 hover:text-amber-300 transition-colors whitespace-nowrap px-2"
                    >
                        Default view
                    </button>
                )}

                {/* --- Semantic (text -> embedding) search --- */}
                {onTextSearch && (
                    <div className="relative">
                        <MagnifyingGlassIcon className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-zinc-500 pointer-events-none" />
                        <input
                            data-testid="semantic-search-input"
                            className="w-64 bg-zinc-900 border border-zinc-800 rounded-lg pl-9 pr-3 py-2 text-sm text-zinc-200 focus:outline-none focus:border-cyan-500 placeholder-zinc-600 transition-colors"
                            placeholder='Describe it… e.g. "a river"'
                            title="Semantic search: ranks the grid by similarity to your description (CLIP)"
                            value={textQuery}
                            disabled={isSearching}
                            onChange={e => setTextQuery(e.target.value)}
                            onKeyDown={e => {
                                if (e.key === 'Enter' && textQuery.trim() && !isSearching) {
                                    onTextSearch(textQuery.trim(), safeLimit);
                                    setTextQuery('');
                                }
                            }}
                        />
                    </div>
                )}
                {(onTextSearch || onImageSearch) && (
                    <label className="flex items-center gap-1 text-xs text-zinc-500" title="How many results a text or image search returns. Up to 1,000 is fast (indexed); larger runs an exact scan.">
                        <input
                            data-testid="search-result-limit"
                            type="number"
                            min={1}
                            max={MAX_SEARCH_LIMIT}
                            value={resultLimit}
                            disabled={isSearching}
                            onChange={e => setResultLimit(Number(e.target.value))}
                            className="w-20 bg-zinc-900 border border-zinc-800 rounded-lg px-2 py-2 text-sm text-zinc-200 focus:outline-none focus:ring-2 focus:ring-cyan-500 disabled:opacity-50"
                        />
                        results
                    </label>
                )}
                {isSearching && (
                    <span data-testid="search-in-progress" className="flex items-center gap-2 px-3 py-2 text-xs font-medium text-amber-300 bg-amber-500/10 border border-amber-500/30 rounded-lg">
                        <span className="w-3 h-3 rounded-full border-2 border-amber-300 border-t-transparent animate-spin" />
                        Searching…
                    </span>
                )}

                {/* --- Reverse image search (upload / URL -> similar) --- */}
                {onImageSearch && (
                    <Popover.Root open={imageSearchOpen} onOpenChange={setImageSearchOpen}>
                        <Popover.Trigger asChild>
                            <button
                                data-testid="image-search-trigger"
                                disabled={isSearching}
                                title={allowImageUrl
                                    ? 'Reverse image search: upload an image or paste an image URL to rank the grid by CLIP similarity'
                                    : 'Reverse image search: upload an image to rank the grid by CLIP similarity'}
                                className="flex items-center justify-center w-9 h-9 bg-zinc-900 border border-zinc-800 rounded-lg text-zinc-400 hover:text-cyan-400 hover:border-cyan-600 transition-colors"
                            >
                                <ImageIcon className="w-4 h-4" />
                            </button>
                        </Popover.Trigger>
                        <Popover.Portal>
                            <Popover.Content
                                sideOffset={6}
                                align="end"
                                className="z-50 w-72 bg-zinc-950 border border-zinc-800 rounded-lg shadow-xl p-3 space-y-3"
                            >
                                <div className="text-xs font-bold uppercase tracking-wider text-zinc-400">Search by image</div>
                                <div className="text-[11px] text-zinc-500">Returns up to {safeLimit.toLocaleString()} results (the number next to the search box). The grid shows a “Searching…” badge until they land.</div>
                                <input
                                    ref={fileInputRef}
                                    type="file"
                                    accept="image/*"
                                    className="hidden"
                                    data-testid="image-search-file"
                                    onChange={e => {
                                        const f = e.target.files?.[0];
                                        if (f) handleImageFile(f);
                                        e.target.value = '';
                                    }}
                                />
                                <button
                                    onClick={() => fileInputRef.current?.click()}
                                    className="w-full flex items-center justify-center gap-2 px-3 py-2 text-sm font-medium bg-cyan-600 text-white rounded-lg border border-cyan-500 hover:bg-cyan-500 transition-colors"
                                >
                                    <ImageIcon className="w-4 h-4" /> Upload image…
                                </button>
                                {allowImageUrl && <>
                                    <div className="flex items-center gap-2 text-[10px] uppercase tracking-wider text-zinc-600">
                                        <div className="flex-1 h-px bg-zinc-800" /> or <div className="flex-1 h-px bg-zinc-800" />
                                    </div>
                                    <div className="flex gap-2">
                                        <input
                                            data-testid="image-search-url"
                                            className="flex-1 bg-zinc-900 border border-zinc-800 rounded-lg px-3 py-2 text-sm text-zinc-200 focus:outline-none focus:border-cyan-500 placeholder-zinc-600"
                                            placeholder="Paste image URL"
                                            value={imageUrl}
                                            onChange={e => setImageUrl(e.target.value)}
                                            onKeyDown={e => {
                                                if (e.key === 'Enter' && imageUrl.trim()) {
                                                    onImageSearch({ imageUrl: imageUrl.trim(), label: imageUrl.trim() }, safeLimit);
                                                    setImageUrl('');
                                                    setImageSearchOpen(false);
                                                }
                                            }}
                                        />
                                        <button
                                            onClick={() => {
                                                if (imageUrl.trim()) {
                                                    onImageSearch({ imageUrl: imageUrl.trim(), label: imageUrl.trim() }, safeLimit);
                                                    setImageUrl('');
                                                    setImageSearchOpen(false);
                                                }
                                            }}
                                            className="px-3 py-2 text-sm font-medium bg-zinc-800 text-zinc-200 rounded-lg border border-zinc-700 hover:bg-zinc-700 transition-colors"
                                        >
                                            Go
                                        </button>
                                    </div>
                                </>}
                            </Popover.Content>
                        </Popover.Portal>
                    </Popover.Root>
                )}

                {/* --- Add Filter Button --- */}
                <Popover.Root open={isAdding} onOpenChange={setIsAdding}>
                    <Popover.Trigger asChild>
                        <button
                            className={`
                                flex items-center gap-2 px-4 py-2 text-xs font-bold uppercase tracking-wider transition-all rounded shadow-sm border
                                ${isAdding
                                    ? 'bg-cyan-700 text-white border-cyan-600 ring-2 ring-cyan-900'
                                    : 'bg-cyan-600 text-white border-cyan-500 hover:bg-cyan-500 hover:shadow-cyan-500/20 shadow-lg'
                                }
                            `}
                        >
                            <PlusIcon className="w-4 h-4" />
                            <span>Add Filter</span>
                        </button>
                    </Popover.Trigger>
                    <Popover.Portal>
                        <Popover.Content
                            className="z-[100] bg-zinc-950 border border-zinc-800 rounded-xl shadow-2xl p-0 w-[800px] animate-in fade-in zoom-in-95"
                            sideOffset={5}
                            align="end"
                            avoidCollisions={true}
                            onInteractOutside={(e) => {
                                if ((e.target as HTMLElement | null)?.closest('[data-searchable-dropdown]')) e.preventDefault();
                            }}
                        >
                            {/* If field not selected, show picker - WIDE layout */}
                            {!selectedFieldForAdd ? (
                                <div className="flex flex-col h-[400px]">
                                    <div className="p-4 border-b border-zinc-800">
                                        <div className="relative">
                                            <MagnifyingGlassIcon className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-zinc-500" />
                                            <input
                                                autoFocus
                                                className="w-full bg-zinc-900 border border-zinc-800 rounded-lg pl-10 pr-4 py-2 text-sm text-zinc-200 focus:outline-none focus:border-cyan-500 placeholder-zinc-600 transition-colors"
                                                placeholder="Search fields to filter by..."
                                                value={searchTerm}
                                                onChange={e => setSearchTerm(e.target.value)}
                                            />
                                        </div>
                                    </div>
                                    <div className="flex-1 overflow-y-auto p-2 grid grid-cols-3 gap-2 content-start">
                                        {filteredFields.map(field => (
                                            <button
                                                key={field.name}
                                                onClick={() => setSelectedFieldForAdd(field.name)}
                                                className="text-left px-3 py-2 text-sm text-zinc-300 bg-zinc-900/50 border border-transparent rounded hover:bg-zinc-800 hover:border-zinc-700 hover:text-white transition-all flex items-center justify-between group h-fit"
                                            >
                                                <span className="font-medium truncate mr-2">{field.name}</span>
                                                <span className="text-[10px] text-zinc-600 bg-zinc-950 px-1.5 py-0.5 rounded group-hover:text-zinc-500 uppercase">{field.type}</span>
                                            </button>
                                        ))}
                                        {filteredFields.length === 0 && (
                                            <div className="col-span-3 py-8 text-sm text-zinc-500 text-center">
                                                No fields found matching &quot;{searchTerm}&quot;
                                            </div>
                                        )}
                                    </div>
                                </div>
                            ) : (
                                <FilterStageEditor
                                    stage={{
                                        type: 'filter',
                                        description: `Filter (${selectedFieldForAdd})`,
                                        filter: {
                                            field: selectedFieldForAdd,
                                            // datetime defaults to a range op — an exact '=' on a
                                            // timestamp matches ~nothing (0 results), which reads as
                                            // "the filter doesn't work". '>=' is what date filtering wants.
                                            operator: schema.find(f => f.name === selectedFieldForAdd)?.type === 'datetime' ? '>=' : '=',
                                            value: ''
                                        }
                                    }}
                                    onSave={handleAddSave}
                                    onCancel={() => {
                                        setSelectedFieldForAdd(null);
                                    }}
                                    datasetId={datasetId}
                                    schema={schema}
                                />
                            )}
                        </Popover.Content>
                    </Popover.Portal>
                </Popover.Root>

                <div className="h-4 w-px bg-zinc-800 mx-1" />

                <button
                    onClick={onToggleLightningMode}
                    className={`
                        p-1.5 rounded-md transition-colors
                        ${lightningMode
                            ? 'text-yellow-400 bg-yellow-400/10 hover:bg-yellow-400/20'
                            : 'text-zinc-600 hover:bg-zinc-900 hover:text-zinc-300'
                        }
                    `}
                    title={`Fast mode: ${lightningMode ? 'ON' : 'OFF'}`}
                >
                    <Rocket className="w-4 h-4" />
                </button>
            </div>
        </div>
    );
}

