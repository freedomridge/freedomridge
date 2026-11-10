import React, { useState } from 'react';
import { ViewStage, SchemaField, type ComplexFilter } from '@/lib/api';
import { Cross2Icon, PlusIcon, MagnifyingGlassIcon } from '@radix-ui/react-icons';
import { FilterStageEditor, FilterModeSwitch } from './filter-stage-editor';
import { describeFilter, isAutoFilterLabel } from '@/lib/filter-utils';
import { type DefaultView, defaultViewStages, isDefaultViewStage } from '@/lib/default-view';
import { isLassoStage, isReadOnlyStage } from '@/lib/filter-query';
import { SEARCH_LIMIT_CHOICES, SEARCH_LIMIT_TITLE } from './find-similar';
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
    // Beside Add Filter, at the start of the bar: the other ways to narrow the images (Find similar).
    actions?: React.ReactNode;
    // At the end of the bar: what the filters come to (the image count).
    trailing?: React.ReactNode;
    // The page's latest search: its chip offers "top N", and changing it runs the search again.
    activeSearchId?: string | null;
    onSearchLimit?: (limit: number) => void;
}

// A selection or search chip includes or excludes its ids. A search keeps them in the one rule of an AND group (the
// grid ranks by it), and the server ignores an operator set on a group, so the toggle flips that rule. Any other
// group (a Select All's token, with its deselections) has no rule to flip, so its chip offers no toggle.
function idRule(filter: ComplexFilter | undefined): ComplexFilter | undefined {
    if (!filter?.logic) return filter;
    const only = filter.logic === 'AND' && filter.filters?.length === 1 ? filter.filters[0] : undefined;
    return only?.field === 'id' && (only.operator === 'in' || only.operator === 'not in') ? only : filter;
}

function withIdOperator(filter: ComplexFilter, operator: 'in' | 'not in'): ComplexFilter {
    const rule = idRule(filter);
    return filter.logic && rule !== filter ? { ...filter, filters: [{ ...rule, operator }] } : { ...filter, operator };
}

function canExcludeIds(filter: ComplexFilter | undefined): boolean {
    const rule = idRule(filter);
    return !!rule && !rule.logic;
}

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
    actions,
    trailing,
    activeSearchId,
    onSearchLimit,
}: QueryBuilderBarProps) {
    const [editingIndex, setEditingIndex] = useState<number | null>(null);
    const [isAdding, setIsAdding] = useState(false);
    // The Add Filter card picks a field (Builder) or takes the filter as text (Query, docs/FILTER_QUERY.md).
    const [addMode, setAddMode] = useState<'builder' | 'query'>('builder');
    const [searchTerm, setSearchTerm] = useState('');
    const [selectedFieldForAdd, setSelectedFieldForAdd] = useState<string | null>(null);

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
        setAddMode('builder');
        setSelectedFieldForAdd(null);
        setSearchTerm('');
    };

    // The chip's words. The page's latest search shows how many of the closest images it asked for, which can
    // be changed there; a lasso says which map it was drawn on.
    const chipLabel = (stage: ViewStage) => {
        const search = stage.metadata?.search as { id: string; label: string; found: number; limit: number; incomplete?: boolean } | undefined;
        if (search && search.id === activeSearchId && onSearchLimit && idRule(stage.filter)?.operator === 'in') {
            return (
                <>
                    {search.label}
                    <select
                        data-testid="search-chip-limit"
                        value={search.limit}
                        title={SEARCH_LIMIT_TITLE}
                        onClick={e => e.stopPropagation()}
                        onMouseDown={e => e.stopPropagation()}
                        onChange={e => onSearchLimit(Number(e.target.value))}
                        className="ml-2 cursor-pointer rounded border border-zinc-700 bg-zinc-900 px-1 py-0.5 text-[11px] text-zinc-300 outline-none hover:border-zinc-500"
                    >
                        {[...new Set([...SEARCH_LIMIT_CHOICES, search.limit])].sort((a, b) => a - b).map(n => (
                            <option key={n} value={n}>top {n.toLocaleString()}</option>
                        ))}
                    </select>
                    {search.found < search.limit && (
                        <span
                            className={`ml-1 ${search.incomplete ? 'text-amber-300' : 'text-zinc-500'}`}
                            title={search.incomplete
                                ? 'The search stopped checking for more matches after a few seconds, so more samples may match. An index on the filtered field, or a narrower filter, makes it complete.'
                                : undefined}
                        >
                            ({search.found.toLocaleString()} found{search.incomplete ? ', more may match' : ''})
                        </span>
                    )}
                </>
            );
        }
        if (isLassoStage(stage)) {
            return `${stage.filter.operator === 'embedding_polygon_exclude' ? 'Exclude ' : ''}Embeddings region · ${stage.filter.field} map`;
        }
        return stage.description;
    };

    return (
        <div className="flex items-center w-full bg-zinc-950 border border-zinc-800 rounded-lg p-1 shadow-sm">

            {/* --- Ways to narrow the images, before the chips they make --- */}
            <div className="mr-2 flex shrink-0 items-center gap-2 border-r border-zinc-800 pr-3">
                <Popover.Root open={isAdding} onOpenChange={(open) => { setIsAdding(open); if (!open) setAddMode('builder'); }}>
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
                            align="start"
                            avoidCollisions={true}
                            onInteractOutside={(e) => {
                                if ((e.target as HTMLElement | null)?.closest('[data-searchable-dropdown]')) e.preventDefault();
                            }}
                        >
                            {addMode === 'query' ? (
                                <FilterStageEditor
                                    stage={{ type: 'filter', description: '', filter: { logic: 'AND', filters: [] } }}
                                    initialMode="query"
                                    onEmptyBuilder={() => setAddMode('builder')}
                                    onSave={handleAddSave}
                                    onCancel={() => { setIsAdding(false); setAddMode('builder'); }}
                                    datasetId={datasetId}
                                    schema={schema}
                                />
                            ) : !selectedFieldForAdd ? (
                                /* If field not selected, show picker - WIDE layout */
                                <div className="flex flex-col h-[400px]">
                                    <div className="p-4 border-b border-zinc-800 flex items-center gap-3">
                                        <FilterModeSwitch mode="builder" onChange={() => setAddMode('query')} />
                                        <div className="relative flex-1">
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
                {actions}
            </div>

            {/* --- Existing Filters (Pipeline) --- */}
            <div className="flex items-center flex-1 self-stretch overflow-x-auto no-scrollbar gap-1">
                {stages.length === 0 && (
                    <span data-testid="no-filters" className="flex-1 truncate px-2 text-xs text-zinc-600">
                        No filters: showing every image.
                    </span>
                )}
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
                    const isReadOnly = isReadOnlyStage(stage);

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
                            <span className={`whitespace-nowrap truncate ${stage.metadata?.search || isLassoStage(stage) ? 'max-w-[360px]' : 'max-w-[200px]'}`} title={stage.description}>
                                {stage.type === 'map_region' && <span className="mr-1">🌍</span>}
                                {isLassoStage(stage) && <span className="mr-1">⬡</span>}
                                {stage.type === 'similarity' && <span className="mr-1">🔍</span>}
                                {(stage.metadata?.collapse || stage.metadata?.dupGroupCollapse) && <span className="mr-1">⧉</span>}
                                {stage.metadata?.concept && <span className="mr-1">⊘</span>}
                                {isDefaultViewStage(stage, defaultView) && <span data-testid="default-view-star" className="mr-1 text-amber-300">★</span>}
                                {chipLabel(stage)}
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

                    if (isLassoStage(stage)) {
                        // Toggle between embedding_polygon (include) and embedding_polygon_exclude, as a map region does
                        const isExclude = stage.filter.operator === 'embedding_polygon_exclude';
                        return (
                            <div key={index} className="cursor-pointer" onClick={() => {
                                onUpdateStage(index, {
                                    ...stage,
                                    description: isExclude ? 'Embeddings Region' : 'Exclude Embeddings Region',
                                    filter: { ...stage.filter, operator: isExclude ? 'embedding_polygon' : 'embedding_polygon_exclude' },
                                });
                            }} title="Click to toggle include/exclude">
                                {ChipContent}
                            </div>
                        );
                    }

                    if ((stage.type === 'selection' || stage.type === 'similarity') && !canExcludeIds(stage.filter)) {
                        return <div key={index}>{ChipContent}</div>;
                    }

                    if (stage.type === 'selection' || stage.type === 'similarity') {
                        // Toggle between 'in' (include) and 'not in' (exclude)
                        const isExclude = idRule(stage.filter)?.operator === 'not in';
                        return (
                            <div key={index} className="cursor-pointer" onClick={() => {
                                const newOp = isExclude ? 'in' : 'not in';
                                // Use metadata flag to preserve original description cleanly
                                const origDesc = stage.metadata?.originalDescription ?? stage.description;
                                onUpdateStage(index, {
                                    ...stage,
                                    description: isExclude ? origDesc : `Exclude: ${origDesc}`,
                                    filter: withIdOperator(stage.filter, newOp),
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

            {/* --- Clear, and what the filters come to --- */}
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
                {trailing}
            </div>
        </div>
    );
}
