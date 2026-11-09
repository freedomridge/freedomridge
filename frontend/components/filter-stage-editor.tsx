import React, { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { ViewStage, ComplexFilter, SchemaField, getDatasetCoverage, derivedFieldNames, formatFilterQuery, parseFilterQuery, FilterQueryProblem } from '@/lib/api';
import { PlusIcon, TrashIcon, CodeIcon } from '@radix-ui/react-icons';
import { offsetOf } from '@/lib/filter-query';
import { SearchableSelect } from './searchable-select';

// Datetime rule value input. `<input type="datetime-local">` only emits a value once BOTH the
// date AND time are set, so a date-only pick stays '' and the rule silently loses the date —
// which then filters to nothing (=) or everything (>= no-ops on ''). This COMMITS a real
// default (the field's latest date at midnight, else today) whenever the value is empty, so the
// rule always carries a concrete 'YYYY-MM-DDT00:00' that actually filters; the pre-filled time
// also means picking just a date yields a complete value.
function DatetimeFilterInput({ node, path, onChange, fieldDef }: {
    node: ComplexFilter;
    path: number[];
    onChange: (path: number[], newNode: ComplexFilter) => void;
    fieldDef?: SchemaField;
}) {
    // Date-only slice keeps this a stable string across renders (same day) — safe as an effect dep.
    const defaultVal = (fieldDef?.max != null ? String(fieldDef.max) : new Date().toISOString()).slice(0, 10) + 'T00:00';
    const isEmpty = node.value === undefined || node.value === null || node.value === '';
    useEffect(() => {
        // Seed a committed default whenever the value is empty (initial render, or after an
        // operator/field change reset it). Once set, isEmpty is false so this no-ops — no loop.
        if (isEmpty) {
            onChange(path, { ...node, value: defaultVal });
        }
    }, [isEmpty, defaultVal, node, path, onChange]);
    return (
        <input
            type="datetime-local"
            className="w-full bg-zinc-800 border border-zinc-700 rounded px-2 py-1 text-xs text-zinc-200 outline-none focus:border-cyan-500 placeholder-zinc-600"
            value={node.value ? String(node.value).slice(0, 16) : defaultVal}
            onChange={(e) => onChange(path, { ...node, value: e.target.value })}
        />
    );
}

interface FilterStageEditorProps {
    stage: ViewStage;
    onSave: (stage: ViewStage) => void;
    onCancel: () => void;
    onDelete?: () => void;
    datasetId: number;
    schema: SchemaField[];
    className?: string; // Allow custom styling from parent
    // Builder (the form) or Query (this filter as text, docs/FILTER_QUERY.md); the card can switch between them.
    initialMode?: 'builder' | 'query';
    // The Add Filter card: Builder with no text goes back to picking a field.
    onEmptyBuilder?: () => void;
}

// Builder | Query: how the filter card edits its filter.
export function FilterModeSwitch({ mode, onChange, busy = false }: { mode: 'builder' | 'query'; onChange: (mode: 'builder' | 'query') => void; busy?: boolean }) {
    return (
        <div role="group" aria-label="Edit the filter as" className="flex shrink-0 rounded-md border border-zinc-800 p-0.5 text-xs font-medium">
            {(['builder', 'query'] as const).map(option => (
                <button
                    key={option}
                    type="button"
                    data-testid={`filter-mode-${option}`}
                    aria-pressed={mode === option}
                    disabled={busy}
                    onClick={() => { if (mode !== option) onChange(option); }}
                    title={option === 'query'
                        ? 'Type the filter in the query language: paste a list, e.g. serial in (103000752 103001025)'
                        : 'Build the filter by picking a field, an operator and values'}
                    className={`flex items-center gap-1.5 rounded px-2.5 py-1 transition-colors ${mode === option ? 'bg-zinc-800 text-zinc-100' : 'text-zinc-500 hover:text-zinc-200'}`}
                >
                    {option === 'query' && <CodeIcon className="h-3.5 w-3.5" />}
                    {option === 'query' ? 'Query' : 'Builder'}
                </button>
            ))}
        </div>
    );
}

// Operators definitions
const OPERATORS: Record<string, string[]> = {
    string: ['=', '!=', 'in', 'not in', 'contains'],
    number: ['=', '!=', '>', '<', '>=', '<='],
    datetime: ['>=', '<=', '=', '!=', '>', '<'],
    enum: ['in', 'not in', '=', '!='],
    array: ['in', 'not in'],
    boolean: ['=', '!=']
};

// Operators that take NO value (presence / null checks). Overlay fields (heatmap,
// segmentation, keypoints) only support presence; other field types get them as extras
// so "filter by presence" stages (e.g. an overlay's exists filter) are editable instead
// of falling back to a blank `=`.
const VALUELESS_OPS = ['exists', 'not_exists', 'is_null', 'is_not_null'];
// Overlays, and the derived fields (embeddings / hashes / location), have presence only.
const PRESENCE_ONLY_TYPES = ['heatmap', 'class_probabilities', 'segmentation', 'keypoints', 'cuboids', 'derived'];
function opsForType(t: string): string[] {
    return PRESENCE_ONLY_TYPES.includes(t)
        ? ['exists', 'not_exists']
        : [...(OPERATORS[t] || OPERATORS.string), 'exists', 'not_exists', 'is_null', 'is_not_null'];
}

// (MultiValueSelect was replaced by the shared SearchableSelect — cached-first with a
//  server-autocomplete fallback for high-cardinality fields, consistent with the sidebar.)

export function FilterStageEditor({ stage, onSave, onCancel, onDelete, datasetId, schema, className, initialMode = 'builder', onEmptyBuilder }: FilterStageEditorProps) {
    // Always wrap in a logic group to enable "building" (add rule/group) features immediately
    const initialState = stage.filter.logic
        ? JSON.parse(JSON.stringify(stage.filter))
        : { logic: 'AND', filters: [JSON.parse(JSON.stringify(stage.filter))] };

    const [draftFilter, setDraftFilter] = useState<ComplexFilter>(initialState);

    // The dataset's fields plus its derived ones (same cached /coverage query as the grid warning).
    const { data: coverage } = useQuery({
        queryKey: ['coverage', datasetId],
        queryFn: () => getDatasetCoverage(datasetId),
        staleTime: 30 * 60_000,
        refetchOnWindowFocus: false,
    });
    const fields: SchemaField[] = [
        ...schema,
        ...(coverage ? derivedFieldNames(coverage) : [])
            .filter(name => !schema.some(f => f.name === name))
            .map(name => ({ name, type: 'derived' as const })),
    ];

    const updateFilter = (newFilter: ComplexFilter) => {
        setDraftFilter({ ...newFilter });
    };

    // --- Query mode: this filter as text. Its rules are written one per line, joined with AND. ---
    const [mode, setMode] = useState<'builder' | 'query'>(initialMode);
    const [text, setText] = useState('');
    const [problem, setProblem] = useState<string | null>(null);
    const [busy, setBusy] = useState(false);
    const textArea = useRef<HTMLTextAreaElement>(null);
    const applying = useRef(false);  // Ctrl+Enter twice before the parse answers must not save twice
    const rules = (f: ComplexFilter): ComplexFilter[] =>
        (f.logic === 'AND' ? (f.filters ?? []) : [f]).filter(rule => rule.logic || rule.field);
    const showQuery = async () => {
        setBusy(true);
        setProblem(null);
        try {
            const { parts } = await formatFilterQuery(datasetId, rules(draftFilter));
            if (parts.some(part => part == null)) {
                setProblem("Part of this filter can't be written as text yet; edit it in the builder.");
                return;
            }
            setText(parts.join('\nAND '));
            setMode('query');
        } catch (error) {
            setProblem(error instanceof Error ? error.message : String(error));
        } finally {
            setBusy(false);
        }
    };
    // The text as filters, or null with the mistake shown (line and column, the cursor put on it).
    const readText = async (): Promise<ComplexFilter[] | null> => {
        setBusy(true);
        setProblem(null);
        try {
            return (await parseFilterQuery(datasetId, text, [])).filters;
        } catch (error) {
            if (error instanceof FilterQueryProblem) {
                const hint = error.suggestions.length ? ` (did you mean ${error.suggestions.map(n => `'${n}'`).join(' or ')}?)` : '';
                setProblem(`Line ${error.line}, column ${error.column}: ${error.message}${hint}`);
                const at = offsetOf(text, error.line, error.column);
                textArea.current?.focus();
                textArea.current?.setSelectionRange(at, at);
            } else {
                setProblem(error instanceof Error ? error.message : String(error));
            }
            return null;
        } finally {
            setBusy(false);
        }
    };
    const showBuilder = async () => {
        if (!text.trim()) {
            if (onEmptyBuilder) {
                onEmptyBuilder();
                return;
            }
            setDraftFilter({ logic: 'AND', filters: [] });
            setMode('builder');
            return;
        }
        const filters = await readText();
        if (!filters) return;
        setDraftFilter({ logic: 'AND', filters });
        setMode('builder');
    };
    const apply = async () => {
        if (mode === 'builder') {
            onSave({ ...stage, filter: draftFilter });
            return;
        }
        if (applying.current) return;
        applying.current = true;
        try {
            const filters = text.trim() ? await readText() : [];
            if (!filters) return;
            if (filters.length === 0) {
                setProblem('Type a filter, e.g. split = train');
                return;
            }
            onSave({ ...stage, filter: filters.length === 1 ? filters[0] : { logic: 'AND', filters } });
        } finally {
            applying.current = false;
        }
    };

    return (
        <div className={`p-4 bg-zinc-950 rounded-lg flex flex-col gap-4 ${className || ''}`}>
            <div className="flex items-center gap-3">
                <FilterModeSwitch mode={mode} busy={busy} onChange={next => void (next === 'query' ? showQuery() : showBuilder())} />
                {mode === 'query' && <span className="text-[11px] text-zinc-500">This filter as text. Ctrl+Enter applies.</span>}
            </div>
            {mode === 'query' ? (
                <div className="flex flex-col gap-2">
                    <textarea
                        ref={textArea}
                        data-testid="filter-card-query"
                        autoFocus
                        spellCheck={false}
                        rows={Math.min(10, Math.max(3, text.split('\n').length))}
                        value={text}
                        onChange={e => { setText(e.target.value); setProblem(null); }}
                        onKeyDown={e => {
                            if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
                                e.preventDefault();
                                void apply();
                            }
                        }}
                        placeholder="e.g. serial in (103000752 103001025)   or   split = train"
                        className="w-full resize-y rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2 font-mono text-xs leading-relaxed text-zinc-200 placeholder-zinc-600 outline-none focus:border-cyan-600"
                    />
                    <div className="text-[11px] text-zinc-500">
                        = != &gt; &lt; · in (a b c) · contains · between · exists · is null · AND OR NOT ( ). Values in a list can be
                        separated by spaces or new lines, so a pasted column works.
                    </div>
                </div>
            ) : (
            <div className="max-h-[60vh] overflow-y-auto pr-2">
                <FilterNode
                    node={draftFilter}
                    path={[]}
                    datasetId={datasetId}
                    schema={fields}
                    onChange={(path, node) => {
                        if (path.length === 0) {
                            updateFilter(node);
                        } else {
                            // Deep update
                            const newRoot = JSON.parse(JSON.stringify(draftFilter));
                            let current = newRoot;
                            // Parent of the node to change
                            for (let i = 0; i < path.length - 1; i++) {
                                current = current.filters[path[i]];
                            }
                            // Replace child
                            current.filters[path[path.length - 1]] = node;
                            updateFilter(newRoot);
                        }
                    }}
                    onRemove={(path) => {
                        const newRoot = JSON.parse(JSON.stringify(draftFilter));
                        let current = newRoot;
                        // Parent of the node to remove
                        for (let i = 0; i < path.length - 1; i++) {
                            current = current.filters[path[i]];
                        }
                        current.filters.splice(path[path.length - 1], 1);
                        updateFilter(newRoot);
                    }}
                />
            </div>
            )}
            {problem && <div data-testid="filter-card-problem" className="text-xs text-red-300">{problem}</div>}

            <div className="flex justify-between items-center pt-2 border-t border-zinc-900">
                <div>
                    {onDelete && (
                        <button
                            onClick={onDelete}
                            className="text-xs text-red-500 hover:text-red-400 flex items-center gap-1 transition-colors px-2 py-1 rounded hover:bg-red-500/10"
                        >
                            <TrashIcon className="w-3.5 h-3.5" />
                            Delete Filter
                        </button>
                    )}
                </div>
                <div className="flex gap-2">
                    <button
                        onClick={onCancel}
                        className="text-xs px-3 py-1.5 text-zinc-500 hover:text-white transition-colors"
                    >
                        Cancel
                    </button>
                    <button
                        data-testid="filter-card-apply"
                        onClick={() => void apply()}
                        disabled={busy}
                        className="text-xs px-3 py-1.5 bg-cyan-600 text-white rounded hover:bg-cyan-500 font-medium transition-colors disabled:opacity-50"
                    >
                        Apply
                    </button>
                </div>
            </div>
        </div>
    );
}

interface FilterNodeProps {
    node: ComplexFilter;
    path: number[];
    datasetId: number;
    schema: SchemaField[];
    onChange: (path: number[], newNode: ComplexFilter) => void;
    onRemove: (path: number[]) => void;
}

function FilterNode({ node, path, datasetId, schema, onChange, onRemove }: FilterNodeProps) {
    if (node.logic) {
        return (
            <div className="pl-4 border-l-2 border-zinc-800 relative group/group my-1">
                <div className="flex items-center gap-2 mb-2">
                    <div className="flex bg-zinc-900 rounded p-0.5 border border-zinc-800">
                        <button
                            onClick={() => onChange(path, { ...node, logic: 'AND' })}
                            className={`text-[10px] px-2 py-0.5 rounded ${node.logic === 'AND' ? 'bg-zinc-700 text-white' : 'text-zinc-500 hover:text-zinc-300'}`}
                        >
                            AND
                        </button>
                        <button
                            onClick={() => onChange(path, { ...node, logic: 'OR' })}
                            className={`text-[10px] px-2 py-0.5 rounded ${node.logic === 'OR' ? 'bg-zinc-700 text-white' : 'text-zinc-500 hover:text-zinc-300'}`}
                        >
                            OR
                        </button>
                        <button
                            onClick={() => onChange(path, { ...node, logic: 'NOT' })}
                            className={`text-[10px] px-2 py-0.5 rounded ${node.logic === 'NOT' ? 'bg-zinc-700 text-white' : 'text-zinc-500 hover:text-zinc-300'}`}
                        >
                            NOT
                        </button>
                    </div>

                    <button
                        onClick={() => {
                            const newFilters = [...(node.filters || [])];
                            // Default new rule
                            const defaultField = schema[0]?.name || 'id';
                            newFilters.push({ field: defaultField, operator: '=', value: '' });
                            onChange(path, { ...node, filters: newFilters });
                        }}
                        className="text-[10px] text-cyan-500 hover:text-cyan-400 flex items-center gap-0.5"
                    >
                        <PlusIcon className="w-3 h-3" /> Rule
                    </button>
                    <button
                        onClick={() => {
                            const newFilters = [...(node.filters || [])];
                            newFilters.push({ logic: 'AND', filters: [] });
                            onChange(path, { ...node, filters: newFilters });
                        }}
                        className="text-[10px] text-zinc-500 hover:text-zinc-400 flex items-center gap-0.5"
                    >
                        <PlusIcon className="w-3 h-3" /> Group
                    </button>

                    {path.length > 0 && (
                        <button
                            onClick={() => onRemove(path)}
                            className="ml-auto opacity-0 group-hover/group:opacity-100 text-zinc-600 hover:text-red-400 transition-all p-1"
                        >
                            <TrashIcon className="w-3 h-3" />
                        </button>
                    )}
                </div>

                <div className="space-y-1">
                    {node.filters?.map((child, i) => (
                        <FilterNode
                            key={i}
                            node={child}
                            path={[...path, i]}
                            datasetId={datasetId}
                            schema={schema}
                            onChange={onChange}
                            onRemove={onRemove}
                        />
                    ))}
                    {(!node.filters || node.filters.length === 0) && (
                        <div className="text-[10px] text-zinc-600 italic py-1">No rules in this group</div>
                    )}
                </div>
            </div>
        );
    }

    // Leaf Node (Rule)
    const fieldDef = schema.find(s => s.name === node.field);
    const fieldType = fieldDef?.type || 'string';
    const isCategorical = fieldType === 'enum' || fieldType === 'array' || fieldType === 'string';

    // Determine operators based on exact type (+ presence/null ops for every field).
    // Always include the rule's CURRENT operator: if the field's type isn't known
    // (schema still loading, or the field is missing from it), the type-default list
    // may lack it — and a <select> whose value matches no option silently displays
    // the first one, misrendering e.g. a datetime `>=` rule as `=`.
    const typeOps = opsForType(fieldType);
    const availableOps = node.operator && !typeOps.includes(node.operator) ? [node.operator, ...typeOps] : typeOps;

    return (
        <div data-testid="filter-rule" className="flex items-center gap-2 group/rule bg-zinc-900/50 p-1.5 rounded hover:bg-zinc-900 border border-transparent hover:border-zinc-800 transition-colors">
            {/* Field Select */}
            <div className="relative min-w-[120px]">
                <select
                    value={node.field}
                    onChange={(e) => {
                        const newField = e.target.value;
                        const newDef = schema.find(s => s.name === newField);
                        const newType = newDef?.type || 'string';
                        const newOps = opsForType(newType);
                        // Keep the operator if still valid (e.g. exists when switching
                        // between overlay fields); else default to the new type's first op.
                        // Exception: switching TO a datetime field with the generic '=' default
                        // → use '>=' (an exact '=' on a timestamp matches ~nothing).
                        const cur = node.operator ?? '';
                        const op = newType === 'datetime' && cur === '=' ? '>=' : (newOps.includes(cur) ? cur : newOps[0]);
                        onChange(path, {
                            ...node,
                            field: newField,
                            operator: op as ComplexFilter['operator'],
                            value: VALUELESS_OPS.includes(op) ? null : '',
                        });
                    }}
                    className="w-full bg-zinc-800 border border-zinc-700 rounded px-2 py-1 text-xs text-zinc-200 outline-none focus:border-cyan-500 appearance-none cursor-pointer"
                >
                    {schema.map(f => (
                        <option key={f.name} value={f.name}>{f.name}</option>
                    ))}
                </select>
            </div>

            {/* Operator Select */}
            <div className="w-[80px]">
                <select
                    value={node.operator}
                    onChange={(e) => onChange(path, { ...node, operator: e.target.value as ComplexFilter['operator'] })}
                    className="w-full bg-zinc-800 border border-zinc-700 rounded px-2 py-1 text-xs text-zinc-400 outline-none focus:border-cyan-500 appearance-none cursor-pointer text-center"
                >
                    {availableOps.map(op => (
                        <option key={op} value={op}>{op}</option>
                    ))}
                </select>
            </div>

            {/* Value Input */}
            <div className="flex-1 min-w-[140px]">
                {VALUELESS_OPS.includes(node.operator ?? '') ? (
                    <span className="text-xs text-zinc-600 italic px-1">(no value needed)</span>
                ) : isCategorical && (node.operator === 'in' || node.operator === 'not in') ? (
                    // Multi-value: cached-first list + server-autocomplete fallback (same as
                    // the sidebar) so high-cardinality fields aren't capped at the top-200.
                    <SearchableSelect
                        datasetId={datasetId}
                        fieldName={node.field || ''}
                        cachedValues={fieldDef?.values || []}
                        valuesComplete={fieldDef?.values_complete === true && !fieldDef?.values_truncated}
                        selected={Array.isArray(node.value) ? node.value : (node.value ? [String(node.value)] : [])}
                        onChange={(vals) => onChange(path, { ...node, value: vals })}
                        placeholder={`+ ${node.field}`}
                    />
                ) : isCategorical && (node.operator === '=' || node.operator === '!=') ? (
                    // Single-value (= / !=): same picker in single mode.
                    <SearchableSelect
                        single
                        datasetId={datasetId}
                        fieldName={node.field || ''}
                        cachedValues={fieldDef?.values || []}
                        valuesComplete={fieldDef?.values_complete === true && !fieldDef?.values_truncated}
                        selected={node.value !== undefined && node.value !== null && node.value !== '' ? [String(node.value)] : []}
                        onChange={(vals) => onChange(path, { ...node, value: vals[0] ?? '' })}
                        placeholder={`Select ${node.field}...`}
                    />
                ) : fieldType === 'datetime' ? (
                    <DatetimeFilterInput node={node} path={path} onChange={onChange} fieldDef={fieldDef} />
                ) : (
                    <input
                        type={fieldType === 'number' ? 'number' : 'text'}
                        className="w-full bg-zinc-800 border border-zinc-700 rounded px-2 py-1 text-xs text-zinc-200 outline-none focus:border-cyan-500 placeholder-zinc-600"
                        placeholder="Value..."
                        value={node.value === undefined || node.value === null ? '' : String(node.value)}
                        onChange={(e) => {
                            const valStr = e.target.value;
                            if (fieldType === 'number') {
                                if (valStr === '') {
                                    // Allow empty string to be set, effectively clearing the filter value
                                    onChange(path, { ...node, value: '' });
                                } else {
                                    const num = Number(valStr);
                                    if (!isNaN(num)) {
                                        onChange(path, { ...node, value: num });
                                    } else {
                                        // Not a number: the value is left unchanged, so only valid numbers can be typed.
                                    }
                                }
                            } else {
                                onChange(path, { ...node, value: valStr });
                            }
                        }}
                    />
                )}
            </div>

            <button
                onClick={() => onRemove(path)}
                className="opacity-0 group-hover/rule:opacity-100 text-zinc-600 hover:text-red-400 p-1"
                title="Remove rule"
            >
                <TrashIcon className="w-3 h-3" />
            </button>
        </div>
    );
}
