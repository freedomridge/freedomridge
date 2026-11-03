// Run with: node --test tests/create-dataset.test.cjs
/* eslint-disable @typescript-eslint/no-require-imports -- Transpile the real API module for Node tests. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { test } = require('node:test');
const ts = require('typescript');

require.extensions['.ts'] = (module, filename) => {
    const source = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
        compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 }, fileName: filename,
    }).outputText;
    module._compile(source, filename);
};
const { createDataset } = require('../lib/api.ts');

test('create dataset sends credentials and form fields, and surfaces API errors', async () => {
    const originalFetch = global.fetch;
    const data = { name: 'New dataset', description: 'Example' };
    try {
        global.fetch = async (url, options) => {
            assert.equal(url, '/api/datasets');
            assert.equal(options.method, 'POST');
            assert.equal(options.credentials, 'include');
            assert.equal(options.headers['Content-Type'], 'application/json');
            assert.deepEqual(JSON.parse(options.body), data);
            return Response.json({ id: 7, ...data, is_global: false });
        };
        assert.deepEqual(await createDataset(data), { id: 7, ...data, is_global: false });
        global.fetch = async () => Response.json({ detail: 'Dataset with this name already exists' }, { status: 400 });
        await assert.rejects(createDataset(data), /Dataset with this name already exists/);
        global.fetch = async () => new Response('Unavailable', { status: 503 });
        await assert.rejects(createDataset(data), /Failed to create dataset/);
    } finally {
        global.fetch = originalFetch;
    }
});
