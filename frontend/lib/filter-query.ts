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

/** A named region test (docs/REGIONS.md): keep (geo_region) or remove (geo_region_exclude) the samples located in `key`. */
export interface RegionTest {
    key: string;
    exclude: boolean;
}

/**
 * The rule a filter comes down to: itself, or the one rule of an AND group around it, however deeply wrapped (a filter
 * card saved from its Builder tab wraps a lone rule in an AND group). An OR or a NOT is never seen through.
 */
export function soleRule(filter: ComplexFilter | undefined): ComplexFilter | undefined {
    let node = filter;
    while (node?.logic === 'AND' && node.filters?.length === 1) node = node.filters[0];
    return node;
}

/** The region test a stage's filter is when it is one on its own, or the one rule of AND groups around it; else null. */
export function regionTestOf(filter: ComplexFilter | undefined): RegionTest | null {
    const node = soleRule(filter);
    if (!node || node.logic || node.field !== 'location' || typeof node.value !== 'string') return null;
    if (node.operator !== 'geo_region' && node.operator !== 'geo_region_exclude') return null;
    return { key: node.value, exclude: node.operator === 'geo_region_exclude' };
}

/** A named region, included or excluded (lib/regions.ts): its chip only turns it around, and it has a text form. */
export function isRegionStage(stage: ViewStage): boolean {
    return regionTestOf(stage.filter) !== null;
}

/** A chip the bar never lets anyone edit: a map region, a named region, an embeddings lasso, a similarity search, a
 * selection, a concept score, a duplicate collapse, the objects with no image. */
export function isReadOnlyStage(stage: ViewStage): boolean {
    return stage.type === 'map_region' || stage.type === 'similarity' || stage.type === 'selection' || isLassoStage(stage)
        || isRegionStage(stage) || !!stage.metadata?.collapse || !!stage.metadata?.dupGroupCollapse || !!stage.metadata?.concept
        || !!stage.metadata?.unlinked;
}

/** A concept's score filter: it keeps (concept_select) or hides (concept_exclude) the samples whose score passes. */
export function isConceptFilter(f: ComplexFilter | undefined): boolean {
    return f?.operator === 'concept_select' || f?.operator === 'concept_exclude';
}

/** Whether the filter tests a concept's score anywhere in it. */
export function filterHasConcept(f: ComplexFilter | undefined): boolean {
    if (!f) return false;
    if (isConceptFilter(f)) return true;
    return (f.filters || []).some(filterHasConcept);
}

/** A concept's exclude or show only chip, from the Concepts panel or as text: read-only, but it has a text form. */
export function isConceptScoreStage(stage: ViewStage): boolean {
    return !!stage.metadata?.concept && isConceptFilter(stage.filter);
}

/** The words of a concept's score filter: the Concepts panel's chip label. */
export function conceptLabel(operator: ComplexFilter['operator'], name: string): string {
    return `${operator === 'concept_select' ? 'Only' : 'Exclude'}: ${name}`;
}

/**
 * The chip of a concept's score filter, {field: '_concept', operator, value: {concept_id, op, threshold}}, as the
 * Concepts panel makes it. A concept part of a `q=` link or of a filter card's text gets this chip too, so the panel
 * knows it as its concept's, with the same label and the same exclude and show only.
 */
export function conceptStage(name: string, filter: ComplexFilter): ViewStage {
    const { concept_id: conceptId, op = '>', threshold } = filter.value ?? {};
    return {
        type: 'filter',
        description: conceptLabel(filter.operator, name),
        filter,
        metadata: {
            concept: true, conceptId, conceptName: name, op, threshold,
            mode: filter.operator === 'concept_select' ? 'select' : 'exclude',
        },
    };
}

// A stage that is one concept test but not the Concepts panel's chip: made outside the app, such as by the SDK's
// create_view(query=...) or on the server.
function bareConceptStage(stage: ViewStage): boolean {
    return stage.type === 'filter' && !stage.metadata?.concept && isConceptFilter(stage.filter);
}

/** Whether withConceptChips would change any of the stages (it then needs the concepts' names). */
export function needsConceptChips(stages: ViewStage[]): boolean {
    return stages.some(bareConceptStage);
}

/**
 * Stages the app didn't make, with each one that is a concept test on its own made the Concepts panel's chip
 * (conceptStage), named from `concepts` as the concept is now, its other metadata (the default view's mark) kept. A
 * chip the panel made, any other stage, and a concept `concepts` doesn't name stay as they are; with none to change,
 * the same array comes back.
 */
export function withConceptChips(stages: ViewStage[], concepts: ReadonlyMap<number, string>): ViewStage[] {
    if (!needsConceptChips(stages)) return stages;
    return stages.map(stage => {
        const name = bareConceptStage(stage) ? concepts.get(stage.filter.value?.concept_id) : undefined;
        if (name === undefined) return stage;
        const chip = conceptStage(name, stage.filter);
        return { ...chip, metadata: { ...stage.metadata, ...chip.metadata } };
    });
}

// The concept ids a filter's concept tests name, anywhere in it.
function conceptIdsOf(f: ComplexFilter | undefined): unknown[] {
    if (!f) return [];
    return [...(isConceptFilter(f) ? [f.value?.concept_id] : []), ...(f.filters || []).flatMap(conceptIdsOf)];
}

/**
 * The stages without each one that names a concept `concepts` doesn't have (deleted since the link or view was made):
 * its test would keep every sample or none, and the server refuses it. A concept test inside a larger filter takes its
 * whole stage with it, as what is left would filter differently from what the stage says. `dropped` holds the
 * labels of the stages left out, the only names left for them; with none left out, the same array comes back.
 */
export function withoutMissingConcepts(stages: ViewStage[], concepts: ReadonlyMap<number, string>): { stages: ViewStage[]; dropped: string[] } {
    const missing = (stage: ViewStage) => conceptIdsOf(stage.filter).some(id => !concepts.has(Number(id)));
    if (!stages.some(missing)) return { stages, dropped: [] };
    return { stages: stages.filter(stage => !missing(stage)), dropped: stages.filter(missing).map(stage => stage.description) };
}

/** What the reader is told about the stages withoutMissingConcepts left out of `from` (this link, the view “…”). */
export function missingConceptsNotice(dropped: string[], from: string): string {
    const names = dropped.map(label => `“${label}”`).join(', ');
    return dropped.length === 1
        ? `From ${from}, ${names} was left out: it filters by a concept this dataset no longer has.`
        : `From ${from}, ${names} were left out: they filter by concepts this dataset no longer has.`;
}

// A field value the server kept from the assistant still sealed (`pv1.…`): one sealed under an earlier key, which no
// one can open any more. A filter on it would match nothing.
const isSealed = (value: unknown) => typeof value === 'string' && value.startsWith('pv1.');

// The fields of `f` tested against a sealed value, in order.
function sealedFields(f: ComplexFilter | undefined): string[] {
    if (!f) return [];
    const own = f.field && (isSealed(f.value) || (Array.isArray(f.value) && f.value.some(isSealed))) ? [f.field] : [];
    return [...own, ...(f.filters ?? []).flatMap(sealedFields)];
}

/**
 * Stages from an assistant's working view (a `w=` link) without those that test a field against a value still sealed,
 * which can't be read any more: `stages` the rest, `fields` the fields of those left out, for the notice.
 */
export function withoutSealedValues(stages: ViewStage[]): { stages: ViewStage[]; fields: string[] } {
    const sealed = stages.map(stage => sealedFields(stage.filter));
    if (!sealed.some(fields => fields.length)) return { stages, fields: [] };
    return { stages: stages.filter((_, i) => !sealed[i].length), fields: [...new Set(sealed.flat())] };
}

/** What the reader is told about the stages withoutSealedValues left out of `from` (this link). */
export function sealedValuesNotice(fields: string[], from: string): string {
    const names = fields.map(field => `“${field}”`).join(', ');
    return fields.length === 1
        ? `From ${from}, the filter on ${names} was left out: its values can no longer be read. The assistant can make the view again.`
        : `From ${from}, the filters on ${names} were left out: their values can no longer be read. The assistant can make the view again.`;
}

/** The character offset of a 1-based line and column in `text`, to put the cursor on a mistake. */
export function offsetOf(text: string, line: number, column: number): number {
    const lines = text.split('\n');
    const before = lines.slice(0, Math.max(0, line - 1)).reduce((sum, item) => sum + item.length + 1, 0);
    return Math.min(text.length, before + Math.max(0, column - 1));
}
