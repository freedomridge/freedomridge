// Run with: node --test tests/csv-cell-edges.test.cjs
/* eslint-disable @typescript-eslint/no-require-imports -- Uses the Node CommonJS loader to transpile local TS. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const ts = require('typescript');

require.extensions['.ts'] = (module, filename) => {
    const source = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
        compilerOptions: { module: ts.ModuleKind.CommonJS },
        fileName: filename,
    }).outputText;
    module._compile(source, filename);
};

const { escapeCsvField: cell } = require(path.join(__dirname, '..', 'lib', 'csv.ts'));

test('every text is quoted; a number, null and an empty text are written bare', () => {
    assert.equal(cell('plain'), '"plain"');
    assert.equal(cell('width'), '"width"');
    assert.equal(cell(true), '"true"');
    assert.equal(cell(-5), '-5');
    assert.equal(cell(-0.000001), '-0.000001');
    assert.equal(cell(-1e-7), '-1e-7');
    assert.equal(cell(null), '');
    assert.equal(cell(undefined), '');
    assert.equal(cell(''), '');
    assert.equal(cell([]), '');
    assert.equal(cell({ a: '=x' }), '"{""a"":""=x""}"');
    assert.equal(cell({ a: 'x;=y' }), `"{""a"":""x;'=y""}"`);
});

test('a text is one number only with nothing else in it', () => {
    for (const text of ['-5', '-1e-3', '+1E+3', '-.5', '-5.', '-0']) assert.equal(cell(text), `"${text}"`);
    for (const text of ['-5 ', '-inf', '-Infinity', '-1_000', '-0x10', '-5%', '-1e5e5', '+', '-']) {
        assert.equal(cell(text), `"'${text}"`, text);
    }
    assert.equal(cell('\uFF0D5'), `"'\uFF0D5"`);  // only an ASCII sign makes a number
    assert.equal(cell('\u22125'), `"'\u22125"`);
    assert.equal(cell('"-5'), `"'""-5"`);
});

test('a sign after leading white space or double quotes, or a look-alike sign, starts a formula', () => {
    assert.equal(cell(' =1+1'), `"' =1+1"`);
    assert.equal(cell(' -5'), `"' -5"`);
    for (const space of ['\u000B', '\u000C', '\u00A0', '\u3000', '\u200B', '\uFEFF']) {
        assert.equal(cell(`${space}=1`), `"'${space}=1"`);
    }
    const signs = ['\uFF1D', '\uFF0B', '\uFF0D', '\uFF20', '\u2212', '\uFE62', '\uFE63', '\uFE66'];
    for (const sign of signs) assert.equal(cell(`${sign}1+1`), `"'${sign}1+1"`);
    assert.equal(cell(' "" @x'), `"' """" @x"`);
    assert.equal(cell(' plain'), '" plain"');
    assert.equal(cell('\u00A0plain'), '"\u00A0plain"');
});

test('a tab or a carriage return first gets an apostrophe at the very start, whatever follows', () => {
    assert.equal(cell('\tplain'), `"'\tplain"`);
    assert.equal(cell('\rplain'), `"'\rplain"`);
    assert.equal(cell('\t-5'), `"'\t-5"`);
    assert.equal(cell('\r=1'), `"'\r'=1"`);
    assert.equal(cell('\r'), `"'\r"`);
    assert.equal(cell('\nplain'), '"\nplain"');
});

test('each piece after a ; a tab or a line break is checked on its own', () => {
    assert.equal(cell('one\r\n=1'), `"one\r\n'=1"`);
    assert.equal(cell('a;\n=1'), `"a;\n'=1"`);
    assert.equal(cell('\n=1+1'), `"\n'=1+1"`);
    assert.equal(cell('a\r\n+x'), `"a\r\n'+x"`);
    assert.equal(cell('a; \uFEFF=1'), `"a;' \uFEFF=1"`);
    for (const lineBreak of ['\u0085', '\u2028', '\u2029']) assert.equal(cell(`a${lineBreak}=1`), `"a${lineBreak}'=1"`);
    assert.equal(cell('\u2028=1'), `"\u2028'=1"`);
    assert.equal(cell('a;;=1'), `"a;;'=1"`);
    assert.equal(cell(';'), '";"');
    assert.equal(cell(' \n @x'), `" \n' @x"`);
});

test('number pieces are left alone only when every piece is a number or empty', () => {
    assert.equal(cell('-5;3'), '"-5;3"');
    assert.equal(cell('\n-5'), '"\n-5"');
    assert.equal(cell('-5\n'), '"-5\n"');
    assert.equal(cell([-5, 3, '', '+3.2']), '"-5;3;;+3.2"');
    assert.equal(cell('a;-5'), `"a;'-5"`);
    assert.equal(cell('5;-5;=x;-y'), `"5;'-5;'=x;'-y"`);
    assert.equal(cell(['=a', '-5', ' @b']), `"'=a;'-5;' @b"`);
});

test("section 3's rows", () => {
    assert.equal(cell('=1+1'), `"'=1+1"`);
    assert.equal(cell('=HYPERLINK("http://x","y")'), `"'=HYPERLINK(""http://x"",""y"")"`);
    assert.equal(cell('+cmd'), `"'+cmd"`);
    assert.equal(cell('-cmd'), `"'-cmd"`);
    assert.equal(cell('@SUM(A1)'), `"'@SUM(A1)"`);
    assert.equal(cell('\t=1'), `"'\t'=1"`);
    assert.equal(cell(-5), '-5');
    assert.equal(cell('-5'), '"-5"');
    assert.equal(cell('+3.2'), '"+3.2"');
    assert.equal(cell('-5 apples'), `"'-5 apples"`);
    assert.equal(cell('--5'), `"'--5"`);
    assert.equal(cell('a,b'), '"a,b"');
    assert.equal(cell('say "hi"'), '"say ""hi"""');
    assert.equal(cell('l1\nl2'), '"l1\nl2"');
    assert.equal(cell('plain'), '"plain"');
    assert.equal(cell(null), '');
    assert.equal(cell(['=a', 'b']), `"'=a;b"`);
    assert.equal(cell(['a', '=b']), `"a;'=b"`);
    assert.equal(cell('=x'), `"'=x"`);
    assert.equal(cell('width'), '"width"');
    assert.equal(cell(' =1+1'), `"' =1+1"`);
    assert.equal(cell('\n=1+1'), `"\n'=1+1"`);
    assert.equal(cell('\uFF1D1+1'), `"'\uFF1D1+1"`);
    assert.equal(cell('- 5'), `"'- 5"`);
    assert.equal(cell('-inf'), `"'-inf"`);
    assert.equal(cell([-5, 3]), '"-5;3"');
    assert.equal(cell([-5]), '"-5"');
    assert.equal(cell('a;=1+1'), `"a;'=1+1"`);
    assert.equal(cell('x;@SUM(A1:A9)'), `"x;'@SUM(A1:A9)"`);
    assert.equal(cell('a\t=1+1'), `"a\t'=1+1"`);
    assert.equal(cell('one\n=1+1'), `"one\n'=1+1"`);
    assert.equal(cell('a;b'), '"a;b"');
    assert.equal(cell('a;-5;+3.2'), `"a;'-5;'+3.2"`);
    assert.equal(cell('-5;3;+3.2'), '"-5;3;+3.2"');
    assert.equal(cell('\u00A0=1'), `"'\u00A0=1"`);
    assert.equal(cell('\u22121+1'), `"'\u22121+1"`);
    assert.equal(cell('a;"=1+1'), `"a;'""=1+1"`);
    assert.equal(cell('range 3; -5 to 5'), `"range 3;' -5 to 5"`);
    assert.equal(cell(['@ann', '@bob']), `"'@ann;'@bob"`);
    assert.equal(cell('-5\n*HYPERLINK(A1)'), `"'-5\n*HYPERLINK(A1)"`);
    assert.equal(cell('-2\t*cmd'), `"'-2\t*cmd"`);
    assert.equal(cell('+3.2\n/1+X(A1)'), `"'+3.2\n/1+X(A1)"`);
    assert.equal([-5, '5*HYPERLINK(A1)'].map(cell).join(','), '-5,"5*HYPERLINK(A1)"');
    assert.equal([-5, '*X(A1)'].map(cell).join(','), '-5,"*X(A1)"');
});
