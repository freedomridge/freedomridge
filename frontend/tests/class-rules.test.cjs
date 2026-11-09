// Run with: node --test tests/class-rules.test.cjs
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

const { ARGMAX, classMap, classOf, defaultRule, planeWidth, pixelValues, sameRule } = require(path.join(__dirname, '../lib/class-rules.ts'));

test('argmax takes the most probable class, ties to the lower index, and never draws class 0', () => {
    assert.equal(classOf([10, 200, 45], ARGMAX), 1);
    assert.equal(classOf([90, 90, 75], ARGMAX), null);  // a tie between 0 and 1 goes to 0, which isn't drawn
    assert.equal(classOf([5, 100, 100], ARGMAX), 1);
});

test('thresholds pass in order, on summed classes, at or above min, with a fallback', () => {
    const rule = {
        type: 'rules',
        rules: [
            { classes: [3], min: 0.3, class: 3 },
            { classes: [1, 2], min: 0.5, class: 1 },
        ],
        fallback: 4,
    };
    assert.equal(classOf([0, 0, 0, 77, 0], rule), 3);        // 77 >= 0.3 * 255 = 76.5
    assert.equal(classOf([0, 0, 0, 76, 0], rule), 4);        // just under: none passes, the fallback
    assert.equal(classOf([0, 64, 64, 200, 0], rule), 3);     // the first rule that passes wins
    assert.equal(classOf([0, 64, 64, 10, 0], rule), 1);      // 128 >= 127.5, summed
    assert.equal(classOf([0, 0, 0, 0, 0], { ...rule, fallback: null }), null);
    assert.equal(classOf([0, 0, 0, 0, 0], { ...rule, fallback: 0 }), null);  // a class-0 fallback draws nothing
});

test('a decoded image holds the planes side by side', () => {
    // Two classes, planes 2 x 1: class 0 then class 1, as RGBA.
    const gray = [200, 30, 10, 220];   // class 0 at x=0,1, then class 1 at x=0,1
    const data = gray.flatMap(value => [value, value, value, 255]);
    assert.equal(planeWidth(4, 2), 2);
    assert.deepEqual(pixelValues(data, 4, 2, 0, 0), [200, 10]);
    assert.deepEqual(pixelValues(data, 4, 2, 1, 0), [30, 220]);
    assert.deepEqual(Array.from(classMap(data, 4, 1, 2, ARGMAX)), [-1, 1]);
    assert.throws(() => planeWidth(5, 2), /doesn't hold 2 classes/);
});

test('a rule saved and read back is the same rule, though jsonb reordered its keys', () => {
    const shown = { type: 'rules', rules: [{ classes: [1], min: 0.9, class: 1 }], fallback: null };
    const stored = { fallback: null, type: 'rules', rules: [{ min: 0.9, class: 1, classes: [1] }] };
    assert.equal(sameRule(shown, stored), true);
    assert.equal(sameRule(shown, { ...shown, fallback: 2 }), false);
    assert.equal(sameRule(ARGMAX, { type: 'argmax' }), true);
    assert.equal(sameRule(ARGMAX, shown), false);
});

test('a field opens with its default rule, argmax without one', () => {
    const saved = { type: 'rules', rules: [{ classes: [2], min: 0.4, class: 2 }], fallback: null };
    assert.deepEqual(defaultRule({ weeds: saved }, 'weeds'), saved);
    assert.deepEqual(defaultRule({ weeds: saved }, 'other'), ARGMAX);
    assert.deepEqual(defaultRule(undefined, 'weeds'), ARGMAX);
});
