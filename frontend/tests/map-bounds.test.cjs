// Run with: node --test tests/map-bounds.test.cjs
/* eslint-disable @typescript-eslint/no-require-imports -- Uses the Node CommonJS loader to transpile local TS. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const ts = require('typescript');

require.extensions['.ts'] = (module, filename) => {
    const source = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
        compilerOptions: { module: ts.ModuleKind.CommonJS },
        fileName: filename,
    }).outputText;
    module._compile(source, filename);
};

const { tileBbox, viewBbox } = require(path.join(__dirname, '..', 'lib', 'map-bounds.ts'));

// mapbox-gl's getBounds() gives unwrapped longitudes (its Mercator unproject is x * 360 - 180, and the centre isn't
// wrapped while world copies render): a view over Fiji is west 170, east 190.
test('a view across the antimeridian keeps its east past 180', () => {
    assert.deepEqual(viewBbox([170, -20, 190, 5]), [170, -20, 190, 5]);
    assert.deepEqual(tileBbox([170, -20, 190, 5], 5), [157.5, -22.5, 202.5, 22.5]);
});

test('every world copy asks for the same region', () => {
    for (const copy of [-720, -360, 360, 720]) {
        assert.deepEqual(viewBbox([170 + copy, -20, 190 + copy, 5]), [170, -20, 190, 5]);
    }
    // Wholly east of the antimeridian, on the next copy: back to plain longitudes. It used to become west 200,
    // east 180, an empty box.
    assert.deepEqual(viewBbox([200, 0, 220, 10]), [-160, 0, -140, 10]);
    assert.deepEqual(tileBbox(viewBbox([200, 0, 220, 10]), 5), [-180, 0, -135, 22.5]);
});

test('a view or a tile 360 degrees wide or more is the whole world, one stable tile', () => {
    assert.deepEqual(viewBbox([-400, -85, 400, 85]), [-180, -85, 180, 85]);
    assert.deepEqual(tileBbox(viewBbox([-400, -85, 400, 85]), 1), [-180, -85, 180, 85]);
    assert.deepEqual(tileBbox(viewBbox([100, -60, 260, 60]), 2), [-180, -85, 180, 85]);
    assert.deepEqual(tileBbox(viewBbox([-100, -60, 60, 60]), 2), [-180, -85, 180, 85]);
});

test('a view inside one world copy is tiled as before', () => {
    assert.deepEqual(viewBbox([-97.3, 30.1, -90.2, 35.6]), [-97.3, 30.1, -90.2, 35.6]);
    assert.deepEqual(tileBbox([-97.3, 30.1, -90.2, 35.6], 6), [-101.25, 22.5, -90, 45]);
    assert.deepEqual(tileBbox([170, -20, 179.5, 5], 5), [157.5, -22.5, 180, 22.5]);
    // From the zoom where every point shows, the view is sent as it is.
    assert.deepEqual(tileBbox([179.9991, -0.0004, 180.0007, 0.0004], 18), [179.9991, -0.0004, 180.0007, 0.0004]);
});
