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

// A key of the simple-filter map is a field's name, or the name with a marker for how it filters:
// `exclude:<field>` holds the values the field must not have, `<field>:min` and `<field>:max` a datetime bar's
// bounds. No field a filter can name holds a ':' (letters, digits, `_` and `.`, after `@local.` for a mirror's
// own), so a field whose own name starts with `exclude_` or ends with `_min` / `_max` filters itself. Those
// spellings were the markers before, and read `exclude_reason` as "not reason": see upgradeDraftKeys.
const EXCLUDE_MARK = 'exclude:';
export const excludeKeyFor = (field: string): string => `${EXCLUDE_MARK}${field}`;
export const boundKeyFor = (field: string, bound: 'min' | 'max'): string => `${field}:${bound}`;

/** The field a simple-filter key filters, whether it excludes, and the bound it holds, if any. */
export function parseDraftKey(key: string): { field: string; exclude: boolean; bound?: 'min' | 'max' } {
    const exclude = key.startsWith(EXCLUDE_MARK);
    const rest = exclude ? key.slice(EXCLUDE_MARK.length) : key;
    const bound = rest.endsWith(':min') ? 'min' : rest.endsWith(':max') ? 'max' : undefined;
    return { field: bound ? rest.slice(0, -':min'.length) : rest, exclude, bound };
}

/**
 * A simple-filter map saved with the old markers (`exclude_<field>`, and `<field>_min` / `<field>_max` with a
 * string or number), read as it was read then and given the new ones, so a draft saved before keeps filtering
 * what it did. Only for a map saved before the new markers: it reads a field named `exclude_reason` as "not reason".
 */
export function upgradeDraftKeys(simpleFilters: Record<string, SimpleFilterValue>): Record<string, SimpleFilterValue> {
    const upgraded: Record<string, SimpleFilterValue> = {};
    for (const [key, value] of Object.entries(simpleFilters)) {
        const exclude = key.startsWith('exclude_');
        let field = exclude ? key.slice('exclude_'.length) : key;
        if ((typeof value === 'string' || typeof value === 'number') && (field.endsWith('_min') || field.endsWith('_max'))) {
            field = boundKeyFor(field.slice(0, -4), field.endsWith('_min') ? 'min' : 'max');
        }
        upgraded[exclude ? excludeKeyFor(field) : field] = value;
    }
    return upgraded;
}

// A datetime bound with a time of day, as its UTC date and time written to the precision it was given (a minute, a
// second or a fraction mean different things to `=` and `<=`): "2026-01-01 08:00 UTC". One written with an offset is
// shown at its UTC time; text that isn't such a datetime is shown as written.
export function describeDatetime(text: string): string {
    const match = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(\.\d+)?)?\s*(Z|[+-]\d{2}(?::?\d{2})?)?$/i.exec(text);
    if (!match) return text;
    const [, date, hour, minute, second, fraction, zone] = match;
    let clock = `${date} ${hour}:${minute}`;
    if (zone && zone.toUpperCase() !== 'Z') {
        // An offset moves whole minutes: the date, hour and minute change, the seconds and fraction don't.
        const digits = zone.slice(1).replace(':', '');
        const offset = (zone[0] === '-' ? -1 : 1) * (Number(digits.slice(0, 2)) * 60 + Number(digits.slice(2, 4) || 0));
        const local = Date.UTC(Number(date.slice(0, 4)), Number(date.slice(5, 7)) - 1, Number(date.slice(8, 10)), Number(hour), Number(minute));
        if (Number.isNaN(local)) return text;
        const utc = new Date(local - offset * 60_000).toISOString();
        clock = `${utc.slice(0, 10)} ${utc.slice(11, 16)}`;
    }
    return `${clock}${second !== undefined ? `:${second}${fraction ?? ''}` : ''} UTC`;
}

const describeValue = (v: unknown): string => {
    if (typeof v === 'string' && /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/.test(v)) return describeDatetime(v);
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

        const { field, exclude: isExclude, bound } = parseDraftKey(key);

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

        // Datetime range bounds arrive as their own keys (`<field>:min` / `<field>:max`
        // with a string value, produced by the histogram panel): emit >= / <= on the
        // field — otherwise the bound falls through to a `contains` on a nonexistent
        // field and empties the grid on apply (preview != apply).
        if ((typeof value === 'string' || typeof value === 'number') && bound) {
            const boundFilter: ComplexFilter = { field, operator: bound === 'min' ? '>=' : '<=', value };
            filters.push(isExclude ? { logic: 'NOT' as const, filters: [boundFilter] } : boundFilter);
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

        const { field, exclude: isExclude, bound } = parseDraftKey(key);

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

        // Datetime range bounds as their own keys: emit a >= / <=
        // stage on the field instead of a `contains` on '<field>:min'.
        if ((typeof value === 'string' || typeof value === 'number') && bound) {
            const isMin = bound === 'min';
            const boundFilter: ComplexFilter = { field, operator: isMin ? '>=' : '<=', value };
            stages.push({
                type: 'filter',
                description: `${field} ${isMin ? '>=' : '<='} ${value}${isExclude ? ' (Excluded)' : ''}`,
                filter: isExclude ? { logic: 'NOT' as const, filters: [boundFilter] } : boundFilter,
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
