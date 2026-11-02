// Run with: node --test tests/dataset-deletion.test.cjs
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


const { DatasetDescription } = require('../components/dataset-description.tsx');
const { SelectAllButton } = require('../components/select-all-button.tsx');

test('dataset credits and license links remain visible and escape untrusted descriptions', () => {
    const html = renderToStaticMarkup(React.createElement(DatasetDescription, {
        description: 'KITTI — Geiger et al. CC BY-NC-SA 3.0 https://www.cvlibs.net/datasets/kitti/ <script>bad()</script>',
    }));
    assert.match(html, /CC BY-NC-SA 3.0/);
    assert.match(html, /href="https:\/\/www.cvlibs.net\/datasets\/kitti\/"/);
    assert.match(html, /&lt;script&gt;/);
    assert.doesNotMatch(html, /<script>/);
    assert.equal(renderToStaticMarkup(React.createElement(DatasetDescription, { description: null })), '');
});

test('guest large selection is explained; small selection and power users retain the control', () => {
    const render = (total, guest) => renderToStaticMarkup(React.createElement(SelectAllButton, {
        total, guest, onSelectAll() {},
    }));
    assert.match(render(100_000, true), /disabled=""/);
    assert.match(render(100_000, true), /Browse, filter, export or save/);
    assert.match(render(null, true), /disabled=""/);
    assert.doesNotMatch(render(50_000, true), /disabled=/);
    assert.doesNotMatch(render(100_000, false), /disabled=/);
});


const { sparseSample } = require('../lib/api.ts');
test('sampling errors show useful detail for busy and invalid requests', async () => {
    const originalFetch = global.fetch;
    try {
        for (const [status, detail, expected] of [
            [503, 'Sparse sampling is busy, retry shortly', /busy, retry shortly/],
            [422, [{ msg: 'fields must be unique' }], /fields must be unique/],
        ]) {
            global.fetch = async () => new Response(JSON.stringify({ detail }), { status });
            await assert.rejects(sparseSample(1, ['score'], { count: 1 }), error => {
                assert.match(error.message, expected);
                assert.doesNotMatch(error.message, /[{}]/);
                return true;
            });
        }
    } finally {
        global.fetch = originalFetch;
    }
});
