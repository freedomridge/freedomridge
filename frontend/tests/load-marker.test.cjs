// Run with: node --test tests/load-marker.test.cjs
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

const { crashedBefore, loadMarker } = require(path.join(__dirname, '..', 'lib', 'load-marker.ts'));

test('a marker from this page load is a remount mid-load, not a crash', () => {
    // The embeddings panel remounted while loading (a Find similar search): the old instance's marker is still there.
    assert.equal(crashedBefore(loadMarker(30, 1000.5), 1000.5), false);
});

test('a marker from another page load means that load crashed the tab', () => {
    assert.equal(crashedBefore(loadMarker(30, 1000.5), 2000.25), true);
    assert.equal(crashedBefore('30', 2000.25), true);  // an older marker: a bare percentage
    assert.equal(crashedBefore('not json', 2000.25), true);
});

test('no marker, no crash', () => {
    assert.equal(crashedBefore(null, 1000.5), false);
});
