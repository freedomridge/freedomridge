// Run with: node --test tests/image-grain-cases.test.cjs
/* eslint-disable @typescript-eslint/no-require-imports -- Uses the Node CommonJS loader to transpile local TS/TSX. */
// The image grain the dataset page applies (withGrain) is the one the assistant's working-view tools apply on the
// server (backend/assistant/grain.py): both reproduce every case of tests/fixtures/image-grain-cases.json exactly
// (backend/tests/test_assistant_grain.py is the server's half), so the two can't drift apart.
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

const { withGrain } = require('../contexts/FilterContext.tsx');

const cases = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures', 'image-grain-cases.json'), 'utf8'));

for (const { name, stages, has_objects: hasObjects, filter } of cases) {
    test(`the image grain of a view: ${name}`, () => {
        assert.deepEqual(withGrain({ logic: 'AND', filters: stages }, 'image', null, hasObjects), filter);
    });
}
