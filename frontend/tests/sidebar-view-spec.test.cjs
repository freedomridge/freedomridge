// Run with: node --test tests/sidebar-view-spec.test.cjs
/* eslint-disable @typescript-eslint/no-require-imports -- Uses the Node CommonJS loader to transpile local TS/TSX. */
// The page's one function for the sidebar's view (contract g12, points 13, 14 and 15): sidebarView(applied, pending,
// ticks, hasObjects) in contexts/FilterContext.tsx, beside withGrain. Written from the contract, not from the code. It
// pins what the function does with what it is given: the pending selections (a brushed set, ticked grid rows, a drawn
// polygon, a lasso) and the other fields' ticks go INSIDE the page's wrapping, as an applied stage does; the kind is at
// the top of the AND; no empty group is sent for no ticks; on a dataset without objects the list's view is today's,
// part for part, the ticked rows among its parts. Which pending selections the page gives each caller (the picker gets
// no ticked rows, point 15) is the page's choice, tested end to end in e2e/sidebar-field-values.spec.ts.
//
// The arguments, as this file reads point 14 (it names them, not their types; change them here, in one place):
// - applied: the applied stages' filter, {logic: 'AND', filters: stages}, as withGrain's base (the page's globalFilters);
// - pending: the pending selections, a list of filter nodes in the order the page builds them today (brushed ids,
//   ticked rows' ids, the polygon, the lasso);
// - ticks: the other fields' ticks as convertSimpleFiltersToComplex makes them (an empty AND group when nothing else is
//   ticked), or null for the list and the range, which have none.
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

const view = (stages, pending, ticks, hasObjects) => sidebarView({ logic: 'AND', filters: stages }, pending, ticks, hasObjects);

const KIND = { field: 'media_type', operator: '!=', value: 'object' };
const SQUARE = [[0, 0], [5, 0], [5, 5], [0, 5]];
const BRUSHED = { field: 'id', operator: 'in', value: [11, 12, 13] };
const TICKED_ROWS = { field: 'id', operator: 'in', value: [21, 22] };
const POLYGON = { field: 'location', operator: 'geo_polygon', value: SQUARE };
const LASSO = { field: 'clip_img', operator: 'embedding_polygon', value: SQUARE, layout_id: null, model_id: null };
const OBJECT_LASSO = { field: 'clip_obj', operator: 'embedding_polygon', value: SQUARE, layout_id: null, model_id: null };
const APPLIED = {
    'nothing applied': [],
    'an image field': [{ field: 'colour', operator: '=', value: 'red' }],
    'an object field and an image field': [
        { field: 'label', operator: 'in', value: ['car'] },
        { logic: 'NOT', filters: [{ field: 'colour', operator: '=', value: 'blue' }] },
    ],
};
// As the page builds them today: a brushed set is left out while a lasso is drawn.
const PENDING = {
    'a drawn polygon': [POLYGON],
    'a lasso': [LASSO],
    "a lasso on the objects' map": [OBJECT_LASSO],
    'a brushed set': [BRUSHED],
    'ticked grid rows': [TICKED_ROWS],
    'a brushed set, ticked rows and a polygon': [BRUSHED, TICKED_ROWS, POLYGON],
    'ticked rows, a polygon and a lasso': [TICKED_ROWS, POLYGON, LASSO],
};
const TICKS = {
    'one field ticked': convertSimpleFiltersToComplex({ make: ['ford'] }),
    'two fields ticked': convertSimpleFiltersToComplex({ make: ['ford', 'audi'], source: ['drone'] }),
};
const NO_TICKS = convertSimpleFiltersToComplex({});

const isGroup = (node) => !!node && typeof node === 'object' && (node.logic !== undefined || Array.isArray(node.filters));
const isFilterNode = (value) => !!value && typeof value === 'object' && !Array.isArray(value)
    && (isGroup(value) || typeof value.operator === 'string');

// A filter's meaning, written one way: an AND inside an AND (or an OR inside an OR) is its parts, a group of one part
// is that part, the parts of a group are in one order, and a filter held as a test's value is written the same way.
function normal(node) {
    if (isGroup(node)) {
        const logic = node.logic ?? 'AND';
        let parts = (node.filters ?? []).map(normal);
        if (logic === 'NOT') return { logic, filters: parts };
        parts = parts.flatMap(part => (part.logic === logic ? part.filters : [part]));
        parts.sort((a, b) => key(a).localeCompare(key(b)));
        return parts.length === 1 ? parts[0] : { logic, filters: parts };
    }
    const out = {};
    for (const name of Object.keys(node).sort()) {
        out[name] = name === 'value' && isFilterNode(node.value) ? normal(node.value) : node[name];
    }
    return out;
}

function key(node) {
    return JSON.stringify(node, (_, value) => (value && typeof value === 'object' && !Array.isArray(value)
        ? Object.fromEntries(Object.keys(value).sort().map(name => [name, value[name]]))
        : value));
}

// Every group anywhere in a filter, values of tests included.
function groups(node, found = []) {
    if (Array.isArray(node)) {
        node.forEach(item => groups(item, found));
    } else if (node && typeof node === 'object') {
        if (isGroup(node)) found.push(node);
        Object.values(node).forEach(item => groups(item, found));
    }
    return found;
}

// What every reader's view keeps: an AND with the kind at its top (point 13), and no empty group (point 14).
function checked(result, where) {
    assert.equal(result?.logic, 'AND', `${where}: the view is an AND`);
    assert.ok(result.filters.some(part => key(part) === key(KIND)), `${where}: the kind is at the top of the AND`);
    for (const group of groups(result)) {
        assert.ok((group.filters ?? []).length > 0, `${where}: an empty group in ${key(result)}`);
    }
    return result;
}

// The parts of a filter at the top of its AND, as written: a pending selection or a tick among them sits beside the
// wrapping, not inside it.
const topParts = (result) => result.filters.map(key);

for (const [appliedName, stages] of Object.entries(APPLIED)) {
    for (const [pendingName, pending] of Object.entries(PENDING)) {
        test(`13, 14: on a dataset with objects, the list's view with ${appliedName} and ${pendingName}: the pending selections inside the wrapping`, () => {
            const where = `${appliedName} / ${pendingName}`;
            const result = checked(view(stages, pending, null, true), where);
            const expected = withGrain({ logic: 'AND', filters: [...stages, ...pending] }, 'image', null, true);
            assert.deepEqual(normal(result), normal(expected), where);
            for (const selection of pending) {
                assert.ok(!topParts(result).includes(key(selection)), `${where}: ${key(selection)} beside the wrapping`);
            }
        });

        for (const [ticksName, ticks] of Object.entries(TICKS)) {
            test(`6, 13, 14: on a dataset with objects, the picker's view with ${appliedName}, ${pendingName} and ${ticksName}: the ticks inside the wrapping too`, () => {
                const where = `${appliedName} / ${pendingName} / ${ticksName}`;
                const result = checked(view(stages, pending, ticks, true), where);
                const expected = withGrain({ logic: 'AND', filters: [...stages, ...pending, ticks] }, 'image', null, true);
                assert.deepEqual(normal(result), normal(expected), where);
                assert.ok(!topParts(result).includes(key(ticks)), `${where}: the ticks beside the wrapping`);
            });
        }

        test(`14, 15: on a dataset without objects, the list's view with ${appliedName} and ${pendingName} is today's, part for part`, () => {
            const where = `${appliedName} / ${pendingName}`;
            const result = checked(view(stages, pending, null, false), where);
            assert.deepEqual(result, { logic: 'AND', filters: [KIND, ...stages, ...pending] }, where);
        });

        for (const [ticksName, ticks] of Object.entries(TICKS)) {
            test(`13, 14: on a dataset without objects, the picker's view with ${appliedName}, ${pendingName} and ${ticksName}: today's parts and the ticks, in any order`, () => {
                const where = `${appliedName} / ${pendingName} / ${ticksName}`;
                const result = checked(view(stages, pending, ticks, false), where);
                assert.deepEqual(normal(result), normal({ logic: 'AND', filters: [KIND, ...stages, ...pending, ticks] }), where);
            });
        }

        for (const hasObjects of [true, false]) {
            test(`14: no empty group for no ticks (${hasObjects ? 'with' : 'without'} objects, ${appliedName}, ${pendingName}): the picker's view is the list's`, () => {
                const where = `${appliedName} / ${pendingName} / objects: ${hasObjects}`;
                const picker = checked(view(stages, pending, NO_TICKS, hasObjects), where);
                assert.deepEqual(normal(picker), normal(view(stages, pending, null, hasObjects)), where);
            });
        }
    }
}

test('14: with nothing applied, nothing pending and no ticks, every reader sends the kind alone', () => {
    for (const hasObjects of [true, false]) {
        for (const ticks of [null, NO_TICKS]) {
            const result = view([], [], ticks, hasObjects);
            assert.deepEqual(normal(result), normal({ logic: 'AND', filters: [KIND] }), `objects: ${hasObjects}`);
        }
    }
    assert.deepEqual(view([], [], null, false), { logic: 'AND', filters: [KIND] });
});

test("13: on a dataset with objects, a tick on an object field is read with the applied stages by ONE object, as the grid reads an applied stage", () => {
    const stages = APPLIED['an image field'];
    const ticks = convertSimpleFiltersToComplex({ label: ['car'] });
    const result = checked(view(stages, [], ticks, true), 'a tick on label');
    // The grid's view had the tick been applied as a stage beside the others.
    assert.deepEqual(normal(result), normal(withGrain({ logic: 'AND', filters: [...stages, ticks] }, 'image', null, true)));
});
