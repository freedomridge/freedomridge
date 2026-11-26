// Run with: node --test tests/csv-cells-spec.test.cjs
/* eslint-disable @typescript-eslint/no-require-imports -- Uses the Node CommonJS loader to transpile local TS. */
// A CSV cell the Export dialog writes never runs as a formula in a spreadsheet, and a number stays a number
// (contract "a CSV the app writes can't run as a formula", sections 1 to 3). Written from the contract, not from the
// code: the contract names the one function that writes a cell, `escapeCsvField(value)` in lib/csv.ts (2.1), and
// every case of tests/fixtures/csv-formula-cells.json is its value and the whole cell written for it, quotes included.
// The same file drives the Export dialog's browser test (e2e/export-csv-formula-cells.spec.ts) and the value list
// download's (backend/tests/test_csv_formula_cells_spec.py).
//
// Every expected cell follows the contract's four steps (1.1-1.4, "The rule as steps"): pieces cut at `;`, tab, line
// feed, carriage return, U+0085, U+2028 and U+2029; unless every piece is exactly one number or empty, an apostrophe
// in front of each piece whose first character past its leading white space and double quotes is a formula sign; an
// apostrophe at the very start of a text whose first character is a tab or a carriage return; then every text
// wrapped in double quotes. A value that is a number by type is bare, null is an empty cell. The fixture's `rows` are
// whole rows of the dialog: their cells joined with commas, first column first. Readings left to these tests (also
// in the report to the contract's owner):
// - The header row (`=x`, `width`) is the same function's (2.1: "for every cell and for the header").
// - "-.5" is read as a number: an optional sign, then ".5", which 1.2 names a number.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const { test } = require('node:test');
const ts = require('typescript');

const root = path.resolve(__dirname, '..');
const originalResolve = Module._resolveFilename;
Module._resolveFilename = function (specifier, ...args) {
    if (specifier.startsWith('@/')) specifier = path.join(root, specifier.slice(2));
    return originalResolve.call(this, specifier, ...args);
};
require.extensions['.ts'] = (module, filename) => {
    const source = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
        compilerOptions: { module: ts.ModuleKind.CommonJS, esModuleInterop: true },
        fileName: filename,
    }).outputText;
    module._compile(source, filename);
};

const { cases, rows } = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures', 'csv-formula-cells.json'), 'utf8'));

// Loaded once; when lib/csv.ts or its export is missing, every case fails saying so instead of the file failing to load.
let escapeCsvField;
let missing = '';
try {
    ({ escapeCsvField } = require(path.join(root, 'lib', 'csv.ts')));
    if (typeof escapeCsvField !== 'function') missing = 'lib/csv.ts does not export a function escapeCsvField';
} catch (error) {
    missing = `lib/csv.ts could not be loaded: ${error.message.split('\n')[0]}`;
}

test('the fixture holds every row of section 3', () => {
    // 44 rows: section 3's table, the two header rows and the two whole rows among them.
    assert.equal(cases.filter(c => c.contract === '3').length + rows.filter(r => r.contract === '3').length, 44);
    assert.equal(new Set([...cases, ...rows].map(c => c.case)).size, cases.length + rows.length);
});

for (const c of cases) {
    test(`${c.case} (${c.contract}) is written ${JSON.stringify(c.cell)}`, () => {
        assert.ok(!missing, missing);
        assert.equal(escapeCsvField(c.value), c.cell, `${JSON.stringify(c.value)}${c.note ? ` (${c.note})` : ''}`);
    });
}

for (const r of rows) {
    test(`${r.case} (${r.contract}) is the line ${JSON.stringify(r.line)}`, () => {
        // What a reader taking `;` for the separator sees is the raw line, so the line is compared, not its cells.
        assert.ok(!missing, missing);
        assert.equal(r.values.map(v => escapeCsvField(v)).join(','), r.line, JSON.stringify(r.values));
    });
}
