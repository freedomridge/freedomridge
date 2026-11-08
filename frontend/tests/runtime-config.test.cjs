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

async function tokenWith(env) {
    const saved = { MAPBOX_TOKEN: process.env.MAPBOX_TOKEN, NEXT_PUBLIC_MAPBOX_TOKEN: process.env.NEXT_PUBLIC_MAPBOX_TOKEN };
    for (const [key, value] of Object.entries(env)) {
        if (value === undefined) delete process.env[key];
        else process.env[key] = value;
    }
    try {
        return (await GET().json()).mapboxToken;
    } finally {
        for (const [key, value] of Object.entries(saved)) {
            if (value === undefined) delete process.env[key];
            else process.env[key] = value;
        }
    }
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
