// Run with: node --test tests/access-level.test.cjs
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

const { EVERYONE_LEVELS, LEVELS, can, levelLabel, needsLevel, refusal } = require('../lib/access-level.ts');

test('the levels run view, tag, edit, manage, and a global dataset gives everyone at most edit', () => {
    assert.deepEqual([...LEVELS], ['view', 'tag', 'edit', 'manage']);
    assert.deepEqual([...EVERYONE_LEVELS], ['view', 'tag', 'edit']);
});

test('each level allows itself and every level before it, and nothing above', () => {
    for (const [i, level] of LEVELS.entries()) {
        for (const [j, needed] of LEVELS.entries()) {
            assert.equal(can(level, needed), i >= j, `${level} for ${needed}`);
        }
    }
});

test('no level, or one not known yet, allows nothing', () => {
    for (const needed of LEVELS) {
        assert.equal(can(null, needed), false);
        assert.equal(can(undefined, needed), false);
    }
});

test('the reason is the server\'s refusal, and there is none when the level allows it', () => {
    assert.equal(needsLevel('tag'), 'This needs tag access to the dataset');
    assert.equal(needsLevel('manage'), 'This needs manage access to the dataset');
    assert.equal(refusal('view', 'edit'), 'This needs edit access to the dataset');
    assert.equal(refusal('tag', 'tag'), undefined);
    assert.equal(refusal('manage', 'edit'), undefined);
    assert.equal(refusal(undefined, 'view'), 'This needs view access to the dataset');
});

test('a level reads capitalised as a choice', () => {
    assert.deepEqual(LEVELS.map(levelLabel), ['View', 'Tag', 'Edit', 'Manage']);
});

const { ApiError, LEVEL_REFUSED_EVENT, isLevelRefusal, noticeLevelRefusal } = require('../lib/api.ts');

test('a level refusal is the server\'s exact text, for any of the four levels', () => {
    for (const level of LEVELS) assert.equal(isLevelRefusal(needsLevel(level)), true);
    assert.equal(isLevelRefusal('Access denied for this dataset'), false);
    assert.equal(isLevelRefusal('This needs admin access to the dataset'), false);
    assert.equal(isLevelRefusal('Error: This needs edit access to the dataset'), false);
    assert.equal(isLevelRefusal(undefined), false);
    assert.equal(isLevelRefusal({ detail: needsLevel('edit') }), false);
});

test('a 403 level refusal is announced, from an ApiError or a raw fetch, and nothing else is', t => {
    const previous = global.window;
    global.window = new EventTarget();
    t.after(() => { global.window = previous; });
    let heard = 0;
    global.window.addEventListener(LEVEL_REFUSED_EVENT, () => { heard += 1; });
    noticeLevelRefusal(403, needsLevel('tag'));
    assert.equal(heard, 1);
    noticeLevelRefusal(403, 'Only an administrator can stop, resume or discard this job');
    noticeLevelRefusal(409, needsLevel('edit'));
    noticeLevelRefusal(403, undefined);
    assert.equal(heard, 1);
    new ApiError(403, needsLevel('manage'));
    assert.equal(heard, 2);
    new ApiError(403, 'Access denied for this dataset');
    new ApiError(500, needsLevel('edit'));
    assert.equal(heard, 2);
});
