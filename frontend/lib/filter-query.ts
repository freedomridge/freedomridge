// Helpers for the filter bar's chips and the query text in each filter's card (docs/FILTER_QUERY.md).
import type { ComplexFilter, ViewStage } from './api';

/** Whether the filter holds a server selection token (a big Select All), which the server forgets after 2 hours. */
export function filterHasInSelection(f: ComplexFilter | undefined): boolean {
    if (!f) return false;
    if (f.operator === 'in_selection') return true;
    return (f.filters || []).some(filterHasInSelection);
}

/**
 * An applied selection that couldn't be kept across a reload or Back (a big Select All's token, which the server
 * forgets after 2 hours, or an id list too long to save): its chip stays, greyed and marked expired, and applies
 * nothing, so the count changing is explained. It keeps the chip's words and count, never the ids or the token, and
 * the app keeps it apart from the stages: it never reaches the server, a saved view or a link.
 */
export interface ExpiredSelection {
    expired: true;
    description: string;
    count: number | null;
}

export const EXPIRED_SELECTION_TITLE =
    "This selection couldn't be kept across a reload or Back, so it no longer filters the images. "
    + 'Select the samples again, or remove it.';

/** The expired chip a selection stage leaves behind. */
export function expiredSelection(stage: ViewStage): ExpiredSelection {
    const count = stage.metadata?.count;
    return { expired: true, description: stage.description, count: typeof count === 'number' ? count : null };
}

/** An embeddings lasso, included or excluded: a polygon on one field's map, with no form to edit it in. */
export function isLassoStage(stage: ViewStage): boolean {
    return stage.filter?.operator === 'embedding_polygon' || stage.filter?.operator === 'embedding_polygon_exclude';
}

/** A chip the bar never lets anyone edit: a map region, an embeddings lasso, a similarity search, a selection, a
 * concept score, a duplicate collapse. */
export function isReadOnlyStage(stage: ViewStage): boolean {
    return stage.type === 'map_region' || stage.type === 'similarity' || stage.type === 'selection' || isLassoStage(stage)
        || !!stage.metadata?.collapse || !!stage.metadata?.dupGroupCollapse || !!stage.metadata?.concept;
}

/** The character offset of a 1-based line and column in `text`, to put the cursor on a mistake. */
export function offsetOf(text: string, line: number, column: number): number {
    const lines = text.split('\n');
    const before = lines.slice(0, Math.max(0, line - 1)).reduce((sum, item) => sum + item.length + 1, 0);
    return Math.min(text.length, before + Math.max(0, column - 1));
}
