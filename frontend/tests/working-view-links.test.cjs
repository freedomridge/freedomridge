// Run with: node --test tests/working-view-links.test.cjs
/* eslint-disable @typescript-eslint/no-require-imports -- Uses the Node CommonJS loader to transpile local TS. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { test } = require('node:test');
const ts = require('typescript');

require.extensions['.ts'] = (module, filename) => {
    const source = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
        compilerOptions: { module: ts.ModuleKind.CommonJS, esModuleInterop: true },
        fileName: filename,
    }).outputText;
    module._compile(source, filename);
};

const { datasetAddress, datasetLink, linkSetsFilters, MAX_LINK_LENGTH, openingOf, readLink, TOO_LONG_NOTICE } = require('../lib/links.ts');
const { conceptNames, workingViewStages } = require('../lib/filter-utils.ts');
const { missingConceptsNotice, sealedValuesNotice, withConceptChips, withoutMissingConcepts, withoutSealedValues } = require('../lib/filter-query.ts');

const params = (query) => new URLSearchParams(query);

test('w= is a link that sets the filters, beside view, q and f', () => {
    assert.deepEqual(readLink(params('w=wv_abc123')), { w: 'wv_abc123' });
    assert.deepEqual(readLink(params('w=wv_abc123&sample=7&sort=idx&dir=desc')), { w: 'wv_abc123', sample: '7', sort: 'idx', dir: 'desc' });
    assert.equal(linkSetsFilters(readLink(params('w=wv_abc123'))), true);
    assert.equal(linkSetsFilters(readLink(params('w='))), true);  // an empty handle is still a link to open (and refuse)
    assert.equal(linkSetsFilters(readLink(params('sample=7'))), false);
    // Opened from the address (not one of the page's own history entries), the page opens on the link.
    const opening = openingOf(null, '/datasets/4?w=wv_abc123', '/datasets/4?w=wv_abc123');
    assert.equal(opening.entry, null);
    assert.deepEqual(opening.link, { w: 'wv_abc123' });
});

const kansas = { field: 'location', operator: 'geo_region', value: 'US-KS' };
const concept = { field: '_concept', operator: 'concept_exclude', value: { concept_id: 7, op: '>', threshold: 0.62 } };
const serials = Array.from({ length: 2000 }, (_, i) => `SN-${String(i).padStart(5, '0')}`);

test('a working view\'s ordinary filters are named from their filters, values shown; other chips keep their words', () => {
    const stages = [
        { type: 'filter', description: 'serial_number in (2000 values kept from the assistant)', filter: { field: 'serial_number', operator: 'in', value: serials } },
        { type: 'filter', description: 'split = "train"', filter: { field: 'split', operator: '=', value: 'train' } },
        { type: 'filter', description: 'location within "US-KS"', filter: kansas },
        { type: 'filter', description: 'not concept "equipment" > 0.62', filter: concept },
        { type: 'similarity', description: 'Similar to 4410 (50)', filter: { field: 'id', operator: 'in', value: [4410, 9213] }, metadata: { count: 2 } },
    ];
    const shown = workingViewStages(stages);
    assert.equal(shown[0].description, 'serial_number: SN-00000, SN-00001 +1998');  // the person sees the values
    assert.deepEqual(shown[0].filter, stages[0].filter);
    assert.equal(shown[1].description, 'split = train');
    // A region chip names itself from its region, and a search keeps its words. A concept test on its own is named in
    // the app's words too (by its id until it is opened, when it becomes the Concepts panel's chip).
    assert.equal(shown[2], stages[2]);
    assert.deepEqual(shown[3], { ...stages[3], description: 'not concept #7 > 0.62' });
    assert.equal(shown[4], stages[4]);
    assert.deepEqual(workingViewStages([]), []);
    // A concept inside a larger filter is an ordinary chip, named by the app with the concept's name now.
    const larger = { type: 'filter', description: 'whatever the view said', filter: { logic: 'OR', filters: [concept, stages[1].filter] } };
    assert.equal(workingViewStages([larger], conceptNames([{ id: 7, name: 'equipment' }]))[0].description,
        'not concept equipment > 0.62 or split = train');
    assert.equal(workingViewStages([larger])[0].description, 'not concept #7 > 0.62 or split = train');
});

test('a stage left out for a concept that is gone is named in the app\'s words, never the view\'s', () => {
    // As the page opens a w= link: named by the app, then concept tests become chips and missing concepts go.
    const gone = { type: 'filter', description: 'words a model wrote', filter: { field: '_concept', operator: 'concept_select', value: { concept_id: 999, op: '>=', threshold: 0.5 } } };
    const goneInLarger = { type: 'filter', description: 'more words', filter: { logic: 'OR', filters: [gone.filter, { field: 'split', operator: '=', value: 'val' }] } };
    const kept = { type: 'filter', description: 'and more', filter: concept };
    const names = conceptNames([{ id: 7, name: 'equipment' }]);
    const { stages, dropped } = withoutMissingConcepts(withConceptChips(workingViewStages([gone, kept, goneInLarger]), names), names);
    assert.deepEqual(dropped, ['concept #999 ≥ 0.5', 'concept #999 ≥ 0.5 or split = val']);
    assert.equal(missingConceptsNotice(dropped, 'this link'),
        'From this link, “concept #999 ≥ 0.5”, “concept #999 ≥ 0.5 or split = val” were left out: they filter by concepts this dataset no longer has.');
    assert.equal(missingConceptsNotice(dropped.slice(0, 1), 'this link'),
        'From this link, “concept #999 ≥ 0.5” was left out: it filters by a concept this dataset no longer has.');
    // The concept the dataset has is its panel's chip.
    assert.equal(stages.length, 1);
    assert.equal(stages[0].description, 'Exclude: equipment');
});

test('a stage testing a value still sealed (pv1.…) is left out, and the notice names its field', () => {
    const sealedIn = { type: 'filter', description: 'serial in (3 values kept from the assistant)', filter: { field: 'serial', operator: 'in', value: ['SN-1', 'pv1.AbC9xyz', 'SN-3'] } };
    const sealedEq = { type: 'filter', description: 'lot = …', filter: { logic: 'OR', filters: [{ field: 'split', operator: '=', value: 'val' }, { field: 'lot', operator: '=', value: 'pv1.QQ12' }] } };
    const fine = { type: 'filter', description: 'split = train', filter: { field: 'split', operator: '=', value: 'train' } };
    const notSealed = { type: 'filter', description: 'note', filter: { field: 'note', operator: 'contains', value: 'xpv1.' } };
    assert.deepEqual(withoutSealedValues([fine, notSealed]), { stages: [fine, notSealed], fields: [] });
    assert.deepEqual(withoutSealedValues([sealedIn, fine]), { stages: [fine], fields: ['serial'] });
    assert.deepEqual(withoutSealedValues([sealedIn, sealedEq, fine, { ...sealedIn }]), { stages: [fine], fields: ['serial', 'lot'] });
    assert.equal(sealedValuesNotice(['serial'], 'this link'),
        'From this link, the filter on “serial” was left out: its values can no longer be read. The assistant can make the view again.');
    assert.equal(sealedValuesNotice(['serial', 'lot'], 'this link'),
        'From this link, the filters on “serial”, “lot” were left out: their values can no longer be read. The assistant can make the view again.');
});

test('Copy link and the address write a working view\'s filters as q= or f=, never w=', async () => {
    const origin = 'https://data.example.org';
    const plain = workingViewStages([{ type: 'filter', description: 'x', filter: { field: 'split', operator: '=', value: 'train' } }]);
    const asText = await datasetLink({ origin, datasetId: 4, stages: plain, parts: ['split = train'] });
    assert.equal(asText, `${origin}/datasets/4?q=split+%3D+train`);
    const encoded = await datasetLink({ origin, datasetId: 4, stages: plain, parts: null });
    assert.match(encoded, /^https:\/\/data\.example\.org\/datasets\/4\?f=1\./);
    for (const link of [asText, encoded]) assert.equal(new URL(link).searchParams.has('w'), false);
    // A view too long for an address: Copy link says so, and the address names no filter (nor the handle).
    const long = workingViewStages([{ type: 'filter', description: 'x', filter: { field: 'serial_number', operator: 'in', value: serials } }]);
    const text = `serial_number in (${serials.join(' ')})`;
    assert.ok(text.length > MAX_LINK_LENGTH);
    await assert.rejects(datasetLink({ origin, datasetId: 4, stages: long, parts: [text] }), { message: TOO_LONG_NOTICE });
    assert.equal(await datasetAddress({ origin, datasetId: 4, stages: long, parts: [text] }), '/datasets/4');
});
