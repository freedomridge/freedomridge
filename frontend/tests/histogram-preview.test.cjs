// Run with: node --test tests/histogram-preview.test.cjs
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
const { HistogramPreview } = require('../components/histogram-panel.tsx');

async function requestedUrl(call) {
    const originalFetch = global.fetch;
    let url;
    global.fetch = async (requested) => {
        url = new URL(requested, 'http://app');
        return new Response(JSON.stringify({ field: 'device_id', total: 0, buckets: [] }), { status: 200 });
    };
    try { await call(); } finally { global.fetch = originalFetch; }
    return url;
}

test('the exact histogram asks for numbers only when the switch is on', async () => {
    const plain = await requestedUrl(() => api.getHistogram(7, 'device_id', undefined, undefined));
    assert.equal(plain.pathname, '/api/datasets/7/histogram');
    assert.equal(plain.searchParams.get('as_number'), null);
    const numeric = await requestedUrl(() => api.getHistogram(7, 'device_id', undefined, undefined, true));
    assert.equal(numeric.pathname, '/api/datasets/7/histogram');
    assert.equal(numeric.searchParams.get('as_number'), 'true');
});

test('the preview is a separate request with the same parameters', async () => {
    const filters = { field: 'media_type', operator: '!=', value: 'object' };
    const url = await requestedUrl(() => api.getHistogramPreview(7, 'country', filters, undefined, true));
    assert.equal(url.pathname, '/api/datasets/7/histogram/preview');
    assert.equal(url.searchParams.get('field'), 'country');
    assert.deepEqual(JSON.parse(url.searchParams.get('filters')), filters);
    assert.equal(url.searchParams.get('as_number'), 'true');
});

test('a categorical preview is labelled approximate, shows shares, and cannot filter', () => {
    const html = renderToStaticMarkup(React.createElement(HistogramPreview, { data: {
        field: 'color', total: 20000, missing: 0, distinct_total: 2, other_count: 0,
        buckets: [{ key: 'red', count: 15000, label: 'red' }, { key: 'blue', count: 5000, label: 'blue' }],
        approximate: true,
        preview: { status: 'ok', method: 'uniform_id_sample', draws: 20000, matched: 20000, estimated_matching_rows: 2000000 },
    } }));
    assert.match(html, /Approximate preview — not exact/);
    assert.match(html, /random sample of 20,000 matching samples/);
    assert.match(html, /small categories may be missing/);
    assert.match(html, /can&#x27;t be used to filter/);
    assert.match(html, /≈75\.0%/);
    assert.match(html, /Categories seen in the sample: 2/);
    assert.doesNotMatch(html, /<button/);
    assert.equal((html.match(/aria-disabled="true"/g) || []).length, 2);
    // Sample counts are never shown as if they were the population's counts.
    assert.doesNotMatch(html, />15000</);
});

test('a numeric preview says its bin ranges come from the sample', () => {
    const html = renderToStaticMarkup(React.createElement(HistogramPreview, { data: {
        field: 'width', total: 1000, missing: 100, bins: 2,
        buckets: [
            { key: 1, count: 600, label: '0.00 - 5.00', range: { min: 0, max: 5 } },
            { key: 2, count: 300, label: '5.00 - 10.00', range: { min: 5, max: 10 } },
        ],
        approximate: true,
        preview: { status: 'ok', matched: 1000 },
    } }));
    assert.match(html, /bin ranges come from the sample/);
    assert.match(html, /Bins: 2 \(ranges from the sample\)/);
    assert.match(html, /Missing: ≈10\.0%/);
    assert.doesNotMatch(html, /<button/);
});

test('a preview shows the share left in others and announces itself', () => {
    // Without the others share, ~60% of a high-cardinality sample had no bar at all.
    const html = renderToStaticMarkup(React.createElement(HistogramPreview, { data: {
        field: 'device_id', total: 20000, missing: 0, distinct_total: 9000, other_count: 12000,
        buckets: [{ key: 'A1', count: 5000, label: 'A1' }, { key: 'B2', count: 3000, label: 'B2' }],
        approximate: true,
        preview: { status: 'ok', method: 'uniform_id_sample', draws: 20000, matched: 20000, estimated_matching_rows: 2000000 },
    } }));
    assert.match(html, /Others \(no bar\): ≈60(\.0)?%/);
    assert.match(html, /role="status"/);
});
