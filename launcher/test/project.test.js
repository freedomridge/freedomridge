'use strict';
// Each folder is its own installation: its own Docker Compose project, so its own containers and database. A `docker`
// on PATH stands in for Docker here: it logs what it was asked, and lists the containers FAKE_DOCKER_FOLDERS names.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const launcher = require('../bin/freedomridge.js');

const root = fs.mkdtempSync(path.join(os.tmpdir(), 'fr-project-'));
const home = path.join(root, 'home');
const bin = path.join(root, 'bin');
const log = path.join(root, 'docker.log');
fs.mkdirSync(home);
fs.mkdirSync(bin);
fs.writeFileSync(path.join(bin, 'docker'), `#!/bin/sh
echo "COMPOSE_PROJECT_NAME=$COMPOSE_PROJECT_NAME $*" >> "${log}"
if [ "$1" = ps ]; then printf '%s' "$FAKE_DOCKER_FOLDERS"; fi
exit 0
`, { mode: 0o755 });
process.env.HOME = home;
process.env.PATH = `${bin}${path.delimiter}${process.env.PATH}`;
test.after(() => fs.rmSync(root, { recursive: true, force: true }));

function calls() {
    const text = fs.existsSync(log) ? fs.readFileSync(log, 'utf8') : '';
    fs.rmSync(log, { force: true });
    return text.split('\n').filter(Boolean);
}

function folder(name, { installed = false } = {}) {
    const dir = path.join(root, name);
    fs.mkdirSync(dir, { recursive: true });
    if (installed) fs.writeFileSync(path.join(dir, 'docker-compose.try.yml'), 'name: freedomridge-try\n');
    return dir;
}

test('another folder gets its own project, kept in its .env', () => {
    process.env.FAKE_DOCKER_FOLDERS = '';
    const dir = folder('other');
    const project = launcher.checkProjectFolder(dir);
    assert.match(project, /^freedomridge-try-[0-9a-f]{12}$/);
    assert.equal(launcher.readEnv(dir).COMPOSE_PROJECT_NAME, project);
    assert.deepEqual(calls(), [
        `COMPOSE_PROJECT_NAME= ps --all --filter label=com.docker.compose.project=${project} `
        + '--format {{.Label "com.docker.compose.project.working_dir"}}',
    ]);
    // The same project every time, and another for another folder.
    assert.equal(launcher.folderProject(dir), project);
    assert.notEqual(launcher.folderProject(folder('third')), project);
});

test('~/freedomridge, and a folder an earlier launcher started, keep the project they have', () => {
    process.env.FAKE_DOCKER_FOLDERS = '';
    const standard = folder('home/freedomridge');
    assert.equal(standard, launcher.parseArgs([]).dir);
    assert.equal(launcher.checkProjectFolder(standard), launcher.PROJECT);
    const earlier = folder('earlier', { installed: true });
    assert.equal(launcher.checkProjectFolder(earlier), launcher.PROJECT);
    for (const dir of [standard, earlier]) assert.equal(launcher.readEnv(dir).COMPOSE_PROJECT_NAME, undefined);
    assert.match(fs.readFileSync(path.join(__dirname, '..', '..', 'docker-compose.try.yml'), 'utf8'),
        new RegExp(`^name: ${launcher.PROJECT}$`, 'm'));
    calls();
});

test('a project whose containers belong to another folder is refused, its own are fine', () => {
    const dir = folder('home/freedomridge');
    const elsewhere = folder('elsewhere');
    process.env.FAKE_DOCKER_FOLDERS = `${elsewhere}\n`;
    assert.throws(() => launcher.checkProjectFolder(dir),
        new RegExp(`belong to ${elsewhere}; starting them from ${dir} would replace them`));
    // Its own containers, named by a path that leads to the same folder.
    const link = path.join(root, 'link');
    fs.symlinkSync(dir, link);
    process.env.FAKE_DOCKER_FOLDERS = `${dir}\n${link}\n`;
    assert.equal(launcher.checkProjectFolder(dir), launcher.PROJECT);
    calls();
});

test('the folder names its project, not a COMPOSE_PROJECT_NAME in the shell', () => {
    process.env.FAKE_DOCKER_FOLDERS = '';
    const dir = folder('other');
    process.env.COMPOSE_PROJECT_NAME = 'from-the-shell';
    try {
        launcher.compose(dir, ['ps'], 'pipe');
    } finally {
        delete process.env.COMPOSE_PROJECT_NAME;
    }
    assert.deepEqual(calls(), [`COMPOSE_PROJECT_NAME= compose -f ${path.join(dir, 'docker-compose.try.yml')} ps`]);
});
