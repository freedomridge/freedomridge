// Run with: node --test tests/index-advisor-notes.test.cjs
/* eslint-disable @typescript-eslint/no-require-imports -- Transpile the real module for Node tests. */
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
const { overlapPreferenceNote } = require('../lib/index-advisor-notes.ts');

const index = (name, scans, size_bytes) => ({ name, scans, size_bytes });
const pair = (redundant, kept) => ({ redundant, kept, rule: 'prefix', reason: 'r' });

test('a removable index used more than its replacement says where its lookups would go', () => {
    // The live sample_hashes case: (sample_id) next to the unique (sample_id, field_name).
    const note = overlapPreferenceNote(pair(
        index('idx_sample_hashes_dataset', 14732042, 148062208),
        index('sample_hashes_sample_id_field_name_key', 330463, 363446272),
    ));
    assert.equal(note,
        'Postgres currently uses this index more than sample_hashes_sample_id_field_name_key '
        + `(${(14732042).toLocaleString()} vs ${(330463).toLocaleString()} uses). `
        + 'Removing it moves those lookups to sample_hashes_sample_id_field_name_key, which is 2.5× larger.');
});

test('no note when the replacement is used at least as much (the unused metadata GIN)', () => {
    assert.equal(overlapPreferenceNote(pair(index('idx_samples_metadata_gin', 0, 5741158400),
        index('idx_samples_metadata', 133, 14318174208))), null);
    assert.equal(overlapPreferenceNote(pair(index('a', 5, 10), index('b', 5, 20))), null);
});

test('no size clause when the replacement is not larger, and no division by zero', () => {
    const smaller = overlapPreferenceNote(pair(index('a', 9, 100), index('b', 1, 50)));
    assert.ok(smaller.endsWith('moves those lookups to b.'));
    const empty = overlapPreferenceNote(pair(index('a', 9, 0), index('b', 1, 50)));
    assert.ok(empty.endsWith('moves those lookups to b.'));
    assert.ok(!empty.includes('Infinity') && !empty.includes('NaN'));
});
