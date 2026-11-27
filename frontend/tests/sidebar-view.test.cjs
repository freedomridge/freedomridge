// Run with: node --test tests/sidebar-view.test.cjs
/* eslint-disable @typescript-eslint/no-require-imports -- Uses the Node CommonJS loader to transpile local TS/TSX. */
// The one view every reader of the filter sidebar is given (sidebarView, over withGrain): the applied stages and the
// pending selections, and for the value picker the other fields' ticks, all inside the image grain's wrapping, so the
// kind is at the top of the AND. On a dataset without objects the list's view is the one the page sent before, part
// for part.
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

const { sidebarView, withGrain } = require('../contexts/FilterContext.tsx');
const { convertSimpleFiltersToComplex } = require('../lib/filter-utils.ts');

const IMAGES = { field: 'media_type', operator: '!=', value: 'object' };
const SPLIT = { field: 'split', operator: '=', value: 'train' };
const APPLIED = { logic: 'AND', filters: [SPLIT] };
const POLYGON = { field: 'location', operator: 'geo_polygon', value: [[0, 0], [1, 0], [1, 1]] };
const OBJECT_LASSO = { field: 'clip_obj', operator: 'embedding_polygon', value: [[0, 0], [1, 0], [1, 1]], layout_id: null, model_id: null };

test('without objects, the list reads the view the page sent before, part for part', () => {
    const before = withGrain(APPLIED, 'image', null, false);
    assert.deepEqual(sidebarView(APPLIED, [], null, false), before);
    // The pending selections came after the grain-wrapped stages.
    assert.deepEqual(sidebarView(APPLIED, [POLYGON], null, false), { logic: 'AND', filters: [...before.filters, POLYGON] });
    // Lightning mode: no pending selections, the image grain's view itself.
    assert.deepEqual(sidebarView(APPLIED, [], null, true), withGrain(APPLIED, 'image', null, true));
});

test('with objects, the pending selections go inside the wrapping, a lasso of the objects coupled to their images', () => {
    const view = sidebarView(APPLIED, [OBJECT_LASSO], null, true);
    assert.deepEqual(view, withGrain({ logic: 'AND', filters: [SPLIT, OBJECT_LASSO] }, 'image', null, true));
    assert.deepEqual(view.filters[0], IMAGES);
    assert.ok(view.filters.some((part) => part.operator === 'has_matching_object' && part.field === '_crossemb'));
    // Nothing applied, only the lasso: the kind and the lasso's coupling, nothing beside them unwrapped.
    const alone = sidebarView({ logic: 'AND', filters: [] }, [OBJECT_LASSO], null, true);
    assert.deepEqual(alone.filters.map((part) => part.field), ['media_type', '_crossemb']);
});

test("the picker's view has the other fields' ticks inside the wrapping, one tick or several", () => {
    const one = convertSimpleFiltersToComplex({ colour: ['red'] });
    assert.deepEqual(
        sidebarView(APPLIED, [], one, true),
        withGrain({ logic: 'AND', filters: [SPLIT, { field: 'colour', operator: 'in', value: ['red'] }] }, 'image', null, true),
    );
    const several = convertSimpleFiltersToComplex({ colour: ['red'], label: { _op: 'text', values: ['car'], contains: ['tr'] } });
    assert.deepEqual(sidebarView(APPLIED, [], several, true), withGrain({ logic: 'AND', filters: [SPLIT, ...several.filters] }, 'image', null, true));
    // No ticks: no empty group, the list's view.
    assert.deepEqual(sidebarView(APPLIED, [], convertSimpleFiltersToComplex({}), true), sidebarView(APPLIED, [], null, true));
    // Without objects the ticks come after the stages in the plain AND.
    assert.deepEqual(sidebarView(APPLIED, [], one, false).filters, [IMAGES, SPLIT, one]);
});

// What the page gives it (page.tsx): the list and the range all four pending selections, in the order the page always
// sent them; the picker what the grid previews (the brushed set, the polygon, the lasso) and not the ticked grid rows.
const BRUSHED = { field: 'id', operator: 'in', value: [11, 12, 13] };
const TICKED_ROWS = { field: 'id', operator: 'in', value: [12] };
const IMAGE_LASSO = { field: 'clip', operator: 'embedding_polygon', value: [[0, 0], [2, 0], [2, 2]], layout_id: null, model_id: null };
const LIST_PENDING = [BRUSHED, TICKED_ROWS, POLYGON, IMAGE_LASSO];
const PICKER_PENDING = [BRUSHED, POLYGON, IMAGE_LASSO];

test('without objects, the list reads the four pending selections part for part as before; the picker not the ticked rows', () => {
    const before = withGrain(APPLIED, 'image', null, false);
    assert.deepEqual(sidebarView(APPLIED, LIST_PENDING, null, false), { logic: 'AND', filters: [...before.filters, ...LIST_PENDING] });
    const one = convertSimpleFiltersToComplex({ colour: ['red'] });
    const picker = sidebarView(APPLIED, PICKER_PENDING, one, false);
    assert.deepEqual(picker.filters, [IMAGES, SPLIT, BRUSHED, POLYGON, IMAGE_LASSO, one]);
    assert.ok(!picker.filters.includes(TICKED_ROWS));
});

test('with objects, the pending selections are inside the wrapping for the list and for the picker', () => {
    const list = sidebarView(APPLIED, LIST_PENDING, null, true);
    assert.deepEqual(list, withGrain({ logic: 'AND', filters: [SPLIT, ...LIST_PENDING] }, 'image', null, true));
    assert.deepEqual(list.filters.map((part) => part.field ?? part.logic), ['media_type', 'OR']);
    assert.deepEqual(list.filters[1].filters[0].value.filters, [SPLIT, ...LIST_PENDING]);
    const one = convertSimpleFiltersToComplex({ colour: ['red'] });
    const picker = sidebarView(APPLIED, PICKER_PENDING, one, true);
    assert.deepEqual(picker, withGrain({ logic: 'AND', filters: [SPLIT, ...PICKER_PENDING, one] }, 'image', null, true));
    assert.deepEqual(picker.filters[1].filters[0].value.filters, [SPLIT, BRUSHED, POLYGON, IMAGE_LASSO, one]);
    // An object lasso among the picker's selections: coupled to its images, as for the list.
    const withObjectLasso = sidebarView(APPLIED, [BRUSHED, POLYGON, OBJECT_LASSO], one, true);
    assert.deepEqual(withObjectLasso.filters.map((part) => part.field ?? part.logic), ['media_type', 'OR', '_crossemb']);
});
