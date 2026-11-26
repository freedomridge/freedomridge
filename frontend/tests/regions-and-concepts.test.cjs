// Run with: node --test tests/regions-and-concepts.test.cjs
/* eslint-disable @typescript-eslint/no-require-imports -- Uses the Node CommonJS loader to transpile local TS. */
// A named region's chip and a concept's chip in one view (lib/regions.ts, lib/filter-query.ts, lib/links.ts): two
// features that each gave its chip a text form. Together they go in one `q=` and come back as the same chips.
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
    conceptStage, isConceptScoreStage, isReadOnlyStage, isRegionStage, regionTestOf, withConceptChips, withoutMissingConcepts,
} = require('../lib/filter-query.ts');
const { conceptNames, stageOfFilter } = require('../lib/filter-utils.ts');
const { datasetLink, mayBeText, readLink } = require('../lib/links.ts');
const { regionTests } = require('../lib/regions.ts');

const region = { field: 'location', operator: 'geo_region', value: 'US-NE' };
const hide = { field: '_concept', operator: 'concept_exclude', value: { concept_id: 7, op: '>', threshold: 0.9 } };
const plain = { field: 'idx', operator: '>=', value: 3 };
const names = conceptNames([{ id: 7, name: 'equipment' }]);
// As the app holds them: the region and the plain filter as stages from a card or a link, the concept as the panel's chip.
const stages = [
    { type: 'filter', description: 'location within US-NE', filter: region },
    conceptStage('equipment', hide),
    { type: 'filter', description: 'idx ≥ 3', filter: plain },
];
// As POST filter_query/format writes them, in chip order.
const parts = ['location within "US-NE"', 'not concept "equipment" > 0.9', 'idx >= 3'];

test('each chip is its own kind: the region chip is no concept chip and the concept chip no region chip', () => {
    assert.deepEqual(stages.map(isRegionStage), [true, false, false]);
    assert.deepEqual(stages.map(isConceptScoreStage), [false, true, false]);
    assert.deepEqual(stages.map(isReadOnlyStage), [true, true, false]);
    assert.equal(regionTestOf(hide), null);
    assert.deepEqual(regionTests(stages.map(stage => stage.filter)), [{ key: 'US-NE', exclude: false }]);
});

test('a region chip, a concept chip and a plain filter are written as one q=', async () => {
    assert.equal(mayBeText(stages), true);
    const link = await datasetLink({ origin: 'http://x.test', datasetId: 5, stages, parts });
    const params = new URL(link).searchParams;
    assert.equal(params.get('q'), 'location within "US-NE" AND not concept "equipment" > 0.9 AND idx >= 3');
    assert.equal(params.get('f'), null);
    assert.equal(readLink(params).q, parts.join(' AND '));
    // A chip neither text form covers (a concept's sources) puts every stage, the region's too, in f=.
    const sources = { type: 'filter', description: 'Sources: equipment', filter: { field: 'id', operator: 'in', value: [1] }, metadata: { concept: true, mode: 'sources' } };
    assert.equal(mayBeText([...stages, sources]), false);
});

test('the q= read back (the server\'s filters, in order) is the same three chips', () => {
    const read = [region, hide, plain].map(filter => stageOfFilter(filter, names));
    assert.equal(isRegionStage(read[0]), true);
    assert.deepEqual(read[0].filter, region);
    assert.deepEqual(read[1], stages[1]);
    assert.deepEqual(read[2], { type: 'filter', description: 'idx ≥ 3', filter: plain });
});

test('the concept checks made when stages are opened leave the region chip alone', () => {
    assert.equal(withConceptChips(stages, names), stages);
    assert.deepEqual(withoutMissingConcepts(stages, names), { stages, dropped: [] });
    // With the concept gone, only its chip is left out.
    assert.deepEqual(withoutMissingConcepts(stages, conceptNames([])), { stages: [stages[0], stages[2]], dropped: ['Exclude: equipment'] });
});
