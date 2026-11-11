// Run with: node --test tests/links.test.cjs
/* eslint-disable @typescript-eslint/no-require-imports -- Uses the Node CommonJS loader to transpile local TS. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const zlib = require('node:zlib');
const { test } = require('node:test');
const ts = require('typescript');

require.extensions['.ts'] = (module, filename) => {
    const source = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
        compilerOptions: { module: ts.ModuleKind.CommonJS, esModuleInterop: true },
        fileName: filename,
    }).outputText;
    module._compile(source, filename);
};

const {
    readLink, linkSetsFilters, withoutLinkParameters, encodeStages, decodeStages, loginPath, safeNext,
    LinkError, MAX_INFLATED_BYTES, UNKNOWN_VERSION_NOTICE, BROKEN_LINK_NOTICE, OVERSIZED_LINK_NOTICE,
    datasetLink, viewLink, mayBeText, MAX_LINK_LENGTH, TOO_LONG_NOTICE,
} = require('../lib/links.ts');
const FIXTURES = require('./link-fixtures.json');

const base64url = (bytes) => Buffer.from(bytes).toString('base64url');
const encodeRaw = (payload) => `1.${base64url(zlib.deflateRawSync(Buffer.from(typeof payload === 'string' ? payload : JSON.stringify(payload))))}`;

async function refused(value, notice) {
    await assert.rejects(decodeStages(value), (error) => error instanceof LinkError && error.message === notice);
}

test('every frozen f= link still opens on its stages', async () => {
    assert.ok(FIXTURES.f.length >= 6);
    for (const fixture of FIXTURES.f) {
        assert.deepEqual(await decodeStages(fixture.f), fixture.stages, fixture.name);
    }
});

test('the frozen links cover each kind of chip', () => {
    const operators = JSON.stringify(FIXTURES.f.map((fixture) => fixture.stages));
    for (const operator of ['geo_polygon', 'embedding_polygon', 'concept_exclude', '"in"']) assert.ok(operators.includes(operator), operator);
    assert.ok(FIXTURES.f.some((fixture) => fixture.stages.some((stage) => stage.type === 'similarity')));
    assert.ok(FIXTURES.f.some((fixture) => fixture.stages.some((stage) => stage.type === 'selection')));
});

test('stages encode and decode to the same stages, ids kept in their order', async () => {
    for (const fixture of FIXTURES.f) {
        const value = await encodeStages(fixture.stages);
        assert.match(value, /^1\.[A-Za-z0-9_-]+$/);
        assert.deepEqual(await decodeStages(value), fixture.stages, fixture.name);
    }
    const ranked = [{ type: 'similarity', description: 'top 5', filter: { logic: 'AND', filters: [{ field: 'id', operator: 'in', value: [9, 3, 7, 1, 5] }] } }];
    assert.deepEqual(await decodeStages(await encodeStages(ranked)), ranked);
    // Text outside ASCII comes back as it was.
    const named = [{ type: 'filter', description: 'idx ≥ 6 · “café”', filter: { field: 'idx', operator: '>=', value: 6 } }];
    assert.deepEqual(await decodeStages(await encodeStages(named)), named);
    assert.deepEqual(await decodeStages(await encodeStages([])), []);
});

test('an encoded value reads with any raw-deflate reader (the format is not this build\'s own)', async () => {
    const stages = FIXTURES.f[0].stages;
    const value = await encodeStages(stages);
    const inflated = zlib.inflateRawSync(Buffer.from(value.slice(2), 'base64url')).toString('utf8');
    assert.deepEqual(JSON.parse(inflated), { stages });
    assert.deepEqual(await decodeStages(encodeRaw({ stages })), stages);
});

test('a version this build does not know is never guessed at', async () => {
    const value = await encodeStages(FIXTURES.f[0].stages);
    for (const other of [`2${value.slice(1)}`, `0${value.slice(1)}`, value.slice(2), `v1.${value.slice(2)}`, '', '1']) {
        await refused(other, UNKNOWN_VERSION_NOTICE);
    }
});

test('a damaged or wrongly shaped payload is refused, and nothing of it is used', async () => {
    const stage = { type: 'filter', description: 'x', filter: { field: 'idx', operator: '>=', value: 6 } };
    const value = await encodeStages([stage]);
    for (const bad of [
        `${value.slice(0, value.length - 6)}`,  // cut short when copied
        `1.${value.slice(2).replace(/[A-Za-z]/, '+')}`,  // not base64url
        '1.',
        '1.%%%',
        encodeRaw('not json'),
        encodeRaw([stage]),
        encodeRaw({ stages: 'x' }),
        encodeRaw({ stage: [stage] }),
        encodeRaw({ stages: [{ ...stage, type: 3 }] }),
        encodeRaw({ stages: [{ ...stage, description: undefined }] }),
        encodeRaw({ stages: [{ ...stage, filter: [] }] }),
        encodeRaw({ stages: [{ ...stage, filter: 'idx >= 6' }] }),
        encodeRaw({ stages: [{ ...stage, metadata: 'x' }] }),
        encodeRaw({ stages: [stage, null] }),
        `1.${base64url(zlib.deflateRawSync(Buffer.from([0xff, 0xfe, 0x7b])))}`,  // not UTF-8
    ]) {
        await refused(bad, BROKEN_LINK_NOTICE);
    }
    // Metadata is optional; when present it is an object.
    assert.deepEqual(await decodeStages(encodeRaw({ stages: [{ ...stage, metadata: { count: 1 } }] })), [{ ...stage, metadata: { count: 1 } }]);
});

test('a payload that inflates past the bound is refused, however short the link', async () => {
    const huge = JSON.stringify({ stages: [{ type: 'filter', description: ' '.repeat(MAX_INFLATED_BYTES), filter: {} }] });
    const link = encodeRaw(huge);
    assert.ok(link.length < 5_000, `${link.length}`);
    await refused(link, OVERSIZED_LINK_NOTICE);
    // Just under the bound still opens.
    const fits = JSON.stringify({ stages: [{ type: 'filter', description: '', filter: {} }] });
    const pad = MAX_INFLATED_BYTES - fits.length;
    const atBound = JSON.stringify({ stages: [{ type: 'filter', description: ' '.repeat(pad), filter: {} }] });
    assert.equal(Buffer.byteLength(atBound), MAX_INFLATED_BYTES);
    assert.equal((await decodeStages(encodeRaw(atBound)))[0].description.length, pad);
});

test('the link is read from the query string; a sample or a sort alone sets no filters', () => {
    assert.equal(readLink(new URLSearchParams('')), null);
    assert.equal(readLink(new URLSearchParams('other=1')), null);
    assert.deepEqual(readLink(new URLSearchParams('q=split%20%3D%20train&sort=idx&dir=desc')), { q: 'split = train', sort: 'idx', dir: 'desc' });
    assert.deepEqual(readLink(new URLSearchParams('dir=sideways')), { dir: 'asc' });
    assert.deepEqual(readLink(new URLSearchParams('q=')), { q: '' });
    for (const search of ['view=abc', 'q=', 'f=1.x', 'view=a&q=b&f=c']) assert.equal(linkSetsFilters(readLink(new URLSearchParams(search))), true, search);
    for (const search of ['sample=5', 'sort=idx&dir=asc', '']) assert.equal(linkSetsFilters(readLink(new URLSearchParams(search))), false, search);
});

test('removing the link keeps the rest of the address', () => {
    assert.equal(withoutLinkParameters('http://x.test/datasets/5?q=a&f=1.b&view=c&sort=d&dir=asc&sample=9'), '/datasets/5');
    assert.equal(withoutLinkParameters('http://x.test/datasets/5?keep=1&q=a#top'), '/datasets/5?keep=1#top');
    assert.equal(withoutLinkParameters('http://x.test/datasets/5'), '/datasets/5');
});

test('the login page is told where to come back to, except to itself or the home page', () => {
    assert.equal(loginPath({ pathname: '/datasets/5', search: '?q=split%20%3D%20train' }), '/login?next=%2Fdatasets%2F5%3Fq%3Dsplit%2520%253D%2520train');
    assert.equal(loginPath({ pathname: '/datasets/5', search: '' }, true), '/login?expired=true&next=%2Fdatasets%2F5');
    assert.equal(loginPath({ pathname: '/', search: '' }), '/login');
    assert.equal(loginPath({ pathname: '/', search: '' }, true), '/login?expired=true');
    assert.equal(loginPath({ pathname: '/login', search: '?next=%2Fx' }), '/login');
    // What the login page reads back is the page asked for.
    const next = new URLSearchParams(loginPath({ pathname: '/datasets/5', search: '?q=a%20%3D%201&sample=3' }).split('?')[1]).get('next');
    assert.equal(safeNext(next, 'http://x.test'), '/datasets/5?q=a%20%3D%201&sample=3');
});

test('after login only a page of this app is followed', () => {
    const origin = 'http://x.test';
    assert.equal(safeNext('/datasets/5?q=a#h', origin), '/datasets/5?q=a#h');
    assert.equal(safeNext('datasets/5', origin), '/datasets/5');
    assert.equal(safeNext('http://x.test/datasets/5', origin), '/datasets/5');
    for (const next of [
        null, undefined, '', '//evil.example', '/\\evil.example', '\\\\evil.example', 'https://evil.example',
        'http://x.test.evil.example/', '/\t/evil.example', '/\n/evil.example', 'javascript:alert(1)', 'http://[::1',
    ]) {
        assert.equal(safeNext(next, origin), '/', String(next));
    }
});

// --- Copy link ---

const ORIGIN = 'http://x.test';
const asStages = (filters) => filters.map((filter) => ({ type: 'filter', description: 'chip', filter }));
const fixture = (kind, name) => FIXTURES[kind].find((item) => item.name === name);
const params = (link) => new URL(link).searchParams;

test('ordinary filters that are all text go in q=, one part per chip', async () => {
    // As POST filter_query/format writes these fixtures' chips. backend/tests/test_link_fixtures.py checks that every
    // q fixture's chips, written so and joined with AND, read back as the same chips.
    const brackets = fixture('q', 'AND, OR, NOT and brackets');
    const parts = ['split = "val"', 'NOT (idx >= 9 OR note exists)'];
    const link = await datasetLink({ origin: ORIGIN, datasetId: 5, stages: asStages(brackets.filters), parts });
    assert.equal(new URL(link).pathname, '/datasets/5');
    assert.equal(params(link).get('q'), 'split = "val" AND NOT (idx >= 9 OR note exists)');
    assert.equal(params(link).get('f'), null);
    // A fixture written as format writes it is the fixture's own text.
    const compare = fixture('q', '> >= < <=');
    const compared = await datasetLink({ origin: ORIGIN, datasetId: 5, stages: asStages(compare.filters), parts: compare.q.split(' AND ') });
    assert.equal(readLink(params(compared)).q, compare.q);
});

test('one chip that is not text puts every stage in f=, which decodes to the same stages', async () => {
    const region = fixture('f', 'map region, with a plain filter');
    const link = await datasetLink({ origin: ORIGIN, datasetId: 5, stages: region.stages, parts: ['idx >= 2', null] });
    assert.equal(params(link).get('q'), null);
    assert.match(params(link).get('f'), /^1\.[A-Za-z0-9_-]+$/);
    assert.deepEqual(await decodeStages(params(link).get('f')), region.stages);
    // Picked samples and searches are written as text by format, but stay their chips (a search keeps its rank order).
    for (const name of ['picked samples', 'similarity search results, in rank order']) {
        const { stages } = fixture('f', name);
        assert.equal(mayBeText(stages), false, name);
        const ids = await datasetLink({ origin: ORIGIN, datasetId: 5, stages, parts: stages.map(() => 'id in (1, 2)') });
        assert.equal(params(ids).get('q'), null, name);
        assert.deepEqual(await decodeStages(params(ids).get('f')), stages, name);
    }
    // Parts not asked for, or fewer than the stages: f=.
    const plain = fixture('f', 'plain filter').stages;
    assert.match(params(await datasetLink({ origin: ORIGIN, datasetId: 5, stages: plain, parts: null })).get('f'), /^1\./);
    assert.match(params(await datasetLink({ origin: ORIGIN, datasetId: 5, stages: [...plain, ...plain], parts: ['idx >= 6'] })).get('f'), /^1\./);
});

test('no stages: an empty q, which says "no filters"; a sample and a sort that is not the default are added', async () => {
    const bare = await datasetLink({ origin: ORIGIN, datasetId: 5, stages: [], parts: null });
    assert.equal(bare, 'http://x.test/datasets/5?q=');
    for (const sort of [undefined, {}, { by: undefined, order: 'desc' }, { by: '', order: 'asc' }]) {
        assert.equal(await datasetLink({ origin: ORIGIN, datasetId: 5, stages: [], parts: null, sort }), bare, JSON.stringify(sort));
    }
    const sorted = await datasetLink({ origin: ORIGIN, datasetId: 5, stages: [], parts: null, sort: { by: 'idx', order: 'desc' }, sample: 42 });
    assert.deepEqual(readLink(params(sorted)), { q: '', sort: 'idx', dir: 'desc', sample: '42' });
    const ascending = await datasetLink({ origin: ORIGIN, datasetId: 5, stages: [], parts: null, sort: { by: 'idx' } });
    assert.deepEqual(readLink(params(ascending)), { q: '', sort: 'idx', dir: 'asc' });
});

test('a stage holding a selection token makes no link, and names the chip', async () => {
    const token = {
        type: 'selection', description: 'Grid (60000 Selected)',
        filter: { logic: 'AND', filters: [{ field: 'id', operator: 'in_selection', value: 'tok' }, { field: 'id', operator: 'not in', value: [3] }] },
    };
    const stages = [...fixture('f', 'plain filter').stages, token];
    await assert.rejects(
        datasetLink({ origin: ORIGIN, datasetId: 5, stages, parts: null }),
        (error) => error instanceof LinkError && error.message.includes('“Grid (60000 Selected)”') && error.message.includes('Save a view and copy its link'),
    );
});

test('a link over 6,000 characters is refused, pointing to a saved view', async () => {
    const value = (n) => [`note = "${'x'.repeat(n)}"`];
    const length = async (n) => (await datasetLink({ origin: ORIGIN, datasetId: 5, stages: asStages([{}]), parts: value(n) })).length;
    const fits = MAX_LINK_LENGTH - ((await length(0)));
    assert.equal(await length(fits), MAX_LINK_LENGTH);
    await assert.rejects(
        datasetLink({ origin: ORIGIN, datasetId: 5, stages: asStages([{}]), parts: value(fits + 1) }),
        (error) => error instanceof LinkError && error.message === TOO_LONG_NOTICE,
    );
    // Picked samples: ids of a 3.6M-sample dataset, in no order (a search's rank). A few hundred fit, thousands don't.
    let seed = 7;
    const ids = Array.from({ length: 5000 }, () => (seed = (seed * 48271) % 2147483647) % 3_600_000);
    const picked = (n) => [{ type: 'selection', description: `${n} Selected`, filter: { field: 'id', operator: 'in', value: ids.slice(0, n) } }];
    assert.ok((await datasetLink({ origin: ORIGIN, datasetId: 5, stages: picked(500), parts: null })).length <= MAX_LINK_LENGTH);
    await assert.rejects(datasetLink({ origin: ORIGIN, datasetId: 5, stages: picked(5000), parts: null }), (error) => error.message === TOO_LONG_NOTICE);
});

test('+, &, #, = and text outside ASCII in a value come back as they were', async () => {
    const part = 'note = "a+b & c # d = e ?x=1 café ✓ 100%"';
    const link = await datasetLink({ origin: ORIGIN, datasetId: 5, stages: asStages([{}]), parts: [part], sample: 7 });
    assert.ok(!link.includes('+b'), link);
    assert.equal(new URL(link).hash, '');
    assert.equal(params(link).get('q'), part);
    assert.equal(params(link).get('sample'), '7');
    assert.deepEqual(readLink(params(link)), { q: part, sample: '7' });
});

test('a view link names the view by its slug', () => {
    assert.equal(viewLink(ORIGIN, 5, 'later-half-123'), 'http://x.test/datasets/5?view=later-half-123');
    const odd = viewLink(ORIGIN, 5, 'a b&c#d');
    assert.deepEqual(readLink(params(odd)), { view: 'a b&c#d' });
});
