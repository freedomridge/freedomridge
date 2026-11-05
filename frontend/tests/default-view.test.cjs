// Run with: node --test tests/default-view.test.cjs
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

const { defaultViewStages, isDefaultViewStage, opensOnDefaultView } = require('../lib/default-view.ts');
const { QueryBuilderBar } = require('../components/query-builder-bar.tsx');

const NOT_BLACK = { field: '_concept', operator: 'concept_exclude', value: { concept_id: 3, threshold: 0.9 } };
const FIELD_ONLY = { field: 'split', operator: '==', value: 'train' };
const VIEW = {
    id: 7,
    name: 'Field images',
    slug: 'abc',
    content: {
        filters: { logic: 'AND', filters: [NOT_BLACK] },
        viewStages: [{ type: 'filter', description: 'Not black', filter: NOT_BLACK, metadata: { concept: true } }],
    },
};

test('the default view becomes its stages, each marked with the view', () => {
    const [stage] = defaultViewStages(VIEW);
    assert.equal(stage.description, 'Not black');
    assert.deepEqual(stage.metadata, { concept: true, defaultViewId: 7 });
    assert.equal(isDefaultViewStage(stage, VIEW), true);
    // A stage of an earlier default view, or any other stage, is not this one's.
    assert.equal(isDefaultViewStage(stage, { ...VIEW, id: 8 }), false);
    assert.equal(isDefaultViewStage({ type: 'filter', description: 'x', filter: FIELD_ONLY }, VIEW), false);
    assert.deepEqual(defaultViewStages(null), []);
});

test('a view saved with only its compiled filter becomes one stage named after the view', () => {
    const legacy = { ...VIEW, content: { filters: { logic: 'AND', filters: [FIELD_ONLY] } } };
    assert.deepEqual(defaultViewStages(legacy), [
        { type: 'filter', description: 'Field images', filter: legacy.content.filters, metadata: { defaultViewId: 7 } },
    ]);
    assert.deepEqual(defaultViewStages({ ...VIEW, content: { filters: { logic: 'AND', filters: [] } } }), []);
});

test('the dataset opens on its default view only without kept filters and unless the viewer saw past it', () => {
    const own = [{ type: 'filter', description: 'mine', filter: FIELD_ONLY }];
    assert.equal(opensOnDefaultView([], null, VIEW), true);
    assert.equal(opensOnDefaultView(own, null, VIEW), false);
    assert.equal(opensOnDefaultView([], 7, VIEW), false);
    assert.equal(opensOnDefaultView([], 6, VIEW), true);  // an earlier default view was dismissed, not this one
    assert.equal(opensOnDefaultView([], null, null), false);
});

function bar(stages, defaultView) {
    return renderToStaticMarkup(React.createElement(QueryBuilderBar, {
        stages, defaultView, onShowAll: () => {}, onClearAll: () => {}, onRemoveStage: () => {}, onUpdateStage: () => {},
        onAddStage: () => {}, datasetId: 1, schema: [], lightningMode: true, onToggleLightningMode: () => {},
    }));
}

test('the filter bar labels the default view and offers the way to every image and back', () => {
    const defaults = defaultViewStages(VIEW);
    const own = { type: 'filter', description: 'split = train', filter: FIELD_ONLY };

    const opened = bar(defaults, VIEW);
    assert.match(opened, /Default view: Field images/);
    assert.match(opened, /★/);
    assert.match(opened, />Show all images</);
    assert.doesNotMatch(opened, />Clear All</);  // nothing of the viewer's own to clear

    const narrowed = bar([...defaults, own], VIEW);
    assert.match(narrowed, />Clear All</);
    assert.match(narrowed, />Show all images</);

    const everything = bar([], VIEW);
    assert.doesNotMatch(everything, /Default view: /);
    assert.match(everything, />Default view</);
    assert.doesNotMatch(everything, />Clear All<|>Show all images</);

    // One of two default-view filters removed: the default view is one click away.
    const pair = { ...VIEW, content: { ...VIEW.content, viewStages: [...VIEW.content.viewStages, own] } };
    const [first] = defaultViewStages(pair);
    const partial = bar([first], pair);
    assert.match(partial, />Default view</);
    assert.match(partial, />Show all images</);
    assert.doesNotMatch(bar(defaultViewStages(pair), pair), />Default view</);

    // Without a default view, as before: Clear All with filters, nothing without.
    assert.match(bar([own], null), />Clear All</);
    assert.doesNotMatch(bar([own], null), />Show all images<|>Default view</);
    assert.doesNotMatch(bar([], null), />Clear All<|>Default view</);
});
