'use client';


import { isOverlayActive } from '@/lib/labels';
import { useMemo, useState, useEffect, useCallback } from 'react';
import { useQuery } from '@tanstack/react-query';
import { getHistogram, getHistogramPreview, type HistogramBucket, type HistogramResponse, type SchemaField, type ComplexFilter } from '@/lib/api';
import { useFilters } from '@/contexts/FilterContext';
import { boundKeyFor, convertSimpleFiltersToComplex, describeFilter, excludeKeyFor, isTextFilter } from '@/lib/filter-utils';
import { EyeOpenIcon, EyeNoneIcon } from '@radix-ui/react-icons';

interface Props {
    datasetId: number;
    schema: SchemaField[];
    globalFilters: ComplexFilter;
    activeOverlayFields?: string[];
    onToggleOverlay?: (field: string) => void;
}

// Stable reference (constant across renders) so the availableFields memo doesn't need it
// as a dep — an in-component `[...]` would be a fresh array every render.
const baseFields: SchemaField[] = [
    { name: 'id', type: 'number' },
    { name: 'created_at', type: 'datetime' },
    { name: 'last_modified_at', type: 'datetime' },
];

function formatNumber(value: number | string | null | undefined) {
    if (value === null || value === undefined) return '';
    const num = typeof value === 'number' ? value : Number(value);
    if (!Number.isFinite(num)) return String(value);
    // Use compact formatting for large ranges
    return new Intl.NumberFormat('en', {
        notation: 'compact',
        maximumFractionDigits: 2
    }).format(num);
}

function formatShare(count: number, total: number) {
    if (!total) return '';
    const percent = (100 * count) / total;
    return `≈${percent.toFixed(percent < 1 ? 2 : 1)}%`;
}

/**
 * The APPROXIMATE histogram of a random sample, shown only while the exact one computes.
 * It is labelled as such, shows shares rather than counts, and its bars are not buttons: a
 * sample can miss small categories, so it must never drive a filter or a selection.
 */
export function HistogramPreview({ data }: { data: HistogramResponse }) {
    const buckets = data.buckets;
    const binned = data.bins !== undefined;
    const total = data.total || 0;
    const matched = data.preview?.matched ?? total;
    const maxCount = Math.max(0, ...buckets.map(b => b.count));
    const bucketLabel = (bucket: HistogramBucket) => bucket.range
        ? `${formatNumber(bucket.range.min_label ?? bucket.range.min)} – ${formatNumber(bucket.range.max_label ?? bucket.range.max)}`
        : String(bucket.label ?? bucket.key ?? 'null');
    return (
        <div className="w-full" data-testid="histogram-preview">
            <div data-testid="histogram-preview-banner" role="status" className="mb-3 rounded border border-amber-500/60 bg-amber-500/10 px-3 py-2 text-xs text-amber-100">
                <div className="font-semibold uppercase tracking-wide text-amber-300">Approximate preview — not exact</div>
                <div className="mt-1">
                    Estimated from a random sample of {matched.toLocaleString('en-US')} matching samples.
                    Shares are estimates and small categories may be missing
                    {binned ? '; bin ranges come from the sample' : ''}.
                    It can&apos;t be used to filter. The exact histogram is still computing and will replace it.
                </div>
            </div>
            <div className="overflow-x-auto">
                <div className="flex items-end gap-[6px] min-w-full h-56 px-2 pb-6 pt-4 border border-dashed border-amber-500/40 rounded bg-zinc-950">
                    {buckets.map((bucket, idx) => {
                        const label = bucketLabel(bucket);
                        const share = formatShare(bucket.count, total);
                        return (
                            <div
                                key={idx}
                                aria-disabled="true"
                                data-testid="histogram-preview-bar"
                                title={`Approximate • ${label} • ${share} of the sample • wait for the exact histogram to filter`}
                                className={`flex-1 ${binned ? 'min-w-[48px]' : 'min-w-[90px]'} h-full flex flex-col justify-end items-center cursor-not-allowed`}
                            >
                                <div
                                    className="w-full border border-dashed border-amber-400 bg-amber-400/30"
                                    style={{ height: `${maxCount ? (bucket.count / maxCount) * 100 : 0}%` }}
                                />
                                <div className="mt-2 text-[11px] text-zinc-300 truncate w-full text-center px-1">{label}</div>
                                <div className="text-[11px] text-amber-300 mb-1">{share}</div>
                            </div>
                        );
                    })}
                </div>
            </div>
            <div className="mt-2 text-xs text-zinc-400 flex justify-between gap-2">
                <span>
                    {binned
                        ? `Bins: ${buckets.length} (ranges from the sample)`
                        : `Categories seen in the sample: ${data.distinct_total ?? buckets.length}`}
                </span>
                {(data.other_count ?? 0) > 0 && (
                    <span data-testid="histogram-preview-others">Others (no bar): {formatShare(data.other_count ?? 0, total)}</span>
                )}
                {(data.missing ?? 0) > 0 && <span>Missing: {formatShare(data.missing ?? 0, total)}</span>}
                <span>Computing exact…</span>
            </div>
        </div>
    );
}

export function HistogramPanel({
    datasetId,
    schema,
    globalFilters,
    activeOverlayFields = [],
    onToggleOverlay
}: Props) {
    const { simpleFilters, setSimpleFilters, setFilterMode } = useFilters();

    const availableFields = useMemo(() => {
        const filtered = schema.filter((f) => f.name !== 'location' && f.name !== 'metadata');
        const merged = [...baseFields, ...filtered];
        const seen = new Set<string>();
        return merged.filter(f => {
            if (seen.has(f.name)) return false;
            seen.add(f.name);
            return true;
        });
    }, [schema]);

    const [field, setField] = useState<string>(availableFields[0]?.name || '');
    useEffect(() => {
        if (!field && availableFields[0]?.name) {
            // eslint-disable-next-line react-hooks/set-state-in-effect -- initialize the selected field to the first available field once the schema-derived list is non-empty (the initial useState default can be '' before schema loads)
            setField(availableFields[0].name);
        }
    }, [availableFields, field]);
    const fieldMeta = availableFields.find((f) => f.name === field);
    const shouldBin = fieldMeta?.type === 'number' || fieldMeta?.type === 'datetime';
    const bins = shouldBin ? undefined : undefined; // let backend choose smart bin count
    // A field declared as text is grouped by value, even when its values look numeric; the
    // "Treat as number" switch asks for numeric bins instead (remembered per field).
    const declaredText = fieldMeta?.declared_type === 'string' && fieldMeta?.origin === 'metadata';
    const [numberFields, setNumberFields] = useState<Record<string, boolean>>({});
    const asNumber = declaredText && !!numberFields[field];

    const { data, isLoading, error, refetch } = useQuery<HistogramResponse>({
        queryKey: ['histogram', datasetId, field, globalFilters, bins, asNumber],
        queryFn: () => getHistogram(datasetId, field, globalFilters, bins, asNumber),
        enabled: !!datasetId && !!field,
    });
    // While the exact histogram computes, a broad set gets a labelled approximate preview. The
    // server decides what is broad (no buckets otherwise). The exact result always replaces it.
    const { data: preview } = useQuery<HistogramResponse>({
        queryKey: ['histogram-preview', datasetId, field, globalFilters, bins, asNumber],
        queryFn: () => getHistogramPreview(datasetId, field, globalFilters, bins, asNumber),
        enabled: !!datasetId && !!field && data === undefined && !error,
        retry: false,
    });
    // Time bars (labelled with UTC instants) are drawn as such even for a time-named field typed text, whose bars
    // the server bins by time too.
    const binned = shouldBin || (asNumber && data?.bins !== undefined) || data?.buckets?.[0]?.range?.min_label != null;

    const renderOverlayToggle = (fieldName: string) => {
        if (!onToggleOverlay) return null;
        const isActive = isOverlayActive(fieldName, activeOverlayFields, schema);
        return (
            <button
                aria-pressed={isActive}
                onClick={() => onToggleOverlay(fieldName)}
                className={`p-1 rounded hover:bg-zinc-800 transition-colors ${isActive ? 'text-cyan-400' : 'text-zinc-500 hover:text-zinc-300'}`}
                title={isActive ? "Hide from image" : "Show on image"}
            >
                {isActive ? <EyeOpenIcon /> : <EyeNoneIcon />}
            </button>
        );
    };

    const excludeKey = excludeKeyFor(field);

    // Helper to get array of values (handles single value or array)
    const getValues = useCallback((key: string): (string | number)[] => {
        const val = simpleFilters?.[key];
        if (isTextFilter(val)) return val.values;
        if (Array.isArray(val)) return val;
        if (val !== undefined && val !== null) return [val];
        return [];
    }, [simpleFilters]);

    const includedValues = useMemo(() => new Set(getValues(field)), [getValues, field]);
    const excludedValues = useMemo(() => new Set(getValues(excludeKey)), [getValues, excludeKey]);

    // Clicking a bar cycles its filter: include, then exclude, then none. Categorical values use the
    // field's include and exclude value lists; a number range is stored as { min, max } under the field
    // or its exclude key; a datetime range has no exclude form, so a second click clears it.

    // A time-named field that isn't declared a datetime (its text keeps time bars) compares its text, so a filter
    // can't select a bar's instants: its bars aren't offered as filters (declaring it a datetime makes them so).
    const timeBarsOnText = (bucket: HistogramBucket) => bucket.range?.min_label != null && fieldMeta?.type !== 'datetime';

    const applyBucket = (bucket: HistogramBucket) => {
        if (timeBarsOnText(bucket)) return;
        setFilterMode('simple');
        setSimpleFilters((prev: Record<string, unknown>) => {
            const next = { ...prev };

            // Numerical/Datetime Range Logic
            if (bucket.range) {
                // If this EXACT range is already included, switch to exclude.
                // If excluded, switch to none.
                // If different range, switch to include (replacing old).

                // Check status
                const isDatetime = fieldMeta?.type === 'datetime';
                let isIncluded = false;
                let isExcluded = false;

                // Check inclusion
                if (isDatetime) {
                    const minLabel = bucket.range.min_label;
                    const maxLabel = bucket.range.max_label;
                    if (prev[boundKeyFor(field, 'min')] === minLabel && prev[boundKeyFor(field, 'max')] === maxLabel) isIncluded = true;
                    // Excluded check
                    if (prev[excludeKeyFor(boundKeyFor(field, 'min'))] === minLabel && prev[excludeKeyFor(boundKeyFor(field, 'max'))] === maxLabel) isExcluded = true;
                } else {
                    const sel = prev[field] as { min?: number; max?: number } | undefined; // { min, max }
                    if (sel && sel.min === bucket.range.min && sel.max === bucket.range.max) isIncluded = true;
                    // Excluded check
                    const excl = prev[excludeKey] as { min?: number; max?: number } | undefined;
                    if (excl && excl.min === bucket.range.min && excl.max === bucket.range.max) isExcluded = true;
                }

                // Toggle Logic
                if (isIncluded) {
                    // Include -> Exclude
                    // Clear include
                    if (isDatetime) {
                        delete next[boundKeyFor(field, 'min')];
                        delete next[boundKeyFor(field, 'max')];
                    } else {
                        delete next[field];
                    }
                    // Set Exclude
                    if (isDatetime) {
                        // The filter context has no exclude form for a datetime range
                        // (`exclude:<field>:min` / `:max`), so a second click only clears the inclusion.
                    } else {
                        next[excludeKey] = { min: bucket.range.min, max: bucket.range.max };
                    }
                } else if (isExcluded) {
                    // Exclude -> None
                    if (!isDatetime) delete next[excludeKey];
                } else {
                    // None -> Include (Clear Exclude first just in case)
                    if (!isDatetime) delete next[excludeKey];

                    if (isDatetime) {
                        const minLabel = bucket.range.min_label;
                        const maxLabel = bucket.range.max_label;
                        next[boundKeyFor(field, 'min')] = minLabel;
                        next[boundKeyFor(field, 'max')] = maxLabel;
                    } else {
                        next[field] = { min: bucket.range.min, max: bucket.range.max };
                    }
                }

                return next;
            }

            // Categorical / Discrete Value Logic
            else if (bucket.key !== null && bucket.key !== undefined) {
                const key = bucket.key;

                // Check current state
                const isInc = includedValues.has(key);
                const isExc = excludedValues.has(key);

                // Get fresh arrays
                let inc = [...getValues(field)];
                let exc = [...getValues(excludeKey)];

                if (isInc) {
                    // Include -> Exclude
                    inc = inc.filter(v => v !== key);
                    exc.push(key);
                } else if (isExc) {
                    // Exclude -> None
                    exc = exc.filter(v => v !== key);
                } else {
                    // None -> Include
                    inc.push(key);
                    // Ensure not in exclude (shouldn't be, but safety)
                    exc = exc.filter(v => v !== key);
                }

                // Update state
                for (const [filterKey, values] of [[field, inc], [excludeKey, exc]] as const) {
                    const previous = prev[filterKey];
                    if (isTextFilter(previous) && previous.contains.length > 0) {
                        next[filterKey] = { ...previous, values: values.map(String) };
                    } else if (values.length > 0) next[filterKey] = values;
                    else delete next[filterKey];
                }

                return next;
            }

            return next;
        });
    };

    const clearSelection = () => {
        setSimpleFilters((prev: Record<string, unknown>) => {
            const next = { ...prev };
            delete next[field];
            delete next[excludeKey];
            delete next[boundKeyFor(field, 'min')];
            delete next[boundKeyFor(field, 'max')];
            return next;
        });
    };

    const maxCount = useMemo(() => {
        if (!data?.buckets?.length) return 0;
        return Math.max(...data.buckets.map(b => b.count));
    }, [data]);

    const getBucketState = (bucket: HistogramBucket): 'included' | 'excluded' | 'none' => {
        if (bucket.range) {
            // Basic range check (only exact match supported for now)
            if (fieldMeta?.type === 'datetime') {
                const minSel = simpleFilters[boundKeyFor(field, 'min')];
                const maxSel = simpleFilters[boundKeyFor(field, 'max')];
                if (minSel === bucket.range.min_label && maxSel === bucket.range.max_label) return 'included';
                // Datetime range exclusion is not fully implemented in context, so no 'excluded' state for datetime ranges here.
                return 'none';
            }
            const sel = simpleFilters[field];
            const excl = simpleFilters[excludeKey];

            // Check Include
            if (sel && typeof sel === 'object' && sel.min === bucket.range.min && sel.max === bucket.range.max) return 'included';
            // Check Exclude
            if (excl && typeof excl === 'object' && excl.min === bucket.range.min && excl.max === bucket.range.max) return 'excluded';

            return 'none';
        }

        const key = bucket.key;
        if (key === null || key === undefined) return 'none';
        if (includedValues.has(key)) return 'included';
        if (excludedValues.has(key)) return 'excluded';
        return 'none';
    };

    const selectedText = (() => {
        if (isTextFilter(simpleFilters[field]) || isTextFilter(simpleFilters[excludeKey])) {
            return describeFilter(convertSimpleFiltersToComplex({
                [field]: simpleFilters[field],
                [excludeKey]: simpleFilters[excludeKey],
            }));
        }
        const parts: string[] = [];

        // Ranges
        if (fieldMeta?.type === 'datetime') {
            const min = simpleFilters[boundKeyFor(field, 'min')];
            const max = simpleFilters[boundKeyFor(field, 'max')];
            if (min || max) parts.push(`${min || '...'} - ${max || '...'} UTC`);
            // No explicit datetime range exclusion in context yet.
        } else {
            const sel = simpleFilters[field];
            // `typeof null === 'object'`, so `'min' in sel` would throw on a null value (reachable
            // via an older/corrupt persisted filter). Guard with truthiness.
            if (sel && typeof sel === 'object' && 'min' in sel) parts.push(`${formatNumber(sel.min)} - ${formatNumber(sel.max)}`);
            const excl = simpleFilters[excludeKey];
            if (excl && typeof excl === 'object' && 'min' in excl) parts.push(`NOT ${formatNumber(excl.min)} - ${formatNumber(excl.max)}`);
        }

        // Categorical. A numeric bar selection is a {min, max} object, already shown as the range
        // above — listing it here printed "In: [object Object]".
        const included = Array.from(includedValues).filter(v => typeof v !== 'object');
        const excluded = Array.from(excludedValues).filter(v => typeof v !== 'object');
        if (included.length > 0) parts.push(`In: ${included.map(v => String(v)).join(', ')}`);
        if (excluded.length > 0) parts.push(`Not: ${excluded.map(v => String(v)).join(', ')}`);

        return parts.join(' | ');
    })();

    const renderNumericHistogram = (buckets: HistogramBucket[]) => {
        const count = buckets.length;
        const firstRange = buckets[0]?.range;
        const lastRange = buckets[count - 1]?.range;
        // Datetime bars are labelled with UTC text (their range in seconds would read as a number).
        const utc = firstRange?.min_label != null;
        const minLabel = firstRange ? formatNumber(firstRange.min_label ?? firstRange.min) : 'min';
        const maxLabel = lastRange ? formatNumber(lastRange.max_label ?? lastRange.max) : 'max';
        const chartHeight = 180;
        const missingCount = data?.missing ?? 0;

        return (
            <div className="w-full">
                <div className="border border-zinc-800 rounded bg-zinc-950 px-3 pb-8 pt-4 relative overflow-hidden">
                    <div className="overflow-x-auto pb-2">
                        <div className="flex items-end gap-[4px]" style={{ height: chartHeight }}>
                            {buckets.map((bucket, idx) => {
                                const range = bucket.range;
                                const startLabel = range ? formatNumber(range.min_label ?? range.min) : '';
                                const endLabel = range ? formatNumber(range.max_label ?? range.max) : '';
                                const height = maxCount ? (bucket.count / maxCount) * chartHeight : 0;
                                const state = getBucketState(bucket);

                                let bgClass = 'bg-cyan-400/70 hover:bg-cyan-500/80';
                                if (state === 'included') bgClass = 'bg-cyan-500 hover:bg-cyan-400';
                                if (state === 'excluded') bgClass = 'bg-red-500 hover:bg-red-400';

                                const action = timeBarsOnText(bucket)
                                    ? `Declare ${field} a datetime (Dataset Settings) to filter by these times`
                                    : state === 'none' ? 'Click to Filter' : state === 'included' ? 'Click to Exclude' : 'Click to Reset';
                                return (
                                    <div key={idx} className="flex flex-col items-center" style={{ minWidth: 20 }}>
                                        <button
                                            onClick={() => applyBucket(bucket)}
                                            disabled={timeBarsOnText(bucket)}
                                            className={`w-full rounded-[2px] transition-colors ${bgClass} disabled:cursor-default`}
                                            style={{ height }}
                                            title={`${bucket.count} samples • ${startLabel} – ${endLabel}${utc ? ' UTC' : ''} • ${action}`}
                                        />
                                        <div className="text-[10px] leading-tight text-zinc-300 text-center px-1 pt-1 whitespace-nowrap">
                                            {startLabel}
                                            <div className="text-[9px] text-zinc-500">to</div>
                                            {endLabel}
                                        </div>
                                    </div>
                                );
                            })}
                        </div>
                    </div>
                    <div className="absolute inset-x-3 bottom-2 flex text-[11px] text-zinc-500 justify-between">
                        <span>{minLabel}</span>
                        <span>{maxLabel}</span>
                    </div>
                </div>
                <div className="mt-2 text-xs text-zinc-400 flex justify-between">
                    <span>Bins: {count}</span>
                    {utc && <span data-testid="histogram-utc">Times in UTC</span>}
                    <span>Total: {data?.total ?? 0}</span>
                    {missingCount > 0 && <span>Missing: {missingCount}</span>}
                </div>
            </div>
        );
    };

    const renderCategorical = (buckets: HistogramBucket[]) => {
        const missingCount = data?.missing ?? 0;
        return (
            <div className="w-full">
                <div className="overflow-x-auto">
                    <div className="flex items-end gap-[6px] min-w-full h-64 px-2 pb-6 pt-4 border border-zinc-800 rounded bg-zinc-950">
                        {buckets.map((bucket, idx) => {
                            const heightPercent = maxCount ? (bucket.count / maxCount) * 100 : 0;
                            const state = getBucketState(bucket);

                            let bgClass = 'bg-zinc-800 hover:bg-cyan-500/20';
                            let barClass = 'bg-cyan-400';
                            let opacity = 0.7;

                            if (state === 'included') {
                                bgClass = 'bg-cyan-500/40';
                                opacity = 0.95;
                            } else if (state === 'excluded') {
                                bgClass = 'bg-red-900/40 hover:bg-red-900/60'; // Darker background for container
                                barClass = 'bg-red-500'; // Red bar
                                opacity = 0.95;
                            }

                            return (
                                <button
                                    key={idx}
                                    onClick={() => applyBucket(bucket)}
                                    className={`flex-1 min-w-[90px] flex flex-col justify-end items-center rounded-[2px] transition-colors ${bgClass}`}
                                    title={`${bucket.label || bucket.key || 'null'} • ${bucket.count} samples • ${state}`}
                                    style={{ height: '100%' }}
                                >
                                    <div className={`w-full ${barClass}`} style={{ height: `${heightPercent}%`, opacity }} />
                                    <div className="mt-2 text-[11px] text-zinc-300 truncate w-full text-center px-1">
                                        {bucket.label ?? bucket.key ?? 'null'}
                                    </div>
                                    <div className="text-[11px] text-zinc-400 mb-1">
                                        {bucket.count}
                                    </div>

                                </button>
                            );
                        })}
                    </div>
                </div>
                <div className="mt-2 text-xs text-zinc-400 flex justify-between">
                    <span>
                        Categories: {buckets.length}
                        {typeof data?.distinct_total === 'number' && data.distinct_total > buckets.length
                            ? ` of ${data.distinct_total}` : ''}
                        {data?.other_count ? ` (+${data.other_count} in others)` : ''}
                    </span>
                    <span>Total: {data?.total ?? 0}</span>
                    {missingCount > 0 && <span>Missing: {missingCount}</span>}
                </div>
            </div>
        );
    };

    const renderBody = () => {
        if (error) {
            return (
                <div className="text-sm text-red-400 flex items-center gap-2">
                    Failed to load histogram.
                    <button onClick={() => refetch()} className="text-xs text-cyan-400 hover:text-cyan-300 underline">Retry</button>
                </div>
            );
        }
        if (isLoading) {
            if (preview?.preview?.status === 'ok' && preview.buckets.length > 0) return <HistogramPreview data={preview} />;
            return <div className="text-sm text-zinc-400">Loading histogram…</div>;
        }
        if (!data || data.buckets.length === 0) return <div className="text-sm text-zinc-400">No data for this field.</div>;
        const notNumeric = asNumber && data.bins === undefined && (
            <div data-testid="histogram-as-number-note" className="mb-2 text-xs text-zinc-400">
                Not binned: fewer than half of these values are numbers, or they hold a single number. Shown by value.
            </div>
        );
        if (binned) return renderNumericHistogram(data.buckets);
        return <>{notNumeric}{renderCategorical(data.buckets)}</>;
    };



    return (
        <div className="h-full flex flex-col bg-zinc-900">
            <div className="p-4 border-b border-zinc-800 flex items-center justify-between gap-4">
                <div className="flex items-center gap-3">
                    <h2 className="text-sm font-bold text-white">Histogram</h2>
                    <select
                        value={field}
                        onChange={(e) => setField(e.target.value)}
                        data-testid="histogram-field-select"
                        className="bg-zinc-800 border border-zinc-700 text-xs text-zinc-200 rounded px-2 py-1 focus:outline-none focus:ring-1 focus:ring-cyan-500"
                    >
                        {availableFields.map(f => (
                            <option key={f.name} value={f.name}>{f.name}</option>
                        ))}
                    </select>
                    {renderOverlayToggle(field)}
                    {declaredText && (
                        <label
                            className="flex items-center gap-1 text-xs text-zinc-400 cursor-pointer"
                            title="This field is declared as text, so its values are grouped as categories. Tick to bin its numeric values instead."
                        >
                            <input
                                type="checkbox"
                                data-testid="histogram-as-number"
                                checked={asNumber}
                                onChange={(e) => setNumberFields(prev => ({ ...prev, [field]: e.target.checked }))}
                                className="accent-cyan-500"
                            />
                            Treat as number
                        </label>
                    )}
                </div>

                {selectedText && (
                    <button
                        onClick={clearSelection}
                        data-testid="histogram-filter-chip"
                        className="bg-cyan-500/20 hover:bg-cyan-500/30 text-cyan-400 hover:text-cyan-300 border border-cyan-500/50 text-xs px-2 py-1 rounded flex items-center gap-2 transition-colors"
                        title="Clear staged filter"
                    >
                        <span>
                            Filtering {field}: {selectedText}
                        </span>
                        <span className="text-xs">✕</span>
                    </button>
                )}
            </div>

            <div className="flex-1 overflow-y-auto p-4">
                {renderBody()}
            </div>
        </div>
    );
}
