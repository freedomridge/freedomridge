// Run with: node --test tests/runtime-config.test.cjs
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
const { GET } = require(path.join(__dirname, '..', 'app', 'runtime-config', 'route.ts'));

async function configWith(env) {
    const saved = Object.fromEntries(Object.keys(env).map((key) => [key, process.env[key]]));
    for (const [key, value] of Object.entries(env)) {
        if (value === undefined) delete process.env[key];
        else process.env[key] = value;
    }
    try {
        return await GET().json();
    } finally {
        for (const [key, value] of Object.entries(saved)) {
            if (value === undefined) delete process.env[key];
            else process.env[key] = value;
        }
    }
}

async function tokenWith(env) {
    return (await configWith(env)).mapboxToken;
}

async function liveAddressWith(value) {
    return (await configWith({ FRIDGE_LIVE_ADDRESS: value })).liveAddress;
}

test('the runtime MAPBOX_TOKEN wins over the one compiled in at build time', async () => {
    assert.equal(await tokenWith({ MAPBOX_TOKEN: 'pk.runtime', NEXT_PUBLIC_MAPBOX_TOKEN: 'pk.build' }), 'pk.runtime');
});

test('without MAPBOX_TOKEN, the build-time token is used', async () => {
    assert.equal(await tokenWith({ MAPBOX_TOKEN: undefined, NEXT_PUBLIC_MAPBOX_TOKEN: 'pk.build' }), 'pk.build');
});

test('with neither, there is no token, so the map says how to set one', async () => {
    assert.equal(await tokenWith({ MAPBOX_TOKEN: undefined, NEXT_PUBLIC_MAPBOX_TOKEN: undefined }), null);
});

test('the live address is on when FRIDGE_LIVE_ADDRESS is unset, empty or anything but off', async () => {
    assert.equal(await liveAddressWith(undefined), true);
    assert.equal(await liveAddressWith(''), true);  // what the Compose files pass when it isn't set
    assert.equal(await liveAddressWith('on'), true);
});

test('FRIDGE_LIVE_ADDRESS=off, false, 0 or no switches the live address off, in any case', async () => {
    for (const value of ['off', 'OFF', ' Off ', 'false', '0', 'no']) {
        assert.equal(await liveAddressWith(value), false, value);
    }
});
