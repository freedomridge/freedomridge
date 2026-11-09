#!/usr/bin/env node
'use strict';
// npx freedomridge: start this release of Freedom Ridge on this machine with Docker. It runs the release's
// docker-compose.try.yml (bundled in this package with the files it mounts) from a folder, ~/freedomridge by
// default, and brings that folder's database up to this release first, so the same command also upgrades.
const { spawn, spawnSync } = require('node:child_process');
const fs = require('node:fs');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');

const VERSION = require('../package.json').version;
const RELEASE = path.join(__dirname, '..', 'release');
// The release files, at the paths docker-compose.try.yml expects them in its folder.
const FILES = ['docker-compose.try.yml', 'backend/db/schema.sql', 'backend/db/postgresql.conf'];
const SERVICES = ['db', 'backend', 'job-runner', 'frontend'];
const APP_SERVICES = ['backend', 'job-runner', 'frontend'];
const DEFAULT_PORT = 3000;
const DEFAULT_API_PORT = 8000;

const HELP = `Freedom Ridge ${VERSION}: run it on this machine with Docker.

Usage: npx freedomridge [command] [options]

Commands:
  start           Start Freedom Ridge (the default). A folder running an earlier
                  version is upgraded: its database is migrated first.
  stop            Stop it. The data stays.
  status          What runs, and where.
  logs [service]  Follow the logs: all, or one of ${SERVICES.join(', ')}.

Options:
  --dir DIR       Where it keeps its files and your images (default ~/freedomridge).
  --port N        The web app's port (default ${DEFAULT_PORT}); kept for later starts.
  --api-port N    The API's port, for the Python SDK (default ${DEFAULT_API_PORT}); kept for later starts.
  --no-open       Don't open the browser.
  --no-pull       Use the images already on this machine.
  -h, --help      This help.
  -v, --version   The version.

Set MAPBOX_TOKEN (in the shell, or in the folder's .env) to turn the map on.
`;

class Failure extends Error {}

function fail(message) {
    throw new Failure(message);
}

function parseArgs(argv) {
    const options = { command: 'start', dir: path.join(os.homedir(), 'freedomridge'), open: true, pull: true };
    const positional = [];
    const port = (flag, value) => {
        const number = Number(value);
        if (!Number.isInteger(number) || number < 1 || number > 65535) fail(`${flag} takes a port number, not ${value}`);
        return number;
    };
    for (let i = 0; i < argv.length; i++) {
        const arg = argv[i];
        const [flag, inline] = arg.startsWith('--') && arg.includes('=') ? arg.split(/=(.*)/s) : [arg, undefined];
        const value = () => {
            if (inline !== undefined) return inline;
            if (i + 1 >= argv.length) fail(`${flag} needs a value`);
            return argv[++i];
        };
        if (flag === '-h' || flag === '--help') options.help = true;
        else if (flag === '-v' || flag === '--version') options.version = true;
        else if (flag === '--dir') options.dir = path.resolve(value().replace(/^~(?=$|\/)/, os.homedir()));
        else if (flag === '--port') options.port = port(flag, value());
        else if (flag === '--api-port') options.apiPort = port(flag, value());
        else if (flag === '--no-open') options.open = false;
        else if (flag === '--no-pull') options.pull = false;
        else if (flag.startsWith('-')) fail(`Unknown option ${flag} (see --help)`);
        else positional.push(arg);
    }
    if (positional.length > 0) options.command = positional[0];
    if (!['start', 'stop', 'status', 'logs'].includes(options.command)) {
        fail(`Unknown command ${options.command} (see --help)`);
    }
    if (options.command === 'logs' && positional[1]) {
        if (!SERVICES.includes(positional[1])) fail(`No service ${positional[1]}: one of ${SERVICES.join(', ')}`);
        options.service = positional[1];
    } else if (positional.length > 1) {
        fail(`Unexpected ${positional[1]} (see --help)`);
    }
    return options;
}

// The folder's .env: Compose reads it for the ports (and MAPBOX_TOKEN); this keeps every other line as it is.
function readEnv(dir) {
    const file = path.join(dir, '.env');
    const values = {};
    if (!fs.existsSync(file)) return values;
    for (const line of fs.readFileSync(file, 'utf8').split('\n')) {
        const match = line.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$/);
        if (match) values[match[1]] = match[2].replace(/^(['"])(.*)\1$/, '$2');
    }
    return values;
}

function writeEnv(dir, updates) {
    const file = path.join(dir, '.env');
    const text = fs.existsSync(file) ? fs.readFileSync(file, 'utf8').replace(/\n$/, '') : '';
    const lines = text ? text.split('\n') : [];
    const pending = { ...updates };
    const kept = lines.map(line => {
        const key = (line.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=/) || [])[1];
        if (key && key in pending) {
            const value = pending[key];
            delete pending[key];
            return `${key}=${value}`;
        }
        return line;
    });
    for (const [key, value] of Object.entries(pending)) kept.push(`${key}=${value}`);
    fs.writeFileSync(file, kept.join('\n') + '\n');
}

// "1.10.0" > "1.9.2": numeric, part by part.
function compareVersions(a, b) {
    const pa = a.split('.').map(Number);
    const pb = b.split('.').map(Number);
    for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
        if ((pa[i] || 0) !== (pb[i] || 0)) return (pa[i] || 0) - (pb[i] || 0);
    }
    return 0;
}

// ghcr.io/freedomridge/backend:2.4.0 -> 2.4.0
function versionOfImage(image) {
    const match = /^ghcr\.io\/freedomridge\/[a-z-]+:(\d+\.\d+\.\d+)$/.exec(image || '');
    return match ? match[1] : null;
}

// `docker compose ps` lines, "service image state", as {service: {image, state}}.
function parsePs(text) {
    const services = {};
    for (const line of text.split('\n')) {
        const [service, image, state] = line.trim().split(/\s+/);
        if (service && image) services[service] = { image, state };
    }
    return services;
}

function compose(dir, args, stdio = 'inherit') {
    // The compose file mounts ./data at the same path in the containers, by ${PWD}: Node doesn't set PWD for a
    // child started in another folder, so it's set here.
    const result = spawnSync('docker', ['compose', '-f', path.join(dir, 'docker-compose.try.yml'), ...args], {
        cwd: dir,
        env: { ...process.env, PWD: dir },
        stdio,
        encoding: 'utf8',
    });
    if (result.error) fail(`Couldn't run docker: ${result.error.message}`);
    return result;
}

function checkDocker() {
    if (process.platform === 'win32') {
        fail('On Windows, run this inside WSL 2, with Docker Desktop\'s WSL integration turned on.');
    }
    const server = spawnSync('docker', ['version', '--format', '{{.Server.Version}}'], { encoding: 'utf8' });
    if (server.error && server.error.code === 'ENOENT') {
        fail('Docker isn\'t installed. Install Docker Desktop (macOS) or Docker Engine (Linux): https://docs.docker.com/get-docker/');
    }
    if (server.status !== 0) {
        const why = (server.stderr || '').trim().split('\n').pop();
        fail(`Docker doesn't answer${why ? ` (${why})` : ''}. Start Docker Desktop or the Docker service, and run this again.`);
    }
    if (spawnSync('docker', ['compose', 'version'], { stdio: 'ignore' }).status !== 0) {
        fail('This needs Docker Compose v2, the `docker compose` command: https://docs.docker.com/compose/install/');
    }
}

function installed(dir) {
    if (!fs.existsSync(path.join(dir, 'docker-compose.try.yml'))) {
        fail(`No Freedom Ridge in ${dir}: start it first with npx freedomridge${dir === parseArgs([]).dir ? '' : ` --dir ${dir}`}`);
    }
}

function running(dir) {
    if (!fs.existsSync(path.join(dir, 'docker-compose.try.yml'))) return {};
    const result = compose(dir, ['ps', '--format', '{{.Service}} {{.Image}} {{.State}}'], 'pipe');
    return result.status === 0 ? parsePs(result.stdout) : {};
}

function ports(dir) {
    const env = readEnv(dir);
    return { port: Number(env.FRIDGE_PORT) || DEFAULT_PORT, apiPort: Number(env.FRIDGE_API_PORT) || DEFAULT_API_PORT };
}

function portFree(port) {
    return new Promise(resolve => {
        const server = net.createServer();
        server.once('error', () => resolve(false));
        server.once('listening', () => server.close(() => resolve(true)));
        server.listen(port, '127.0.0.1');
    });
}

const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

async function waitFor(what, seconds, check) {
    const end = Date.now() + seconds * 1000;
    process.stdout.write(`Waiting for ${what}`);
    while (Date.now() < end) {
        if (await check()) {
            process.stdout.write(' ready\n');
            return;
        }
        process.stdout.write('.');
        await delay(2000);
    }
    process.stdout.write('\n');
    fail(`${what} wasn't ready after ${seconds} seconds. See npx freedomridge logs.`);
}

async function answers(url, accept) {
    try {
        const response = await fetch(url, { signal: AbortSignal.timeout(3000) });
        return await accept(response);
    } catch {
        return false;
    }
}

function copyRelease(dir) {
    for (const file of FILES) {
        fs.mkdirSync(path.dirname(path.join(dir, file)), { recursive: true });
        fs.copyFileSync(path.join(RELEASE, file), path.join(dir, file));
    }
    fs.mkdirSync(path.join(dir, 'data'), { recursive: true });
}

function summary(dir, port, apiPort) {
    const home = os.homedir();
    const shown = dir.startsWith(home + path.sep) ? `~${dir.slice(home.length)}` : dir;
    return `
Freedom Ridge ${VERSION} is running at http://localhost:${port}
  First time: log in as admin / admin123, and choose a new password.
  Your images: put them in ${shown}/data; the app sees them at the same paths.
  Python SDK: pip install "freedomridge==${VERSION}", then
    fridge.connect("http://localhost:${apiPort}/api", api_token="...")   (tokens: Settings -> API Tokens)
  npx freedomridge stop | status | logs
`;
}

function openBrowser(url) {
    if (!process.stdout.isTTY || process.env.CI) return;
    const command = process.platform === 'darwin' ? 'open' : 'xdg-open';
    const child = spawn(command, [url], { stdio: 'ignore', detached: true });
    child.on('error', () => {});
    child.unref();
}

async function start(options) {
    checkDocker();
    const { dir } = options;
    fs.mkdirSync(dir, { recursive: true });

    // A folder running a newer release is left alone: its database has migrations this one doesn't know.
    const before = running(dir);
    const runningVersion = versionOfImage(before.backend && before.backend.image);
    if (runningVersion && compareVersions(runningVersion, VERSION) > 0) {
        fail(`${dir} runs Freedom Ridge ${runningVersion}, newer than this launcher (${VERSION}). Run npx freedomridge@${runningVersion} instead.`);
    }
    const updates = {};
    if (options.port) updates.FRIDGE_PORT = options.port;
    if (options.apiPort) updates.FRIDGE_API_PORT = options.apiPort;
    if (Object.keys(updates).length > 0) writeEnv(dir, updates);
    const { port, apiPort } = ports(dir);

    const current = SERVICES.every(service => before[service] && before[service].state === 'running'
        && versionOfImage(before[service].image) === VERSION);
    if (current && !options.port && !options.apiPort) {
        console.log(`Freedom Ridge ${VERSION} is already running in ${dir}.`);
    } else {
        copyRelease(dir);
        if (options.pull) {
            console.log(`Pulling Freedom Ridge ${VERSION}'s images (the first time, a few GB)...`);
            if (compose(dir, ['pull']).status !== 0) fail('Pulling the images failed (above). Is this machine online?');
        }
        // The app stops while the database is brought up to this release.
        if (Object.keys(before).length > 0) compose(dir, ['stop', ...APP_SERVICES], 'pipe');
        for (const [flag, number] of [['--port', port], ['--api-port', apiPort]]) {
            if (!(await portFree(number))) fail(`Port ${number} is in use by another program: choose another with ${flag}.`);
        }
        if (compose(dir, ['up', '-d', 'db']).status !== 0) fail('The database didn\'t start (above).');
        // Over TCP: while a new database loads its schema, PostgreSQL answers only on its local socket.
        await waitFor('the database', 180, async () =>
            compose(dir, ['exec', '-T', 'db', 'pg_isready', '-q', '-h', '127.0.0.1', '-U', 'freedomridge'], 'ignore').status === 0);
        // The release's own migration script, from its worker image: nothing to do for a new database, every
        // migration since for one an earlier release made. It refuses a database a newer release made.
        const migrated = compose(dir, ['run', '--rm', '--no-deps', '-T', 'job-runner', 'bash', 'scripts/apply_migrations.sh', '--database-url-env']);
        if (migrated.status !== 0) fail('The database couldn\'t be brought up to this release (above). The app was not started.');
        if (compose(dir, ['up', '-d']).status !== 0) fail('Freedom Ridge didn\'t start (above).');
    }
    await waitFor('the server', 300, () => answers(`http://127.0.0.1:${apiPort}/api/health`, async response => {
        const health = await response.json();
        return health.status === 'ok' && health.version === VERSION;
    }));
    await waitFor('the web app', 120, () => answers(`http://127.0.0.1:${port}/`, async response => response.ok));
    console.log(summary(dir, port, apiPort));
    if (options.open) openBrowser(`http://localhost:${port}`);
}

async function main(argv) {
    const options = parseArgs(argv);
    if (options.help) return void process.stdout.write(HELP);
    if (options.version) return void console.log(VERSION);
    if (options.command === 'start') return start(options);
    checkDocker();
    installed(options.dir);
    if (options.command === 'stop') {
        if (compose(options.dir, ['stop']).status === 0) console.log('Stopped. Start it again with npx freedomridge.');
    } else if (options.command === 'status') {
        compose(options.dir, ['ps']);
        const { port, apiPort } = ports(options.dir);
        console.log(`\nWeb app: http://localhost:${port}   API (SDK): http://localhost:${apiPort}/api   Folder: ${options.dir}`);
    } else {
        compose(options.dir, ['logs', '--follow', '--tail', '200', ...(options.service ? [options.service] : [])]);
    }
}

if (require.main === module) {
    main(process.argv.slice(2)).catch(error => {
        console.error(error instanceof Failure ? error.message : error);
        process.exit(1);
    });
}

module.exports = { parseArgs, readEnv, writeEnv, compareVersions, versionOfImage, parsePs, summary, FILES, VERSION };
