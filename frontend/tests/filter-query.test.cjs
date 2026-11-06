// Run with: node --test tests/filter-query.test.cjs
/* eslint-disable @typescript-eslint/no-require-imports -- Uses the Node CommonJS loader to transpile local TS/TSX. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const { test } = require('node:test');
const ts = require('typescript');

const root = path.resolve(__dirname, '..');
const originalResolve = Module._resolveFilename;
Module._resolveFilename = function (specifier, ...args) {
    if (specifier.startsWith('@/')) specifier = path.join(root, specifier.slice(2));
    return originalResolve.call(this, specifier, ...args);
};
for (const extension of ['.ts', '.tsx']) {
    require.extensions[extension] = (module, filename) => {
        const source = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
            compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
            fileName: filename,
        }).outputText;
        module._compile(source, filename);
    };
}

const { isKeptStage, queryEditorText, stagesFromQuery, offsetOf } = require('../lib/filter-query.ts');

const SPLIT = { type: 'filter', description: 'My split', filter: { field: 'split', operator: '==', value: 'train' } };
const DEFAULT = {
    type: 'filter',
    description: 'Clean images',
    filter: { field: 'score', operator: '>', value: 0.2 },
    metadata: { defaultViewId: 7 },
};
const CONCEPT = {
    type: 'filter',
    description: 'Not black',
    filter: { field: '_concept', operator: 'concept_exclude', value: { concept_id: 3, threshold: 0.9 } },
    metadata: { concept: true },
};
const REGION = { type: 'map_region', description: 'Map region', filter: { field: 'location', operator: 'geo_polygon', value: [] } };
const STAGES = [SPLIT, CONCEPT, DEFAULT, REGION];
// What the server writes for them: no text for the concept or the region.
const PARTS = ['split = "train"', null, 'score > 0.2', 'location geo_polygon'];

test('the editor shows each chip as a part, and the chips it keeps as comments', () => {
    assert.equal(
        queryEditorText(STAGES, PARTS),
        '# kept: Not black\n# kept: Map region\nsplit = "train"\nAND score > 0.2',
    );
    assert.equal(queryEditorText([], []), '');
    // A read-only chip is kept even when the server could write it.
    assert.equal(isKeptStage(REGION, 'location geo_polygon'), true);
    assert.equal(isKeptStage(SPLIT, 'split = "train"'), false);
    assert.equal(isKeptStage(SPLIT, null), true);
});

test('applying keeps the uneditable chips, reuses unchanged ones and makes the rest new', () => {
    const parsed = {
        filters: [
            { field: 'score', operator: '>', value: 0.2 },
            { field: 'serial', operator: 'in', value: ['103000752', '103001025'] },
        ],
        parts: ['score > 0.2', 'serial in ("103000752", "103001025")'],
        reuse: [2, null],  // the default view's chip, unchanged; the split chip was deleted from the text
    };
    const stages = stagesFromQuery(STAGES, PARTS, parsed);
    assert.equal(stages.length, 4);
    // Every chip that stays keeps its place; the new one comes last.
    assert.equal(stages[0], CONCEPT);
    assert.equal(stages[1], DEFAULT);  // the same object: its label and its default-view mark stay
    assert.equal(stages[2], REGION);
    assert.deepEqual(stages[3], {
        type: 'filter',
        description: 'serial: 103000752, 103001025',
        filter: parsed.filters[1],
    });
    // A reuse index pointing at a kept chip is never taken as an edit of it.
    const odd = stagesFromQuery(STAGES, PARTS, { filters: [parsed.filters[0]], parts: ['x'], reuse: [1] });
    assert.deepEqual(odd.slice(0, 2), [CONCEPT, REGION]);
    assert.equal(odd[2].type, 'filter');
    assert.notEqual(odd[2], CONCEPT);
});

test('applying the text unchanged changes nothing', () => {
    const unchanged = { filters: [SPLIT.filter, DEFAULT.filter], parts: [PARTS[0], PARTS[2]], reuse: [0, 2] };
    assert.deepEqual(stagesFromQuery(STAGES, PARTS, unchanged), STAGES);
    stagesFromQuery(STAGES, PARTS, unchanged).forEach((stage, index) => assert.equal(stage, STAGES[index]));
});

test('a mistake\'s line and column become a cursor position', () => {
    const text = 'split = train\nAND seriel in (1 2)';
    assert.equal(text.slice(offsetOf(text, 2, 5)), 'seriel in (1 2)');
    assert.equal(offsetOf(text, 1, 1), 0);
    assert.equal(offsetOf(text, 9, 9), text.length);
});
