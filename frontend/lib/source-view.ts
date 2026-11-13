// The saved view the applied filters came from: opened from Saved Views, as the dataset's default view, or from a
// `?view=` link. While the stages differ from the view's as it was opened (a stale lasso redrawn, a filter added), its
// owner or an admin can replace the view's filters with them ("Update this view"); anyone can still save a new one.
import type { ViewStage } from './api';

export interface SourceView {
    id: number;
    slug: string;
    name: string;
    userId: number | null;
    // A mirror's copy of a view on its primary: it changes there, so only "save as new" is offered.
    fromPrimary: boolean;
    // Anyone who can open the dataset can open it: the address and Copy link name it (`?view=`) while it is applied
    // as it was opened. Absent in a source kept before 2.12, which is then taken as private.
    isPublic?: boolean;
    // The view's stages as they were opened, to tell whether the applied ones differ.
    stages: ViewStage[];
    // Its updated_at as it was opened: an update names it, and is refused if the view's filters changed since.
    updatedAt: string | null;
}

/** The view a set of stages was opened from. */
export function sourceOf(
    view: {
        id: number; slug: string; name: string; user_id?: number | null; from_primary?: boolean; is_public?: boolean;
        updated_at?: string | null;
    },
    stages: ViewStage[],
): SourceView {
    return {
        id: view.id,
        slug: view.slug,
        name: view.name,
        userId: view.user_id ?? null,
        fromPrimary: !!view.from_primary,
        isPublic: !!view.is_public,
        stages,
        updatedAt: view.updated_at ?? null,
    };
}

// The one key a stage gets when it's applied rather than saved: the default view marks its stages with its id
// (lib/default-view.ts) so the bar labels them. Everything else (the filter, its description, other metadata such as
// a search's or a collapse's) is what the view holds.
function saved(stage: ViewStage): ViewStage {
    const { metadata, ...rest } = stage;
    if (!metadata) return rest as ViewStage;
    const { defaultViewId: _applied, ...kept } = metadata;  // eslint-disable-line @typescript-eslint/no-unused-vars
    return (Object.keys(kept).length ? { ...rest, metadata: kept } : rest) as ViewStage;
}

// JSON with every object's keys sorted: a view comes back from the database (JSONB) with its keys in another order.
function canonical(value: unknown): string {
    if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
    if (value && typeof value === 'object') {
        const entries = Object.entries(value as Record<string, unknown>).filter(([, v]) => v !== undefined);
        return `{${entries.sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)).map(([k, v]) => `${JSON.stringify(k)}:${canonical(v)}`).join(',')}}`;
    }
    return JSON.stringify(value);
}

/** Whether two lists of stages hold the same filters, in order, the default view's mark aside. */
export function sameStages(a: ViewStage[], b: ViewStage[]): boolean {
    return a.length === b.length && a.every((stage, i) => canonical(saved(stage)) === canonical(saved(b[i])));
}

/** The stages as a view saves them: without the default view's mark. */
export function stagesToSave(stages: ViewStage[]): ViewStage[] {
    return stages.map(saved);
}

/** Who may replace a view's filters: its owner or an admin, as for deleting or sharing it; never a mirror's copy. */
export function mayUpdateView(source: SourceView | null, user: { id: number; role: string } | null | undefined): boolean {
    return !!source && !!user && !source.fromPrimary && (source.userId === user.id || user.role === 'admin');
}
