// Run with: node --test tests/map-writer-errors.test.cjs
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
const { reduceEmbeddings, ApiError } = require('../lib/api.ts');

for (const [code, legacy] of [['map_refit_active', false], ['map_managed', false], ['map_refit_active', true]]) {
    test(`Run UMAP reduction surfaces ${code} (${legacy ? 'legacy' : 'string'} detail)`, async () => {
        const originalFetch = global.fetch;
        const message = code === 'map_refit_active'
            ? 'A map re-fit is in progress; try again when it finishes.'
            : 'This map is managed by scheduled re-fits; run a re-fit to change its layout.';
        const body = legacy ? { detail: { message, code } } : { detail: message, code };
        global.fetch = async (url, options) => {
            assert.equal(url, '/api/datasets/1/embeddings/reduce');
            assert.equal(options.method, 'POST');
            assert.deepEqual(JSON.parse(options.body), { field_name: 'clip' });
            return new Response(JSON.stringify(body), { status: 409 });
        };
        try {
            await assert.rejects(reduceEmbeddings(1, 'clip'), error => {
                assert.ok(error instanceof ApiError);
                assert.equal(error.status, 409);
                assert.equal(error.code, code);
                assert.equal(error.message, message);
                assert.ok(!String(error).includes('[object Object]'));
                return true;
            });
        } finally { global.fetch = originalFetch; }
    });
}

test('Run UMAP reduction displays saved coordinates and failed ranking', async () => {
    const originalFetch = global.fetch;
    const detail = 'Coordinates saved; ranking failed. Retry ranking without repeating the reduction.';
    global.fetch = async () => new Response(JSON.stringify({detail, code: 'map_rank_failed'}), {status: 503});
    try {
        await assert.rejects(reduceEmbeddings(1, 'clip'), error => {
            assert.ok(error instanceof ApiError);
            assert.equal(error.status, 503);
            assert.equal(error.code, 'map_rank_failed');
            assert.equal(error.message, detail);
            return true;
        });
    } finally { global.fetch = originalFetch; }
});
