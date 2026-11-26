// Run with: node --test tests/regions.test.cjs
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

const {
    bboxCorners, newlyIncluded, outlineLines, regionChipLabel, regionTestOf, regionTests, toggledRegionFilter,
} = require('../lib/regions.ts');
const { isReadOnlyStage, isRegionStage } = require('../lib/filter-query.ts');
const { describeFilter } = require('../lib/filter-utils.ts');
const { mayBeText } = require('../lib/links.ts');

const kansas = { field: 'location', operator: 'geo_region', value: 'US-KS' };
const notKansas = { field: 'location', operator: 'geo_region_exclude', value: 'US-KS' };
const polygon = { field: 'location', operator: 'geo_polygon', value: [[0, 0], [1, 0], [1, 1]] };
const split = { field: 'split', operator: '=', value: 'train' };

test('a region test is a lone geo_region node, or the one rule of an AND group', () => {
    assert.deepEqual(regionTestOf(kansas), { key: 'US-KS', exclude: false });
    assert.deepEqual(regionTestOf(notKansas), { key: 'US-KS', exclude: true });
    assert.deepEqual(regionTestOf({ logic: 'AND', filters: [notKansas] }), { key: 'US-KS', exclude: true });
    for (const other of [polygon, split, undefined, { logic: 'OR', filters: [kansas] }, { logic: 'AND', filters: [kansas, split] },
        { logic: 'NOT', filters: [kansas] }, { ...kansas, field: 'gps' }, { ...kansas, value: ['US-KS'] }]) {
        assert.equal(regionTestOf(other), null, JSON.stringify(other));
    }
    assert.deepEqual(regionTests([split, kansas, undefined, { field: 'location', operator: 'geo_region_exclude', value: 'US:midwest' }]),
        [{ key: 'US-KS', exclude: false }, { key: 'US:midwest', exclude: true }]);
});

test('toggling turns a region test around and back, in either form, keeping its key', () => {
    assert.deepEqual(toggledRegionFilter(kansas), notKansas);
    assert.deepEqual(toggledRegionFilter(notKansas), kansas);
    assert.deepEqual(toggledRegionFilter({ logic: 'AND', filters: [kansas] }), { logic: 'AND', filters: [notKansas] });
});

test('the chip names the region and its country, a country alone, and the key while the name is unknown', () => {
    const state = { kind: 'state', name: 'Kansas', country_name: 'United States of America' };
    assert.equal(regionChipLabel({ key: 'US-KS', exclude: false }, state), 'In Kansas, United States of America');
    assert.equal(regionChipLabel({ key: 'US-KS', exclude: true }, state), 'Not in Kansas, United States of America');
    assert.equal(regionChipLabel({ key: 'US', exclude: false }, { kind: 'country', name: 'United States of America', country_name: 'United States of America' }),
        'In United States of America');
    assert.equal(regionChipLabel({ key: 'US:midwest', exclude: true }, { kind: 'group', name: 'Midwest', country_name: 'United States of America' }),
        'Not in Midwest, United States of America');
    assert.equal(regionChipLabel({ key: 'US-KZ', exclude: false }), 'In US-KZ');
    assert.equal(regionChipLabel({ key: 'US-KZ', exclude: true }, null), 'Not in US-KZ');
});

test('a region is newly applied when its key wasn\'t: not when turned around, removed, or excluded', () => {
    const ks = { key: 'US-KS', exclude: false };
    const ne = { key: 'US-NE', exclude: false };
    assert.deepEqual(newlyIncluded([], [ks]), ['US-KS']);
    assert.deepEqual(newlyIncluded([ks], [ks, ne]), ['US-NE']);
    assert.deepEqual(newlyIncluded([{ key: 'US-KS', exclude: true }], [ks]), []);  // turned around
    assert.deepEqual(newlyIncluded([ks], [{ key: 'US-KS', exclude: true }]), []);
    assert.deepEqual(newlyIncluded([ks, ne], [ne]), []);  // removed
    assert.deepEqual(newlyIncluded([], [{ key: 'US-KS', exclude: true }]), []);  // excluded
    assert.deepEqual(newlyIncluded([{ key: 'us-ks', exclude: false }], [ks]), []);  // keys in any case
    assert.deepEqual(newlyIncluded([ks], [ks]), []);
});

test('a box across the antimeridian goes east past 180', () => {
    assert.deepEqual(bboxCorners([-102.05, 36.99, -94.59, 40]), [[-102.05, 36.99], [-94.59, 40]]);
    assert.deepEqual(bboxCorners([172.48, 18.91, -66.98, 71.41]), [[172.48, 18.91], [293.02, 71.41]]);
});

test('an outline\'s lines leave out the edges along the antimeridian, and keep every other edge', () => {
    // Two pieces of one island cut at the antimeridian, as Natural Earth gives them.
    const west = [[179, -16], [180, -16], [180, -17], [179, -17], [179, -16]];
    const east = [[-180, -16], [-179, -16], [-179, -17], [-180, -17], [-180, -16]];
    const lines = outlineLines({ type: 'MultiPolygon', coordinates: [[west], [east]] });
    assert.equal(lines.type, 'MultiLineString');
    assert.deepEqual(lines.coordinates, [
        [[179, -16], [180, -16]],
        [[180, -17], [179, -17], [179, -16]],
        [[-180, -16], [-179, -16], [-179, -17], [-180, -17]],
    ]);
    // A ring away from the antimeridian, holes included, is drawn whole.
    const square = [[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]];
    const hole = [[0.2, 0.2], [0.4, 0.2], [0.4, 0.4], [0.2, 0.2]];
    assert.deepEqual(outlineLines({ type: 'MultiPolygon', coordinates: [[square, hole]] }).coordinates, [square, hole]);
    // An edge from 180 to -180 is no edge along it.
    const across = [[180, 1], [-180, 2], [-179, 2], [180, 1]];
    assert.deepEqual(outlineLines({ type: 'MultiPolygon', coordinates: [[across]] }).coordinates, [across]);
});

test('a region chip is read-only, labelled by its text form, and may go in q= beside ordinary filters', () => {
    const stage = { type: 'filter', description: describeFilter(kansas), filter: kansas };
    assert.equal(stage.description, 'location within US-KS');
    assert.equal(describeFilter(notKansas), 'location not within US-KS');
    assert.equal(describeFilter({ logic: 'OR', filters: [kansas, split] }), 'location within US-KS or split = train');
    assert.equal(isRegionStage(stage), true);
    assert.equal(isReadOnlyStage(stage), true);
    // However it was typed: a map region's type with a region's filter is a region chip too.
    const typed = { type: 'map_region', description: 'Kansas', filter: { logic: 'AND', filters: [notKansas] } };
    assert.equal(isRegionStage(typed), true);
    const plain = { type: 'filter', description: 'split = train', filter: split };
    assert.equal(mayBeText([plain, stage, typed]), true);
    // A drawn map region still goes in f=.
    const drawn = { type: 'map_region', description: 'Map Region', filter: polygon };
    assert.equal(isRegionStage(drawn), false);
    assert.equal(mayBeText([stage, drawn]), false);
    assert.equal(isRegionStage({ type: 'filter', description: 'mixed', filter: { logic: 'OR', filters: [kansas, split] } }), false);
});
