'use client';

import { useState, useRef, useEffect } from 'react';
import { createPortal } from 'react-dom';
import { autocompleteField, type ComplexFilter } from '@/lib/api';

/**
 * Cached-first value picker with debounced, abortable server searches. Complete value lists
 * answer matches locally; incomplete previews still search the server when text matches a
 * cached value. Query and filter changes cancel stale work and restart the search.
 *
 * `single`: single-value mode (operators like = / !=). Selecting a value replaces the
 * selection and closes the dropdown; the parent maps scalar <-> [scalar].
 *
 * `exactOnly`: the selection becomes an exact `in [...]` the user never chose explicitly (the
 * sidebar's value list). Enter then adds only a value that IS listed — typed "Forest" + Enter
 * became `in ["Forest"]` → 0 samples while the list showed the substring matches. The
 * one exception is a server search that ran out of time, where the exact value is the way in.
 */
export function SearchableSelect({ datasetId, fieldName, cachedValues, selected, onChange, filters, placeholder, single, exactOnly, valuesComplete = false, containsTerms = [], onContainsChange, showCachedValues = false }: {
    datasetId: number;
    fieldName: string;
    cachedValues: string[];
    valuesComplete?: boolean;
    selected: string[];
    onChange: (vals: string[]) => void;
    filters?: ComplexFilter;
    placeholder?: string;
    single?: boolean;
    exactOnly?: boolean;
    containsTerms?: string[];
    onContainsChange?: (terms: string[]) => void;
    showCachedValues?: boolean;
}) {
    const [query, setQuery] = useState('');
    const [enterRefused, setEnterRefused] = useState(false);
    const [open, setOpen] = useState(false);
    const [serverResults, setServerResults] = useState<Array<{value: string, count: number | null}> | null>(null);
    // False when the server's bounded scan hit its budget: an empty result then means "could not
    // search all values", not "no such value" — the empty state must say so (Enter still applies
    // the typed value as an exact filter, which IS indexed).
    const [serverExhaustive, setServerExhaustive] = useState(true);
    const [serverFailed, setServerFailed] = useState(false);
    const [loading, setLoading] = useState(false);
    const ref = useRef<HTMLDivElement>(null);
    const inputWrapRef = useRef<HTMLDivElement>(null);
    const dropdownRef = useRef<HTMLDivElement>(null);
    const requestRef = useRef<AbortController | null>(null);
    const selectedSet = new Set(selected);
    const checkboxValues = showCachedValues ? [...new Set([...cachedValues, ...selected])] : selected;
    // The dropdown is portaled to <body> with fixed positioning so it can't be clipped by an
    // ancestor's overflow (e.g. the Query Builder editor's max-h-[60vh] overflow-y-auto). Track
    // the input's viewport rect to place it.
    const [coords, setCoords] = useState<{ left: number; top: number; width: number; maxHeight: number; above: boolean } | null>(null);

    useEffect(() => {
        const handler = (e: MouseEvent) => {
            const t = e.target as Node;
            if (ref.current?.contains(t)) return;
            if (dropdownRef.current?.contains(t)) return;  // clicks in the portaled list are inside
            setOpen(false);
        };
        document.addEventListener('mousedown', handler);
        return () => document.removeEventListener('mousedown', handler);
    }, []);

    useEffect(() => {
        if (!open) return;  // coords is stale-but-unread while closed (render gates on `open`)
        const update = () => {
            const el = inputWrapRef.current;
            if (!el) return;
            const rect = el.getBoundingClientRect();
            const below = Math.max(0, window.innerHeight - rect.bottom - 8);
            const above = Math.max(0, rect.top - 8);
            const openAbove = below < Math.min(240, above);
            const width = Math.min(rect.width, window.innerWidth - 16);
            setCoords({
                left: Math.max(8, Math.min(rect.left, window.innerWidth - width - 8)),
                top: openAbove ? rect.top - 4 : rect.bottom + 4,
                width,
                maxHeight: Math.min(240, openAbove ? above : below),
                above: openAbove,
            });
        };
        update();
        window.addEventListener('scroll', update, true);  // capture: any scrolling ancestor
        window.addEventListener('resize', update);
        return () => {
            window.removeEventListener('scroll', update, true);
            window.removeEventListener('resize', update);
        };
    }, [open]);

    const filtersKey = JSON.stringify(filters ?? null);
    const cachedValuesKey = JSON.stringify(cachedValues);

    useEffect(() => {
        const controller = new AbortController();
        requestRef.current = controller;
        const currentValues = JSON.parse(cachedValuesKey) as string[];
        const currentFilters = JSON.parse(filtersKey) as ComplexFilter | null;
        // eslint-disable-next-line react-hooks/set-state-in-effect -- synchronize existing per-view UI state after a prop or client-mount change
        setServerResults(null);
        setServerExhaustive(true);
        setServerFailed(false);
        const cachedMatch = valuesComplete && currentValues.some(value => value.toLowerCase().includes(query.toLowerCase()));
        if (!open || (!query.trim() && currentValues.length > 0) || (query.trim() && cachedMatch)) {
            setLoading(false);
            return () => controller.abort();
        }
        setLoading(true);
        const search = async () => {
            try {
                const data = await autocompleteField(datasetId, fieldName, query, currentFilters ?? undefined, 100, controller.signal);
                if (controller.signal.aborted) return;
                setServerResults(data.values);
                setServerExhaustive(data.exhaustive);
            } catch {
                if (controller.signal.aborted) return;
                setServerExhaustive(false);
                setServerFailed(true);
            } finally {
                if (!controller.signal.aborted) setLoading(false);
            }
        };
        const timer = query.trim() ? setTimeout(search, 300) : undefined;
        if (!query.trim()) void search();
        return () => {
            if (timer !== undefined) clearTimeout(timer);
            controller.abort();
        };
    }, [open, query, datasetId, fieldName, filtersKey, cachedValuesKey, valuesComplete]);

    const toggleValue = (val: string) => {
        if (single) {
            onChange(selectedSet.has(val) ? [] : [val]);
            setQuery('');
            setOpen(false);
            return;
        }
        if (selectedSet.has(val)) {
            onChange(selected.filter(v => v !== val));
        } else {
            onChange([...selected, val]);
        }
        // Don't clear query — let user keep selecting from results
    };

    const addContains = () => {
        const term = query.trim();
        if (!term || !onContainsChange) return;
        if (!containsTerms.some(existing => existing.toLowerCase() === term.toLowerCase())) {
            onContainsChange([...containsTerms, term]);
        }
        requestRef.current?.abort();
        setQuery('');
        setOpen(false);
    };

    const displayResults: Array<{value: string, count: number | null}> = query.trim()
        ? (serverResults || cachedValues
            .filter(v => !selectedSet.has(v) && v.toLowerCase().includes(query.toLowerCase()))
            .map(v => ({ value: v, count: null })))
        : (cachedValues.length > 0
            // Enum fields: instant cached preview, no server hit.
            ? cachedValues.filter(v => !selectedSet.has(v)).map(v => ({ value: v, count: null }))
            // Cache-less field (high-card string): the filter-aware top values fetched on open.
            : (serverResults || []).filter(r => !selectedSet.has(r.value)));

    return (
        <div ref={ref} className="space-y-1">
            {/* Selected values as checkboxes */}
            {checkboxValues.length > 0 && (
                <div className="space-y-0.5 max-h-48 overflow-y-auto">
                    {checkboxValues.map(val => (
                        <label key={val} className="flex items-center gap-2 px-1 py-0.5 cursor-pointer hover:bg-zinc-800 rounded text-sm">
                            <input
                                type="checkbox"
                                checked={selectedSet.has(val)}
                                onChange={() => toggleValue(val)}
                                className="accent-cyan-500"
                            />
                            <span className={`${selectedSet.has(val) ? 'text-cyan-400' : 'text-zinc-300'} truncate`}>{val}</span>
                        </label>
                    ))}
                </div>
            )}

            {containsTerms.map(term => (
                <label key={term} className="flex items-center gap-2 px-1 py-0.5 cursor-pointer hover:bg-zinc-800 rounded text-sm">
                    <input
                        type="checkbox"
                        checked={true}
                        onChange={() => onContainsChange?.(containsTerms.filter(existing => existing !== term))}
                        className="accent-cyan-500"
                    />
                    <span className="text-cyan-400 truncate">Contains {JSON.stringify(term)}</span>
                </label>
            ))}

            {/* Search input */}
            <div className="relative" ref={inputWrapRef}>
                <input
                    type="text"
                    value={query}
                    onChange={(e) => {
                        requestRef.current?.abort();
                        setQuery(e.target.value);
                        setEnterRefused(false);
                        setOpen(true);
                    }}
                    onFocus={() => setOpen(true)}
                    onKeyDown={(e) => {
                        if (e.key === 'Enter') {
                            if (e.nativeEvent.isComposing) return;
                            e.preventDefault();
                            if (onContainsChange) {
                                addContains();
                                return;
                            }
                            const typed = query.trim();
                            const listed = displayResults.find(r => r.value === typed);
                            if (displayResults.length === 1) {
                                toggleValue(displayResults[0].value);
                            } else if (listed) {
                                toggleValue(listed.value);
                            } else if (typed && (!exactOnly || (serverResults !== null && !serverExhaustive && displayResults.length === 0))) {
                                toggleValue(typed);
                            } else if (typed) {
                                setEnterRefused(true);
                            }
                        } else if (e.key === 'Escape') {
                            setOpen(false);
                        }
                    }}
                    placeholder={placeholder || `+ filter by ${fieldName}`}
                    className="w-full bg-zinc-800 border border-zinc-700 rounded-md px-3 py-1.5 text-sm text-white focus:outline-none focus:ring-2 focus:ring-cyan-500"
                />
                {open && coords && createPortal(
                    <div
                        ref={dropdownRef}
                        data-searchable-dropdown
                        style={{ position: 'fixed', left: coords.left, top: coords.top, width: coords.width, maxHeight: coords.maxHeight, transform: coords.above ? 'translateY(-100%)' : undefined, zIndex: 200 }}
                        className="bg-zinc-800 border border-zinc-700 rounded-md shadow-lg max-h-60 overflow-y-auto"
                    >
                        {onContainsChange && query.trim() && (
                            <button
                                type="button"
                                onClick={addContains}
                                className="w-full text-left px-3 py-1.5 text-sm text-cyan-400 hover:bg-zinc-700 border-b border-zinc-700"
                            >
                                Contains {JSON.stringify(query.trim())}
                            </button>
                        )}
                        {loading && <div className="px-3 py-2 text-xs text-zinc-500">Searching...</div>}
                        {!loading && enterRefused && displayResults.length > 0 && (
                            <div data-testid="autocomplete-pick-hint" className="px-3 py-2 text-xs text-amber-500/90 border-b border-zinc-700">
                                Pick a value from the list — Enter adds a value only when it matches one exactly.
                            </div>
                        )}
                        {!loading && displayResults.map(r => (
                            <button
                                key={r.value}
                                onClick={() => toggleValue(r.value)}
                                className="w-full flex items-center justify-between px-3 py-1.5 text-sm hover:bg-zinc-700 transition-colors"
                            >
                                <span className="text-zinc-300 truncate">{r.value}</span>
                                {r.count !== null && <span className="text-zinc-600 text-xs ml-2 shrink-0">{r.count.toLocaleString()}</span>}
                            </button>
                        ))}
                        {!loading && serverFailed && (
                            <div data-testid="autocomplete-search-failed" className="px-3 py-2 text-xs text-amber-500/90">
                                Value search failed. Change your search to retry; cached values may be incomplete.
                            </div>
                        )}
                        {!loading && !serverFailed && displayResults.length === 0 && query.trim() && (
                            serverResults !== null && !serverExhaustive ? (
                                <div data-testid="autocomplete-not-exhaustive" className="px-3 py-2 text-xs text-amber-500/90">
                                    {onContainsChange
                                        ? 'Could not search all values in time. Choose Contains to apply a substring filter.'
                                        : 'Could not search all values in time. Press Enter to filter by the exact value you typed.'}
                                </div>
                            ) : (
                                <div className="px-3 py-2 text-xs text-zinc-500">No matches</div>
                            )
                        )}
                    </div>,
                    document.body,
                )}
            </div>
        </div>
    );
}
