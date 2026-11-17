// Run with: node --test tests/media-field-pick.test.cjs
/* eslint-disable @typescript-eslint/no-require-imports -- Uses the Node CommonJS loader to transpile local TS. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { test } = require('node:test');
const ts = require('typescript');

require.extensions['.ts'] = (module, filename) => {
    const source = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
        compilerOptions: { module: ts.ModuleKind.CommonJS, esModuleInterop: true, target: ts.ScriptTarget.ES2017 },  // as tsconfig.json
        fileName: filename,
    }).outputText;
    module._compile(source, filename);
};

const { mediaFieldLabel, readMediaFieldPick, rememberMediaFieldPick, shownMediaField } = require('../lib/media-field-pick.ts');

// A browser's localStorage for the duration of `run` (Node has none).
function withStorage(storage, run) {
    const hadWindow = 'window' in global;
    const previous = global.window;
    global.window = { localStorage: storage };
    try { return run(); } finally {
        if (hadWindow) global.window = previous; else delete global.window;
    }
}
function memoryStorage() {
    const data = new Map();
    return {
        getItem: key => (data.has(key) ? data.get(key) : null),
        setItem: (key, value) => data.set(key, String(value)),
        removeItem: key => data.delete(key),
        data,
    };
}
const throwing = {
    getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); }, removeItem() { throw new Error('blocked'); },
};

test('a pick is read back for its dataset and view only', () => {
    withStorage(memoryStorage(), () => {
        assert.equal(readMediaFieldPick(4, 'grid'), null);
        assert.equal(rememberMediaFieldPick(4, 'grid', 'thumbnail', 'filepath'), 'thumbnail');
        assert.equal(rememberMediaFieldPick(4, 'modal', 'preview', 'filepath'), 'preview');
        assert.equal(readMediaFieldPick(4, 'grid'), 'thumbnail');
        assert.equal(readMediaFieldPick(4, 'modal'), 'preview');
        assert.equal(readMediaFieldPick(5, 'grid'), null);
        assert.equal(readMediaFieldPick(5, 'modal'), null);
        // The last pick wins.
        rememberMediaFieldPick(4, 'grid', 'filepath', 'preview');
        assert.equal(readMediaFieldPick(4, 'grid'), 'filepath');
    });
});

test('picking the default forgets the pick, for that view only', () => {
    const storage = memoryStorage();
    withStorage(storage, () => {
        rememberMediaFieldPick(4, 'grid', 'thumbnail', 'filepath');
        rememberMediaFieldPick(4, 'modal', 'thumbnail', 'filepath');
        rememberMediaFieldPick(5, 'grid', 'thumbnail', 'filepath');
        // The grid's default is thumbnail now: picking it holds no pick and removes the stored one.
        assert.equal(rememberMediaFieldPick(4, 'grid', 'thumbnail', 'thumbnail'), null);
        assert.equal(readMediaFieldPick(4, 'grid'), null);
        assert.equal(readMediaFieldPick(4, 'modal'), 'thumbnail');
        assert.equal(readMediaFieldPick(5, 'grid'), 'thumbnail');
        assert.equal(storage.data.has('fridge:mediaField:grid:4'), false);
        // With no default known, filepath is the default (as shown).
        assert.equal(rememberMediaFieldPick(4, 'modal', 'filepath', undefined), null);
        assert.equal(readMediaFieldPick(4, 'modal'), null);
        // Following the default: a later default is what is shown.
        assert.equal(shownMediaField(readMediaFieldPick(4, 'grid'), ['filepath', 'thumbnail', 'preview'], 'preview'), 'preview');
    });
});

test('without storage nothing is remembered and nothing throws', () => {
    withStorage(throwing, () => {
        // The pick still holds for the page; nothing is stored.
        assert.equal(rememberMediaFieldPick(4, 'grid', 'thumbnail', 'filepath'), 'thumbnail');
        assert.equal(rememberMediaFieldPick(4, 'grid', 'filepath', 'filepath'), null);
        assert.equal(readMediaFieldPick(4, 'grid'), null);
    });
    // No window at all (the server's render).
    assert.equal('window' in global, false);
    assert.equal(rememberMediaFieldPick(4, 'grid', 'thumbnail', 'filepath'), 'thumbnail');
    assert.equal(readMediaFieldPick(4, 'grid'), null);
});

test('the pick is shown while it is one of the media fields, else the dataset\'s default', () => {
    const fields = ['filepath', 'thumbnail'];
    assert.equal(shownMediaField('thumbnail', fields, 'filepath'), 'thumbnail');
    assert.equal(shownMediaField(null, fields, 'thumbnail'), 'thumbnail');
    assert.equal(shownMediaField('preview', fields, 'thumbnail'), 'thumbnail');
    // Before the dataset has loaded: its default, or filepath.
    assert.equal(shownMediaField('thumbnail', undefined, undefined), 'filepath');
    assert.equal(shownMediaField(null, fields, undefined), 'filepath');
});

test('a View select marks the dataset\'s default option', () => {
    assert.equal(mediaFieldLabel('thumbnail', 'thumbnail'), 'thumbnail (default)');
    assert.equal(mediaFieldLabel('filepath', 'thumbnail'), 'filepath');
    assert.equal(mediaFieldLabel('filepath', undefined), 'filepath (default)');
});
