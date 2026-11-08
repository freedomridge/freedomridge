// Run with: node --test tests/region-exclude-grain.test.cjs
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

const { withGrain } = require('../contexts/FilterContext.tsx');

// An excluded region keeps the rows with no location or no point on its map. On a dataset with objects, the grids
// couple each filter across the grains (an image matches through one of its objects, an object through its parent
// image), and an object with no point on an image map would vacuously let its image through an excluded lasso. So
// an excluded region is coupled as NOT (the row, or its partner, is inside it).
const POLYGON = [[0, 0], [1, 0], [1, 1]];
const leaf = (operator, field) => ({ field, operator, value: POLYGON });

function notInside(node) {
    assert.equal(node.logic, 'NOT');
    const [either] = node.filters;
    assert.equal(either.logic, 'OR');
    return either.filters.map(arm => [arm.operator, arm.value.filters[0].operator]);
}

test('an excluded lasso or map region is coupled as NOT inside, on the image grid and the object grid', () => {
    for (const [exclude, include, field] of [
        ['embedding_polygon_exclude', 'embedding_polygon', 'clip'],
        ['embedding_polygon_exclude', 'embedding_polygon', 'clip_obj'],
        ['geo_polygon_exclude', 'geo_polygon', 'location'],
    ]) {
        for (const [grain, cross] of [['image', 'has_matching_object'], ['object', 'parent_matches']]) {
            const coupled = withGrain({ logic: 'AND', filters: [leaf(exclude, field)] }, grain, null, true);
            const last = coupled.filters[coupled.filters.length - 1];
            assert.deepEqual(notInside(last), [['cg_self', include], [cross, include]], `${exclude} ${field} on ${grain}`);
            // The exclude itself never reaches the server's cross-grain link.
            assert.equal(JSON.stringify(coupled).includes(exclude), false);
        }
    }
});

test('an included lasso is coupled as before, and a dataset without objects sends an exclude as it is', () => {
    const coupled = withGrain({ logic: 'AND', filters: [leaf('embedding_polygon', 'clip_obj')] }, 'image', null, true);
    assert.equal(coupled.filters.some(f => f.operator === 'has_matching_object' && f.field === '_crossemb'), true);
    assert.equal(JSON.stringify(coupled).includes('"NOT"'), false);
    const plain = withGrain({ logic: 'AND', filters: [leaf('geo_polygon_exclude', 'location')] }, 'image', null, false);
    assert.deepEqual(plain.filters[plain.filters.length - 1], leaf('geo_polygon_exclude', 'location'));
});

test('an excluded region with fewer than 3 points excludes nothing', () => {
    const line = { field: 'clip', operator: 'embedding_polygon_exclude', value: [[0, 0], [1, 1]] };
    const coupled = withGrain({ logic: 'AND', filters: [line] }, 'image', null, true);
    assert.equal(JSON.stringify(coupled).includes('embedding_polygon'), false);
    assert.equal(JSON.stringify(coupled).includes('"NOT"'), false);
});
