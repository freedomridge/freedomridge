// Run with: node --test tests/local-fields.test.cjs
/* eslint-disable @typescript-eslint/no-require-imports -- Uses the Node CommonJS loader to transpile local TS. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { test } = require('node:test');
const ts = require('typescript');

require.extensions['.ts'] = (module, filename) => {
    const source = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
        compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2017 },
        fileName: filename,
    }).outputText;
    module._compile(source, filename);
};
const { isLocalField, localValue, localEntries } = require('../lib/local-fields.ts');

test("a mirror's own fields are read from local_metadata and local_tags by their full names", () => {
    const sample = { metadata: { weeds: 'primary' }, local_metadata: { weeds: 3 }, local_tags: ['checked'] };
    assert.equal(isLocalField('@local.weeds'), true);
    assert.equal(isLocalField('weeds'), false);
    assert.equal(localValue(sample, '@local.weeds'), 3);
    assert.deepEqual(localValue(sample, '@local.tags'), ['checked']);
    assert.equal(localValue(sample, '@local.kind'), undefined);
    assert.deepEqual(localEntries(sample), { '@local.weeds': 3, '@local.tags': ['checked'] });
    assert.deepEqual(localEntries({ metadata: {} }), {});
    assert.deepEqual(localEntries({ local_metadata: {}, local_tags: [] }), {});
});
