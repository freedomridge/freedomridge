// The image field a grid or the sample view shows, one of the dataset's media_fields. The dataset's defaults, the
// fields they open on for everyone (grid_media_field, modal_media_field), are set in Dataset Settings → General. A
// user's own pick in a View select changes only their view: this browser remembers it per dataset, the grid's and the
// sample view's apart, as it remembers the grid's sort (components/grid-panel.tsx).
export type MediaFieldView = 'grid' | 'modal';

const storageKey = (datasetId: number, view: MediaFieldView) => `fridge:mediaField:${view}:${datasetId}`;

/** The field last picked for `view` on this dataset in this browser, or null (none, or no storage). */
export function readMediaFieldPick(datasetId: number, view: MediaFieldView): string | null {
    try {
        return window.localStorage.getItem(storageKey(datasetId, view)) || null;
    } catch {
        return null;
    }
}

/**
 * Remember the field the user picked for `view` on this dataset, and return the pick to hold. Picking the dataset's
 * default forgets the pick (null): the user follows the default again, also when it changes later.
 */
export function rememberMediaFieldPick(datasetId: number, view: MediaFieldView, field: string, datasetDefault: string | undefined): string | null {
    const pick = field === (datasetDefault || 'filepath') ? null : field;
    try {
        if (pick === null) window.localStorage.removeItem(storageKey(datasetId, view));
        else window.localStorage.setItem(storageKey(datasetId, view), pick);
    } catch {
        // No storage (private window, blocked site data): the pick lasts until the page is left.
    }
    return pick;
}

/** The field shown: the user's pick while it is one of the dataset's media fields, else the dataset's default. */
export function shownMediaField(pick: string | null, mediaFields: string[] | undefined, datasetDefault: string | undefined): string {
    return pick && mediaFields?.includes(pick) ? pick : datasetDefault || 'filepath';
}

/** A View select's option text: the dataset's default is marked, so anyone can see it and go back to it. */
export function mediaFieldLabel(field: string, datasetDefault: string | undefined): string {
    return field === (datasetDefault || 'filepath') ? `${field} (default)` : field;
}
