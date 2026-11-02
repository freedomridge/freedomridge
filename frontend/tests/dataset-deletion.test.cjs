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

test('e2e cleanup waits for 202 jobs and resumes stopped cleanup from a crashed run', async () => {
    const { deleteDatasetAndWait } = require('../e2e/api.ts');
    const originalFetch = global.fetch;
    const calls = [];
    const states = ['cancelled', 'running', 'succeeded'];
    global.fetch = async (url, options) => {
        calls.push([url, options.method || 'GET']);
        const job = url.endsWith('/jobs/cleanup');
        return new Response(JSON.stringify(job
            ? { id: 'cleanup', state: states.shift() }
            : { job_id: 'cleanup', state: 'queued' }), { status: job ? 200 : 202 });
    };
    try {
        await deleteDatasetAndWait('session_id=test', 7);
        assert.equal(states.length, 0);
        assert.ok(calls.some(([url, method]) => url.endsWith('/datasets/7/deletion/resume') && method === 'POST'));
        await assert.rejects(deleteDatasetAndWait('session_id=test', 7, 0), /Timed out.*cleanup/);
    } finally {
        global.fetch = originalFetch;
    }
});

test('delete accepts 202, polls retained job and resumes the same deletion', async () => {
    const originalFetch = global.fetch;
    const calls = [];
    global.fetch = async (url, options) => {
        calls.push([url, options]);
        const result = url.endsWith('/jobs/deletion-id')
            ? { id: 'deletion-id', state: 'succeeded' }
            : { job_id: 'deletion-id', state: 'queued' };
        return new Response(JSON.stringify(result), { status: url.endsWith('/jobs/deletion-id') ? 200 : 202 });
    };
    try {
        assert.equal((await api.deleteDataset(7)).job_id, 'deletion-id');
        assert.equal((await api.resumeDatasetDeletion(7)).job_id, 'deletion-id');
        assert.equal((await api.getJob('deletion-id')).state, 'succeeded');
        assert.equal(calls[0][1].method, 'DELETE');
        assert.match(calls[1][0], /\/datasets\/7\/deletion\/resume$/);
        assert.equal(calls[1][1].method, 'POST');
        assert.ok(calls.every(([, options]) => options.credentials === 'include'));
    } finally {
        global.fetch = originalFetch;
    }
});

test('deletion reports progress, stopped read-only state, resume and completion', () => {
    const { DeletionMessage } = require('../components/dataset-deletion.tsx');
    const render = (state, canResume = true) => renderToStaticMarkup(
        React.createElement(DeletionMessage, {
            job: { state, progress: { done: 1234, message: 'Removing samples' } },
            canResume, resume() {}, pending: false,
        }),
    );
    assert.match(render('running'), /deletion in progress.*Writes are disabled/);
    assert.match(render('running'), /1,234 rows removed/);
    for (const state of ['failed', 'cancelled']) {
        assert.match(render(state), /remains read-only/);
        assert.match(render(state), /Already removed data cannot be recovered/);
        assert.match(render(state), /Resume deletion/);
        assert.doesNotMatch(render(state, false), /<button/);
    }
    assert.match(render('succeeded'), /Dataset deleted/);
    assert.doesNotMatch(render('succeeded'), /Resume deletion/);
});

test('PATCH fields remain visible if measurement finishes before the page loads', () => {
    const toast = require('../contexts/ToastContext.tsx');
    const user = require('../contexts/UserContext.tsx');
    const originalToast = toast.useToast;
    const originalUser = user.useUser;
    toast.useToast = () => ({ addToast() {} });
    user.useUser = () => ({ user: { role: 'admin' } });
    // The field's section is expanded (remembered by the browser), so its range shows.
    global.window = { localStorage: { getItem: () => '["new_measurement"]', setItem() {} } };
    try {
        const { QueryClient, QueryClientProvider } = require('@tanstack/react-query');
        const { FilterSidebar } = require('../components/filter-sidebar.tsx');
        for (const pending of [true, false]) {
            const client = new QueryClient({ defaultOptions: { queries: { gcTime: Infinity } } });
            const markup = renderToStaticMarkup(React.createElement(QueryClientProvider, { client }, React.createElement(FilterSidebar, {
                datasetId: 1, filters: {}, onChange() {},
                schema: [{
                    name: 'new_measurement', type: 'number', declared_type: 'number',
                    origin: 'metadata', stats_pending: pending,
                    ...(pending ? {} : { min: 123, max: 123 }),
                }],
            })));
            assert.match(markup, />new_measurement</);
            assert.equal(markup.includes('data-testid="pending-field"'), pending);
            assert.equal(markup.includes('Statistics pending'), pending);
            if (!pending) assert.match(markup, /123 - 123/);
        }
    } finally {
        toast.useToast = originalToast;
        user.useUser = originalUser;
        delete global.window;
    }
});
