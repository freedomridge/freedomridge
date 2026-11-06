import { ComplexFilter, ViewStage } from './api';

// A range bound for numeric/datetime fields (either side optional).
export type RangeFilter = { min?: string | number; max?: string | number };
// "Has/no/null this field" marker carried through the simple-filter shape.
export type OpFilter = { _op: 'exists' | 'not_exists' | 'is_null' };
export type TextFilter = { _op: 'text'; values: string[]; contains: string[] };

export function isTextFilter(value: unknown): value is TextFilter {
    return typeof value === 'object' && value !== null && '_op' in value && value._op === 'text'
        && 'values' in value && Array.isArray(value.values) && value.values.every(item => typeof item === 'string')
        && 'contains' in value && Array.isArray(value.contains) && value.contains.every(item => typeof item === 'string');
}
// One per-field value in the simple-filter map. Scalars/arrays for equality &
// membership; a range object for min/max; an op marker for presence filters.
export type SimpleFilterValue = string | number | boolean | string[] | number[] | RangeFilter | OpFilter | TextFilter;

const describeValue = (v: unknown): string => {
    if (typeof v === 'string' && /^\d{4}-\d{2}-\d{2}T/.test(v)) return v.slice(0, 10);
    if (typeof v === 'number' && !Number.isInteger(v)) return String(Number(v.toFixed(3)));
    return String(v);
};

const describeList = (v: unknown): string => {
    if (!Array.isArray(v)) return describeValue(v);
    const head = v.slice(0, 2).map(describeValue).join(', ');
    return v.length > 2 ? `${head} +${v.length - 2}` : head;
};

/**
 * A filter stage's chip label that names the value(s): `tags: AnnualCrop`, `country: US, CA +3`,
 * `score: 0.5–0.9`, `idx ≥ 9`. Chips said only "Filter (tags)", and Apply clears the sidebar's
 * draft, so the active value was visible nowhere.
 */
export function describeFilter(f: ComplexFilter): string {
    if (f.logic) {
        const kids = f.filters ?? [];
        if (f.logic === 'NOT') return `not ${kids.map(child => child.logic ? `(${describeFilter(child)})` : describeFilter(child)).join('; ')}`;
        if (f.logic === 'OR' && kids.length > 0 && kids.every(child => !child.logic && child.field === kids[0].field && (child.operator === 'in' || child.operator === 'contains'))) {
            return `${kids[0].field}: ${kids.map(child => child.operator === 'in' ? describeList(child.value) : `contains ${JSON.stringify(child.value)}`).join(' or ')}`;
        }
        if (kids.length === 2 && !kids[0].logic && !kids[1].logic && kids[0].field === kids[1].field) {
            const lo = kids.find(k => k.operator === '>=');
            const hi = kids.find(k => k.operator === '<=');
            if (lo && hi) return `${lo.field}: ${describeValue(lo.value)}–${describeValue(hi.value)}`;
        }
        return kids.map(describeFilter).join(f.logic === 'OR' ? ' or ' : '; ');
    }
    const symbol: Record<string, string> = { '>=': '≥', '<=': '≤', '!=': '≠' };
    switch (f.operator) {
        case 'in': return `${f.field}: ${describeList(f.value)}`;
        case 'not in': return `${f.field}: not ${describeList(f.value)}`;
        case 'contains': return `${f.field}: contains ${JSON.stringify(f.value)}`;
        case 'is_null':
        case 'not_exists': return `${f.field}: no value`;
        case 'exists':
        case 'is_not_null': return `${f.field}: has a value`;
        default: return `${f.field} ${symbol[f.operator ?? ''] ?? f.operator} ${describeList(f.value)}`;
    }
}

/** An auto-generated chip label (not one the user or a search named), safe to regenerate on edit. */
export function isAutoFilterLabel(description: string, filter: ComplexFilter): boolean {
    return description === 'Advanced Query' || /^Filter( \(|$)/.test(description) || description === describeFilter(filter);
}

/**
 * Converts simple per-field filters to ComplexFilter format
 * Example: { brightness: { min: 0.5 }, species: "dog" }
 * Becomes: { logic: 'AND', filters: [...] }
 */
export function convertSimpleFiltersToComplex(simpleFilters: Record<string, SimpleFilterValue>): ComplexFilter {
    const filters: ComplexFilter[] = [];

    for (const [key, value] of Object.entries(simpleFilters)) {
        if (value === null || value === undefined || value === '') continue;

        const isExclude = key.startsWith('exclude_');
        const field = isExclude ? key.replace('exclude_', '') : key;

        if (isTextFilter(value)) {
            const alternatives: ComplexFilter[] = [];
            if (value.values.length > 0) alternatives.push({ field, operator: 'in', value: value.values });
            for (const term of value.contains) {
                if (term.trim()) alternatives.push({ field, operator: 'contains', value: term });
            }
            if (alternatives.length > 0) {
                const combined: ComplexFilter = alternatives.length === 1 ? alternatives[0] : { logic: 'OR', filters: alternatives };
                filters.push(isExclude ? { logic: 'NOT', filters: [combined] } : combined);
            }
            continue;
        }

        // Special-operator markers — must be checked before the generic object
        // (range) branch, which would otherwise swallow them silently. Used by
        // the sidebar to carry "show samples with/without this field" semantics
        // through the simple-filter shape into the complex filter tree.
        if (value && typeof value === 'object' && !Array.isArray(value) && typeof (value as OpFilter)._op === 'string') {
            const op = (value as OpFilter)._op;
            if (op === 'exists') {
                filters.push({ field, operator: 'exists', value: null });
            } else if (op === 'not_exists') {
                filters.push({ field, operator: 'not_exists', value: null });
            } else if (op === 'is_null') {
                filters.push({ field, operator: 'is_null', value: null });
            }
            continue;
        }

        // Datetime range bounds arrive as flat suffixed keys (field_min / field_max
        // with a string value, produced by the histogram panel). Strip the suffix
        // and emit >= / <= on the real field, mirroring FilterContext — otherwise
        // 'field_min' falls through to a `contains` on a nonexistent field and
        // empties the grid on apply (preview != apply).
        if ((typeof value === 'string' || typeof value === 'number') && (field.endsWith('_min') || field.endsWith('_max'))) {
            const isMin = field.endsWith('_min');
            const bound: ComplexFilter = { field: field.slice(0, -4), operator: isMin ? '>=' : '<=', value };
            filters.push(isExclude ? { logic: 'NOT' as const, filters: [bound] } : bound);
            continue;
        }

        // Range filter (min/max)
        if (typeof value === 'object' && !Array.isArray(value)) {
            const range = value as RangeFilter;
            const rangeFilters: ComplexFilter[] = [];
            if ('min' in range && range.min !== undefined && range.min !== null && range.min !== '') {
                rangeFilters.push({
                    field,
                    operator: '>=',
                    value: isNaN(Number(range.min)) ? range.min : Number(range.min)
                });
            }
            if ('max' in range && range.max !== undefined && range.max !== null && range.max !== '') {
                rangeFilters.push({
                    field,
                    operator: '<=',
                    value: isNaN(Number(range.max)) ? range.max : Number(range.max)
                });
            }

            if (rangeFilters.length > 0) {
                const combinedRange = rangeFilters.length === 1 ? rangeFilters[0] : { logic: 'AND' as const, filters: rangeFilters };
                if (isExclude) {
                    filters.push({
                        logic: 'NOT' as const,
                        filters: [combinedRange]
                    });
                } else {
                    filters.push(combinedRange);
                }
            }
        }
        // Array filter (multi-select)
        else if (Array.isArray(value)) {
            if (value.length > 0) filters.push({ field, operator: isExclude ? 'not in' : 'in', value });
        }
        // String filter (partial match)
        else if (typeof value === 'string') {
            if (isExclude) {
                filters.push({
                    logic: 'NOT' as const,
                    filters: [{ field, operator: 'contains', value }]
                });
            } else {
                filters.push({
                    field,
                    operator: 'contains',
                    value
                });
            }
        }
        // Other value filter (exact match - number, boolean)
        else {
            filters.push({
                field,
                operator: isExclude ? '!=' : '=',
                value
            });
        }
    }

    // If no filters, return empty AND group
    if (filters.length === 0) {
        return { logic: 'AND', filters: [] };
    }

    // If only one filter, return it directly
    if (filters.length === 1) {
        return filters[0];
    }

    // Multiple filters, wrap in AND
    return {
        logic: 'AND',
        filters
    };
}

export function getSimpleFilterStages(simpleFilters: Record<string, SimpleFilterValue>): ViewStage[] {
    const stages: ViewStage[] = [];

    for (const [key, value] of Object.entries(simpleFilters)) {
        if (value === null || value === undefined || value === '') continue;

        const isExclude = key.startsWith('exclude_');
        const field = isExclude ? key.replace('exclude_', '') : key;

        // Skip empty arrays
        if (Array.isArray(value) && value.length === 0) continue;

        if (isTextFilter(value)) {
            const filter = convertSimpleFiltersToComplex({ [key]: value });
            if (filter.logic === 'AND' && !filter.filters?.length) continue;
            stages.push({ type: 'filter', description: describeFilter(filter), filter });
            continue;
        }

        // Special-operator markers (exists / not_exists / is_null). Same early
        // branch as convertSimpleFiltersToComplex; must run before the generic
        // object branch or these would fall through as empty-range filters.
        if (value && typeof value === 'object' && !Array.isArray(value) && typeof (value as OpFilter)._op === 'string') {
            const op = (value as OpFilter)._op;
            if (op === 'exists' || op === 'not_exists' || op === 'is_null') {
                const descByOp = { exists: `Has ${field}`, not_exists: `No ${field}`, is_null: `${field} is null` };
                stages.push({
                    type: 'filter',
                    description: descByOp[op],
                    filter: { field, operator: op, value: null },
                });
            }
            continue;
        }

        // Datetime range bounds as flat suffixed keys: emit a >= / <=
        // stage on the real field instead of a `contains` on 'field_min'.
        if ((typeof value === 'string' || typeof value === 'number') && (field.endsWith('_min') || field.endsWith('_max'))) {
            const isMin = field.endsWith('_min');
            const realField = field.slice(0, -4);
            const bound: ComplexFilter = { field: realField, operator: isMin ? '>=' : '<=', value };
            stages.push({
                type: 'filter',
                description: `${realField} ${isMin ? '>=' : '<='} ${value}${isExclude ? ' (Excluded)' : ''}`,
                filter: isExclude ? { logic: 'NOT' as const, filters: [bound] } : bound,
            });
            continue;
        }

        let derivedFilter: ComplexFilter;
        const description = `${field} ${isExclude ? '(Excluded)' : ''}: ${typeof value === 'object' ? JSON.stringify(value) : value}`;

        // Re-use logic for creating single-field complex filters
        // Range
        if (typeof value === 'object' && !Array.isArray(value)) {
            const range = value as RangeFilter;
            const rangeFilters: ComplexFilter[] = [];
            if (range.min !== undefined && range.min !== null && range.min !== '') rangeFilters.push({ field, operator: '>=', value: range.min });
            if (range.max !== undefined && range.max !== null && range.max !== '') rangeFilters.push({ field, operator: '<=', value: range.max });

            if (rangeFilters.length === 0) continue;
            const combined = rangeFilters.length === 1 ? rangeFilters[0] : { logic: 'AND' as const, filters: rangeFilters };

            if (isExclude) {
                derivedFilter = { logic: 'NOT' as const, filters: [combined] };
            } else {
                derivedFilter = combined;
            }
        }
        // Array
        else if (Array.isArray(value)) {
            derivedFilter = { field, operator: isExclude ? 'not in' : 'in', value };
        }
        // String
        else if (typeof value === 'string') {
            if (isExclude) {
                derivedFilter = { logic: 'NOT' as const, filters: [{ field, operator: 'contains', value }] };
            } else {
                derivedFilter = { field, operator: 'contains', value };
            }
        }
        // Exact
        else {
            derivedFilter = { field, operator: isExclude ? '!=' : '=', value };
        }

        stages.push({
            type: 'filter',
            description,
            filter: derivedFilter
        });
    }
    return stages;
}

export function getSelectionStage(ids: number[], source?: string): import('./api').ViewStage {
    return {
        type: 'selection',
        description: source ? `${source} (${ids.length} Selected)` : `${ids.length} Selected`,
        filter: {
            field: 'id',
            operator: 'in',
            value: ids
        },
        metadata: { count: ids.length }
    };
}

export function getMapStage(polygon: { geometry: { coordinates: number[][][] } }): ViewStage {
    return {
        type: 'map_region',
        description: 'Map Region',
        filter: {
            field: 'location',
            operator: 'geo_polygon',
            value: polygon.geometry.coordinates[0]
        }
    };
}
