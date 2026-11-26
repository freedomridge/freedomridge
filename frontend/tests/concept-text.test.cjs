// Run with: node --test tests/concept-text.test.cjs
/* eslint-disable @typescript-eslint/no-require-imports -- Uses the Node CommonJS loader to transpile local TS. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { test } = require('node:test');
const ts = require('typescript');

require.extensions['.ts'] = (module, filename) => {
    const source = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
        compilerOptions: { module: ts.ModuleKind.CommonJS, esModuleInterop: true },
        fileName: filename,
    }).outputText;
    module._compile(source, filename);
};

const {
    conceptStage, filterHasConcept, isConceptFilter, isConceptScoreStage, isReadOnlyStage, missingConceptsNotice, needsConceptChips,
    withConceptChips, withoutMissingConcepts,
} = require('../lib/filter-query.ts');
const { conceptNames, describeFilter, isAutoFilterLabel, stageOfFilter } = require('../lib/filter-utils.ts');
const { decodeStages, mayBeText } = require('../lib/links.ts');
const FIXTURES = require('./link-fixtures.json');

const hide = { field: '_concept', operator: 'concept_exclude', value: { concept_id: 7, op: '>', threshold: 0.62 } };
const keep = { field: '_concept', operator: 'concept_select', value: { concept_id: 7, op: '<=', threshold: 0.3 } };
const split = { field: 'split', operator: '=', value: 'train' };
const names = conceptNames([{ id: 7, name: 'equipment' }, { id: 8, name: 'sky' }]);

test('a concept filter becomes the chip the Concepts panel makes, read-only, with its comparison', () => {
    assert.deepEqual(conceptStage('equipment', hide), {
        type: 'filter',
        description: 'Exclude: equipment',
        filter: hide,
        metadata: { concept: true, conceptId: 7, conceptName: 'equipment', op: '>', threshold: 0.62, mode: 'exclude' },
    });
    const only = conceptStage('equipment', keep);
    assert.equal(only.description, 'Only: equipment');
    assert.deepEqual(only.metadata, { concept: true, conceptId: 7, conceptName: 'equipment', op: '<=', threshold: 0.3, mode: 'select' });
    for (const stage of [conceptStage('equipment', hide), only]) {
        assert.equal(isReadOnlyStage(stage), true);
        assert.equal(isConceptScoreStage(stage), true);
    }
    // A filter written without a comparison compiles with `>`.
    assert.equal(conceptStage('equipment', { ...hide, value: { concept_id: 7, threshold: 0.5 } }).metadata.op, '>');
});

test('a concept filter is found on its own and inside a larger filter', () => {
    assert.equal(isConceptFilter(hide), true);
    assert.equal(isConceptFilter(keep), true);
    assert.equal(isConceptFilter(split), false);
    assert.equal(isConceptFilter(undefined), false);
    assert.equal(filterHasConcept({ logic: 'OR', filters: [split, { logic: 'NOT', filters: [keep] }] }), true);
    assert.equal(filterHasConcept({ logic: 'AND', filters: [split] }), false);
});

test('a concept in a filter is labelled with its name and comparison, by its id when the name is unknown', () => {
    assert.equal(describeFilter(hide, names), 'not concept equipment > 0.62');
    assert.equal(describeFilter(keep, names), 'concept equipment ≤ 0.3');
    assert.equal(describeFilter(hide), 'not concept #7 > 0.62');
    assert.equal(describeFilter({ logic: 'OR', filters: [split, keep] }, names), 'split = train or concept equipment ≤ 0.3');
    assert.equal(describeFilter({ logic: 'NOT', filters: [{ logic: 'AND', filters: [keep, split] }] }, names),
        'not (concept equipment ≤ 0.3; split = train)');
    assert.equal(isAutoFilterLabel('split = train or concept equipment ≤ 0.3', { logic: 'OR', filters: [split, keep] }, names), true);
});

test('a filter as a new chip: a concept on its own is the panel\'s chip, anything else an ordinary one', () => {
    assert.deepEqual(stageOfFilter(hide, names), conceptStage('equipment', hide));
    // A concept the list doesn't name stays an ordinary chip, labelled by its id.
    assert.deepEqual(stageOfFilter(hide, conceptNames([])), { type: 'filter', description: 'not concept #7 > 0.62', filter: hide });
    const mixed = { logic: 'OR', filters: [split, hide] };
    assert.deepEqual(stageOfFilter(mixed, names), { type: 'filter', description: 'split = train or not concept equipment > 0.62', filter: mixed });
    assert.deepEqual(stageOfFilter(split), { type: 'filter', description: 'split = train', filter: split });
});

test('a concept\'s exclude and show only chips may go in q=; its sources chip may not', () => {
    const plain = { type: 'filter', description: 'split = train', filter: split };
    assert.equal(mayBeText([plain, conceptStage('equipment', hide), conceptStage('sky', keep)]), true);
    const sources = {
        type: 'filter', description: 'Sources: equipment', filter: { field: 'id', operator: 'in', value: [1, 2] },
        metadata: { concept: true, conceptId: 7, conceptName: 'equipment', mode: 'sources' },
    };
    assert.equal(isConceptScoreStage(sources), false);
    assert.equal(mayBeText([plain, sources]), false);
});

test('a stage the app didn\'t make that is one concept test becomes the panel\'s chip, named as the concept is now', () => {
    // As the SDK's create_view(query=...) saves one: the part's text as its label, no metadata.
    const bare = { type: 'filter', description: 'not concept "old name" > 0.62', filter: hide };
    const marked = { ...bare, metadata: { defaultViewId: 4 } };
    const plain = { type: 'filter', description: 'split = train', filter: split };
    assert.equal(needsConceptChips([plain, bare]), true);
    const shown = withConceptChips([plain, bare, marked], names);
    assert.equal(shown[0], plain);
    assert.deepEqual(shown[1], conceptStage('equipment', hide));
    // Other metadata, such as the default view's mark, is kept.
    assert.deepEqual(shown[2], { ...conceptStage('equipment', hide), metadata: { ...conceptStage('equipment', hide).metadata, defaultViewId: 4 } });
    assert.equal(isReadOnlyStage(shown[1]), true);
    // A concept the dataset doesn't have stays as it is.
    assert.deepEqual(withConceptChips([bare], conceptNames([])), [bare]);
});

test('the panel\'s own chips, larger filters and other stages are left alone, the same array back', () => {
    const panel = conceptStage('renamed since', hide);
    const larger = { type: 'filter', description: 'mixed', filter: { logic: 'AND', filters: [hide] } };
    const sources = { type: 'filter', description: 'Sources: equipment', filter: { field: 'id', operator: 'in', value: [1] }, metadata: { concept: true, mode: 'sources' } };
    const stages = [panel, larger, sources];
    assert.equal(needsConceptChips(stages), false);
    assert.equal(withConceptChips(stages, names), stages);
});

test('every frozen f= link opens on exactly the stages it holds', async () => {
    // The fixtures' concept chip (concept 7, "blurry") was made by the panel: named or not, it is kept as it is.
    const all = conceptNames([{ id: 7, name: 'something else' }, { id: 3, name: 'x' }]);
    for (const fixture of FIXTURES.f) {
        const stages = await decodeStages(fixture.f);
        assert.equal(needsConceptChips(stages), false, fixture.name);
        assert.equal(withConceptChips(stages, all), stages, fixture.name);
    }
});

test('a stage naming a concept the dataset no longer has is left out, a larger filter holding one too, and named', () => {
    const plain = { type: 'filter', description: 'split = train', filter: split };
    const gone = { field: '_concept', operator: 'concept_exclude', value: { concept_id: 99, op: '>', threshold: 0.5 } };
    const panelChip = { ...conceptStage('scrap', gone), description: 'Exclude: scrap' };
    const larger = { type: 'filter', description: 'split = train or concept #99', filter: { logic: 'OR', filters: [split, gone] } };
    const kept = conceptStage('equipment', hide);
    const opened = withoutMissingConcepts([plain, panelChip, kept, larger], names);
    assert.deepEqual(opened, { stages: [plain, kept], dropped: ['Exclude: scrap', 'split = train or concept #99'] });
    // A concept id written as text is the compiler's number; a test with no id names nothing the dataset has.
    assert.equal(withoutMissingConcepts([{ ...plain, filter: { ...hide, value: { ...hide.value, concept_id: '7' } } }], names).dropped.length, 0);
    assert.deepEqual(withoutMissingConcepts([{ ...plain, filter: { ...hide, value: {} } }], names).dropped, ['split = train']);
    // Nothing to leave out: the same array.
    const stages = [plain, kept];
    assert.equal(withoutMissingConcepts(stages, names).stages, stages);
});

test('the notice names what was left out and where from', () => {
    assert.equal(missingConceptsNotice(['old link'], 'this link'),
        'From this link, “old link” was left out: it filters by a concept this dataset no longer has.');
    assert.equal(missingConceptsNotice(['a', 'b'], 'the view “Weeds”'),
        'From the view “Weeds”, “a”, “b” were left out: they filter by concepts this dataset no longer has.');
});

test('the frozen concept f= link keeps its chip while its concept exists, and is left out once it doesn\'t', async () => {
    const { stages } = FIXTURES.f.find((fixture) => fixture.name === 'concept filter');
    const decoded = await decodeStages(FIXTURES.f.find((fixture) => fixture.name === 'concept filter').f);
    assert.deepEqual(decoded, stages);
    assert.equal(withoutMissingConcepts(decoded, conceptNames([{ id: 7, name: 'blurry' }])).stages, decoded);
    assert.deepEqual(withoutMissingConcepts(decoded, conceptNames([{ id: 8, name: 'sky' }])), { stages: [], dropped: ['Exclude: blurry'] });
    // The other frozen links name no concept: nothing is ever left out of them.
    for (const fixture of FIXTURES.f.filter((item) => item.name !== 'concept filter')) {
        const opened = await decodeStages(fixture.f);
        assert.equal(withoutMissingConcepts(opened, conceptNames([])).stages, opened, fixture.name);
    }
});
