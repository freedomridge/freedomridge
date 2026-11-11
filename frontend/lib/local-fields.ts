// A mirror's own fields, addressed as `@local.<name>` (docs/FOLLOWERS.md): on a mirror's samples they arrive as
// `local_metadata` (by name, without the prefix) and `local_tags` (`@local.tags`), beside the primary's metadata.

export const LOCAL_PREFIX = '@local.';
export const LOCAL_TAGS = '@local.tags';

export interface LocalFieldsHolder {
    local_metadata?: Record<string, unknown> | null;
    local_tags?: string[] | null;
}

export function isLocalField(name: string): boolean {
    return name.startsWith(LOCAL_PREFIX);
}

/** The value of an `@local.` field on a sample, or undefined. */
export function localValue(sample: LocalFieldsHolder, name: string): unknown {
    const key = name.slice(LOCAL_PREFIX.length);
    return key === 'tags' ? (sample.local_tags ?? undefined) : sample.local_metadata?.[key];
}

/** A sample's own fields by their full names (`@local.weeds`), `@local.tags` only when it has some. */
export function localEntries(sample: LocalFieldsHolder): Record<string, unknown> {
    const entries: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(sample.local_metadata ?? {})) entries[LOCAL_PREFIX + key] = value;
    if (sample.local_tags && sample.local_tags.length > 0) entries[LOCAL_TAGS] = sample.local_tags;
    return entries;
}
