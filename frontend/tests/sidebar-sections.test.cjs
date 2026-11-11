// Run with: node --test tests/sidebar-sections.test.cjs
/* eslint-disable @typescript-eslint/no-require-imports -- Uses the Node CommonJS loader to transpile local TS/TSX. */
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

const sections = require('../lib/sidebar-sections.ts');
const { convertSimpleFiltersToComplex } = require('../lib/filter-utils.ts');
const api = require('../lib/api.ts');
const toast = require('../contexts/ToastContext.tsx');
const user = require('../contexts/UserContext.tsx');
toast.useToast = () => ({ addToast() {} });
user.useUser = () => ({ user: { role: 'admin' } });
const { FilterSidebar } = require('../components/filter-sidebar.tsx');

// A browser's localStorage for the duration of `run` (Node has none).
function withStorage(storage, run) {
    const hadWindow = 'window' in global;
    const previous = global.window;
    global.window = { localStorage: storage };
    try { return run(); } finally {
        if (hadWindow) global.window = previous; else delete global.window;
    }
}
function memoryStorage(entries = {}) {
    const data = new Map(Object.entries(entries));
    return { getItem: key => (data.has(key) ? data.get(key) : null), setItem: (key, value) => data.set(key, String(value)), data };
}

const SCHEMA = [
    { name: 'brightness', type: 'number', origin: 'metadata', min: 10, max: 300, provenance: 'pending' },
    { name: 'color', type: 'enum', origin: 'metadata', values: ['c0', 'c1', 'c2'], values_complete: true, provenance: 'pending' },
    { name: 'batch', type: 'enum', origin: 'metadata', values: ['early', 'late'], values_complete: true, provenance: 'pending' },
    { name: 'flagged', type: 'boolean', origin: 'metadata' },
];
const LATE = { logic: 'AND', filters: [{ field: 'batch', operator: 'in', value: ['late'] }] };

// Render the sidebar; returns its markup, the field names it asked filtered values for, and a way to
// start those queries' requests the way react-query does (one after another, in one go).
function render(props) {
    // gcTime Infinity: no garbage-collection timers left behind when a window is faked.
    const client = new QueryClient({ defaultOptions: { queries: { gcTime: Infinity } } });
    const html = renderToStaticMarkup(React.createElement(QueryClientProvider, { client },
        React.createElement(FilterSidebar, { datasetId: 1, schema: SCHEMA, filters: {}, onChange() {}, lightningMode: true, ...props })));
    const queries = client.getQueryCache().findAll({ queryKey: ['schema', props.datasetId ?? 1, 'facet'] });
    const facets = queries.map(q => q.queryKey[4]);
    const startRequests = () => queries.map(q => q.options.queryFn({ client, queryKey: q.queryKey, signal: new AbortController().signal, meta: undefined }));
    const options = queries.map(q => q.options);
    client.clear();
    return { html, facets, startRequests, options };
}

// A fetch that records each facet request and streams its NDJSON answer line by line, as the test
// sends them (`line`, `raw`, `finish` = the `done` line and the end, `end` = the end alone, or
// `release` for every field and the `done` line); an abort fails the stream.
function holdingFetch() {
    const calls = [];
    const encoder = new TextEncoder();
    const fetch = async (url, init) => {
        const body = JSON.parse(init.body);
        let stream;
        const response = new Response(new ReadableStream({ start(controller) { stream = controller; } }), {
            headers: { 'content-type': 'application/x-ndjson' },
        });
        const call = {
            path: new URL(url, 'http://app').pathname, body, signal: init.signal,
            line: (name, extra = {}) => stream.enqueue(encoder.encode(
                `${JSON.stringify({ name, type: 'number', min: 1, max: 2, provenance: 'exact', ...extra })}\n`)),
            raw: text => stream.enqueue(encoder.encode(`${text}\n`)),
            end: () => stream.close(),
            finish: () => { call.raw('{"done": true}'); call.end(); },
            release: () => { for (const name of body.fields) call.line(name); call.finish(); },
        };
        calls.push(call);
        init.signal.addEventListener('abort', () => stream.error(new DOMException('Aborted', 'AbortError')));
        return response;
    };
    return { calls, fetch };
}
async function withFetch(fake, run) {
    const original = global.fetch;
    global.fetch = fake;
    try { return await run(); } finally { global.fetch = original; }
}
// Longer than the batcher's frame (it settles the lines of one frame together).
const tick = () => new Promise(resolve => setTimeout(resolve, 40));
function toggle(html, name) {
    const tag = html.match(new RegExp(`<button[^>]*data-testid="field-toggle-${name.replace('.', '\\.')}"[^>]*>`));
    assert.ok(tag, `no header toggle for ${name}`);
    return tag[0];
}
const expanded = (html, name) => /aria-expanded="true"/.test(toggle(html, name));

test('the fields a draft or an applied stage constrains', () => {
    const drafts = {
        color: ['red'], 'exclude:shape': ['square'], brightness: { min: 1, max: 2 }, note: { _op: 'is_null' },
        'size:min': 5, label: { _op: 'text', values: [], contains: ['ab'] },
        nothing: [], blank: { _op: 'text', values: [], contains: [] },
    };
    assert.deepEqual([...sections.filteredFieldNames(drafts)].sort(), ['brightness', 'color', 'label', 'note', 'shape', 'size']);
    const applied = { logic: 'AND', filters: [
        { field: 'batch', operator: 'in', value: ['late'] },
        { logic: 'NOT', filters: [{ logic: 'OR', filters: [{ field: 'level', operator: '>=', value: 3 }, { field: 'tags', operator: 'contains', value: 'x' }] }] },
    ] };
    assert.deepEqual([...sections.filteredFieldNames({}, applied)].sort(), ['batch', 'level', 'tags']);
    assert.deepEqual([...sections.filteredFieldNames({ color: ['red'] }, applied)].sort(), ['batch', 'color', 'level', 'tags']);
    assert.equal(sections.filteredFieldNames({}, { logic: 'AND', filters: [] }).size, 0);
});

test('expanded fields are remembered per dataset, and storage problems never throw', () => {
    // No browser storage at all.
    assert.deepEqual(sections.readExpandedFields(1), []);
    sections.storeFieldExpanded(1, 'a', true, []);
    // Storage that refuses access.
    const refusing = { getItem() { throw new Error('denied'); }, setItem() { throw new Error('quota'); } };
    withStorage(refusing, () => {
        assert.deepEqual(sections.readExpandedFields(1), []);
        sections.storeFieldExpanded(1, 'a', true, []);
    });
    const storage = memoryStorage({ 'fridge-sidebar-expanded-3': '{not json', 'fridge-sidebar-expanded-4': '{"a":1}', 'fridge-sidebar-expanded-5': '["a",2,null,"b"]' });
    withStorage(storage, () => {
        sections.storeFieldExpanded(1, 'brightness', true, []);
        sections.storeFieldExpanded(1, 'color', true, []);
        assert.deepEqual(sections.readExpandedFields(1), ['brightness', 'color']);
        assert.deepEqual(sections.readExpandedFields(2), []);
        assert.deepEqual(sections.readExpandedFields(3), []);
        assert.deepEqual(sections.readExpandedFields(4), []);
        assert.deepEqual(sections.readExpandedFields(5), ['a', 'b']);
    });
    assert.equal(storage.data.get('fridge-sidebar-expanded-1'), '["brightness","color"]');
});

test('every field starts collapsed: a header with its name and type, no inputs', () => {
    const { html, facets } = render({});
    for (const field of SCHEMA) {
        assert.equal(expanded(html, field.name), false, field.name);
        assert.match(toggle(html, field.name), /type="button"/);
        assert.doesNotMatch(toggle(html, field.name), /aria-controls/);
        assert.match(html, new RegExp(`>${field.name}</span><span[^>]*>${field.type}</span>`));
    }
    assert.doesNotMatch(html, /10 - 300/);
    assert.doesNotMatch(html, /type="checkbox"/);
    assert.doesNotMatch(html, />True</);
    assert.doesNotMatch(html, /data-filtered/);
    assert.deepEqual(facets, []);
});

test('a field with a draft or an applied filter starts expanded, with its inputs', () => {
    const { html } = render({ filters: { color: ['c1'] }, appliedFilters: LATE, globalFilters: LATE, filterActive: true });
    assert.equal(expanded(html, 'color'), true);
    assert.equal(expanded(html, 'batch'), true);
    assert.equal(expanded(html, 'brightness'), false);
    assert.equal(expanded(html, 'flagged'), false);
    assert.match(html, /<input type="checkbox" class="accent-cyan-500" checked=""\/><span class="text-cyan-400">c1<\/span>/);
    assert.match(html, />late<\/span>/);
    assert.doesNotMatch(html, /10 - 300/);
    assert.match(html, /data-testid="field-section-color" data-filtered="true"/);
    assert.match(html, /data-testid="field-section-batch" data-filtered="true"/);
    assert.match(html, /data-testid="field-section-brightness" class/);
    assert.match(html, /<span class="sr-only">, filtered<\/span>/);
    // The expanded header names the region it controls, which is there.
    const controls = toggle(html, 'color').match(/aria-controls="([^"]+)"/)[1];
    assert.ok(html.includes(`id="${controls}"`));
    assert.match(toggle(html, 'color'), /focus-visible:ring-2/);
});

test('legacy exclusions remain read-only and separate from include-only inputs for every picker type', () => {
    for (const field of [
        SCHEMA[1],
        { ...SCHEMA[1], declared_type: 'string' },
        { ...SCHEMA[1], type: 'string' },
        { ...SCHEMA[1], type: 'array', origin: 'native' },
    ]) {
        const filters = { 'exclude:color': ['c1'] };
        const before = structuredClone(filters);
        const { html } = render({ schema: [field], filters });
        assert.equal(expanded(html, 'color'), true);
        assert.match(html, /Additional filter: color: not c1/);
        assert.match(html, /aria-label="Remove color exclusion"/);
        assert.doesNotMatch(html, /match mode|<option[^>]*>Exclude<|checked=""/);
        assert.deepEqual(filters, before);
        assert.deepEqual(convertSimpleFiltersToComplex(filters), { field: 'color', operator: 'not in', value: ['c1'] });
    }
});

test('a legacy excluded text union stays visible beside included values', () => {
    const selection = { _op: 'text', values: ['c1'], contains: ['red'] };
    const filters = { color: ['c2'], 'exclude:color': selection };
    const props = { schema: [{ ...SCHEMA[1], declared_type: 'string' }], filters };
    const { html } = render(props);
    assert.match(html, /Additional filter: not \(color: c1 or contains &quot;red&quot;\)/);
    assert.doesNotMatch(html, /match mode/);
    assert.deepEqual(convertSimpleFiltersToComplex({ 'exclude:color': selection }), {
        logic: 'NOT', filters: [{ logic: 'OR', filters: [
            { field: 'color', operator: 'in', value: ['c1'] },
            { field: 'color', operator: 'contains', value: 'red' },
        ] }],
    });
});

test('lightning mode with a stage: only expanded fields ask for filtered values', () => {
    // Nothing the user expanded: only the filtered field (expanded because it filters the view).
    const filtered = render({ appliedFilters: LATE, globalFilters: LATE, filterActive: true });
    assert.deepEqual(filtered.facets, ['batch']);
    assert.match(filtered.html, /data-testid="facet-status-brightness" data-provenance="pending"[^>]*><span>Dataset values · expand for filtered values</);
    assert.match(filtered.html, /data-testid="facet-status-batch" data-provenance="pending"[^>]*><span>Updating filtered values…</);
    // A remembered expanded field asks too; collapsed ones still do not.
    const storage = memoryStorage({ 'fridge-sidebar-expanded-1': '["brightness"]' });
    const remembered = withStorage(storage, () => render({ appliedFilters: LATE, globalFilters: LATE, filterActive: true }));
    assert.deepEqual(remembered.facets.sort(), ['batch', 'brightness']);
    // With no stage there is nothing to compute, expanded or not.
    const unfiltered = withStorage(storage, () => render({}));
    assert.deepEqual(unfiltered.facets, []);
    assert.doesNotMatch(unfiltered.html, /facet-status-/);
});

test('the expanded fields are the ones remembered for this dataset', () => {
    const storage = memoryStorage({ 'fridge-sidebar-expanded-1': '["brightness"]' });
    withStorage(storage, () => {
        const first = render({ datasetId: 1 });
        assert.equal(expanded(first.html, 'brightness'), true);
        assert.match(first.html, /10 - 300/);
        const other = render({ datasetId: 2 });
        assert.equal(expanded(other.html, 'brightness'), false);
        assert.doesNotMatch(other.html, /10 - 300/);
    });
});

test('lightning off: no filtered-value requests or status lines; an expanded field shows the eager values', () => {
    const eager = SCHEMA.map(field => (field.name === 'brightness' ? { ...field, min: 280, max: 300, provenance: 'exact' } : { ...field, provenance: 'exact' }));
    const storage = memoryStorage({ 'fridge-sidebar-expanded-1': '["brightness"]' });
    const { html, facets } = withStorage(storage, () => render({
        schema: eager, lightningMode: false, appliedFilters: LATE, globalFilters: LATE, filterActive: true,
    }));
    assert.deepEqual(facets, []);
    assert.doesNotMatch(html, /facet-status-/);
    assert.equal(expanded(html, 'brightness'), true);
    assert.match(html, /280 - 300/);
    assert.equal(expanded(html, 'color'), false);
});

test('a toggle keeps what another tab stored and drops fields the dataset no longer has', () => {
    const known = ['brightness', 'color', 'batch'];
    // Another tab expanded color; `gone` was a field that has since been deleted.
    const storage = memoryStorage({ 'fridge-sidebar-expanded-1': '["color","gone"]' });
    withStorage(storage, () => {
        sections.storeFieldExpanded(1, 'brightness', true, known);
        assert.deepEqual(sections.readExpandedFields(1), ['color', 'brightness']);
        storage.setItem('fridge-sidebar-expanded-1', '["color","brightness","batch"]');  // the other tab again
        sections.storeFieldExpanded(1, 'color', false, known);
        assert.deepEqual(sections.readExpandedFields(1), ['brightness', 'batch']);
        // While the schema is unknown nothing is pruned.
        storage.setItem('fridge-sidebar-expanded-1', '["later"]');
        sections.storeFieldExpanded(1, 'brightness', true, []);
        assert.deepEqual(sections.readExpandedFields(1), ['later', 'brightness']);
    });
});

test('facets started together share one request; an aborted field stops waiting alone', async () => {
    const { calls, fetch } = holdingFetch();
    await withFetch(fetch, async () => {
        const gone = new AbortController();
        gone.abort();  // dropped before the request goes out: not asked for
        const later = new AbortController();
        const a = api.getFieldFacet(7, LATE, 'a');
        const b = api.getFieldFacet(7, LATE, 'b', later.signal);
        const c = api.getFieldFacet(7, LATE, 'c', gone.signal);
        const other = api.getFieldFacet(7, { logic: 'AND', filters: [] }, 'a');  // another filter: its own request
        await assert.rejects(c, { name: 'AbortError' });
        await tick();
        assert.equal(calls.length, 2);
        assert.equal(calls[0].path, '/api/datasets/7/field-stats');
        assert.deepEqual(calls[0].body, { filters: LATE, lightning_mode: true, fields: ['a', 'b'], stream: true });
        assert.deepEqual(calls[1].body.fields, ['a']);
        later.abort();
        await assert.rejects(b, { name: 'AbortError' });
        assert.equal(calls[0].signal.aborted, false);  // `a` still waits on it
        calls[0].release();
        assert.deepEqual((await a).map(field => field.name), ['a']);
        calls[1].release();
        assert.deepEqual((await other).map(field => field.name), ['a']);
        // Once every field in a request is aborted, the request is aborted (the server then stops).
        const x = new AbortController();
        const y = new AbortController();
        const pending = [api.getFieldFacet(7, LATE, 'x', x.signal), api.getFieldFacet(7, LATE, 'y', y.signal)];
        await tick();
        assert.equal(calls.length, 3);
        x.abort();
        assert.equal(calls[2].signal.aborted, false);
        y.abort();
        assert.equal(calls[2].signal.aborted, true);
        for (const promise of pending) await assert.rejects(promise, { name: 'AbortError' });
        // A failed request fails each of its fields.
        const failing = [api.getFieldFacet(7, LATE, 'p'), api.getFieldFacet(7, LATE, 'q')];
        await tick();
        calls[3].signal.dispatchEvent(new Event('abort'));
        for (const promise of failing) await assert.rejects(promise);
    });
});

test('after a filter change the expanded fields send one request, not one each', async () => {
    const storage = memoryStorage({ 'fridge-sidebar-expanded-1': '["brightness","color"]' });
    const { facets, startRequests } = withStorage(storage, () => render({ appliedFilters: LATE, globalFilters: LATE, filterActive: true }));
    assert.deepEqual(facets, ['brightness', 'color', 'batch']);
    const { calls, fetch } = holdingFetch();
    await withFetch(fetch, async () => {
        const results = startRequests();
        await tick();
        assert.equal(calls.length, 1);
        assert.deepEqual(calls[0].body.fields, ['brightness', 'color', 'batch']);
        calls[0].release();
        assert.deepEqual((await Promise.all(results)).map(fields => fields.map(field => field.name)), [['brightness'], ['color'], ['batch']]);
    });
});

test('a field whose statistics are pending has nothing to expand and asks for nothing', () => {
    const schema = [...SCHEMA, { name: 'sampled_at', type: 'datetime', origin: 'metadata', stats_pending: true, provenance: 'pending' }];
    const stage = { logic: 'AND', filters: [{ field: 'sampled_at', operator: '>=', value: '2024-01-01' }] };
    const storage = memoryStorage({ 'fridge-sidebar-expanded-1': '["sampled_at"]' });
    const { html, facets } = withStorage(storage, () => render({ schema, appliedFilters: stage, globalFilters: stage, filterActive: true }));
    assert.deepEqual(facets, []);
    assert.doesNotMatch(html, /field-toggle-sampled_at/);
    assert.match(html, /data-testid="pending-field">Statistics pending</);
});

test('while the previous filter\'s schema is shown, no field claims filtered values', () => {
    // The default view's eager answer, kept as placeholder while the first stage's request runs.
    const previous = SCHEMA.map(field => ({ ...field, provenance: 'exact' }));
    const props = { schema: previous, appliedFilters: LATE, globalFilters: LATE, filterActive: true };
    const { html } = render({ ...props, valuesUpdating: true });
    assert.doesNotMatch(html, /Filtered values/);
    assert.doesNotMatch(html, /data-provenance="exact"/);
    assert.match(html, /data-testid="facet-status-brightness" data-provenance="updating"[^>]*><span>Updating filtered values…</);
    // Once this filter's own answer is in, its labels show.
    assert.match(render(props).html, /data-testid="facet-status-brightness" data-provenance="exact"[^>]*><span class="text-cyan-400">Filtered values</);
});

test('each field resolves when its own line arrives; an error line fails that field alone', async () => {
    const { calls, fetch } = holdingFetch();
    await withFetch(fetch, async () => {
        const settled = [];
        const track = (name, promise) => promise.then(
            fields => settled.push(`${name}:${fields.map(field => field.min).join()}`),
            error => settled.push(`${name}:failed ${error.message}`),
        );
        const fast = track('fast', api.getFieldFacet(8, LATE, 'fast'));
        const slow = track('slow', api.getFieldFacet(8, LATE, 'slow'));
        const broken = track('plant-id', api.getFieldFacet(8, LATE, 'plant-id'));
        const missing = track('gone', api.getFieldFacet(8, LATE, 'gone'));
        await tick();
        assert.equal(calls.length, 1);
        calls[0].line('fast', { min: 5 });
        await tick();
        assert.deepEqual(settled, ['fast:5']);  // the slow field holds nothing back
        calls[0].line('plant-id', { provenance: 'cached', error: "Invalid field name: 'plant-id'" });
        await tick();
        assert.deepEqual(settled, ['fast:5', "plant-id:failed Invalid field name: 'plant-id'"]);
        calls[0].line('slow', { min: 7 });
        calls[0].finish();
        await Promise.all([fast, slow, broken, missing]);
        // A field the answer did not include keeps its sidebar values (an empty facet list).
        assert.deepEqual(settled, ['fast:5', "plant-id:failed Invalid field name: 'plant-id'", 'slow:7', 'gone:']);
    });
});

test('the facet request reuses the sidebar request\'s breadth for the same filter', async () => {
    const { calls, fetch } = holdingFetch();
    const sidebar = (breadth) => async () => new Response('[]', { headers: breadth ? { 'X-Filter-Breadth': breadth } : {} });
    const other = { logic: 'AND', filters: [{ field: 'level', operator: '>=', value: 3 }] };
    await withFetch(sidebar('selective'), () => api.getDatasetSchema(9, LATE, true));
    await withFetch(sidebar('undecided'), () => api.getDatasetSchema(9, other, true));
    await withFetch(fetch, async () => {
        const known = api.getFieldFacet(9, LATE, 'a');
        const undecided = api.getFieldFacet(9, other, 'a');
        const unknown = api.getFieldFacet(10, LATE, 'a');
        await tick();
        assert.deepEqual(calls.map(call => call.body.breadth), ['selective', undefined, undefined]);
        for (const call of calls) call.release();
        await Promise.all([known, undecided, unknown]);
    });
});

test('only a promoted range is asked for while the previous filter\'s schema stands in', () => {
    // The default view's answer (labels `exact`) stands in for the new filter's.
    const schema = [...SCHEMA, { name: 'speed', type: 'number', origin: 'metadata', is_promoted: true, min: 0, max: 9, provenance: 'exact' }];
    const storage = memoryStorage({ 'fridge-sidebar-expanded-1': '["brightness","color","speed"]' });
    const props = { schema, appliedFilters: LATE, globalFilters: LATE, filterActive: true };
    const updating = withStorage(storage, () => render({ ...props, valuesUpdating: true }));
    assert.deepEqual(updating.facets, ['speed']);  // it never needs the probe or the sidebar's answer
    const current = withStorage(storage, () => render({
        ...props, schema: schema.map(field => ({ ...field, provenance: 'pending' })),
    }));
    assert.deepEqual(current.facets, ['brightness', 'color', 'batch', 'speed']);
});

test('a stream that ends without its done line fails every field still waiting', async () => {
    const { calls, fetch } = holdingFetch();
    await withFetch(fetch, async () => {
        const settled = [];
        const track = (name, promise) => promise.then(() => settled.push(`${name}:ok`), error => settled.push(`${name}:${error.message}`));
        const all = [track('a', api.getFieldFacet(11, LATE, 'a')), track('b', api.getFieldFacet(11, LATE, 'b'))];
        await tick();
        calls[0].line('a');
        calls[0].end();  // the server's request failed after the 200 and the stream just ended
        await Promise.all(all);
        assert.deepEqual(settled.sort(), ['a:ok', 'b:The filtered values stopped before they were complete']);
        assert.equal(calls[0].signal.aborted, true);
    });
});

test('a line that is not JSON fails the field it names, not the ones after it', async () => {
    const { calls, fetch } = holdingFetch();
    await withFetch(fetch, async () => {
        const settled = [];
        const track = (name, promise) => promise.then(() => settled.push(`${name}:ok`), error => settled.push(`${name}:${error.message}`));
        const all = ['f004', 'f005', 'f006'].map(name => track(name, api.getFieldFacet(12, LATE, name)));
        await tick();
        calls[0].line('f004');
        calls[0].raw('{"name": "f005", "type": "number", "min": 1, "max": NaN}');
        calls[0].line('f006');
        calls[0].finish();
        await Promise.all(all);
        assert.deepEqual(settled.sort(), ['f004:ok', "f005:Could not read this field's filtered values", 'f006:ok']);
    });
});

test('the lines of one frame settle together, and query notifications run in one task', async () => {
    const { calls, fetch } = holdingFetch();
    await withFetch(fetch, async () => {
        const settled = [];
        const all = ['a', 'b'].map(name => api.getFieldFacet(13, LATE, name).then(() => settled.push(name)));
        await tick();
        calls[0].line('a');
        await new Promise(resolve => setTimeout(resolve, 4));
        assert.deepEqual(settled, []);  // held for the rest of the frame
        calls[0].line('b');
        await tick();
        assert.deepEqual(settled, ['a', 'b']);
        calls[0].finish();
        await Promise.all(all);
    });
    const { notifyManager } = require('@tanstack/react-query');
    require('../lib/query-notify.ts').coalesceQueryNotifications();
    const timers = [];
    const originalSetTimeout = global.setTimeout;
    global.setTimeout = (callback, ms) => { timers.push(ms); return originalSetTimeout(callback, ms); };
    const ran = [];
    try {
        for (const name of ['x', 'y', 'z']) notifyManager.schedule(() => ran.push(name));
    } finally {
        global.setTimeout = originalSetTimeout;
    }
    assert.equal(timers.length, 1);  // one task for the three notifications
    await tick();
    assert.deepEqual(ran, ['x', 'y', 'z']);
});

test('an undecided sidebar answer forgets the breadth remembered for that filter', async () => {
    const { calls, fetch } = holdingFetch();
    const sidebar = (breadth) => async () => new Response('[]', { headers: breadth ? { 'X-Filter-Breadth': breadth } : {} });
    await withFetch(sidebar('selective'), () => api.getDatasetSchema(14, LATE, true));
    await withFetch(sidebar('undecided'), () => api.getDatasetSchema(14, LATE, true));
    await withFetch(fetch, async () => {
        const facet = api.getFieldFacet(14, LATE, 'a');
        await tick();
        assert.equal(calls[0].body.breadth, undefined);
        calls[0].release();
        await facet;
    });
});

test('a field no filter can name is never asked for, and says so without Retry', () => {
    const schema = [...SCHEMA, { name: 'plant-id', type: 'number', origin: 'metadata', min: 1, max: 20, provenance: 'cached', filterable: false }];
    const storage = memoryStorage({ 'fridge-sidebar-expanded-1': '["plant-id"]' });
    const { html, facets } = withStorage(storage, () => render({ schema, appliedFilters: LATE, globalFilters: LATE, filterActive: true }));
    assert.deepEqual(facets, ['batch']);
    assert.match(html, /data-testid="facet-status-plant-id" data-provenance="cached"[^>]*><span title="Its name has characters[^"]*">Dataset values · this field can&#x27;t be filtered</);
    assert.doesNotMatch(html, /Retry/);
});

const RESCORED = 'Concept "sky" is being rescored; use it again when its scoring job finishes';

test('a concept being rescored refuses before the stream or in it; each field gets the 409 that retries', async () => {
    // Before the stream: the request answers 409.
    await withFetch(async () => new Response(JSON.stringify({ detail: RESCORED }), { status: 409 }), async () => {
        const errors = await Promise.all(['a', 'b'].map(name => api.getFieldFacet(15, LATE, name).catch(error => error)));
        for (const error of errors) {
            assert.ok(error instanceof api.ApiError && error.status === 409 && api.isRescoringRefusal(error));
        }
    });
    // In the stream: the fields not yet answered come back as refusal lines.
    const { calls, fetch } = holdingFetch();
    await withFetch(fetch, async () => {
        const answers = ['a', 'b'].map(name => api.getFieldFacet(16, LATE, name).then(() => 'ok', error => error));
        await tick();
        calls[0].line('a');
        calls[0].raw(JSON.stringify({ name: 'b', refusal: { status: 409, detail: RESCORED } }));
        calls[0].finish();
        const [a, b] = await Promise.all(answers);
        assert.equal(a, 'ok');
        assert.ok(b instanceof api.ApiError && api.isRescoringRefusal(b));
    });
    // The sidebar's facet queries retry such a refusal (and nothing else), as every other view does.
    const { options } = render({ appliedFilters: LATE, globalFilters: LATE, filterActive: true });
    assert.equal(options[0].retry(0, new api.ApiError(409, RESCORED)), true);
    assert.equal(options[0].retry(0, new Error('boom')), false);
    assert.equal(options[0].retry(0, new api.ApiError(409, 'The map of clip_img was re-fitted')), false);
});

test('a lasso refused in the stream reaches the map_refitted path that drops it', async () => {
    const { calls, fetch } = holdingFetch();
    const page = new EventTarget();
    const heard = [];
    page.addEventListener(api.MAP_REFITTED_EVENT, event => heard.push(event.detail));
    const hadWindow = 'window' in global;
    global.window = page;
    try {
        await withFetch(fetch, async () => {
            const answer = api.getFieldFacet(17, LATE, 'brightness').then(() => 'ok', error => error);
            await tick();
            calls[0].raw(JSON.stringify({ name: 'brightness', refusal: {
                status: 409, detail: 'The map of clip_img was re-fitted on 2026-10-21 08:58 UTC after this lasso selection was drawn',
                code: 'map_refitted', dataset_id: 17, field: 'clip_img', model_id: 'new-model',
            } }));
            calls[0].finish();
            const error = await answer;
            assert.ok(error instanceof api.ApiError && error.status === 409 && error.code === 'map_refitted');
            assert.equal(api.isRescoringRefusal(error), false);
        });
    } finally {
        if (hadWindow) global.window = page; else delete global.window;
    }
    assert.deepEqual(heard, [{ datasetId: 17, field: 'clip_img', modelId: 'new-model' }]);
});

test('fields refused at different moments retry together, in one request', async () => {
    const storage = memoryStorage({ 'fridge-sidebar-expanded-1': '["brightness","color"]' });
    const { options } = withStorage(storage, () => render({ appliedFilters: LATE, globalFilters: LATE, filterActive: true }));
    // Each query was refused once (react-query's state while it retries), the second field 40 ms later.
    const client = { getQueryState: () => ({ fetchFailureReason: new api.ApiError(409, RESCORED), fetchFailureCount: 1 }) };
    const retry = option => option.queryFn({ client, queryKey: option.queryKey, signal: new AbortController().signal, meta: undefined });
    // The page's clock 80 ms before its next retry turn, so the test does not wait the whole period.
    const now = performance.now;
    const offset = (api.RESCORING_RETRY_MS - 80) - (now.call(performance) % api.RESCORING_RETRY_MS);
    performance.now = () => now.call(performance) + offset;
    const { calls, fetch } = holdingFetch();
    try {
        await withFetch(fetch, async () => {
            const first = retry(options[0]);
            await tick();
            const second = retry(options[1]);
            assert.equal(calls.length, 0);  // both wait for the turn
            await tick();
            await tick();
            assert.equal(calls.length, 1);
            assert.deepEqual(calls[0].body.fields, ['brightness', 'color']);
            calls[0].release();
            assert.deepEqual((await Promise.all([first, second])).map(fields => fields.map(field => field.name)), [['brightness'], ['color']]);
        });
    } finally {
        performance.now = now;
    }
    assert.equal(options[0].retryDelay, 0);  // the turn is the wait
    assert.equal(api.rescoringRetryPeriod(11), api.RESCORING_RETRY_MS);
    assert.equal(api.rescoringRetryPeriod(12), 6 * api.RESCORING_RETRY_MS);
});

test('small dataset: every section opens expanded, and one facet request computes them all', async () => {
    const { html, facets, startRequests } = render({ appliedFilters: LATE, globalFilters: LATE, filterActive: true, expandAll: true });
    for (const field of SCHEMA) assert.equal(expanded(html, field.name), true, field.name);
    assert.match(html, /10 - 300/);
    assert.deepEqual(facets, ['brightness', 'color', 'batch']);
    const { calls, fetch } = holdingFetch();
    await withFetch(fetch, async () => {
        const results = startRequests();
        await tick();
        assert.equal(calls.length, 1);
        assert.deepEqual(calls[0].body.fields, ['brightness', 'color', 'batch']);
        calls[0].release();
        await Promise.all(results);
    });
    // Without a stage nothing is computed, expanded or not; lightning off computes nothing on expand either.
    assert.deepEqual(render({ expandAll: true }).facets, []);
    assert.deepEqual(render({ appliedFilters: LATE, globalFilters: LATE, filterActive: true, expandAll: true, lightningMode: false }).facets, []);
});

test('large dataset: unindexed sections keep the collapsed default; filtered and expanded fields open', () => {
    const { html, facets } = render({ appliedFilters: LATE, globalFilters: LATE, filterActive: true, expandAll: false });
    assert.equal(expanded(html, 'batch'), true);
    for (const name of ['brightness', 'color', 'flagged']) assert.equal(expanded(html, name), false, name);
    assert.deepEqual(facets, ['batch']);
});

test('large dataset: indexed sections of every filterable type start expanded', () => {
    const fields = [
        { name: 'text', type: 'string' },
        { name: 'category', type: 'enum', values: ['a', 'b'], values_complete: true },
        { name: 'count', type: 'number', min: 1, max: 10 },
        { name: 'timestamp', type: 'datetime', min: '2024-01-01', max: '2024-01-02' },
        { name: 'boxes.label', type: 'enum', origin: 'system', values: ['car', 'truck'], values_complete: true },
        { name: 'tags', type: 'array', origin: 'native_array', values: ['a', 'b'], values_complete: true },
        { name: 'flag', type: 'boolean' },
    ];
    for (const is_promoted of [undefined, false, true]) {
        const schema = fields.map(field => ({ ...field, is_promoted }));
        const { html } = render({ schema, expandAll: false });
        for (const field of fields) assert.equal(expanded(html, field.name), is_promoted === true, field.name);
    }
});

test('large dataset: an indexed field remembers collapse and re-expansion per dataset', () => {
    const schema = SCHEMA.map(field => ({ ...field, is_promoted: field.name === 'brightness' }));
    const props = { schema, expandAll: false, appliedFilters: LATE, globalFilters: LATE, filterActive: true };
    withStorage(memoryStorage(), () => {
        assert.deepEqual(render(props).facets, ['brightness', 'batch']);
        sections.storeFieldExpanded(1, 'brightness', false, schema.map(field => field.name));
        assert.deepEqual(sections.readCollapsedFields(1), ['brightness']);
        const collapsed = render(props);
        assert.equal(expanded(collapsed.html, 'brightness'), false);
        assert.deepEqual(collapsed.facets, ['batch']);
        assert.equal(expanded(render({ ...props, datasetId: 2 }).html, 'brightness'), true);
        // A filter on the collapsed field still opens it, as on small datasets.
        assert.equal(expanded(render({ ...props, filters: { brightness: { min: 10 } } }).html, 'brightness'), true);
        sections.storeFieldExpanded(1, 'brightness', true, schema.map(field => field.name));
        assert.deepEqual(sections.readCollapsedFields(1), []);
        assert.deepEqual(sections.readExpandedFields(1), ['brightness']);
        assert.equal(expanded(render(props).html, 'brightness'), true);
    });
});

test('indexed fields with pending statistics still show only their headers', () => {
    const schema = ['number', 'datetime', 'unknown'].map(type => ({
        name: `pending_${type}`, type, origin: 'metadata', is_promoted: true, stats_pending: true, provenance: 'pending',
    }));
    for (const expandAll of [false, true]) {
        const { html, facets } = render({ schema, expandAll, globalFilters: LATE, filterActive: true });
        for (const field of schema) {
            assert.match(html, new RegExp(`data-testid="field-section-${field.name}"`));
            assert.doesNotMatch(html, new RegExp(`field-toggle-${field.name}`));
        }
        assert.equal((html.match(/Statistics pending/g) || []).length, schema.length);
        assert.deepEqual(facets, []);
    }
});

test('an explicit collapse is remembered beside the expansions, and wins over the small-dataset default', () => {
    const storage = memoryStorage({ 'fridge-sidebar-expanded-1': '["color"]' });
    withStorage(storage, () => {
        sections.storeFieldExpanded(1, 'color', false, []);
        sections.storeFieldExpanded(1, 'brightness', false, []);
        sections.storeFieldExpanded(1, 'flagged', true, []);
        assert.deepEqual(sections.readCollapsedFields(1), ['color', 'brightness']);
        assert.deepEqual(sections.readExpandedFields(1), ['flagged']);
        sections.storeFieldExpanded(1, 'color', true, ['color', 'brightness', 'flagged']);
        assert.deepEqual(sections.readCollapsedFields(1), ['brightness']);
        assert.deepEqual(sections.readExpandedFields(1), ['flagged', 'color']);
    });
    const small = withStorage(storage, () => render({ appliedFilters: LATE, globalFilters: LATE, filterActive: true, expandAll: true }));
    assert.equal(expanded(small.html, 'brightness'), false);
    for (const name of ['color', 'batch', 'flagged']) assert.equal(expanded(small.html, name), true, name);
    assert.deepEqual(small.facets, ['color', 'batch']);
    // The same choices on a dataset that has grown past the line: the remembered expansions still open.
    const large = withStorage(storage, () => render({ appliedFilters: LATE, globalFilters: LATE, filterActive: true }));
    assert.deepEqual(['brightness', 'color', 'batch', 'flagged'].map(name => expanded(large.html, name)), [false, true, true, true]);
});

test('arbitrary box and polyline fields have separate overlay rows and legacy alias state', () => {
    const { html } = render({ schema: [
        { name: 'vehicles', type: 'detections' },
        { name: 'pedestrians', type: 'detections' },
        { name: 'lanes', type: 'polygons' },
    ], activeOverlayFields: ['vehicles.label', 'lanes'], onToggleOverlay() {} });
    for (const name of ['vehicles', 'pedestrians', 'lanes']) {
        assert.match(html, new RegExp(`data-testid="overlay-toggle-${name}" aria-pressed="${name !== 'pedestrians'}"`));
    }
});

test("a mirror's own fields come first, under On this server, and the primary's after them", () => {
    const own = [
        { name: '@local.weeds', type: 'number', origin: 'local', owner: 'self', min: 1, max: 9 },
        { name: '@local.tags', type: 'array', origin: 'local', owner: 'self', values: ['checked'] },
    ];
    const { html } = render({ schema: [...SCHEMA.map(f => ({ ...f, owner: 'primary' })), ...own] });
    const at = text => html.indexOf(text);
    assert.ok(at('On this server') > 0 && at('From the primary') > at('On this server'), html);
    assert.ok(at('>@local.weeds<') > at('On this server') && at('>@local.weeds<') < at('From the primary'));
    assert.ok(at('>brightness<') > at('From the primary'));
    // Any other dataset's list has no such headings.
    const plain = render({}).html;
    assert.equal(plain.includes('On this server') || plain.includes('From the primary'), false);
});
