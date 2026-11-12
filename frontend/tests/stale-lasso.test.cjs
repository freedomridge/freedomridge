// Run with: node --test tests/stale-lasso.test.cjs
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

const { lassoIsStale, staleLasso, lassoKey, lassoFilter, STALE_LASSO_WARNING } = require('../lib/stale-lasso.ts');

const POLYGON = [[0, 0], [1, 0], [1, 1]];
const lasso = (layoutId, extra = {}) => ({
    field: 'clip', operator: 'embedding_polygon', value: POLYGON, ...(layoutId === undefined ? {} : { layout_id: layoutId }), ...extra,
});

test('a lasso is stale once its map has a layout it was not drawn on', () => {
    // id vs id
    assert.equal(lassoIsStale('a', 'a'), false);
    assert.equal(lassoIsStale('a', 'b'), true);
    // none vs none: a lasso saved before the upgrade, on a map not laid out again since
    assert.equal(lassoIsStale(undefined, undefined), false);
    assert.equal(lassoIsStale(null, null), false);
    assert.equal(lassoIsStale(undefined, null), false);
    // none vs id: the map was laid out again after the upgrade
    assert.equal(lassoIsStale(undefined, 'b'), true);
    assert.equal(lassoIsStale(null, 'b'), true);
    // id vs none: can't be told (a mirror that hasn't taken its primary's id yet)
    assert.equal(lassoIsStale('a', null), false);
    assert.equal(lassoIsStale('a', undefined), false);
});

test('a chip finds its stale lasso, nested or excluded, and nothing before the dataset answers', () => {
    const layouts = { clip: 'now', clip_patch_4x4: 'patch-now' };
    assert.equal(staleLasso(lasso('now'), layouts), null);
    assert.deepEqual(staleLasso(lasso('before'), layouts), lasso('before'));
    assert.deepEqual(staleLasso(lasso(undefined), layouts), lasso(undefined));
    assert.ok(staleLasso(lasso('before', { operator: 'embedding_polygon_exclude' }), layouts));
    assert.equal(staleLasso(lasso('before', { field: 'dino' }), layouts), null);  // a map with no id
    assert.ok(staleLasso(lasso('patch-before', { field: 'clip_patch_4x4' }), layouts));
    // Nested in a group, or in a cross-grain wrapper's value.
    assert.ok(staleLasso({ logic: 'AND', filters: [{ field: 'n', operator: '>', value: 1 }, lasso('before')] }, layouts));
    assert.ok(staleLasso({ field: '_cgself', operator: 'cg_self', value: { logic: 'AND', filters: [lasso('before')] } }, layouts));
    // Not a lasso, or the dataset's layouts not loaded yet.
    assert.equal(staleLasso({ field: 'n', operator: '>', value: 1, layout_id: 'x' }, layouts), null);
    assert.equal(staleLasso(lasso('before'), undefined), null);
    assert.equal(staleLasso(undefined, layouts), null);
});

test('one lasso is announced once, included or excluded, and each dataset on its own', () => {
    assert.equal(lassoKey(1, lasso('a')), lassoKey(1, lasso('a', { operator: 'embedding_polygon_exclude' })));
    assert.notEqual(lassoKey(1, lasso('a')), lassoKey(1, lasso('b')));
    assert.notEqual(lassoKey(1, lasso('a')), lassoKey(2, lasso('a')));
    assert.notEqual(lassoKey(1, lasso('a')), lassoKey(1, lasso('a', { value: [[0, 0], [2, 0], [2, 2]] })));
    assert.equal(lassoKey(1, lasso(undefined)), lassoKey(1, lasso(null)));
    assert.match(STALE_LASSO_WARNING, /laid out again after this selection was drawn/);
});

test('a new lasso records its layout and, until 2.12, the map model beside it', () => {
    // A follower on 2.10 or earlier refuses a relayed search holding a whole-image lasso without model_id on a
    // re-fitted map.
    assert.deepEqual(lassoFilter({ field: 'clip', polygon: POLYGON, layoutId: 'now', modelId: 'model' }), {
        field: 'clip', operator: 'embedding_polygon', value: POLYGON, layout_id: 'now', model_id: 'model',
    });
    // Drawn on a map with neither (not laid out again since the upgrade, never re-fitted): both null, and not stale.
    const plain = lassoFilter({ field: 'clip', polygon: POLYGON });
    assert.equal(plain.layout_id, null);
    assert.equal(plain.model_id, null);
    assert.equal(staleLasso(plain, {}), null);
});
