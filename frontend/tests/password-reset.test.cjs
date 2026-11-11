// Run with: node --test tests/password-reset.test.cjs
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

const {
    DEACTIVATE_HINT, deactivationMessage, passwordChangeMessage, passwordResetMessage, RESET_PASSWORD_HINT,
    RESET_PASSWORD_KEEP_TOKENS_HINT,
} = require('../lib/password-reset.ts');

test('the reset says what it revoked, and names follower and worker tokens only when there were some', () => {
    assert.equal(passwordResetMessage({ full: 0, follower: 0, worker: 0 }), 'Password reset. They are signed out; they had no API tokens.');
    assert.equal(passwordResetMessage(undefined), 'Password reset. They are signed out; they had no API tokens.');
    assert.equal(passwordResetMessage({ full: 2, follower: 0, worker: 0 }), 'Password reset. They are signed out, and 2 API tokens are revoked.');
    assert.equal(
        passwordResetMessage({ full: 2, follower: 1, worker: 0 }),
        'Password reset. They are signed out, and 3 API tokens are revoked, including 1 follower token. '
        + 'Give that follower a new token (Settings → Followers).',
    );
    assert.equal(
        passwordResetMessage({ full: 0, follower: 2, worker: 1 }),
        'Password reset. They are signed out, and 3 API tokens are revoked, including 2 follower tokens and 1 worker token. '
        + 'Give those followers a new token (Settings → Followers). A worker using it stopped.',
    );
    assert.equal(passwordResetMessage({ full: 1 }), 'Password reset. They are signed out, and 1 API token is revoked.');
});

test('the hint under Reset Password says followers and workers are affected, before the click', () => {
    assert.match(RESET_PASSWORD_HINT, /follower and worker tokens/);
    assert.match(RESET_PASSWORD_HINT, /stop syncing/);
});

test('deactivating says what it revoked, naming follower and worker tokens only when there were some', () => {
    assert.equal(deactivationMessage({ full: 0, follower: 0, worker: 0 }), 'Account deactivated. They had no API tokens.');
    assert.equal(deactivationMessage(undefined), 'Account deactivated. They had no API tokens.');
    assert.equal(deactivationMessage({ full: 1, follower: 0, worker: 0 }), 'Account deactivated, and 1 API token is revoked.');
    assert.equal(deactivationMessage({ full: 2, follower: 0, worker: 0 }), 'Account deactivated, and 2 API tokens are revoked.');
    assert.equal(
        deactivationMessage({ full: 1, follower: 1, worker: 0 }),
        'Account deactivated, and 2 API tokens are revoked, including 1 follower token. '
        + 'Give that follower a new token (Settings → Followers).',
    );
    assert.equal(
        deactivationMessage({ full: 0, follower: 2, worker: 2 }),
        'Account deactivated, and 4 API tokens are revoked, including 2 follower tokens and 2 worker tokens. '
        + 'Give those followers a new token (Settings → Followers). Workers using them stopped.',
    );
});

test('the hint under Active Account says, before saving, that deactivating revokes the tokens for good', () => {
    assert.match(DEACTIVATE_HINT, /revokes their API tokens for good/);
    assert.match(DEACTIVATE_HINT, /follower and worker tokens/);
    assert.match(DEACTIVATE_HINT, /reactivating the account doesn't bring them back/);
});

test('a reset with "Also revoke their API tokens" unticked says the tokens are kept, before and after', () => {
    assert.equal(RESET_PASSWORD_KEEP_TOKENS_HINT, 'Signs them out. Their API tokens are kept and still work.');
    for (const revoked of [undefined, { full: 0, follower: 0, worker: 0 }]) {
        assert.equal(passwordResetMessage(revoked, false), 'Password reset. They are signed out; their API tokens still work.');
    }
    // Ticked, as before (and as a caller that doesn't say).
    assert.equal(passwordResetMessage({ full: 2 }, true), 'Password reset. They are signed out, and 2 API tokens are revoked.');
});

test('changing your own password with "Also revoke my API tokens" says what it revoked, in the same words', () => {
    assert.equal(passwordChangeMessage({ full: 0, follower: 0, worker: 0 }), 'Password changed. You had no API tokens.');
    assert.equal(passwordChangeMessage(undefined), 'Password changed. You had no API tokens.');
    assert.equal(passwordChangeMessage({ full: 1, follower: 0, worker: 0 }), 'Password changed, and 1 API token is revoked.');
    assert.equal(
        passwordChangeMessage({ full: 1, follower: 0, worker: 1 }),
        'Password changed, and 2 API tokens are revoked, including 1 worker token. A worker using it stopped.',
    );
    assert.equal(
        passwordChangeMessage({ full: 0, follower: 2, worker: 0 }),
        'Password changed, and 2 API tokens are revoked, including 2 follower tokens. '
        + 'Give those followers a new token (Settings → Followers).',
    );
});
