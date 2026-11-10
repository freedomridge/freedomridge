// A patch scale as /embeddings/patch-scales lists it: this dataset's own, or one only a follower holds, whose patch
// searches run on that follower (docs/FOLLOWERS.md).
export interface PatchScaleInfo {
    scale: string;
    local?: boolean;  // absent from a server before followers' scales were listed: its own
    provider?: string;
    online?: boolean;
    last_seen_at?: string | null;
}

// How a scale reads in a picker: "4x4" for this dataset's own, "8x8 · on desktop" for a follower's, with
// "(offline)" when no follower holding it has checked in within the last minute.
export function patchScaleLabel(info: PatchScaleInfo): string {
    if (info.local !== false) return info.scale;
    return `${info.scale} · on ${info.provider ?? 'a follower'}${info.online ? '' : ' (offline)'}`;
}

// The part a search's description gains for a follower's scale: " · on desktop", or nothing.
export function patchScaleSource(info: PatchScaleInfo | undefined): string {
    return info && info.local === false ? ` · on ${info.provider ?? 'a follower'}` : '';
}
