// Region boundaries for the e2e suite (docs/REGIONS.md), loaded from a small synthetic file instead of the 40.7 MB
// Natural Earth download. The supported offline path is the backend's command,
//   python -m backend.regions load <file> --set natural-earth-admin1 --no-verify --set-version <v>
// which writes to the database its DATABASE_URL names. Loading is opt-in: only a run whose database is thrown away
// says so, with FRIDGE_E2E_LOAD_REGIONS=1. Both CI runs set it and give the command the e2e database:
// - scripts/ci_local.sh exports DATABASE_URL for the whole run, and its e2e environment is <repo>/.venv-e2e;
// - .github/workflows/ci.yml's e2e job activates <repo>/.venv for every step (setup-uv activate-environment) and runs
//   its database on localhost:5432 as freedomridge/password; it sets DATABASE_URL only for the backend's step, so in
//   GitHub Actions the command is given that URL here.
// Without the variable the region tests skip and touch nothing, whatever else is set (a developer's machine, where a
// database may hold real data). With it, before loading, it checks that the command's database is the one the e2e
// API serves, and it never replaces a set it didn't load.
import { execFileSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { apiFetch, deleteDatasetAndWait } from './api';

export const REGION_SET = 'natural-earth-admin1';
/** The version the fixture is recorded as: the admin card shows it. */
export const FIXTURE_VERSION = 'e2e-fixture-1';
/** What the fixture makes (frontend/e2e/fixtures/regions-e2e.geojson). */
export const FIXTURE_COUNTS = { country: 5, state: 8, group: 3 };

const ROOT = path.resolve(__dirname, '..', '..');
const FILE = path.join(__dirname, 'fixtures', 'regions-e2e.geojson');

/** ci.yml's e2e database (the Install + start backend step's DATABASE_URL). */
const GITHUB_E2E_DATABASE = 'postgres://freedomridge:password@localhost:5432/freedomridge';

/** The Python that has the backend's packages: the activated environment, ci_local's, or the repository's. */
function backendPython(): string | undefined {
    const candidates = [
        process.env.VIRTUAL_ENV ? path.join(process.env.VIRTUAL_ENV, 'bin', 'python') : '',
        path.join(ROOT, '.venv-e2e', 'bin', 'python'),
        path.join(ROOT, '.venv', 'bin', 'python'),
    ];
    return candidates.find(candidate => candidate && fs.existsSync(candidate));
}

// The id of the dataset named argv[1] in the database the backend's code would use.
const FIND_MARKER = `
import asyncio, sys
import asyncpg
from backend.database import DATABASE_URL

async def main():
    connection = await asyncpg.connect(DATABASE_URL, timeout=5)
    try:
        print(await connection.fetchval('SELECT id FROM datasets WHERE name = $1', sys.argv[1]) or '')
    finally:
        await connection.close()

asyncio.run(main())
`;

/** Why the region tests don't run, or null when the run opted in to loading the fixture. */
export const regionsNotOptedIn: string | null = process.env.FRIDGE_E2E_LOAD_REGIONS === '1'
    ? null
    : 'FRIDGE_E2E_LOAD_REGIONS is not 1: the region fixture is loaded only in a CI run, whose e2e database is thrown away';

type Sets = { sets: Array<{ name: string; loaded: { version: string } | null }> };

/**
 * Load the fixture as the set natural-earth-admin1 into the e2e database. Throws, saying why, when it can't; returns
 * the reason instead when a set it didn't load is there (a database with real boundaries: never replaced).
 */
export async function loadRegionFixture(cookie: string): Promise<string | null> {
    if (regionsNotOptedIn) return regionsNotOptedIn;
    const databaseUrl = process.env.DATABASE_URL || (process.env.GITHUB_ACTIONS === 'true' ? GITHUB_E2E_DATABASE : '');
    if (!databaseUrl) return 'DATABASE_URL is not set: set it to the e2e backend\'s database to load the region fixture';
    const env = { ...process.env, DATABASE_URL: databaseUrl };
    const loaded = ((await apiFetch(cookie, '/admin/regions')) as Sets).sets.find(set => set.name === REGION_SET)?.loaded;
    if (loaded && loaded.version !== FIXTURE_VERSION) {
        return `this server has ${REGION_SET} ${loaded.version} loaded; the e2e fixture would replace it`;
    }
    const python = backendPython();
    if (!python) throw new Error('No Python environment with the backend found (VIRTUAL_ENV, .venv-e2e or .venv at the repository root)');

    // The command must write to the e2e API's database: a dataset made through the API has to be in it.
    const marker = `e2e-regions-marker-${randomUUID()}`;
    const { id } = await apiFetch(cookie, '/datasets', { method: 'POST', body: JSON.stringify({ name: marker }) });
    try {
        let found = '';
        try {
            found = execFileSync(python, ['-c', FIND_MARKER, marker], { cwd: ROOT, env, encoding: 'utf8', timeout: 30_000 }).trim();
        } catch (error) {
            throw new Error(`The backend command can't read its database: ${error}`);
        }
        if (found !== String(id)) {
            throw new Error('The backend command\'s database is not the one the e2e API serves: set DATABASE_URL to the e2e database');
        }
    } finally {
        await deleteDatasetAndWait(cookie, id);
    }
    execFileSync(
        python,
        ['-m', 'backend.regions', 'load', FILE, '--set', REGION_SET, '--no-verify', '--set-version', FIXTURE_VERSION],
        { cwd: ROOT, env, stdio: 'pipe', timeout: 120_000 },
    );
    return null;
}
