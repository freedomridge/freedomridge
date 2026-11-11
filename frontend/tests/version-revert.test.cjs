// Run with: node --test tests/version-revert.test.cjs
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
const { revertQuestion, versionMaker } = require('../lib/version-revert.ts');

const version = { name: 'v3', created_at: '2026-10-16T12:00:00Z' };
const since = new Date(version.created_at).toLocaleDateString();
const views = (added, removed = [], filters_changed = []) => ({ added, removed, filters_changed });

test('an exact revert says how many samples and views it deletes and restores', () => {
    const diff = { added: 12408, removed: 3, modified: 57, view_changes: views(['a', 'b'], ['c'], ['d']) };
    assert.deepEqual(revertQuestion(version, diff, true, false, null).split('\n'), [
        'Revert to "v3"?',
        '',
        `It deletes ${(12408).toLocaleString()} samples and 2 public saved views added since ${since}, `
        + 'and restores 3 deleted samples and 1 deleted saved view.',
        'Deleted samples lose their vectors and hashes: undoing brings the samples back without them.',
        '57 changed samples get the data they had then.',
        '1 saved view gets its filters back.',
        'Dataset settings stay unchanged.',
        'The current state is saved first, so this can be undone.',
    ]);
});

test('it names the view the dataset opens on when the revert deletes it', () => {
    const diff = { added: 0, removed: 0, modified: 0, view_changes: views(['later']) };
    const question = revertQuestion(version, diff, true, true, 'later');
    assert.ok(question.includes('One of the views it deletes is "later", the view this dataset opens on.'));
    assert.ok(question.includes('and the view the dataset opens on.'));  // restored with the settings
    assert.ok(!question.includes('lose their vectors'));  // no sample is deleted
    // Not for a default view the version has too, or when nothing added since is deleted.
    assert.ok(!revertQuestion(version, { ...diff, view_changes: views([]) }, true, false, 'later').includes('opens on'));
    assert.ok(!revertQuestion(version, diff, false, false, 'later').includes('opens on'));
});

test('a version that recorded no views claims nothing about them', () => {
    const diff = { added: 1, removed: 0, modified: 1, view_changes: null };
    const exact = revertQuestion(version, diff, true, false, 'later');
    assert.ok(exact.includes(`It deletes 1 sample added since ${since}, and restores 0 deleted samples.`));
    assert.ok(exact.includes('1 changed sample gets the data it had then.'));
    assert.ok(exact.includes('This version recorded no saved views, so the saved views stay as they are.'));
    const kept = revertQuestion(version, diff, false, false, null);
    assert.ok(kept.includes(`Nothing is deleted: the 1 sample added since ${since} stays.`));
    assert.ok(kept.includes('This version recorded no saved views'));
});

test('a restore that keeps what was added says what stays and what comes back', () => {
    const diff = { added: 4, removed: 2, modified: 0, view_changes: views(['a'], ['b', 'c'], []) };
    assert.deepEqual(revertQuestion(version, diff, false, false, null).split('\n'), [
        'Restore sample data from "v3"?',
        '',
        `Nothing is deleted: the 4 samples added since ${since} stay, and so do saved views added since.`,
        '0 changed samples get the data they had then.',
        '0 saved views get their filters back, and 2 deleted saved views are restored.',
        'Dataset settings stay unchanged.',
        'The current state is saved first, so this can be undone.',
    ]);
});

test('a version says who made it and who brought it in', () => {
    assert.equal(versionMaker({ created_by: 'ada', imported_by: null }), 'by ada');
    assert.equal(versionMaker({ created_by: null }), 'by unknown');
    assert.equal(versionMaker({ created_by: 'ada@example.com', imported_by: 'richard' }), 'by ada@example.com, imported by richard');
    assert.equal(versionMaker({ created_by: null, imported_by: 'richard' }), 'by unknown, imported by richard');
});
