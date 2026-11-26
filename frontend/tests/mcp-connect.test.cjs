// Run with: node --test tests/mcp-connect.test.cjs
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

const { claudeCodeCommand, MCP_TOKEN_HELP, MCP_TOKEN_KEPT, mcpAddress, mcpHeader } = require('../lib/mcp-connect.ts');
const { deactivationMessage, passwordResetMessage } = require('../lib/password-reset.ts');

test('an AI client connects to this origin\'s /mcp with the token in the Authorization header', () => {
    assert.equal(mcpAddress('https://fr.example.org'), 'https://fr.example.org/mcp');
    assert.equal(mcpAddress('http://localhost:3000/'), 'http://localhost:3000/mcp');
    assert.equal(mcpHeader('abc_DEF-123'), 'Authorization: Bearer abc_DEF-123');
});

test('Claude Code adds the server as a remote HTTP server with the header, as its documentation says', () => {
    assert.equal(
        claudeCodeCommand('https://fr.example.org', 'abc_DEF-123'),
        'claude mcp add --transport http freedomridge https://fr.example.org/mcp '
        + '--header "Authorization: Bearer abc_DEF-123"',
    );
});

test('the MCP kind has one line of help saying what it opens', () => {
    assert.ok(!MCP_TOKEN_HELP.includes('\n'));
    assert.match(MCP_TOKEN_HELP, /AI client/);
    assert.match(MCP_TOKEN_HELP, /only the MCP server/);
});

test('revoked MCP tokens are counted and named, and only when there were some', () => {
    assert.equal(
        deactivationMessage({ full: 1, follower: 0, worker: 0, mcp: 2 }),
        'Account deactivated, and 3 API tokens are revoked, including 2 MCP tokens. '
        + 'AI clients using them can no longer connect.',
    );
    assert.equal(
        passwordResetMessage({ full: 0, follower: 0, worker: 0, mcp: 1 }),
        'Password reset. They are signed out, and 1 API token is revoked, including 1 MCP token. '
        + 'An AI client using it can no longer connect.',
    );
    assert.equal(deactivationMessage({ full: 0, follower: 0, worker: 0 }), 'Account deactivated. They had no API tokens.');
});

const { callDoor, callDuration, callOutcome, callPerson, callsShowing } = require('../lib/assistant-calls.ts');

test('the recent calls table says how each call ended, who made it and how long it took', () => {
    assert.equal(callOutcome({ ok: true, error_code: null }), 'OK');
    assert.equal(callOutcome({ ok: false, error_code: 'busy' }), 'Busy');
    assert.equal(callOutcome({ ok: false, error_code: 'timeout' }), 'Took too long');
    assert.equal(callOutcome({ ok: false, error_code: 'refused' }), 'Refused');
    assert.equal(callOutcome({ ok: false, error_code: 'something_new' }), 'something_new');
    assert.equal(callPerson({ username: 'sam' }), 'sam');
    assert.equal(callPerson({ username: null }), 'Deleted user');
    assert.equal(callDoor('mcp'), 'MCP');
    assert.equal(callDoor('chat'), 'Chat');
    assert.equal(callDuration(182), '182 ms');
    assert.equal(callDuration(60000), '60.0 s');
    assert.equal(callsShowing({ offset: 0, total: 0 }, 0), 'No calls in the last 30 days.');
    assert.equal(callsShowing({ offset: 50, total: 312 }, 50), 'Calls 51–100 of 312');
});

test('beside the command, the page says where the token ends up and how to keep it out of there', () => {
    assert.match(MCP_TOKEN_KEPT, /shell's history/);
    assert.match(MCP_TOKEN_KEPT, /plain text/);
    assert.match(MCP_TOKEN_KEPT, /\.mcp\.json/);
    assert.ok(MCP_TOKEN_KEPT.includes('"Authorization": "Bearer ${FRIDGE_MCP_TOKEN}"'));
});
