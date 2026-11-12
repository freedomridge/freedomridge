// Run with: node --test tests/datetime-controls.test.cjs
/* eslint-disable @typescript-eslint/no-require-imports -- Uses the Node CommonJS loader to transpile local TS/TSX. */
// The controls that send or show a metadata datetime read it as the server does: text with no offset is UTC. The
// tests run west of UTC, where a date in the browser's own zone is the day before the UTC date.
process.env.TZ = 'America/Chicago';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const { test } = require('node:test');
const ts = require('typescript');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const { QueryClient, QueryClientProvider } = require('@tanstack/react-query');

const root = path.resolve(__dirname, '..');
const originalResolve = Module._resolveFilename;
Module._resolveFilename = function (specifier, ...args) {
    if (specifier.startsWith('@/')) specifier = path.join(root, specifier.slice(2));
    return originalResolve.call(this, specifier, ...args);
};
for (const extension of ['.ts', '.tsx']) {
    require.extensions[extension] = (module, filename) => {
        const source = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
            compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true, target: ts.ScriptTarget.ES2017 },  // as tsconfig.json
            fileName: filename,
        }).outputText;
        module._compile(source, filename);
    };
}

// The sidebar's sliders, kept with the props they were rendered with: there is no DOM to drag a handle in, so a test
// commits values the way the slider does when a drag ends.
const sliders = [];
const sliderPath = require.resolve('@radix-ui/react-slider');
const slider = new Module(sliderPath);
slider.filename = sliderPath;
slider.loaded = true;
slider.exports = {
    Root: props => { sliders.push(props); return React.createElement('div', null, props.children); },
    Track: props => React.createElement('div', null, props.children),
    Range: () => null,
    Thumb: () => null,
};
require.cache[sliderPath] = slider;

const toast = require('../contexts/ToastContext.tsx');
const user = require('../contexts/UserContext.tsx');
toast.useToast = () => ({ addToast() {} });
user.useUser = () => ({ user: { role: 'admin' } });
const { FilterSidebar } = require('../components/filter-sidebar.tsx');
const { FilterStageEditor } = require('../components/filter-stage-editor.tsx');
const { datetimeMs } = require('../lib/datetimes.ts');

// Its latest value is half a millisecond past 02:00 UTC, finer than a slider value (milliseconds).
const FIELD = { name: 'observed_at', type: 'datetime', origin: 'metadata', min: '2026-01-02T03:00:00.000000Z', max: '2026-01-05T02:00:00.000500Z', provenance: 'exact' };

// Render the sidebar with one datetime field; returns its markup, its slider's props and the filters it sent.
function renderSidebar(field, filters = {}) {
    sliders.length = 0;
    const sent = [];
    const client = new QueryClient({ defaultOptions: { queries: { gcTime: Infinity } } });
    const html = renderToStaticMarkup(React.createElement(QueryClientProvider, { client },
        React.createElement(FilterSidebar, { datasetId: 1, schema: [field], filters, onChange: next => sent.push(next), lightningMode: true, expandAll: true })));
    client.clear();
    assert.equal(sliders.length, 1, 'one date slider');
    return { html, slider: sliders[0], sent };
}

test('datetime text is read by the server rule: no offset is UTC, and an offset is applied', () => {
    const tenUtc = Date.UTC(2026, 0, 1, 10);
    for (const text of [
        '2026-01-01T10:00', '2026-01-01 10:00', '2026-01-01T10:00:00', '2026-01-01T10:00:00Z', '2026-01-01T10:00:00.000999Z',
        '2026-01-01T12:00:00+02:00', '2026-01-01T12:00+0200', '2026-01-01T12:00:00+02', '2026-01-01T05:00:00-05:00',
    ]) assert.equal(datetimeMs(text), tenUtc, text);
    assert.equal(datetimeMs('2026-01-01'), Date.UTC(2026, 0, 1));
    assert.equal(datetimeMs('2026-01-01T10:00:00.5Z'), tenUtc + 500);
    assert.equal(datetimeMs('2026-01-01T08:30:00.123456-01:30'), tenUtc + 123);
    for (const text of ['unknown', '', '2026-01-01T10', '2026-01-01T10:00 +02:00', '20260101']) assert.ok(Number.isNaN(datetimeMs(text)), text);
});

test('the date slider sends only the bound whose handle moved: an end left at its limit is no bound', () => {
    const { slider, sent } = renderSidebar(FIELD);
    assert.deepEqual(slider.value, [slider.min, slider.max]);
    // The lower handle moved; the upper one is still at the latest value, so nothing newer is cut off (a bound there
    // would be 02:00:00.000, before the latest sample).
    slider.onValueCommit([Date.parse('2026-01-03T12:00:00Z'), slider.max]);
    assert.deepEqual(sent.pop(), { observed_at: { min: '2026-01-03T12:00:00.000Z' } });
    slider.onValueCommit([slider.min, Date.parse('2026-01-04T00:00:00Z')]);
    assert.deepEqual(sent.pop(), { observed_at: { max: '2026-01-04T00:00:00.000Z' } });
    slider.onValueCommit([Date.parse('2026-01-03T12:00:00Z'), Date.parse('2026-01-04T00:00:00Z')]);
    assert.deepEqual(sent.pop(), { observed_at: { min: '2026-01-03T12:00:00.000Z', max: '2026-01-04T00:00:00.000Z' } });
    slider.onValueCommit([slider.min, slider.max]);
    assert.deepEqual(sent.pop(), {});
});

test('a bound already set stays when the other handle moves, and goes when its handle is back at its end', () => {
    const { slider, sent } = renderSidebar(FIELD, { observed_at: { min: '2026-01-03T12:00:00.000Z' }, other: ['x'] });
    assert.deepEqual(slider.value, [Date.parse('2026-01-03T12:00:00Z'), slider.max]);
    slider.onValueCommit([slider.value[0], Date.parse('2026-01-04T00:00:00Z')]);
    assert.deepEqual(sent.pop(), { observed_at: { min: '2026-01-03T12:00:00.000Z', max: '2026-01-04T00:00:00.000Z' }, other: ['x'] });
    slider.onValueCommit([slider.min, slider.max]);
    assert.deepEqual(sent.pop(), { other: ['x'] });
});

test('the date slider labels its ends with UTC dates, and says so', () => {
    // 03:00 UTC on 2 January is still 1 January in Chicago, and 02:00 UTC on 5 January is 4 January there.
    const { html } = renderSidebar(FIELD);
    assert.match(html, /<span>Jan 2, 2026<\/span><span[^>]*>UTC<\/span><span>Jan 5, 2026<\/span>/);
});

test('bounds written without an offset are UTC on the slider', () => {
    const { slider } = renderSidebar({ ...FIELD, min: '2026-01-02 03:00:00', max: '2026-01-05T02:00:00' });
    assert.equal(slider.min, Date.UTC(2026, 0, 2, 3));
    assert.equal(slider.max, Date.UTC(2026, 0, 5, 2));
});

test('a handle dragged back to its end reaches it, so it sends no bound', () => {
    // The slider snaps a dragged value to round((value - min) / step) * step + min, clamped to [min, max].
    for (const max of ['2026-01-05T02:00:00.000500Z', '2026-01-04T05:00:00Z', '2026-01-02T05:00:00Z', '2026-12-31T23:59:59.999Z', FIELD.min]) {
        const { slider, sent } = renderSidebar({ ...FIELD, max });
        assert.ok(Number.isInteger(slider.step) && slider.step >= 1 && slider.step <= 86_400_000, `${max}: steps of at most a day`);
        const snap = value => Math.min(slider.max, Math.max(slider.min, Math.round((value - slider.min) / slider.step) * slider.step + slider.min));
        assert.equal(snap(slider.max), slider.max, max);
        slider.onValueCommit([snap(slider.min + slider.step), snap(slider.max)]);
        assert.equal(sent.pop().observed_at?.max, undefined, max);
    }
});

// Render the filter card on one datetime rule.
function renderEditor(value) {
    const client = new QueryClient({ defaultOptions: { queries: { gcTime: Infinity } } });
    const html = renderToStaticMarkup(React.createElement(QueryClientProvider, { client }, React.createElement(FilterStageEditor, {
        stage: { type: 'filter', description: 'observed_at ≥ 2026-01-01', filter: { field: 'observed_at', operator: '>=', value } },
        onSave() {}, onCancel() {}, datasetId: 1, schema: [FIELD],
    })));
    client.clear();
    const input = html.match(/<input[^>]*type="datetime-local"[^>]*>/);
    assert.ok(input, 'a datetime picker');
    return { html, input: input[0] };
}

test("the filter card's date picker is labelled UTC and shows a value's UTC time", () => {
    const own = renderEditor('2026-01-01T09:00');
    assert.match(own.input, /aria-label="Date and time \(UTC\)"/);
    assert.match(own.input, /value="2026-01-01T09:00"/);
    assert.match(own.html, />UTC<\/span>/);
    // A value written with an offset (typed as query text, or opened from a link) shows its UTC time.
    assert.match(renderEditor('2026-01-01T10:00:00+02:00').input, /value="2026-01-01T08:00"/);
    assert.match(renderEditor('2026-01-01T09:00:00.000Z').input, /value="2026-01-01T09:00"/);
    // A date alone is midnight UTC (the picker was blank: it takes no date-only value).
    assert.match(renderEditor('2026-01-01').input, /value="2026-01-01T00:00"/);
});
