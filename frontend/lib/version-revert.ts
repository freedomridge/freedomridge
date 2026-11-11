// How the public saved views differ from a version's (view_changes of the status and diff endpoints): the views'
// names. null when the version recorded no views.
export interface ViewChanges {
    added: string[];
    removed: string[];
    filters_changed: string[];
}

// How the dataset as it is now differs from a version (GET .../versions/current-diff/{id}): what a revert to it
// would change.
export interface CurrentDiff {
    added: number;     // samples the dataset has that the version doesn't
    removed: number;   // samples of the version that the dataset no longer has
    modified: number;  // samples in both whose data differs
    view_changes: ViewChanges | null;
}

const counted = (n: number, one: string, many: string) => `${n.toLocaleString()} ${n === 1 ? one : many}`;

/** "by ada, imported by richard": who made a version and, for history brought in, who brought it in. */
export function versionMaker(version: { created_by?: string | null; imported_by?: string | null }): string {
    const maker = `by ${version.created_by ?? 'unknown'}`;
    return version.imported_by ? `${maker}, imported by ${version.imported_by}` : maker;
}

/**
 * What a revert asks before it runs: what it deletes and restores, in numbers counted when it is asked.
 * `exact` puts the dataset back to exactly the version; otherwise what was added since stays.
 * `defaultView` is the name of the view the dataset opens on now, if it has one.
 */
export function revertQuestion(
    version: { name: string; created_at: string },
    diff: CurrentDiff,
    exact: boolean,
    restoreSettings: boolean,
    defaultView: string | null,
): string {
    const since = new Date(version.created_at).toLocaleDateString();
    const views = diff.view_changes;
    const lines: string[] = [];
    if (exact) {
        const deleted = [counted(diff.added, 'sample', 'samples')];
        const restored = [counted(diff.removed, 'deleted sample', 'deleted samples')];
        if (views) {
            deleted.push(counted(views.added.length, 'public saved view', 'public saved views'));
            restored.push(counted(views.removed.length, 'deleted saved view', 'deleted saved views'));
        }
        lines.push(
            `Revert to "${version.name}"?`,
            '',
            `It deletes ${deleted.join(' and ')} added since ${since}, and restores ${restored.join(' and ')}.`,
        );
        if (diff.added > 0) {
            lines.push('Deleted samples lose their vectors and hashes: undoing brings the samples back without them.');
        }
        if (views && defaultView !== null && views.added.includes(defaultView)) {
            lines.push(`One of the views it deletes is "${defaultView}", the view this dataset opens on.`);
        }
    } else {
        const stay = `the ${counted(diff.added, 'sample', 'samples')} added since ${since} ${diff.added === 1 ? 'stays' : 'stay'}`;
        lines.push(
            `Restore sample data from "${version.name}"?`,
            '',
            `Nothing is deleted: ${stay}${views ? ', and so do saved views added since' : ''}.`,
        );
    }
    lines.push(`${counted(diff.modified, 'changed sample gets', 'changed samples get')} the data ${diff.modified === 1 ? 'it' : 'they'} had then.`);
    if (!views) {
        lines.push('This version recorded no saved views, so the saved views stay as they are.');
    } else if (exact) {
        if (views.filters_changed.length > 0) {
            lines.push(`${counted(views.filters_changed.length, 'saved view gets its', 'saved views get their')} filters back.`);
        }
    } else {
        lines.push(
            `${counted(views.filters_changed.length, 'saved view gets its', 'saved views get their')} filters back, and `
            + `${counted(views.removed.length, 'deleted saved view is', 'deleted saved views are')} restored.`,
        );
    }
    lines.push(
        restoreSettings
            ? 'Dataset settings are restored too: description, media fields, class names and colours, and the view the dataset opens on.'
            : 'Dataset settings stay unchanged.',
        'The current state is saved first, so this can be undone.',
    );
    return lines.join('\n');
}
