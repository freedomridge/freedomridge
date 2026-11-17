// A user's level in one dataset (backend/access.py), lowest first: each includes everything before it. GET /datasets
// and GET /datasets/{id} answer it as `access_level`: an administrator's is manage, a guest's at most view. A control
// that needs more than the user's level is shown disabled with the server's own refusal as its reason.
export const LEVELS = ['view', 'tag', 'edit', 'manage'] as const;
export type AccessLevel = typeof LEVELS[number];

// What a global dataset can give every signed-in user (its everyone level): never manage.
export const EVERYONE_LEVELS = ['view', 'tag', 'edit'] as const;

/** Whether `level` allows what needs `needed`. No level (not known yet, or none) allows nothing. */
export function can(level: AccessLevel | null | undefined, needed: AccessLevel): boolean {
    return level != null && LEVELS.indexOf(level) >= LEVELS.indexOf(needed);
}

/** The server's refusal of a request that needs `needed` (403), the reason a control for it is disabled. */
export function needsLevel(needed: AccessLevel): string {
    return `This needs ${needed} access to the dataset`;
}

/** Why a control that needs `needed` is disabled at `level`; undefined when `level` allows it. */
export function refusal(level: AccessLevel | null | undefined, needed: AccessLevel): string | undefined {
    return can(level, needed) ? undefined : needsLevel(needed);
}

/** A level as a choice reads: View, Tag, Edit, Manage. */
export function levelLabel(level: AccessLevel): string {
    return level[0].toUpperCase() + level.slice(1);
}
