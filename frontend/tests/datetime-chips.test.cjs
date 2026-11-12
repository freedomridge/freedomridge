// Run with: node --test tests/datetime-chips.test.cjs
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

const { describeDatetime, describeFilter } = require('../lib/filter-utils.ts');

const leaf = (operator, value, field = 'captured_at') => ({ field, operator, value });

test('a datetime bound with a time shows it, in UTC, to the precision it was written', () => {
    assert.equal(describeDatetime('2026-01-01T08:00Z'), '2026-01-01 08:00 UTC');
    assert.equal(describeDatetime('2026-01-01T08:00:00'), '2026-01-01 08:00:00 UTC'); // no offset is UTC
    assert.equal(describeDatetime('2026-01-01T08:00:00.000Z'), '2026-01-01 08:00:00.000 UTC');
    assert.equal(describeDatetime('2026-01-01T08:00:00.000001Z'), '2026-01-01 08:00:00.000001 UTC');
    assert.equal(describeDatetime('2026-01-01 08:30:15+00:00'), '2026-01-01 08:30:15 UTC');
});

test('a bound written with an offset is shown at its UTC time, across a date', () => {
    assert.equal(describeDatetime('2026-01-01T10:00:00+02:00'), '2026-01-01 08:00:00 UTC');
    assert.equal(describeDatetime('2026-01-01T01:30+0200'), '2025-12-31 23:30 UTC');
    assert.equal(describeDatetime('2026-01-01T22:00:00.5-05'), '2026-01-02 03:00:00.5 UTC');
});

test('a date alone, and text that is not a datetime, show as written', () => {
    assert.equal(describeFilter(leaf('>=', '2026-01-01')), 'captured_at ≥ 2026-01-01');
    assert.equal(describeDatetime('2026-01-01Tlater'), '2026-01-01Tlater');
});

test("a range chip shows both bounds' times", () => {
    const range = { logic: 'AND', filters: [leaf('>=', '2026-01-01T08:00:00.000Z'), leaf('<=', '2026-01-01T09:30:00.000Z')] };
    assert.equal(describeFilter(range), 'captured_at: 2026-01-01 08:00:00.000 UTC–2026-01-01 09:30:00.000 UTC');
    assert.equal(describeFilter(leaf('=', '2026-01-01T10:00+02:00')), 'captured_at = 2026-01-01 08:00 UTC');
});
