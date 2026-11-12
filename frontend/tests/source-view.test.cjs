// Run with: node --test tests/source-view.test.cjs
/* eslint-disable @typescript-eslint/no-require-imports -- Uses the Node CommonJS loader to transpile local TS/TSX. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const { test } = require('node:test');
const ts = require('typescript');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');

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

const { mayUpdateView, sameStages, sourceOf, stagesToSave } = require('../lib/source-view.ts');
const { defaultViewStages } = require('../lib/default-view.ts');
const { STALE_LASSO_WARNING_IN_VIEW } = require('../lib/stale-lasso.ts');
const { QueryBuilderBar } = require('../components/query-builder-bar.tsx');

const LASSO = {
    type: 'filter',
    description: 'clip lasso',
    filter: { field: 'clip', operator: 'embedding_polygon', value: [[0, 0], [1, 0], [1, 1]], layout_id: 'old' },
    metadata: { source: 'embeddings' },
};
const SPLIT = { type: 'filter', description: 'split = train', filter: { field: 'split', operator: '=', value: 'train' } };

test('the applied stages are the view\'s until a filter in them changes', () => {
    // As the database gives the view back (JSONB): its keys in another order.
    const fromDatabase = JSON.parse(JSON.stringify([
        { metadata: { source: 'embeddings' }, filter: { value: LASSO.filter.value, layout_id: 'old', operator: 'embedding_polygon', field: 'clip' }, description: 'clip lasso', type: 'filter' },
        SPLIT,
    ]));
    assert.ok(sameStages(fromDatabase, [LASSO, SPLIT]));
    assert.ok(sameStages([{ ...SPLIT, metadata: undefined }], [SPLIT]));  // a key left undefined isn't saved
    // Redrawn on the map as it is now, another filter, or the same ones in another order: they differ.
    const redrawn = { ...LASSO, filter: { ...LASSO.filter, value: [[0, 0], [2, 0], [2, 2]], layout_id: 'now' } };
    assert.ok(!sameStages([redrawn, SPLIT], [LASSO, SPLIT]));
    assert.ok(!sameStages([LASSO], [LASSO, SPLIT]));
    assert.ok(!sameStages([SPLIT, LASSO], [LASSO, SPLIT]));
    assert.ok(!sameStages([{ ...SPLIT, description: 'train only' }], [SPLIT]));
});

test('the default view\'s mark on its stages is applied, not saved', () => {
    const view = { id: 4, name: 'Train', slug: 'train', content: { filters: {}, viewStages: [LASSO, SPLIT] } };
    const applied = defaultViewStages(view);
    assert.equal(applied[0].metadata.defaultViewId, 4);
    assert.ok(sameStages(applied, view.content.viewStages));
    assert.deepEqual(stagesToSave(applied), [LASSO, SPLIT]);  // other metadata stays
});

test('its owner or an admin may update a view; nobody may update a mirror\'s copy of the primary\'s', () => {
    const own = sourceOf({ id: 1, slug: 'a', name: 'A', user_id: 7 }, [SPLIT]);
    assert.ok(mayUpdateView(own, { id: 7, role: 'power_user' }));
    assert.ok(mayUpdateView(own, { id: 8, role: 'admin' }));
    assert.ok(!mayUpdateView(own, { id: 8, role: 'power_user' }));
    assert.ok(!mayUpdateView(own, null));
    assert.ok(!mayUpdateView(null, { id: 7, role: 'admin' }));
    const copy = sourceOf({ id: 2, slug: 'b', name: 'B', user_id: null, from_primary: true }, [SPLIT]);
    assert.ok(!mayUpdateView(copy, { id: 8, role: 'admin' }));
    // The updated_at it was opened with goes with an update (null: not changed since 2.11).
    assert.equal(own.updatedAt, null);
    assert.equal(sourceOf({ id: 1, slug: 'a', name: 'A', updated_at: '2026-11-11T12:00:00.5+00:00' }, []).updatedAt, '2026-11-11T12:00:00.5+00:00');
});

test('a stale lasso\'s chip says to update the view when the viewer may', () => {
    const chip = canUpdateView => renderToStaticMarkup(React.createElement(QueryBuilderBar, {
        stages: [LASSO], mapLayouts: { clip: 'now' }, canUpdateView, onShowAll: () => {}, onClearAll: () => {},
        onRemoveStage: () => {}, onUpdateStage: () => {}, onAddStage: () => {}, datasetId: 1, schema: [],
    }));
    assert.match(STALE_LASSO_WARNING_IN_VIEW, /Redraw it or remove it, then use Update this view to save it\.$/);
    const title = html => html.match(/data-stale-lasso="true"[^]*?title="([^"]*)"/)[1].replace(/&#x27;/g, "'");
    assert.equal(title(chip(true)), STALE_LASSO_WARNING_IN_VIEW);
    assert.match(title(chip(false)), /Redraw it or remove it\.$/);
});
