// The filter bar's query editor (docs/FILTER_QUERY.md): the chips as text, and text back to chips. The server
// parses and writes the text; this keeps the chips the text can't hold, and the chips it didn't change.
import type { ViewStage } from './api';
import type { ParsedFilterQuery } from './api';
import { describeFilter } from './filter-utils';

/** A chip the bar never lets anyone edit: a map region, a similarity search, a selection, a concept score, a
 * duplicate collapse. */
export function isReadOnlyStage(stage: ViewStage): boolean {
    return stage.type === 'map_region' || stage.type === 'similarity' || stage.type === 'selection'
        || !!stage.metadata?.collapse || !!stage.metadata?.dupGroupCollapse || !!stage.metadata?.concept;
}

/** A chip the query editor leaves as it is: read-only, or with no text form (`part` is null). */
export function isKeptStage(stage: ViewStage, part: string | null | undefined): boolean {
    return isReadOnlyStage(stage) || part == null;
}

/** The editor's text: one line per chip, joined with AND, after a comment line for each chip it keeps. */
export function queryEditorText(stages: ViewStage[], parts: (string | null)[]): string {
    const kept = stages
        .filter((stage, index) => isKeptStage(stage, parts[index]))
        .map(stage => `# kept: ${stage.description.replace(/\s+/g, ' ')}`);
    const text = stages
        .map((stage, index) => (isKeptStage(stage, parts[index]) ? null : parts[index]))
        .filter((part): part is string => part != null)
        .join('\nAND ');
    return [...kept, text].filter(Boolean).join('\n');
}

/** The chips after applying the editor's text. Kept chips stay where they were, and so does a chip equal to
 * a part of the query, with its label and its default-view mark; the other parts become new chips at the end.
 * So applying unchanged text changes nothing. `parts` says which chips are kept: what the server wrote for the
 * current chips just now. */
export function stagesFromQuery(stages: ViewStage[], parts: (string | null)[], parsed: ParsedFilterQuery): ViewStage[] {
    const reused = new Set<number>();
    const fresh: ViewStage[] = [];
    parsed.filters.forEach((filter, index) => {
        const same = parsed.reuse[index];
        if (same != null && stages[same] && !isKeptStage(stages[same], parts[same]) && !reused.has(same)) {
            reused.add(same);
        } else {
            fresh.push({ type: 'filter', description: describeFilter(filter), filter });
        }
    });
    const staying = stages.filter((stage, index) => isKeptStage(stage, parts[index]) || reused.has(index));
    return [...staying, ...fresh];
}

/** The character offset of a 1-based line and column in `text`, to put the cursor on a mistake. */
export function offsetOf(text: string, line: number, column: number): number {
    const lines = text.split('\n');
    const before = lines.slice(0, Math.max(0, line - 1)).reduce((sum, item) => sum + item.length + 1, 0);
    return Math.min(text.length, before + Math.max(0, column - 1));
}
