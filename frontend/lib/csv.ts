// A cell of the CSV the Export dialog writes. Opened in a spreadsheet, a cell that starts with a formula sign runs as a
// formula, so a value someone put in the data could run on the machine of whoever opens the export. Some spreadsheets
// first trim leading white space, and some take look-alikes of the signs too. A text that would start a formula gets
// an apostrophe in front, which spreadsheets take to mean "this is text". A spreadsheet reading the file with another
// separator (`;` where that is the regional list separator, or a tab) begins a new cell after each `;` and tab, and a
// new row after each line break, inside our quotes too, so each piece of the text between them is checked on its own.
// The assistant's value list download keeps the same rule (backend/routers/assistant_views.py).
const FORMULA_SIGNS: ReadonlySet<string> = new Set([
    '=', '+', '-', '@',
    '\uFF1D', '\uFF0B', '\uFF0D', '\uFF20',  // their full-width forms
    '\u2212', '\uFE62', '\uFE63', '\uFE66',  // the minus sign, and the small plus, hyphen and equals
]);
// Where one piece ends and the next begins: a `;`, a tab or a line break.
const PIECE_BREAK = /([;\t\n\r\u0085\u2028\u2029])/;
// Skipped before the sign: a piece's leading white space, one explicit set (never the language's own trim; a tab or a
// line break ends a piece), and its leading double quotes.
const SKIPPED = /^[ \v\f\u00A0\u3000\u200B\uFEFF"]*/;
// A text that is nothing but one number (`-5`, `+3.2`, `-1e-3`) is a number, not a formula.
const ONE_NUMBER = /^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$/;

/** `text` with an apostrophe in front of each piece that begins with a formula sign (unless every piece is one number
 * or empty: `-5;3` is left alone, but `-5`, a line break, `*X(A1)` is one formula), and at the very start when it
 * starts with a tab or a carriage return, whatever follows. */
function guarded(text: string): string {
    const parts = text.split(PIECE_BREAK);  // the pieces, with the break between each two
    const numbers = parts.every((part, i) => i % 2 === 1 || part === '' || ONE_NUMBER.test(part));
    let out = text.charAt(0) === '\t' || text.charAt(0) === '\r' ? "'" : '';
    parts.forEach((part, i) => {
        const sign = i % 2 === 0 && !numbers && FORMULA_SIGNS.has(part.replace(SKIPPED, '').charAt(0));
        out += sign ? `'${part}` : part;
    });
    return out;
}

/** `value` as one CSV cell. A number is written as it is, and null (no value: a field the sample doesn't have, a list
 * never set) or an empty text as an empty cell. An empty list is a value, written `[]`. Anything else is text (a list
 * joined with `;`, an object as JSON), guarded against formulas, and always in double quotes: a spreadsheet that reads
 * the file with `;` takes a line with no `;` for one cell, with the comma as its decimal mark, so a number followed by
 * an unquoted text could read as one formula. */
export function escapeCsvField(value: unknown): string {
    if (value === null || value === undefined) return '';
    if (typeof value === 'number') return String(value);
    if (Array.isArray(value) && value.length === 0) return '"[]"';
    const text = Array.isArray(value)
        ? value.map(v => (v === null || v === undefined) ? '' : String(v)).join(';')
        : typeof value === 'object' ? JSON.stringify(value) : String(value);
    if (text === '') return '';
    return `"${guarded(text).replace(/"/g, '""')}"`;
}
