// Run with: node --test tests/wrapped-region-exclude.test.cjs
/* eslint-disable @typescript-eslint/no-require-imports -- Uses the Node CommonJS loader to transpile local TS/TSX. */
// A filter card saved from its Builder tab wraps a lone rule in an AND group. An excluded place saved that way is the
// same filter and the same chip ("Not in Kansas") as the bare one, so the page couples it across the grains the same
// way: NOT (the row or its partner is inside). A group of two rules, an OR group and a NOT stay filters like the others.
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
const { regionTestOf, soleRule } = require('../lib/filter-query.ts');
const { toggledRegionFilter } = require('../lib/regions.ts');

const notKansas = { field: 'location', operator: 'geo_region_exclude', value: 'US-KS' };
const kansas = { ...notKansas, operator: 'geo_region' };
const polygon = { field: 'location', operator: 'geo_polygon_exclude', value: [[0, 0], [1, 0], [1, 1]] };
const lasso = { field: 'clip_img', operator: 'embedding_polygon_exclude', value: [[0, 0], [1, 0], [1, 1]] };
const crop = { field: 'crop', operator: '=', value: 'wheat' };
const and = (...filters) => ({ logic: 'AND', filters });
const grain = (stages, which = 'image') => withGrain(and(...stages), which, null, true);

test('soleRule sees through AND groups of one rule, however deep, and never through an OR, a NOT or two rules', () => {
    assert.deepEqual(soleRule(notKansas), notKansas);
    assert.deepEqual(soleRule(and(notKansas)), notKansas);
    assert.deepEqual(soleRule(and(and(notKansas))), notKansas);
    for (const kept of [{ logic: 'OR', filters: [notKansas] }, { logic: 'NOT', filters: [kansas] }, and(notKansas, crop)]) {
        assert.deepEqual(soleRule(kept), kept);
        assert.deepEqual(soleRule(and(kept)), kept);
    }
    assert.equal(soleRule(undefined), undefined);
});

test('a wrapped excluded place is coupled as the bare one is, on the image grid and the object grid', () => {
    for (const rule of [notKansas, polygon, lasso]) {
        for (const which of ['image', 'object']) {
            const bare = grain([crop, rule], which);
            assert.deepEqual(grain([crop, and(rule)], which), bare, `${rule.operator} on the ${which} grid`);
            assert.deepEqual(grain([crop, and(and(rule))], which), bare, `${rule.operator} twice wrapped`);
        }
    }
    // A polygon of fewer than 3 points excludes nothing, wrapped or not.
    const short = { ...polygon, value: [[0, 0], [1, 1]] };
    assert.deepEqual(grain([crop, and(short)]), grain([crop, short]));
    assert.deepEqual(grain([crop, and(short)]), grain([crop]));
});

test('an excluded place in a group of two rules, in an OR, or a NOT around a place stays a filter like the others', () => {
    for (const stage of [and(notKansas, crop), { logic: 'OR', filters: [notKansas] }, { logic: 'NOT', filters: [kansas] }]) {
        const filter = grain([stage]);
        const coupled = filter.filters[1];
        assert.equal(filter.filters.length, 2, JSON.stringify(filter));
        assert.equal(coupled.logic, 'OR');
        assert.deepEqual(coupled.filters.map(arm => arm.value), [and(stage), and(stage)]);
    }
});

test('the chip of a twice wrapped place is the region chip, and turning it around keeps the wrapping', () => {
    assert.deepEqual(regionTestOf(and(and(notKansas))), { key: 'US-KS', exclude: true });
    assert.deepEqual(toggledRegionFilter(and(and(notKansas))), and(and(kansas)));
    assert.equal(regionTestOf({ logic: 'OR', filters: [notKansas] }), null);
});
