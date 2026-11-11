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

const { expiredSelection, isLassoStage, isReadOnlyStage, offsetOf } = require('../lib/filter-query.ts');

const SPLIT = { type: 'filter', description: 'My split', filter: { field: 'split', operator: '==', value: 'train' } };
const REGION = { type: 'map_region', description: 'Map region', filter: { field: 'location', operator: 'geo_polygon', value: [] } };
const LASSO = { type: 'filter', description: 'Embeddings Region', filter: { field: 'clip', operator: 'embedding_polygon', value: [[0, 0], [1, 0], [1, 1]] } };
const CONCEPT = {
    type: 'filter',
    description: 'Not black',
    filter: { field: '_concept', operator: 'concept_exclude', value: { concept_id: 3, threshold: 0.9 } },
    metadata: { concept: true },
};

test('a lasso, included or excluded, has no card to edit; a plain filter does', () => {
    const excluded = { ...LASSO, filter: { ...LASSO.filter, operator: 'embedding_polygon_exclude' } };
    assert.equal(isLassoStage(LASSO), true);
    assert.equal(isLassoStage(excluded), true);
    assert.equal(isLassoStage(REGION), false);
    for (const stage of [LASSO, excluded, REGION, CONCEPT]) assert.equal(isReadOnlyStage(stage), true);
    assert.equal(isReadOnlyStage(SPLIT), false);
});

test('a mistake\'s line and column become a cursor position', () => {
    const text = 'split = train\nAND seriel in (1 2)';
    assert.equal(text.slice(offsetOf(text, 2, 5)), 'seriel in (1 2)');
    assert.equal(offsetOf(text, 1, 1), 0);
    assert.equal(offsetOf(text, 9, 9), text.length);
});

test('an expired selection keeps the chip\'s words and count, never its ids or token', () => {
    const token = {
        type: 'selection', description: '60000 Selected',
        filter: { logic: 'AND', filters: [{ field: 'id', operator: 'in_selection', value: 'tok-123' }] },
        metadata: { count: 60000, source: 'Manual', token: 'tok-123' },
    };
    const ids = Array.from({ length: 50001 }, (_, i) => 900000 + i);
    const picked = {
        type: 'selection', description: 'Exclude: Map (50001 Selected)',
        filter: { field: 'id', operator: 'not in', value: ids }, metadata: { count: 50001, source: 'Map' },
    };
    assert.deepEqual(expiredSelection(token), { expired: true, description: '60000 Selected', count: 60000 });
    assert.deepEqual(expiredSelection(picked), { expired: true, description: 'Exclude: Map (50001 Selected)', count: 50001 });
    assert.deepEqual(expiredSelection({ ...picked, metadata: undefined }).count, null);
    for (const stub of [expiredSelection(token), expiredSelection(picked)]) {
        assert.equal(JSON.stringify(stub).includes('tok-123'), false);
        assert.equal(JSON.stringify(stub).includes('900000'), false);
        assert.equal('filter' in stub, false);
    }
});
