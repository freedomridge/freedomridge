import fs from 'fs';
import { STATE_FILE, adminLogin, apiFetch, datasetJobs, readState } from './api';

export default async function globalTeardown() {
    let strays: Awaited<ReturnType<typeof datasetJobs>> = [];
    try {
        const { datasetId, setupJobs } = readState();
        const cookie = await adminLogin();
        strays = (await datasetJobs(cookie, datasetId)).filter(job => !setupJobs.includes(job.id));
        await apiFetch(cookie, `/datasets/${datasetId}`, { method: 'DELETE' });
        fs.unlinkSync(STATE_FILE);
        console.log(`[e2e] deleted fixture dataset id=${datasetId}`);
    } catch (e) {
        console.warn(`[e2e] teardown: ${e}`);
    }
    // Every page open on the shared fixture refreshes when one of its jobs ends, and shows the jobs bar
    // while it runs: a spec that starts one moves other specs' pages under them, depending on timing.
    if (strays.length) {
        throw new Error(`${strays.length} job(s) ran on the shared fixture dataset: `
            + strays.map(job => `${job.kind} by ${job.started_by ?? '?'} at ${job.created_at}`).join('; ')
            + '. Give the spec that started them its own dataset (createFixtureDataset in e2e/api.ts).');
    }
}
