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

const api = require('../lib/api.ts');


test('bulk resume uses the retained job and authenticated endpoint', async () => {
    const originalFetch = global.fetch;
    global.fetch = async (url, options) => {
        assert.equal(url, '/api/jobs/bulk-id/resume');
        assert.equal(options.method, 'POST');
        assert.equal(options.credentials, 'include');
        return new Response(JSON.stringify({job_id: 'bulk-id', state: 'queued'}), {status: 202});
    };
    try { assert.equal((await api.resumeBulkJob('bulk-id')).job_id, 'bulk-id'); }
    finally { global.fetch = originalFetch; }
});

test('bulk history reports exact counts and the committed prefix with authorized resume', () => {
    const { BulkJobOutcome } = require('../components/job-history.tsx');
    const render = (state, canResume, result) => renderToStaticMarkup(React.createElement(BulkJobOutcome, {
        job: {kind: 'bulk_mutation', state, result}, canResume, resume() {}, discard() {}, pending: false,
    }));
    assert.match(render('succeeded', false, {updated_count: 500000}), /500,000 samples updated/);
    assert.match(render('cancelled', true, {deleted_count: 1000}), /1,000 samples deleted.*Completed chunks.*Resume/);
    assert.doesNotMatch(render('failed', false, {deleted_count: 1000}), /<button/);
    // Deleting an image deletes its objects: the ones the job didn't select are named apart.
    assert.match(
        render('succeeded', false, {deleted_count: 1200, objects_deleted: 9840}),
        /1,200 samples deleted, and 9,840 objects with their images\./,
    );
    assert.match(render('succeeded', false, {deleted_count: 1, objects_deleted: 1}), /and 1 object with their images/);
    assert.match(render('succeeded', false, {deleted_count: 3, objects_deleted: 0}), /3 samples deleted\. /);
});

test('bulk discard uses the authenticated endpoint and history records it without actions', async () => {
    const originalFetch = global.fetch;
    global.fetch = async (url, options) => {
        assert.equal(url, '/api/jobs/bulk-id/discard');
        assert.equal(options.method, 'POST');
        assert.equal(options.credentials, 'include');
        return new Response(JSON.stringify({job_id: 'bulk-id', state: 'cancelled', discarded: true}), {status: 200});
    };
    try { assert.equal((await api.discardBulkJob('bulk-id')).discarded, true); }
    finally { global.fetch = originalFetch; }
    const { BulkJobOutcome } = require('../components/job-history.tsx');
    const render = (canResume, result) => renderToStaticMarkup(React.createElement(BulkJobOutcome, {
        job: {kind: 'bulk_mutation', state: 'cancelled', result}, canResume, resume() {}, discard() {}, pending: false,
    }));
    assert.match(render(true, {updated_count: 10}), /Resume<\/button> <button[^>]*>Discard<\/button>/);
    const discarded = render(true, {updated_count: 10, discarded: {by: 'alice'}});
    assert.match(discarded, /10 samples updated.*Discarded by alice.*the rest will not run/);
    assert.doesNotMatch(discarded, /<button/);
});
