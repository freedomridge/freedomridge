import type { AdvisorOverlap } from './api';

/**
 * Facts, not advice, for an overlapping pair whose removable member Postgres actually uses more
 * than the index that would replace it (it usually picks the smaller one). Removing it still works;
 * the note says where those lookups would go. Null when the replacement is used at least as much.
 */
export function overlapPreferenceNote(pair: AdvisorOverlap): string | null {
    const { redundant, kept } = pair;
    if (redundant.scans <= kept.scans) return null;
    const larger = redundant.size_bytes > 0 && kept.size_bytes > redundant.size_bytes
        ? `, which is ${(kept.size_bytes / redundant.size_bytes).toFixed(1)}× larger`
        : '';
    return `Postgres currently uses this index more than ${kept.name} `
        + `(${redundant.scans.toLocaleString()} vs ${kept.scans.toLocaleString()} uses). `
        + `Removing it moves those lookups to ${kept.name}${larger}.`;
}
