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

const { passwordResetMessage, RESET_PASSWORD_HINT } = require('../lib/password-reset.ts');

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
