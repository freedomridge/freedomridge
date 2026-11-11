'use client';

import { isOverlayActive, isDerivedLabelField } from '@/lib/labels';

import { type SchemaField, type Filters, type ComplexFilter, getFieldFacet, isRescoringRefusal, promoteField, rescoringRetryTurn } from '@/lib/api';
import { SearchableSelect } from './searchable-select';
import { convertSimpleFiltersToComplex, describeFilter, excludeKeyFor, isTextFilter, type TextFilter } from '@/lib/filter-utils';
import { filteredFieldNames, readCollapsedFields, readExpandedFields, storeFieldExpanded } from '@/lib/sidebar-sections';
import { EyeOpenIcon, EyeNoneIcon, MixerHorizontalIcon } from '@radix-ui/react-icons';
import { Rocket } from 'lucide-react';
import * as Slider from '@radix-ui/react-slider';
import { useState, useEffect, useId, useMemo, useRef, type ReactNode } from 'react';
import { type QueryFunctionContext, useQueries } from '@tanstack/react-query';
import { useToast } from '@/contexts/ToastContext';
import { useUser } from '@/contexts/UserContext';

// Range slider that COMMITS the filter only on release (onValueCommit) while the
// thumb + label track the drag locally. Previously the sliders fired onChange on
// every onValueChange tick, so a single drag updated the filter dozens of times —
// each one re-running the grid / map / count queries (a request storm, very
// visible on large datasets). Local state re-syncs to the committed value when it
// changes from outside (Reset button, view switch).
function RangeSlider({ min, max, fieldMin, fieldMax, step, variant, formatLabel, onCommit }: {
    min: number;
    max: number;
    fieldMin: number;
    fieldMax: number;
    step: number;
    variant: 'number' | 'datetime';
    formatLabel: (v: number) => string;
    onCommit: (lo: number, hi: number) => void;
}) {
    const [val, setVal] = useState<[number, number]>([min, max]);
    // eslint-disable-next-line react-hooks/set-state-in-effect -- deliberate prop-sync: re-sync the local drag value to the committed [min, max] when it changes from outside (Reset button, view switch)
    useEffect(() => { setVal([min, max]); }, [min, max]);
    const thumbClass = variant === 'number'
        ? 'block w-4 h-4 bg-cyan-500 rounded-full hover:bg-cyan-400 focus:outline-none focus:ring-2 focus:ring-cyan-500'
        : 'block w-4 h-4 bg-white rounded-full shadow-md hover:bg-cyan-100 focus:outline-none focus:ring-2 focus:ring-cyan-500';
    return (
        <>
            {variant === 'number' ? (
                <div className="text-xs px-2 py-0.5 bg-zinc-900 text-white rounded inline-block mb-2">
                    {formatLabel(val[0])} - {formatLabel(val[1])}
                </div>
            ) : (
                <div className="flex justify-between text-xs text-zinc-300 mb-1">
                    <span>{formatLabel(val[0])}</span>
                    <span>{formatLabel(val[1])}</span>
                </div>
            )}
            <Slider.Root
                className="relative flex items-center select-none touch-none w-full h-5"
                value={val}
                min={fieldMin}
                max={fieldMax}
                step={step}
                onValueChange={(v) => setVal([v[0], v[1]])}
                onValueCommit={(v) => onCommit(v[0], v[1])}
            >
                <Slider.Track className={variant === 'number' ? 'bg-zinc-800 relative grow rounded-full h-1' : 'relative bg-zinc-700 rounded-full h-[3px] grow'}>
                    <Slider.Range className="absolute bg-cyan-500 rounded-full h-full" />
                </Slider.Track>
                <Slider.Thumb className={thumbClass} />
                <Slider.Thumb className={thumbClass} />
            </Slider.Root>
        </>
    );
}


// SearchableSelect moved to ./searchable-select (shared with the Query Builder stage editor).

interface Props {
    datasetId: number;
    schema: SchemaField[];
    isLoading?: boolean;
    filters: Filters;
    globalFilters?: ComplexFilter;
    onChange: (filters: Filters) => void;
    activeOverlayFields?: string[];
    onToggleOverlay?: (field: string) => void;
    onSchemaUpdate?: () => void;
    lightningMode?: boolean;
    // A view stage is applied (globalFilters above also carries the grain predicate).
    filterActive?: boolean;
    // The applied view stages without the grain predicate: the fields they name start expanded.
    appliedFilters?: ComplexFilter;
    // The schema shown is still the previous filter's, while this filter's sidebar request runs.
    valuesUpdating?: boolean;
    // Every section starts expanded: the dataset is small enough to filter every field at once (the
    // server's sidebar_expand_all). Read when the sidebar mounts.
    expandAll?: boolean;
}

// A field whose statistics are still pending shows only its header: there is nothing to expand.
function isStatsPending(field: SchemaField): boolean {
    return !!field.stats_pending && (field.type === 'number' || field.type === 'datetime' || field.type === 'unknown');
}

// Whether a field's section has inputs to expand (the field loop skips internal fields, and heatmaps
// are listed under Overlays).
function hasSectionBody(field: SchemaField): boolean {
    return field.name !== 'location' && field.name !== 'metadata' && field.type !== 'heatmap'
        && field.type !== 'class_probabilities' && !isStatsPending(field);
}

// A promoted (indexed) number or datetime: its filtered MIN/MAX is computed for any filter.
function isPromotedRange(field: SchemaField): boolean {
    return !!field.is_promoted && (field.type === 'number' || field.type === 'datetime');
}

function supportsContains(field: SchemaField): boolean {
    return (field.origin === 'metadata' || field.origin === undefined)
        && (field.declared_type === 'string' || field.type === 'string');
}

function SidebarValuePicker({ datasetId, field, filters, globalFilters, onChange, showCachedValues = false }: {
    datasetId: number;
    field: SchemaField;
    filters: Filters;
    globalFilters?: ComplexFilter;
    onChange: (filters: Filters) => void;
    showCachedValues?: boolean;
}) {
    const excludeKey = excludeKeyFor(field.name);
    const current = filters[field.name];
    const allowContains = supportsContains(field);
    const selection: TextFilter = isTextFilter(current) ? current : {
        _op: 'text',
        values: Array.isArray(current) ? current.map(String) : [],
        contains: typeof current === 'string' ? [current] : [],
    };
    const update = (values: string[], contains: string[]) => {
        const next = { ...filters };
        if (contains.length > 0) next[field.name] = { _op: 'text', values, contains };
        else if (values.length > 0) next[field.name] = values;
        else delete next[field.name];
        onChange(next);
    };
    const otherDrafts = { ...filters };
    delete otherDrafts[field.name];
    delete otherDrafts[excludeKey];
    const draft = convertSimpleFiltersToComplex(otherDrafts);
    const searchFilters: ComplexFilter = { logic: 'AND', filters: [draft, ...(globalFilters ? [globalFilters] : [])] };

    return (
        <SearchableSelect
            datasetId={datasetId}
            fieldName={field.name}
            cachedValues={field.values || []}
            valuesComplete={field.values_complete === true && !field.values_truncated}
            exactOnly
            showCachedValues={showCachedValues}
            placeholder={field.type === 'string' ? `Filter by ${field.name}...` : undefined}
            filters={searchFilters}
            selected={selection.values}
            onChange={values => update(values, selection.contains)}
            containsTerms={selection.contains}
            onContainsChange={allowContains ? terms => update(selection.values, terms) : undefined}
        />
    );
}

export function FilterSidebar({
    datasetId,
    schema,
    isLoading,
    filters,
    globalFilters,
    onChange,
    activeOverlayFields = [],
    onToggleOverlay,
    onSchemaUpdate,
    lightningMode = false,
    filterActive = false,
    appliedFilters,
    valuesUpdating = false,
    expandAll: expandAllOnOpen = false,
}: Props) {
    const hasActiveFilters = Object.keys(filters).length > 0;
    const [promotingField, setPromotingField] = useState<string | null>(null);
    const { addToast } = useToast();
    const { user } = useUser();

    // Every field is a collapsible section. On a dataset
    // small enough to filter every field at once every section starts expanded; on a larger one they
    // start collapsed, except indexed fields and fields that filter the view (a draft here or an
    // applied stage). The fields the user expands or collapses are remembered per dataset in this
    // browser. Which default applies is fixed when the page opens: no section moves as filters change,
    // and a dataset that grows past the line changes default on the next load.
    const [expandAll] = useState(expandAllOnOpen);
    const [expandedFields, setExpandedFields] = useState<string[]>(() => readExpandedFields(datasetId));
    const [collapsedFields, setCollapsedFields] = useState<string[]>(() => readCollapsedFields(datasetId));
    // Fields opened because they filter the view. They stay open for this page until the user
    // collapses them, even once their filter goes (the user unticks its last value, or resets it).
    const [openedOnPage, setOpenedOnPage] = useState<string[]>([]);
    // Filtered fields the user collapsed on this page. A field that stops filtering the view drops
    // out, so a later filter on it opens it again.
    const [collapsedFiltered, setCollapsedFiltered] = useState<string[]>([]);
    const filteredFields = useMemo(() => filteredFieldNames(filters, appliedFilters), [filters, appliedFilters]);
    const opening = Array.from(filteredFields).filter(name =>
        !collapsedFiltered.includes(name) && !expandedFields.includes(name) && !openedOnPage.includes(name));
    if (opening.length > 0) setOpenedOnPage([...openedOnPage, ...opening]);
    if (collapsedFiltered.some(name => !filteredFields.has(name))) {
        setCollapsedFiltered(collapsedFiltered.filter(name => filteredFields.has(name)));
    }
    const isExpanded = (field: SchemaField) => expandedFields.includes(field.name) || openedOnPage.includes(field.name)
        || (filteredFields.has(field.name) && !collapsedFiltered.includes(field.name))
        || ((expandAll || field.is_promoted === true) && !collapsedFields.includes(field.name));
    const toggleField = (field: SchemaField) => {
        const { name } = field;
        const open = isExpanded(field);
        setExpandedFields(open ? expandedFields.filter(f => f !== name) : [...expandedFields, name]);
        setCollapsedFields(open ? [...collapsedFields.filter(f => f !== name), name] : collapsedFields.filter(f => f !== name));
        storeFieldExpanded(datasetId, name, !open, schema.map(f => f.name));
        setOpenedOnPage(prev => prev.filter(f => f !== name));
        setCollapsedFiltered(prev => (open && filteredFields.has(name) ? [...prev, name] : prev.filter(f => f !== name)));
    };
    const sectionIdPrefix = useId();
    // A header Reset removes or disables itself; focus moves to the field's header instead of the page.
    const toggleRefs = useRef(new Map<string, HTMLButtonElement>());
    const keepFocus = (name: string) => toggleRefs.current.get(name)?.focus();

    // Lightning mode with a filter applied: every field shows its
    // dataset-wide values at once, and a field's filtered values are computed only while its section
    // is expanded. Nothing is computed for a collapsed field; collapsing one drops its request (an
    // in-flight one is cancelled, a finished one stays cached for when it is expanded again).
    const showProvenance = lightningMode && filterActive;
    // A promoted range is recomputed for any filter and never needs the selectivity probe, so it is
    // asked for at once. The other fields are asked for once this filter's own sidebar answer is in
    // (not while the previous filter's stands in): it names the fields still pending for this filter,
    // and its breadth decision spares the facet request a second selectivity probe.
    // (To compute only the fields the user expanded, not those opened by their filter, test
    // `expandedFields.includes(f.name)` instead of `isExpanded(f)`.)
    const facetFields = showProvenance
        ? schema.filter(f => hasSectionBody(f) && f.filterable !== false && isExpanded(f)
            && (isPromotedRange(f) || (!valuesUpdating && f.provenance === 'pending'))).map(f => f.name)
        : [];
    const facetQueries = useQueries({
        queries: facetFields.map(name => ({
            // Under the schema key, so every invalidation of the sidebar's fields (tag edits, display
            // type, promotion, overlays) refetches the opened ones too.
            queryKey: ['schema', datasetId, 'facet', globalFilters, name],
            queryFn: async ({ signal, client, queryKey }: QueryFunctionContext) => {
                // A concept being rescored refuses the filter until its job ends: retry, as every view
                // does, but at the page's retry turn, so every refused field retries in one request.
                const failed = client.getQueryState(queryKey);
                if (failed && isRescoringRefusal(failed.fetchFailureReason)) {
                    await rescoringRetryTurn(failed.fetchFailureCount - 1, signal);
                }
                return getFieldFacet(datasetId, globalFilters, name, signal);
            },
            staleTime: 60_000,
            retry: (_count: number, error: unknown) => isRescoringRefusal(error),
            retryDelay: 0,  // the wait is the retry turn above
            refetchOnWindowFocus: false,
        })),
    });
    const facetQuery = (name: string) => {
        const index = facetFields.indexOf(name);
        return index < 0 ? undefined : facetQueries[index];
    };
    // Only the facet itself comes from the opened field's request, and only while it answers for the
    // field as the sidebar now describes it; a failed request shows the dataset values.
    const withFacet = (field: SchemaField): SchemaField => {
        const query = facetQuery(field.name);
        const facet = query?.isError ? undefined : query?.data?.find(f => f.name === field.name);
        if (!facet || facet.type !== field.type) return field;
        const { values, values_complete, values_truncated, min, max, provenance } = facet;
        return { ...field, values, values_complete, values_truncated, min, max, provenance };
    };

    // Auto-select single value fields
    /*
    useEffect(() => {
        if (isLoading) return;
        const newFilters = { ...filters };
        let hasChanges = false;
        
        schema.forEach(field => {
            if ((field.type === 'enum' || field.type === 'array') && field.values && field.values.length === 1) {
                const singleVal = field.values[0];
                const currentVal = filters[field.name];
                
                // If not set, or set to different value (shouldn't happen if filtered correctly), set it.
                // Note: array fields usually expect array in filters, enum string.
                if (!currentVal) {
                     newFilters[field.name] = field.type === 'array' ? [singleVal] : singleVal;
                     hasChanges = true;
                }
            }
        });
        
        if (hasChanges) {
             onChange(newFilters);
        }
    }, [schema, filters, isLoading, onChange]); 
    */
    // COMMENTED OUT: Auto-selecting via useEffect can be dangerous and fight with the user's intent to "clear" filters.
    // Instead, rely on the UI hiding the "All" option. If the user clears a filter, they technically see "All" (nothing selected),
    // but if the UI forces the dropdown to the only available option, it effectively shows what they want.
    // The previous edit removes "<option value="">All</option>" when length is 1.
    // This makes the dropdown display the single value.
    // However, the *state* might still be empty. 
    // If the state is empty (no filter), but the dropdown shows "Option A" (because it's the 1st option),
    // the user might think "Option A" is applied.
    // But if there is only 1 option possible, then "Apply Filter: Option A" is redundant vs "No Filter" (which also yields only Option A).
    // So visual consistency is achieved without forcing state change.


    // Check if user can promote fields (power_user or admin)
    const canPromote = user?.role === 'admin' || user?.role === 'power_user';

    const handlePromote = async (field: string) => {
        if (promotingField) return;

        setPromotingField(field);
        addToast(`Promoting field "${field}"...`, 'info', 0); // specific duration 0 means custom handling, but using default behavior for now

        try {
            await promoteField(datasetId, field);
            addToast(`Successfully promoted "${field}". Reloading...`, 'success');

            // Force immediate schema refetch
            if (onSchemaUpdate) {
                onSchemaUpdate();
                // Small delay to ensure backend has committed, then force refresh
                setTimeout(() => window.location.reload(), 1500);
            }
        } catch (e) {
            console.error("Failed to promote field", e);
            const msg = e instanceof Error ? e.message : "Unknown error";
            addToast(`Failed to promote field: ${msg}`, 'error', 5000);
            setPromotingField(null);
        }
    };

    const renderOverlayToggle = (field: string) => {
        if (!onToggleOverlay) return null;
        const isActive = isOverlayActive(field, activeOverlayFields, schema);
        return (
            <button
                onClick={() => onToggleOverlay(field)}
                aria-label={`Toggle ${field} overlay`}
                data-testid={`overlay-toggle-${field}`}
                aria-pressed={isActive}
                className={`p-1 rounded hover:bg-zinc-800 transition-colors ${isActive ? 'text-cyan-400' : 'text-zinc-500 hover:text-zinc-300'}`}
                title={isActive ? "Hide from image" : "Show on image"}
            >
                {isActive ? <EyeOpenIcon /> : <EyeNoneIcon />}
            </button>
        );
    };

    // 3-state cycling filter button for heatmap presence:
    //   none    → no filter applied
    //   exists  → only samples with this metadata key
    //   absent  → only samples without this metadata key
    // Drives `simpleFilters[field] = { _op: 'exists' | 'not_exists' }`; the
    // translation to a ComplexFilter happens in filter-utils. Applying still
    // goes through the normal query-builder flow, so the chip shows up at the
    // top and can be edited or removed from there too.
    const renderPresenceFilter = (field: string) => {
        const current = filters[field];
        const state: 'none' | 'exists' | 'absent' =
            current && typeof current === 'object' && !Array.isArray(current) && (current as { _op?: string })._op === 'exists'
                ? 'exists'
                : current && typeof current === 'object' && !Array.isArray(current) && (current as { _op?: string })._op === 'not_exists'
                ? 'absent'
                : 'none';

        const cycle = () => {
            const next = { ...filters };
            if (state === 'none') next[field] = { _op: 'exists' };
            else if (state === 'exists') next[field] = { _op: 'not_exists' };
            else delete next[field];
            onChange(next);
        };

        const stateClass =
            state === 'exists' ? 'text-cyan-400' :
            state === 'absent' ? 'text-red-400' :
            'text-zinc-500 hover:text-zinc-300';
        const stateTitle =
            state === 'exists' ? `Filter: only samples with "${field}" (click to invert)` :
            state === 'absent' ? `Filter: only samples without "${field}" (click to clear)` :
            `Filter by presence of "${field}"`;

        return (
            <button
                onClick={cycle}
                className={`relative p-1 rounded hover:bg-zinc-800 transition-colors ${stateClass}`}
                title={stateTitle}
            >
                <MixerHorizontalIcon />
                {state !== 'none' && (
                    <span
                        className={`absolute -top-0.5 -right-0.5 text-[10px] font-bold leading-none ${
                            state === 'exists' ? 'text-cyan-400' : 'text-red-400'
                        }`}
                    >
                        {state === 'exists' ? '+' : '−'}
                    </span>
                )}
            </button>
        );
    };

    const renderPromoteButton = (field: SchemaField) => {
        if (field.is_promoted) {
            return (
                <div
                    className="flex items-center justify-center w-6 h-6 rounded bg-yellow-500/20 border border-yellow-500/50"
                    title="Promoted Field (Indexed)"
                >
                    <Rocket className="w-4 h-4 text-yellow-400" />
                </div>
            );
        }

        // Only show promote button if user has permission
        if (!canPromote) return null;

        // Annotation label fields cannot be indexed (nested in JSONB arrays)
        if (isDerivedLabelField(field.name, schema)) return null;

        // Native columns (id, created_at, filepath) are already indexed and live
        // outside `metadata` — promoting them would build a useless
        // metadata->>'created_at' index over NULLs. Never offer it.
        const nativeColumns = ['id', 'created_at', 'filepath'];
        if (nativeColumns.includes(field.name)) return null;

        // Allow promoting number, string, enum, and datetime fields
        if (field.type !== 'number' && field.type !== 'string' && field.type !== 'enum' && field.type !== 'datetime') return null;

        const isPromoting = promotingField === field.name;
        return (
            <button
                onClick={() => handlePromote(field.name)}
                disabled={isPromoting}
                className={`p-1 rounded hover:bg-zinc-800 transition-colors ${isPromoting ? 'text-zinc-500' : 'text-zinc-500 hover:text-yellow-400'}`}
                title="Promote Field (Create Index)"
            >
                {isPromoting ? <span className="animate-spin">⟳</span> : <Rocket className="w-4 h-4" />}
            </button>
        );
    };

    // "(no value)" — filter to samples that have no value for this field (is_null catches
    // both a missing key and a null value). Mutually exclusive with the field's value
    // filter: setting it replaces the value; using the value control clears it. Only
    // rendered when the field actually has missing samples (null_count > 0) — no point
    // offering it for an always-populated field.
    const renderNoValueToggle = (field: SchemaField) => {
        if (!(field.null_count && field.null_count > 0)) return null;
        const fieldName = field.name;
        const active = (filters[fieldName] as { _op?: string } | undefined)?._op === 'is_null';
        return (
            <label className="flex items-center gap-2 px-1 py-0.5 cursor-pointer hover:bg-zinc-800 rounded text-xs">
                <input
                    type="checkbox"
                    checked={active}
                    onChange={() => {
                        const f = { ...filters };
                        if (active) delete f[fieldName];
                        else {
                            f[fieldName] = { _op: 'is_null' };
                        }
                        onChange(f);
                    }}
                    className="accent-cyan-500"
                />
                <span className={`italic ${active ? 'text-cyan-400' : 'text-zinc-500'}`}>(no value)</span>
            </label>
        );
    };

    // Which values a field shows while a filter is applied in lightning mode: part of the section
    // header, so a collapsed field says it too. `field` is the sidebar request's entry: 'pending'
    // means the field fills in for this filter while its section is expanded. While the schema is
    // still the previous filter's (`valuesUpdating`), its labels describe that filter, not this one.
    const renderFacetStatus = (field: SchemaField, open: boolean) => {
        if (!showProvenance || !field.provenance) return null;
        const name = field.name;
        if (field.filterable === false) {
            // Nothing to retry: no filter can use this field's name.
            return (
                <div data-testid={`facet-status-${name}`} data-provenance="cached" className="flex items-center gap-2 pl-[18px] text-[11px] text-zinc-400">
                    <span title="Its name has characters a filter cannot use (for example a hyphen), so it keeps its dataset values.">
                        Dataset values · this field can&apos;t be filtered
                    </span>
                </div>
            );
        }
        const query = facetQuery(name);
        // A field's own answer for this filter counts even while the rest of the schema is the previous one.
        const updating = valuesUpdating && !(query?.data?.some(f => f.name === name) && !query.isError);
        const shown = updating ? undefined : withFacet(field).provenance;
        const rescoring = isRescoringRefusal(query?.failureReason);
        const text = rescoring ? 'Dataset values · waiting for concept scoring…'
            : updating || query?.isFetching ? 'Updating filtered values…'
            : query?.isError ? 'Dataset values · filtered values failed'
            : shown === 'exact' ? 'Filtered values'
            : shown === 'out_of_budget' ? 'Dataset values · filtered values took too long'
            : shown === 'pending' ? (open ? 'Updating filtered values…' : 'Dataset values · expand for filtered values')
            : open ? 'Dataset values · not recomputed for this filter'
            : 'Dataset values';
        // Why the dataset values stay, and the way to filtered ones (promotion only where it exists).
        const advice = shown === 'cached' && !query?.isFetching && !query?.isError
            ? `Fast mode keeps this field's dataset values for the current filter. ${field.origin === 'metadata' && !field.is_promoted
                ? 'Promote the field, or turn fast mode off,' : 'Turn fast mode off'} for filtered values.`
            : undefined;
        return (
            <div data-testid={`facet-status-${name}`} data-provenance={shown ?? 'updating'} className="flex items-center gap-2 pl-[18px] text-[11px] text-zinc-400">
                <span title={advice} className={shown === 'exact' ? 'text-cyan-400' : undefined}>{text}</span>
                {query && !query.isFetching && !updating && (query.isError || shown === 'out_of_budget') && (
                    <button type="button" onClick={() => query.refetch()} className="text-cyan-500 hover:text-cyan-400">
                        Retry
                    </button>
                )}
            </div>
        );
    };

    // One field's section: a header (the toggle with the field's name, type and filter mark, its
    // actions, and its status lines) and, while expanded, its filter inputs. A field with nothing to
    // expand (`body` undefined) gets the same header without a toggle.
    const renderSection = (schemaField: SchemaField, index: number, { actions, status, body }: {
        actions?: ReactNode;
        status?: ReactNode;
        body?: () => ReactNode;
    }) => {
        const name = schemaField.name;
        const excludeKey = excludeKeyFor(name);
        const open = body !== undefined && isExpanded(schemaField);
        const bodyId = `${sectionIdPrefix}-field-${index}`;
        const filtered = filteredFields.has(name);
        const title = (
            <>
                <span className="truncate text-sm font-medium capitalize text-zinc-200">{name}</span>
                <span className="shrink-0 text-[10px] text-zinc-500">{schemaField.type}</span>
                {filtered && (
                    <span title="This field has a filter" className="relative h-1.5 w-1.5 shrink-0 rounded-full bg-cyan-400">
                        <span className="sr-only">, filtered</span>
                    </span>
                )}
            </>
        );
        return (
            <div key={name} data-testid={`field-section-${name}`} data-filtered={filtered ? 'true' : undefined} className="space-y-2">
                <div className="flex items-center justify-between gap-2">
                    {body ? (
                        <button
                            type="button"
                            data-testid={`field-toggle-${name}`}
                            ref={element => {
                                if (element) toggleRefs.current.set(name, element);
                                else toggleRefs.current.delete(name);
                            }}
                            aria-expanded={open}
                            aria-controls={open ? bodyId : undefined}
                            onClick={() => toggleField(schemaField)}
                            className="flex min-w-0 items-center gap-1.5 rounded text-left hover:bg-zinc-800/60 focus:outline-none focus-visible:ring-2 focus-visible:ring-cyan-500"
                        >
                            <span aria-hidden="true" className="w-3 shrink-0 text-xs text-zinc-500">{open ? '▾' : '▸'}</span>
                            {title}
                        </button>
                    ) : (
                        <div className="flex min-w-0 items-center gap-1.5 pl-[18px]">{title}</div>
                    )}
                    {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
                </div>
                {renderFacetStatus(schemaField, open)}
                {status}
                {/* Existing exclusions stay independent of the include-only inputs, even when collapsed. */}
                {filters[excludeKey] !== undefined && (
                    <div className="flex items-center gap-2 text-xs text-zinc-400">
                        <p>Additional filter: {describeFilter(convertSimpleFiltersToComplex({
                            [excludeKey]: filters[excludeKey],
                        }))}</p>
                        <button
                            type="button"
                            aria-label={`Remove ${name} exclusion`}
                            onClick={() => {
                                const next = { ...filters };
                                delete next[excludeKey];
                                onChange(next);
                                keepFocus(name);
                            }}
                            className="shrink-0 text-cyan-400 hover:text-cyan-300"
                        >
                            Remove
                        </button>
                    </div>
                )}
                {open && <div id={bodyId} className="space-y-2">{body()}</div>}
            </div>
        );
    };

    // On a mirror, its own fields (`@local.<name>`, docs/FOLLOWERS.md) come first, under their own heading, and the
    // primary's after them. Any other dataset has none, and its list is as it was.
    const ownFields = schema.filter(f => f.origin === 'local');
    const orderedSchema = ownFields.length > 0 ? [...ownFields, ...schema.filter(f => f.origin !== 'local')] : schema;
    // The rows, one per field of orderedSchema, with the two headings put in before the first of each group.
    const withOwnHeadings = (rows: ReactNode[]): ReactNode[] => {
        if (ownFields.length === 0) return rows;
        const heading = (key: string, text: string) => (
            <h3 key={key} className="text-xs font-semibold text-zinc-500 uppercase tracking-wider">{text}</h3>
        );
        return [
            heading('own-fields-heading', 'On this server'),
            ...rows.slice(0, ownFields.length),
            ...(rows.length > ownFields.length ? [heading('primary-fields-heading', 'From the primary')] : []),
            ...rows.slice(ownFields.length),
        ];
    };

    return (
        <aside className="w-full bg-zinc-900/30">
            <div className="p-6">
                <div className="flex items-center justify-between mb-6">
                    <h2 className="text-xl font-bold text-white">Filters</h2>
                    <div className="flex items-center gap-2">
                        {hasActiveFilters && (
                            <button
                                onClick={() => onChange({})}
                                className="text-xs px-2 py-1 text-red-400 hover:text-red-300 hover:bg-red-500/10 rounded transition-colors"
                                title="Clear all filter fields"
                            >
                                Clear
                            </button>
                        )}
                    </div>
                </div>

                {isLoading && schema.length === 0 ? (
                    // Loading skeleton when no schema yet
                    <div className="space-y-6">
                        {[1, 2, 3, 4, 5].map(i => (
                            <div key={i} className="space-y-2 animate-pulse">
                                <div className="h-4 bg-zinc-800/50 rounded w-24"></div>
                                <div className="h-10 bg-zinc-800/50 rounded"></div>
                            </div>
                        ))}
                    </div>
                ) : (
                    <div className={`space-y-6 ${isLoading ? 'opacity-50' : ''}`}>
                        {isLoading && (
                            <div className="flex items-center gap-2 text-xs text-cyan-400 bg-cyan-500/10 border border-cyan-500/30 rounded px-3 py-2">
                                <span className="animate-spin">⟳</span>
                                <span>Updating filtered values…</span>
                            </div>
                        )}
                        {/* System Fields Section */}
                        <div className="space-y-2 pb-4 border-b border-zinc-800">
                            <h3 className="text-xs font-semibold text-zinc-500 uppercase tracking-wider">System</h3>
                            <div className="flex items-center justify-between">
                                <label className="text-sm font-medium text-zinc-300">ID</label>
                                {renderOverlayToggle('id')}
                            </div>
                            <div className="flex items-center justify-between">
                                <label className="text-sm font-medium text-zinc-300">Filepath</label>
                                {renderOverlayToggle('filepath')}
                            </div>
                        </div>

                        {/* Overlays section — heatmap and mask (segmentation) overlay
                            fields from schema. Each row: label, presence-filter button
                            (cycles none/with/without), overlay toggle (show/hide the
                            rendered overlay). The section is hidden when this dataset has
                            no overlay fields. */}
                        {(() => {
                            const overlayFields = schema.filter(f => f.type === 'detections' || f.type === 'polygons' || f.type === 'heatmap' || f.type === 'class_probabilities' || f.type === 'segmentation' || f.type === 'keypoints' || f.type === 'cuboids' || f.type === 'classification');
                            if (overlayFields.length === 0) return null;
                            return (
                                <div className="space-y-2 pb-4 border-b border-zinc-800">
                                    <h3 className="text-xs font-semibold text-zinc-500 uppercase tracking-wider">Overlays</h3>
                                    {overlayFields.map(f => (
                                        <div key={f.name} className="flex items-center justify-between">
                                            <label className="text-sm font-medium text-zinc-300 truncate" title={f.name}>{f.name}</label>
                                            <div className="flex items-center gap-1">
                                                {renderPresenceFilter(f.name)}
                                                {renderOverlayToggle(f.name)}
                                            </div>
                                        </div>
                                    ))}
                                </div>
                            );
                        })()}

                        {withOwnHeadings(orderedSchema.map((schemaField, index) => {
                            const field = withFacet(schemaField);
                            // Skip internal or complex fields that don't make sense to filter easily yet
                            if (field.name === 'location' || field.name === 'metadata') {
                                return null;
                            }
                            // Heatmap and class-probability fields are rendered above in the dedicated Overlays section.
                            if (field.type === 'heatmap' || field.type === 'class_probabilities') {
                                return null;
                            }

                            if (isStatsPending(field)) {
                                return renderSection(schemaField, index, {
                                    status: <div className="pl-[18px] text-xs text-zinc-500" data-testid="pending-field">Statistics pending</div>,
                                });
                            }

                            if (field.origin === 'metadata' && (field.type === 'number' || field.type === 'datetime') && (field.min == null || field.max == null)) {
                                return renderSection(schemaField, index, {
                                    status: <div className="pl-[18px] text-xs text-zinc-500">No compatible range available</div>,
                                    body: () => renderPresenceFilter(field.name),
                                });
                            }

                            // Number Range (Slider)
                            if (field.type === 'number' && field.min !== undefined && field.max !== undefined) {
                                const currentVal = filters[field.name] || { min: field.min, max: field.max };
                                const min = typeof currentVal === 'object' && 'min' in currentVal ? Number(currentVal.min) : Number(field.min);
                                const max = typeof currentVal === 'object' && 'max' in currentVal ? Number(currentVal.max) : Number(field.max);
                                const fieldMin = Number(field.min);
                                const fieldMax = Number(field.max);

                                // Detect if this is an integer field (like IDs)
                                const isIntegerField = field.name.includes('_id') || field.name.includes('class') ||
                                    (Number.isInteger(fieldMin) && Number.isInteger(fieldMax) && fieldMax - fieldMin < 1000);
                                const step = isIntegerField ? 1 : (fieldMax - fieldMin) / 100;
                                const formatValue = (val: number) => isIntegerField ? Math.round(val).toString() : val.toFixed(1);

                                return renderSection(schemaField, index, {
                                    actions: (
                                        <>
                                            {renderPromoteButton(field)}
                                            {renderOverlayToggle(field.name)}
                                            <button
                                                onClick={() => {
                                                    const newFilters = { ...filters };
                                                    delete newFilters[field.name];
                                                    onChange(newFilters);
                                                    keepFocus(field.name);
                                                }}
                                                disabled={min === fieldMin && max === fieldMax}
                                                className="text-xs text-cyan-400 hover:text-cyan-300 transition-colors disabled:opacity-0"
                                            >
                                                Reset
                                            </button>
                                        </>
                                    ),
                                    body: () => (
                                        <>
                                            <RangeSlider
                                                variant="number"
                                                min={min}
                                                max={max}
                                                fieldMin={fieldMin}
                                                fieldMax={fieldMax}
                                                step={step}
                                                formatLabel={formatValue}
                                                onCommit={(lo, hi) => onChange({
                                                    ...filters,
                                                    [field.name]: { min: lo, max: hi }
                                                })}
                                            />
                                            {renderNoValueToggle(field)}
                                        </>
                                    ),
                                });
                            }

                            // Enum or Array (Select / Search)
                            if (field.type === 'enum' || field.type === 'array') {
                                // Use search input for fields with many values.
                                // Fields with <= 20 values get a simple dropdown.
                                // Fields with more get a searchable input that queries the server.
                                const values = field.values || [];
                                const useSearchInput = values.length > 20 || field.values_complete !== true || field.values_truncated;
                                const allowContains = supportsContains(field);

                                return renderSection(schemaField, index, {
                                    actions: (
                                        <>
                                            {renderPromoteButton(field)}
                                            {renderOverlayToggle(field.name)}
                                            {(filters[field.name] || filters[excludeKeyFor(field.name)]) && (
                                                <button
                                                    onClick={() => {
                                                        const newFilters = { ...filters };
                                                        delete newFilters[field.name];
                                                        delete newFilters[excludeKeyFor(field.name)];
                                                        onChange(newFilters);
                                                        keepFocus(field.name);
                                                    }}
                                                    className="text-xs text-cyan-400 hover:text-cyan-300 transition-colors"
                                                >
                                                    Reset
                                                </button>
                                            )}
                                        </>
                                    ),
                                    body: () => (
                                        <>
                                            {useSearchInput || allowContains ? (
                                                <SidebarValuePicker
                                                    datasetId={datasetId}
                                                    field={field}
                                                    filters={filters}
                                                    globalFilters={globalFilters}
                                                    onChange={onChange}
                                                    showCachedValues={!useSearchInput}
                                                />
                                            ) : (
                                            <div className="space-y-0.5 max-h-48 overflow-y-auto">
                                                {values.map((value) => {
                                                    const currentSelected = (() => {
                                                        const v = filters[field.name];
                                                        if (!v || (typeof v === 'object' && !Array.isArray(v))) return [];
                                                        if (Array.isArray(v)) return v.map(String);
                                                        return [String(v)];
                                                    })();
                                                    const isChecked = currentSelected.includes(value);
                                                    return (
                                                        <label key={value} className="flex items-center gap-2 px-1 py-0.5 cursor-pointer hover:bg-zinc-800 rounded text-sm">
                                                            <input
                                                                type="checkbox"
                                                                checked={isChecked}
                                                                onChange={() => {
                                                                    const newFilters = { ...filters };
                                                                    let newSelected: string[];
                                                                    if (isChecked) {
                                                                        newSelected = currentSelected.filter(v => v !== value);
                                                                    } else {
                                                                        newSelected = [...currentSelected, value];
                                                                    }
                                                                    if (newSelected.length === 0) {
                                                                        delete newFilters[field.name];
                                                                    } else {
                                                                        newFilters[field.name] = newSelected;
                                                                    }
                                                                    onChange(newFilters);
                                                                }}
                                                                className="accent-cyan-500"
                                                            />
                                                            <span className={isChecked ? 'text-cyan-400' : 'text-zinc-300'}>{value}</span>
                                                        </label>
                                                    );
                                                })}
                                            </div>
                                            )}
                                            {renderNoValueToggle(field)}
                                        </>
                                    ),
                                });
                            }

                            // Boolean (True/False/All)
                            if (field.type === 'boolean') {
                                const currentVal = filters[field.name];
                                return renderSection(schemaField, index, {
                                    actions: (
                                        <>
                                            {renderPromoteButton(field)}
                                            {currentVal !== undefined && (
                                                <button
                                                    onClick={() => { const f = { ...filters }; delete f[field.name]; onChange(f); keepFocus(field.name); }}
                                                    className="text-xs text-cyan-400 hover:text-cyan-300 transition-colors"
                                                >Reset</button>
                                            )}
                                        </>
                                    ),
                                    body: () => (
                                        <div className="flex gap-2">
                                            {[
                                                { label: 'All', value: undefined },
                                                { label: 'True', value: true },
                                                { label: 'False', value: false },
                                                { label: 'None', value: 'null' },
                                            ].map(opt => (
                                                <button
                                                    key={String(opt.value)}
                                                    onClick={() => {
                                                        const f = { ...filters };
                                                        if (opt.value === undefined) delete f[field.name];
                                                        else if (opt.value === 'null') f[field.name] = { _op: 'is_null' };
                                                        else f[field.name] = opt.value;
                                                        onChange(f);
                                                    }}
                                                    className={`flex-1 px-2 py-1 text-xs rounded border transition-colors ${
                                                        (opt.value === undefined && currentVal === undefined) || currentVal === opt.value
                                                            ? 'bg-cyan-500/20 text-cyan-400 border-cyan-500/50'
                                                            : 'bg-zinc-800 text-zinc-400 border-zinc-700 hover:border-zinc-600'
                                                    }`}
                                                >
                                                    {opt.label}
                                                </button>
                                            ))}
                                        </div>
                                    ),
                                });
                            }

                            // Datetime (Date Range)
                            if (field.type === 'datetime' && field.min && field.max) {
                                const fieldMin = new Date(field.min as string).getTime();
                                const fieldMax = new Date(field.max as string).getTime();
                                const currentVal = filters[field.name] || { min: field.min, max: field.max };
                                const curMin = typeof currentVal === 'object' && 'min' in currentVal
                                    ? new Date(currentVal.min).getTime() : fieldMin;
                                const curMax = typeof currentVal === 'object' && 'max' in currentVal
                                    ? new Date(currentVal.max).getTime() : fieldMax;

                                const formatDate = (ts: number) => {
                                    const d = new Date(ts);
                                    return d.toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' });
                                };

                                return renderSection(schemaField, index, {
                                    actions: (
                                        <>
                                            {renderPromoteButton(field)}
                                            {renderOverlayToggle(field.name)}
                                            <button
                                                onClick={() => {
                                                    const newFilters = { ...filters };
                                                    delete newFilters[field.name];
                                                    onChange(newFilters);
                                                    keepFocus(field.name);
                                                }}
                                                disabled={curMin === fieldMin && curMax === fieldMax}
                                                className="text-xs text-cyan-400 hover:text-cyan-300 disabled:text-zinc-700 transition-colors"
                                            >
                                                Reset
                                            </button>
                                        </>
                                    ),
                                    body: () => (
                                        <>
                                            <RangeSlider
                                                variant="datetime"
                                                min={curMin}
                                                max={curMax}
                                                fieldMin={fieldMin}
                                                fieldMax={fieldMax}
                                                step={86400000}
                                                formatLabel={formatDate}
                                                onCommit={(lo, hi) => {
                                                    const newFilters = { ...filters };
                                                    if (lo === fieldMin && hi === fieldMax) {
                                                        delete newFilters[field.name];
                                                    } else {
                                                        newFilters[field.name] = {
                                                            min: new Date(lo).toISOString(),
                                                            max: new Date(hi).toISOString()
                                                        };
                                                    }
                                                    onChange(newFilters);
                                                }}
                                            />
                                            {renderNoValueToggle(field)}
                                        </>
                                    ),
                                });
                            }

                            // String (Text Search)
                            if (field.type === 'string') {
                                return renderSection(schemaField, index, {
                                    actions: (
                                        <>
                                            {renderPromoteButton(field)}
                                            {renderOverlayToggle(field.name)}
                                        </>
                                    ),
                                    body: () => (
                                        <>
                                            <SidebarValuePicker
                                                datasetId={datasetId}
                                                field={field}
                                                filters={filters}
                                                globalFilters={globalFilters}
                                                onChange={onChange}
                                            />
                                            {renderNoValueToggle(field)}
                                        </>
                                    ),
                                });
                            }

                            return null;
                        }))}
                    </div>
                )}
            </div>
        </aside>
    );
}
