// Run with: node --test tests/job-status.test.cjs
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

const { waitingNote } = require('../lib/job-status.ts');

const contended = { type: 'QueueContended', message: 'Another index is being built or removed on embeddings; retrying later' };
const created_at = '2026-11-11T12:00:00Z';
const at = (minutes) => Date.parse(created_at) + minutes * 60_000;

test('a job put back because what it needs is busy reads as waiting, with how long', () => {
    assert.equal(
        waitingNote({ state: 'queued', error: contended, created_at }, at(0.5)),
        'Waiting under a minute: Another index is being built or removed on embeddings; retrying later',
    );
    assert.equal(waitingNote({ state: 'running', error: contended, created_at }, at(12)), `Waiting 12 min: ${contended.message}`);
    // From when it started, once it has.
    const started_at = '2026-11-11T13:00:00Z';
    assert.equal(waitingNote({ state: 'queued', error: contended, created_at, started_at }, at(180)), `Waiting 2 h: ${contended.message}`);
    assert.equal(waitingNote({ state: 'queued', error: contended, created_at }, at(135)), `Waiting 2 h 15 min: ${contended.message}`);
});

test('a message that already says it is waiting is not doubled', () => {
    const backup = { type: 'QueueContended', message: 'Waiting for the database backup to finish' };
    assert.equal(waitingNote({ state: 'queued', error: backup, created_at }, at(3)), 'Waiting 3 min: for the database backup to finish');
});

test('a failure, a finished job and a job with no error are not waiting', () => {
    assert.equal(waitingNote({ state: 'queued', error: { type: 'JobConflict', message: 'budget' }, created_at }), null);
    assert.equal(waitingNote({ state: 'failed', error: contended, created_at }), null);
    assert.equal(waitingNote({ state: 'queued', error: null, created_at }), null);
    assert.equal(waitingNote({ state: 'queued', created_at }), null);
});

test('a queued job that has not started says what it waits for, from when it was queued', () => {
    const waiting_for = 'for another index build to finish (one index build at a time)';
    assert.equal(waitingNote({ state: 'queued', created_at, waiting_for }, at(42)), `Waiting 42 min: ${waiting_for}`);
    // What put it back says more once it has started; a finished job waits for nothing.
    assert.equal(waitingNote({ state: 'queued', error: contended, created_at, waiting_for }, at(1)), `Waiting 1 min: ${contended.message}`);
    assert.equal(waitingNote({ state: 'succeeded', created_at, waiting_for }), null);
    assert.equal(waitingNote({ state: 'queued', created_at, waiting_for: null }), null);
});

const { notRebuilt } = require('../lib/job-status.ts');

test('a rebuild that ended without building is not shown as a finished build', () => {
    const ended = { kind: 'field_index_build', state: 'succeeded', progress: { message: 'Not rebuilt yet: Unfinished bulk jobs block this operation: rename job 7' } };
    assert.equal(notRebuilt(ended), true);
    // From the result alone (an admin sees it), or the message alone (anyone with access does).
    assert.equal(notRebuilt({ ...ended, progress: { message: null }, result: { rebuilt: false, message: 'Not rebuilt yet: …' } }), true);
    assert.equal(notRebuilt({ ...ended, progress: { message: 'Field index built' }, result: { message: 'Field captured promoted successfully' } }), false);
    assert.equal(notRebuilt({ ...ended, state: 'failed' }), false);
    assert.equal(notRebuilt({ ...ended, kind: 'ann_index_build' }), false);
});

const { canRetryIndexBuild } = require('../lib/job-status.ts');

test('a failed or cancelled vector index build offers a retry to a power user or admin, until a later build', () => {
    const failed = { kind: 'ann_index_build', state: 'failed', dataset_id: 3, field: 'clip', created_at: '2026-11-11T12:00:00Z' };
    assert.equal(canRetryIndexBuild(failed, [failed], 'admin'), true);
    assert.equal(canRetryIndexBuild({ ...failed, state: 'cancelled' }, [], 'power_user'), true);
    assert.equal(canRetryIndexBuild(failed, [failed], 'viewer'), false);
    assert.equal(canRetryIndexBuild(failed, [failed], undefined), false);
    assert.equal(canRetryIndexBuild({ ...failed, state: 'succeeded' }, [], 'admin'), false);
    assert.equal(canRetryIndexBuild({ ...failed, kind: 'field_index_build' }, [], 'admin'), false);
    assert.equal(canRetryIndexBuild({ ...failed, field: null }, [], 'admin'), false);
    // A later build of the same field that is waiting, running or done replaces it; another field's doesn't.
    const later = { ...failed, state: 'running', created_at: '2026-11-11T13:00:00Z' };
    assert.equal(canRetryIndexBuild(failed, [later, failed], 'admin'), false);
    assert.equal(canRetryIndexBuild(failed, [{ ...later, state: 'failed' }, failed], 'admin'), true);
    assert.equal(canRetryIndexBuild(failed, [{ ...later, field: 'dino' }, failed], 'admin'), true);
});
