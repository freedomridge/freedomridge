// A lasso is a polygon in one map's coordinates, and records the layout of the map it was drawn on (`layout_id`, from
// the scatter). Once the map is laid out again (a reduce, a re-fit, an upload that moved its points), the same
// polygon may select other samples: the lasso keeps filtering, and its chip warns (docs/architecture/JOBS.md).
import type { ComplexFilter } from './api';

export const STALE_LASSO_WARNING =
    'This map was laid out again after this selection was drawn. It may select different samples now. Redraw it or remove it.';
// When the filters came from a saved view the viewer may update (lib/source-view.ts).
export const STALE_LASSO_WARNING_IN_VIEW = `${STALE_LASSO_WARNING.slice(0, -1)}, then use Update this view to save it.`;

const LASSO_OPERATORS: ReadonlySet<string> = new Set(['embedding_polygon', 'embedding_polygon_exclude']);

/** Whether a lasso drawn on layout `recorded` is stale on a map whose layout is now `current`. No recorded id: drawn
 * before 2.11, or on a map not laid out again since then. No current id: the map is still in the layout it had at the
 * upgrade, the one every lasso without an id was drawn on. A lasso with an id on a map with none can't be told
 * apart (a mirror that hasn't taken its primary's id yet), so it doesn't warn. */
export function lassoIsStale(recorded: string | null | undefined, current: string | null | undefined): boolean {
    return current != null && (recorded ?? null) !== current;
}

/** The first lasso in `filter` (nested ones included) drawn on an earlier layout of its map, or null. `mapLayouts`
 * is the dataset's `map_layouts` (GET /datasets/{id}); not loaded yet, it tells nothing. */
export function staleLasso(
    filter: ComplexFilter | undefined,
    mapLayouts: Record<string, string> | null | undefined,
): ComplexFilter | null {
    if (!filter || !mapLayouts) return null;
    if (filter.operator && LASSO_OPERATORS.has(filter.operator) && typeof filter.field === 'string'
        && lassoIsStale(filter.layout_id, mapLayouts[filter.field])) {
        return filter;
    }
    for (const child of filter.filters ?? []) {
        const found = staleLasso(child, mapLayouts);
        if (found) return found;
    }
    const value = filter.value as unknown;
    return typeof value === 'object' && value !== null && !Array.isArray(value)
        ? staleLasso(value as ComplexFilter, mapLayouts)
        : null;
}

/** The filter a lasso makes: its polygon on `field`'s map, with the layout of the map it was drawn on. `model_id` (the
 * re-fitted map's model, from the scatter's map_state) goes beside it until 2.12: a follower on 2.10 or earlier
 * refuses a relayed search holding a whole-image lasso without it, on a map its primary re-fitted. 2.11 ignores it;
 * 2.12 stops writing it. */
export function lassoFilter(region: {
    field: string;
    polygon: number[][];
    layoutId?: string | null;
    modelId?: string | null;
}): ComplexFilter {
    return {
        field: region.field,
        operator: 'embedding_polygon',
        value: region.polygon,
        layout_id: region.layoutId ?? null,
        model_id: region.modelId ?? null,
    };
}

/** One lasso, whichever chip shows it (applied, draft, or opened again from a view or a link), so it is announced
 * once. Include and exclude are one lasso. */
export function lassoKey(datasetId: number | null | undefined, lasso: ComplexFilter): string {
    return JSON.stringify([datasetId ?? null, lasso.field ?? null, lasso.layout_id ?? null, lasso.value ?? null]);
}
