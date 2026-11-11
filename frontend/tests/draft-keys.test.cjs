// Run with: node --test tests/draft-keys.test.cjs
/* eslint-disable @typescript-eslint/no-require-imports -- Uses the Node CommonJS loader to transpile local TS. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { test } = require('node:test');
const ts = require('typescript');

require.extensions['.ts'] = (module, filename) => {
    const source = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
        compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2017 },
        fileName: filename,
    }).outputText;
    module._compile(source, filename);
};
const {
    boundKeyFor, convertSimpleFiltersToComplex, excludeKeyFor, getSimpleFilterStages, upgradeDraftKeys,
} = require('../lib/filter-utils.ts');
const { filteredFieldNames } = require('../lib/sidebar-sections.ts');

// The sidebar keeps a field's draft under the field's name. A field whose own name started with `exclude_` or
// ended with `_min` / `_max` was read as an operator on another field: ticking `blurry` under `exclude_reason`
// gave `reason not in ["blurry"]`, which matched every sample while the chip said the filter was applied.

test('a field named like an exclusion filters itself', () => {
    const tick = { exclude_reason: ['blurry'] };
    const expected = { field: 'exclude_reason', operator: 'in', value: ['blurry'] };
    assert.deepEqual(convertSimpleFiltersToComplex(tick), expected);
    assert.deepEqual(getSimpleFilterStages(tick).map(stage => stage.filter), [expected]);
    assert.deepEqual([...filteredFieldNames(tick)], ['exclude_reason']);
    // Its own exclusion, and another field's.
    assert.deepEqual(convertSimpleFiltersToComplex({ [excludeKeyFor('exclude_reason')]: ['blurry'] }),
        { field: 'exclude_reason', operator: 'not in', value: ['blurry'] });
    assert.deepEqual(convertSimpleFiltersToComplex({ [excludeKeyFor('reason')]: ['blurry'] }),
        { field: 'reason', operator: 'not in', value: ['blurry'] });
    assert.deepEqual(getSimpleFilterStages({ [excludeKeyFor('reason')]: ['blurry'] }).map(stage => stage.filter),
        [{ field: 'reason', operator: 'not in', value: ['blurry'] }]);
});

test('a field named like a bound filters itself', () => {
    for (const [value, operator] of [['deep', 'contains'], [5, '=']]) {
        const draft = { depth_max: value, depth_min: value };
        const expected = { logic: 'AND', filters: [
            { field: 'depth_max', operator, value },
            { field: 'depth_min', operator, value },
        ] };
        assert.deepEqual(convertSimpleFiltersToComplex(draft), expected);
        assert.deepEqual(getSimpleFilterStages(draft).map(stage => stage.filter), expected.filters);
        assert.deepEqual([...filteredFieldNames(draft)].sort(), ['depth_max', 'depth_min']);
    }
    // A datetime bar's bounds on it.
    const bounds = { [boundKeyFor('depth_max', 'min')]: '2024-01-01', [boundKeyFor('depth_max', 'max')]: '2024-02-01' };
    const expected = [
        { field: 'depth_max', operator: '>=', value: '2024-01-01' },
        { field: 'depth_max', operator: '<=', value: '2024-02-01' },
    ];
    assert.deepEqual(convertSimpleFiltersToComplex(bounds), { logic: 'AND', filters: expected });
    assert.deepEqual(getSimpleFilterStages(bounds).map(stage => stage.filter), expected);
    assert.deepEqual(convertSimpleFiltersToComplex({ [excludeKeyFor(boundKeyFor('seen', 'min'))]: '2024-01-01' }),
        { logic: 'NOT', filters: [{ field: 'seen', operator: '>=', value: '2024-01-01' }] });
});

test("a mirror's own fields keep their names too", () => {
    const draft = {
        '@local.exclude_reason': ['blurry'],
        '@local.depth_max': 'deep',
        [excludeKeyFor('@local.weeds')]: ['many'],
        [boundKeyFor('@local.seen', 'min')]: '2024-01-01',
    };
    assert.deepEqual(convertSimpleFiltersToComplex(draft), { logic: 'AND', filters: [
        { field: '@local.exclude_reason', operator: 'in', value: ['blurry'] },
        { field: '@local.depth_max', operator: 'contains', value: 'deep' },
        { field: '@local.weeds', operator: 'not in', value: ['many'] },
        { field: '@local.seen', operator: '>=', value: '2024-01-01' },
    ] });
    assert.deepEqual([...filteredFieldNames(draft)].sort(), ['@local.depth_max', '@local.exclude_reason', '@local.seen', '@local.weeds']);
});

test('a draft saved with the old markers filters what it did', () => {
    const legacy = {
        exclude_color: ['red'],
        sampled_at_min: '2024-01-01',
        sampled_at_max: '2024-02-01',
        exclude_score_min: 3,
        brightness: { min: 1, max: 2 },
        label: { _op: 'text', values: [], contains: ['ab'] },
        tags_max: ['x'],  // a list under a `_max` name was a field of that name then too
        '@local.seen_min': '2024-01-01',
        'exclude_@local.weeds': ['many'],
    };
    const upgraded = upgradeDraftKeys(legacy);
    assert.deepEqual(upgraded, {
        'exclude:color': ['red'],
        'sampled_at:min': '2024-01-01',
        'sampled_at:max': '2024-02-01',
        'exclude:score:min': 3,
        brightness: { min: 1, max: 2 },
        label: { _op: 'text', values: [], contains: ['ab'] },
        tags_max: ['x'],
        '@local.seen:min': '2024-01-01',
        'exclude:@local.weeds': ['many'],
    });
    assert.deepEqual(convertSimpleFiltersToComplex(upgraded), { logic: 'AND', filters: [
        { field: 'color', operator: 'not in', value: ['red'] },
        { field: 'sampled_at', operator: '>=', value: '2024-01-01' },
        { field: 'sampled_at', operator: '<=', value: '2024-02-01' },
        { logic: 'NOT', filters: [{ field: 'score', operator: '>=', value: 3 }] },
        { logic: 'AND', filters: [{ field: 'brightness', operator: '>=', value: 1 }, { field: 'brightness', operator: '<=', value: 2 }] },
        { field: 'label', operator: 'contains', value: 'ab' },
        { field: 'tags_max', operator: 'in', value: ['x'] },
        { field: '@local.seen', operator: '>=', value: '2024-01-01' },
        { field: '@local.weeds', operator: 'not in', value: ['many'] },
    ] });
    // Keys already in the new spelling come through as they are.
    const current = { 'exclude:color': ['red'], 'sampled_at:min': '2024-01-01', color: ['blue'] };
    assert.deepEqual(upgradeDraftKeys(current), current);
});
