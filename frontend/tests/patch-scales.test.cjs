// Run with: node --test tests/patch-scales.test.cjs
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

const { patchScaleLabel, patchScaleSource } = require(path.join(__dirname, '..', 'lib', 'patch-scales.ts'));

test("this dataset's own scale reads as the scale alone", () => {
    assert.equal(patchScaleLabel({ scale: '4x4', local: true }), '4x4');
    assert.equal(patchScaleLabel({ scale: '4x4' }), '4x4');  // an older server lists only its own
    assert.equal(patchScaleSource({ scale: '4x4', local: true }), '');
    assert.equal(patchScaleSource(undefined), '');
});

test("a follower's scale names the follower, and says when it is offline", () => {
    assert.equal(patchScaleLabel({ scale: '8x8', local: false, provider: 'desktop', online: true }), '8x8 · on desktop');
    assert.equal(
        patchScaleLabel({ scale: '8x8', local: false, provider: 'desktop', online: false }),
        '8x8 · on desktop (offline)',
    );
    assert.equal(patchScaleSource({ scale: '8x8', local: false, provider: 'desktop' }), ' · on desktop');
});
