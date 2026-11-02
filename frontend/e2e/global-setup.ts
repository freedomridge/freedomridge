import fs from 'fs';
import path from 'path';
import { API, DATASET_NAME, STATE_FILE, adminLogin, apiFetch, createFixtureDataset, datasetJobs, deleteDatasetAndWait } from './api';

export default async function globalSetup() {
    const cookie = await adminLogin();

    // schema.sql seeds admin with must_change_password=true (right for prod).
    // On a fresh CI database the app forces a change-password modal over every
    // page — Radix aria-hides everything behind it, so role lookups and clicks
    // all time out. Clear the flag by "changing" the password to itself.
    // Conditional on the flag: change-password invalidates the user's OTHER
    // sessions, which would log out a developer's browser on local runs.
    const me = await apiFetch(cookie, '/auth/me');
    if (me.must_change_password) {
        await apiFetch(cookie, '/auth/change-password', {
            method: 'POST',
            body: JSON.stringify({ current_password: 'admin123', new_password: 'admin123' }),
        });
    }

    // Clean up leftovers from a crashed run
    const datasets = await apiFetch(cookie, '/datasets');
    for (const d of datasets) {
        if (d.name === DATASET_NAME) {
            await deleteDatasetAndWait(cookie, d.id);
        }
    }

    // Open to every signed-in user: the guest and demo specs browse this shared fixture.
    const datasetId = await createFixtureDataset(cookie, DATASET_NAME, true);
    // Teardown fails the run on any job after these: specs must not start jobs on the shared fixture.
    const setupJobs = (await datasetJobs(cookie, datasetId)).map(job => job.id);
    fs.writeFileSync(STATE_FILE, JSON.stringify({ datasetId, setupJobs }));

    // Storage state with the backend session cookie — cookies are host-scoped
    // (port ignored), so a cookie minted on :8000 works for the app on :3000.
    // Panel tests reuse it to skip the UI login; the login flow itself is
    // covered by smoke.spec.ts.
    const sessionValue = cookie.split('=')[1];
    fs.writeFileSync(path.join(__dirname, '.auth-state.json'), JSON.stringify({
        cookies: [{
            name: 'session_id', value: sessionValue, domain: 'localhost', path: '/',
            httpOnly: true, secure: false, sameSite: 'Lax', expires: -1,
        }],
        origins: [],
    }));
    console.log(`[e2e] fixture dataset '${DATASET_NAME}' id=${datasetId} (12 samples + embeddings) at ${API}`);
}
