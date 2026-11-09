'use strict';
// Before `npm pack` / `npm publish`: copy the release files the launcher runs into release/, and the license,
// from this repository. The package must be this release: package.json's version, VERSION and the images
// docker-compose.try.yml names all agree, or nothing is packed.
const fs = require('node:fs');
const path = require('node:path');

const launcher = path.join(__dirname, '..');
const root = path.join(launcher, '..');
const { version } = require('../package.json');
const { FILES } = require('../bin/freedomridge.js');

const released = fs.readFileSync(path.join(root, 'VERSION'), 'utf8').trim();
if (released !== version) throw new Error(`launcher/package.json is ${version}, VERSION is ${released}`);
const compose = fs.readFileSync(path.join(root, 'docker-compose.try.yml'), 'utf8');
const tags = [...compose.matchAll(/image: ghcr\.io\/freedomridge\/[a-z-]+:(\S+)/g)].map(match => match[1]);
if (tags.length === 0 || tags.some(tag => tag !== version)) {
    throw new Error(`docker-compose.try.yml names images ${tags.join(', ')}, not ${version}`);
}

fs.rmSync(path.join(launcher, 'release'), { recursive: true, force: true });
for (const file of FILES) {
    const target = path.join(launcher, 'release', file);
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.copyFileSync(path.join(root, file), target);
}
fs.copyFileSync(path.join(root, 'LICENSE'), path.join(launcher, 'LICENSE'));
console.log(`release/: ${FILES.join(', ')} (${version})`);
