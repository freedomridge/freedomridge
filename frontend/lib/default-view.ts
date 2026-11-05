import type { View, ViewStage } from './api';

// A dataset's default view (GET /datasets/{id} `default_view`): the public saved view the app opens the
// dataset on, and goes back to when the filters are cleared. Only the app applies it: the API and the SDK
// return every sample.
export interface DefaultView {
    id: number;
    name: string;
    slug: string;
    content: View['content'];
}

/** The default view's filters as view stages, each marked with the view's id so the filter bar labels it. */
export function defaultViewStages(view: DefaultView | null | undefined): ViewStage[] {
    if (!view) return [];
    const mark = (stage: ViewStage): ViewStage => ({ ...stage, metadata: { ...stage.metadata, defaultViewId: view.id } });
    const stages = view.content?.viewStages;
    if (stages?.length) return stages.map(mark);
    // A view saved before views recorded their stages holds only its compiled filter.
    const filter = view.content?.filters;
    if (filter?.filters?.length || filter?.field) return [mark({ type: 'filter', description: view.name, filter })];
    return [];
}

/** Whether `stage` came from the dataset's current default view (not from an earlier default view). */
export function isDefaultViewStage(stage: ViewStage | undefined, view: DefaultView | null | undefined): boolean {
    return !!view && stage?.metadata?.defaultViewId === view.id;
}

/**
 * Whether the dataset opens on its default view: only when this browser kept no filters from an earlier
 * visit, and the viewer didn't choose to see every image while this view was the default.
 */
export function opensOnDefaultView(
    restored: ViewStage[],
    dismissedViewId: number | null | undefined,
    view: DefaultView | null | undefined,
): boolean {
    return !!view && restored.length === 0 && dismissedViewId !== view.id && defaultViewStages(view).length > 0;
}
