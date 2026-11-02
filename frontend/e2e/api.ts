// Minimal backend API helper for e2e fixture setup/teardown.
// Mirrors the Python integration suites: create a full-featured dataset via
// the API, run GUI tests against it, then delete it.
import fs from 'fs';
import path from 'path';

export const API = process.env.E2E_API_URL || 'http://localhost:8000/api';
export const DATASET_NAME = 'e2e-smoke';
export const STATE_FILE = path.join(__dirname, '.e2e-state.json');

export async function adminLogin(): Promise<string> {
    const res = await fetch(`${API}/auth/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: 'admin', password: 'admin123' }),
    });
    if (!res.ok) throw new Error(`admin login failed: ${res.status}`);
    const setCookie = res.headers.get('set-cookie') || '';
    const m = setCookie.match(/session_id=([^;]+)/);
    if (!m) throw new Error('no session cookie from login');
    return `session_id=${m[1]}`;
}

export async function apiFetch(cookie: string, pathname: string, init: RequestInit = {}) {
    // A 409 with Retry-After is a refusal with no effect that asks for a retry: another spec's index
    // build on the shared samples table, or a busy job queue. Parallel specs hit it during setup.
    const retryUntil = Date.now() + 45_000;
    let res: Response;
    for (;;) {
        res = await fetch(`${API}${pathname}`, {
            ...init,
            headers: {
                'Content-Type': 'application/json',
                Cookie: cookie,
                ...(init.headers || {}),
            },
        });
        if (res.status !== 409 || !res.headers.has('retry-after') || Date.now() > retryUntil) break;
        await new Promise(resolve => setTimeout(resolve, 1_000));
    }
    if (!res.ok) {
        throw new Error(`${init.method || 'GET'} ${pathname} -> ${res.status}: ${await res.text()}`);
    }
    return res.json();
}

export async function deleteDatasetAndWait(cookie: string, id: number, timeoutMs = 30_000) {
    const submitted = await apiFetch(cookie, `/datasets/${id}`, { method: 'DELETE' });
    if (!submitted.job_id) return; // Older synchronous servers.
    const deadline = Date.now() + timeoutMs;
    let resumed = false;
    while (Date.now() < deadline) {
        const job = await apiFetch(cookie, `/jobs/${submitted.job_id}`);
        if (job.state === 'succeeded') return;
        if (job.state === 'failed' || job.state === 'cancelled') {
            if (resumed) throw new Error(`Dataset ${id} deletion job ${job.id} ${job.state}: ${JSON.stringify(job.error)}`);
            await apiFetch(cookie, `/datasets/${id}/deletion/resume`, { method: 'POST' });
            resumed = true;
        }
        await new Promise(resolve => setTimeout(resolve, Math.min(250, Math.max(0, deadline - Date.now()))));
    }
    throw new Error(`Timed out waiting for dataset ${id} deletion job ${submitted.job_id}`);
}

/**
 * Promote (index) a field and wait until it is promoted. Promote answers 202 {job_id} and keeps
 * building when the build outlasts its five-second answer, e.g. queued behind another spec's index.
 */
export async function promoteField(cookie: string, datasetId: number, field: string, timeoutMs = 50_000) {
    const answer = await apiFetch(cookie, `/datasets/${datasetId}/fields/${field}/promote`, { method: 'POST' });
    if (!answer.job_id) return;
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
        const job = await apiFetch(cookie, `/jobs/${answer.job_id}`);
        if (job.state === 'succeeded') return;
        if (job.state === 'failed' || job.state === 'cancelled') {
            throw new Error(`Promoting ${field} on dataset ${datasetId}: job ${job.id} ${job.state}: ${JSON.stringify(job.error)}`);
        }
        await new Promise(resolve => setTimeout(resolve, 250));
    }
    throw new Error(`Timed out waiting for job ${answer.job_id} promoting ${field} on dataset ${datasetId}`);
}

/**
 * Create the 12-sample fixture dataset (with embeddings and cached field stats) and return its id.
 * Global setup makes the shared one; a spec whose actions start dataset jobs (tag edits, concept
 * scoring) makes its own, because a job's end refreshes every page open on its dataset.
 */
export async function createFixtureDataset(cookie: string, name: string, isGlobal = false): Promise<number> {
    const ds = await apiFetch(cookie, '/datasets', {
        method: 'POST',
        body: JSON.stringify({ name, description: 'GUI smoke fixture', is_global: isGlobal }),
    });

    const samples = Array.from({ length: 12 }, (_, i) => ({
        filepath: `s3://e2e/img_${String(i).padStart(2, '0')}.jpg`,
        tags: ['seed', `mod3_${i % 3}`],
        // `note` is present on only the first 6 — gives a field with MISSING values so the
        // "(no value)" filter option (shown only when null_count > 0) is testable.
        // `sampled_at` is an ISO datetime spread across 12 distinct days (Jan 2024) so the
        // datetime-filter flow is testable end-to-end (typed 'datetime' by refresh-stats).
        metadata: {
            idx: i,
            split: i < 6 ? 'train' : 'val',
            score: i * 0.5,
            sampled_at: `2024-01-${String(i + 1).padStart(2, '0')}T00:00:00`,
            ...(i < 6 ? { note: 'x' } : {}),
        },
        location: { type: 'Point', coordinates: [-100.0 + i * 0.01, 40.0 + i * 0.01] },
    }));
    const { ids } = await apiFetch(cookie, `/datasets/${ds.id}/samples`, {
        method: 'POST',
        body: JSON.stringify({ samples }),
    });

    // Embeddings with deterministic scatter positions (a 4x3 grid of points) so
    // the embeddings panel renders without a UMAP run. recompute-viz-rank
    // populates embedding_viz_meta, which feeds the panel's field list and the
    // even-coverage sampler.
    await apiFetch(cookie, `/datasets/${ds.id}/embeddings`, {
        method: 'POST',
        body: JSON.stringify(ids.map((sampleId: number, i: number) => {
            // Two separable groups so an embedding "concept" can discriminate them: idx 0-5
            // (== split 'train') point along axis 0, idx 6-11 ('val') along axis 1. (The
            // scatter uses the x/y coordinates below, not these vectors, so this is safe.)
            const vector = Array.from({ length: 8 }, () => 0);
            vector[i < 6 ? 0 : 1] = 1;
            return { sample_id: sampleId, vector, field_name: 'e2e_clip' };
        })),
    });
    await apiFetch(cookie, `/datasets/${ds.id}/embeddings/coordinates?field_name=e2e_clip`, {
        method: 'POST',
        body: JSON.stringify(ids.map((sampleId: number, i: number) => ({
            sample_id: sampleId,
            x: (i % 4) * 10,
            y: Math.floor(i / 4) * 10,
        }))),
    });
    await apiFetch(cookie, `/datasets/${ds.id}/embeddings/recompute-viz-rank?field_name=e2e_clip`, {
        method: 'POST',
    });
    // Populate the cached field stats. In lightning mode (the default) the
    // filter sidebar / histogram / query-builder serve CACHED values for
    // non-promoted fields — a freshly created dataset has none until
    // refresh-stats runs (locally a long-lived dev DB hides this; a
    // throwaway CI DB does not).
    await apiFetch(cookie, `/datasets/${ds.id}/refresh-stats`, { method: 'POST' });
    return ds.id;
}

/** Every job recorded for a dataset, routine statistics included. */
export async function datasetJobs(cookie: string, id: number): Promise<Array<{ id: string; kind: string; started_by?: string; created_at: string }>> {
    const jobs = [];
    let cursor = '';
    do {
        const page = await apiFetch(cookie, `/datasets/${id}/jobs?include_system_statistics=true&limit=100${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`);
        jobs.push(...page.items);
        cursor = page.next_cursor ?? '';
    } while (cursor);
    return jobs;
}

export function readState(): { datasetId: number; setupJobs: string[] } {
    return JSON.parse(fs.readFileSync(STATE_FILE, 'utf-8'));
}
