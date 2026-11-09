'use strict';
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const launcher = require('../bin/freedomridge.js');

test('arguments: the defaults, each option, and mistakes said plainly', () => {
    const defaults = launcher.parseArgs([]);
    assert.deepEqual(
        { command: defaults.command, dir: defaults.dir, open: defaults.open, pull: defaults.pull },
        { command: 'start', dir: path.join(os.homedir(), 'freedomridge'), open: true, pull: true },
    );
    const given = launcher.parseArgs(['start', '--dir', '~/fr', '--port=3100', '--api-port', '8100', '--no-open', '--no-pull']);
    assert.equal(given.dir, path.join(os.homedir(), 'fr'));
    assert.deepEqual([given.port, given.apiPort, given.open, given.pull], [3100, 8100, false, false]);
    assert.equal(launcher.parseArgs(['logs', 'backend']).service, 'backend');
    assert.throws(() => launcher.parseArgs(['--port', 'abc']), /--port takes a port number, not abc/);
    assert.throws(() => launcher.parseArgs(['--port']), /--port needs a value/);
    assert.throws(() => launcher.parseArgs(['--colour']), /Unknown option --colour/);
    assert.throws(() => launcher.parseArgs(['restart']), /Unknown command restart/);
    assert.throws(() => launcher.parseArgs(['logs', 'nginx']), /No service nginx/);
    assert.throws(() => launcher.parseArgs(['stop', 'now']), /Unexpected now/);
});

test('the folder .env: the ports are set, every other line stays', () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'fr-launcher-'));
    launcher.writeEnv(dir, { FRIDGE_PORT: 3100 });
    assert.equal(fs.readFileSync(path.join(dir, '.env'), 'utf8'), 'FRIDGE_PORT=3100\n');
    fs.appendFileSync(path.join(dir, '.env'), '# my map\nMAPBOX_TOKEN="pk.abc"\n');
    launcher.writeEnv(dir, { FRIDGE_PORT: 3200, FRIDGE_API_PORT: 8200 });
    assert.equal(fs.readFileSync(path.join(dir, '.env'), 'utf8'),
        'FRIDGE_PORT=3200\n# my map\nMAPBOX_TOKEN="pk.abc"\nFRIDGE_API_PORT=8200\n');
    assert.deepEqual(launcher.readEnv(dir), { FRIDGE_PORT: '3200', MAPBOX_TOKEN: 'pk.abc', FRIDGE_API_PORT: '8200' });
    fs.rmSync(dir, { recursive: true });
});

test('versions: compared by number, read from the image Compose runs', () => {
    assert.ok(launcher.compareVersions('2.10.0', '2.9.3') > 0);
    assert.ok(launcher.compareVersions('2.4.0', '2.5.0') < 0);
    assert.equal(launcher.compareVersions('2.4.0', '2.4.0'), 0);
    assert.equal(launcher.versionOfImage('ghcr.io/freedomridge/backend:2.4.0'), '2.4.0');
    assert.equal(launcher.versionOfImage('freedomridge-prod-backend'), null);
    assert.deepEqual(launcher.parsePs('db ghcr.io/freedomridge/db:2.4.0 running\nbackend ghcr.io/freedomridge/backend:2.4.0 exited\n'), {
        db: { image: 'ghcr.io/freedomridge/db:2.4.0', state: 'running' },
        backend: { image: 'ghcr.io/freedomridge/backend:2.4.0', state: 'exited' },
    });
});

test('the package carries this release: its files, and its version everywhere', () => {
    const root = path.join(__dirname, '..', '..');
    execFileSync(process.execPath, [path.join(__dirname, '..', 'scripts', 'bundle.js')], { stdio: 'pipe' });
    for (const file of launcher.FILES) {
        assert.equal(fs.readFileSync(path.join(__dirname, '..', 'release', file), 'utf8'),
            fs.readFileSync(path.join(root, file), 'utf8'), file);
    }
    assert.equal(launcher.VERSION, fs.readFileSync(path.join(root, 'VERSION'), 'utf8').trim());
    assert.match(launcher.summary(path.join(os.homedir(), 'freedomridge'), 3000, 8000), /~\/freedomridge\/data/);
});
