// Run with: node --test tests/mask-paint.test.cjs
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

const { classColor, maskColorKey, maskColorTable, paintMask } = require('../lib/mask-paint.ts');

test('a class colour comes from its ID, then its name, then the palette', () => {
    const config = { idColors: { 1: '#010203' }, valueColors: { weed: '#0a0b0c' } };
    assert.deepEqual(classColor(1, config, { 1: 'crop', 2: 'weed' }), [1, 2, 3]);
    assert.deepEqual(classColor(2, config, { 1: 'crop', 2: 'weed' }), [10, 11, 12]);
    assert.deepEqual(classColor(3, config), [0x43, 0x63, 0xd8]);  // the palette's fourth colour, #4363d8
});

test('equal colour settings give one key, however often the objects are made again', () => {
    const make = () => [{ idColors: { 1: '#ff0000' }, opacity: 0.5 }, { 1: 'crop' }];
    assert.equal(maskColorKey(...make()), maskColorKey(...make()));
    assert.notEqual(maskColorKey({ idColors: { 1: '#ff0000' } }), maskColorKey({ idColors: { 1: '#00ff00' } }));
});

test('the colour table leaves the background and hidden classes transparent', () => {
    const table = maskColorTable(maskColorKey({ idColors: { 1: '#ff0000', 2: '#00ff00' } }), [2], 0.5);
    assert.deepEqual([...table.slice(0, 4)], [0, 0, 0, 0]);
    assert.deepEqual([...table.slice(4, 8)], [255, 0, 0, 128]);
    assert.deepEqual([...table.slice(8, 12)], [0, 0, 0, 0]);
});

test('an index mask paints each pixel its class colour, skipping background, hidden and transparent pixels', () => {
    const table = maskColorTable(maskColorKey({ idColors: { 1: '#ff0000', 2: '#00ff00' } }), [2], 1);
    // pixels: class 1, class 0, class 2 (hidden), class 1 but transparent
    const src = Uint8ClampedArray.from([1, 0, 0, 255, 0, 0, 0, 255, 2, 0, 0, 255, 1, 0, 0, 0]);
    const out = new Uint8ClampedArray(src.length);
    paintMask(src, out, table, 1);
    assert.deepEqual([...out], [255, 0, 0, 255, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]);
});

test('an RGB mask keeps each pixel colour at the opacity, black staying transparent', () => {
    const src = Uint8ClampedArray.from([10, 20, 30, 255, 0, 0, 0, 255]);
    const out = new Uint8ClampedArray(src.length);
    paintMask(src, out, null, 0.5);
    assert.deepEqual([...out], [10, 20, 30, 128, 0, 0, 0, 0]);
});
