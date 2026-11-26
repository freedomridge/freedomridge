// Run with: node --test tests/map-fit-corners.test.cjs
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

const { extentCorners, pointsCorners } = require(path.join(__dirname, '..', 'lib', 'map-bounds.ts'));

// The boxes the map fits to (docs/MAP_REQUIREMENTS.md #7, #12): across the antimeridian the east goes past 180.
test('an extent across the antimeridian is fitted as the narrow box across it', () => {
    assert.deepEqual(extentCorners({ w: 177.4, s: -17.5, e: -178.2, n: -16 }), [[177.4, -17.5], [181.8, -16]]);
    assert.deepEqual(extentCorners({ w: 172.48, s: 51.2, e: -129.99, n: 71.41 }), [[172.48, 51.2], [230.01, 71.41]]);
});

test('an extent that does not cross is fitted as before, a single spot widened a little', () => {
    assert.deepEqual(extentCorners({ w: -97.3, s: 30.1, e: -90.2, n: 35.6 }), [[-97.3, 30.1], [-90.2, 35.6]]);
    assert.deepEqual(extentCorners({ w: -170, s: -10, e: 170, n: 10 }), [[-170, -10], [170, 10]]);
    assert.deepEqual(extentCorners({ w: 10, s: 20, e: 10, n: 20 }), [[9.9995, 19.9995], [10.0005, 20.0005]]);
});

const at = lons => lons.map(lon => ({ lon, lat: 0 }));

test('selected points that fit in half the globe across the antimeridian fit the box across it', () => {
    const fiji = [{ lon: 179, lat: -16 }, { lon: -179, lat: -17.5 }, { lon: 177, lat: -17 }, { lon: -178, lat: -16.5 }];
    assert.deepEqual(pointsCorners(fiji), [[177, -17.5], [182, -16]]);
    assert.deepEqual(pointsCorners(at([10, 177.4, -178.2])), [[10, 0], [181.8, 0]]);  // Europe and Fiji: 171.8 wide
    assert.deepEqual(pointsCorners(at([-122, -74, 115, 153])), [[115, 0], [286, 0]]);  // Australia and the United States
});

test('selected points that do not fit in half the globe that way keep their smallest and largest longitude', () => {
    const kansas = [{ lon: -97.3, lat: 38 }, { lon: -95.1, lat: 39.2 }, { lon: -101, lat: 37.5 }];
    assert.deepEqual(pointsCorners(kansas), [[-101, 37.5], [-95.1, 39.2]]);
    assert.deepEqual(pointsCorners(at([-122, -74, 100, 122])), [[-122, 0], [122, 0]]);  // the United States and China
    // The United States, Europe and Australia: the widest stretch with none, over Asia, is under 180 degrees.
    assert.deepEqual(pointsCorners(at([-122, -74, -9, 30, 115, 153])), [[-122, 0], [153, 0]]);
    assert.deepEqual(pointsCorners(at([-170, -90, -10, 70, 150])), [[-170, 0], [150, 0]]);  // round the globe
});
