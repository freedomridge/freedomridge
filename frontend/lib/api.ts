import type { ClassRule } from './class-rules';
import type { DefaultView } from './default-view';

// Segmentation visualization configuration
export interface FieldColorConfig {
    colorByAttribute: 'label' | 'id';
    valueColors?: Record<string, string>;  // label name -> hex color (for polygons/detections)
    idColors?: Record<string, string>;     // class ID -> hex color (for masks)
    opacity?: number;
}

export interface SegmentationConfig {
    default_mask_targets?: Record<string, string>;  // id -> class name
    mask_targets?: Record<string, Record<string, string>>;  // field -> {id -> class name}
    color_scheme?: {
        color_pool?: string[];
        opacity?: number;
        fields?: Record<string, FieldColorConfig>;
    };
    class_rules?: Record<string, ClassRule>;  // class-probability field -> its default rule
}

export interface Dataset {
    id: number;
    deleting?: boolean;
    deletion_job_id?: string | null;
    name: string;
    description: string;
    media_fields: string[];
    grid_media_field: string;
    modal_media_field: string;
    segmentation_config?: SegmentationConfig;
    // True: every signed-in user sees the dataset. False (the default for new datasets): admins, its
    // owner and users granted access.
    is_global: boolean;
    created_at: string;
    // GET /datasets/{id} only: the filter sidebar opens with every field expanded and computed (the
    // dataset's rows are few enough to filter every field at once).
    sidebar_expand_all?: boolean | null;
    // The public saved view the app opens the dataset on (lib/default-view.ts). Set by an administrator
    // (PATCH default_view_id; null removes it). `default_view` is GET /datasets/{id} only.
    default_view_id?: number | null;
    default_view?: DefaultView | null;
    // Set when the dataset is a read-only mirror of one on a primary instance (docs/FOLLOWERS.md).
    mirror?: DatasetMirror | null;
}

export interface DatasetMirror {
    state: 'copying' | 'following' | 'stopped';
    primary_url: string | null;
    origin_dataset_id: number;
    tags: 'primary' | 'both';
    views: 'primary' | 'both';
    // Shared views on the primary that use a concept: concepts belong to each instance, so they stay there.
    concept_views: string[];
}

export interface ActiveDatasetJob {
    id: string;
    kind: string;
    lane: 'user' | 'maintenance';
    placement: 'server' | 'compute';
    state: 'running' | 'queued';
    started_by: string;
    progress: { done: number; total: number | null; message: string | null };
    cancel_requested: boolean;
    can_cancel: boolean;
    stalled: boolean;
    retry_delayed: boolean;
    // false: a job about this dataset that owns no dataset lane (an index review)
    dataset_lane?: boolean;
}

export interface ActiveDatasetJobs {
    running: ActiveDatasetJob[];
    resuming: ActiveDatasetJob[];
    waiting: ActiveDatasetJob[];
    next_cursor: string | null;
}

export async function getActiveDatasetJobs(datasetId: number, cursor?: string): Promise<ActiveDatasetJobs> {
    const params = new URLSearchParams({ limit: '25' });
    if (cursor) params.set('cursor', cursor);
    const response = await fetch(`${API_BASE}/datasets/${datasetId}/jobs/active?${params}`, { credentials: 'include' });
    if (!response.ok) throw new ApiError(response.status, 'Could not read dataset jobs');
    return response.json();
}

export interface JobRecord {
    created_by?: number | null;
    id: string;
    kind: string;
    dataset_id: number | null;
    lane: 'user' | 'maintenance';
    placement: 'server' | 'compute';
    state: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled';
    progress: { done: number; total: number | null; message: string | null };
    started_by?: string;
    created_at: string;
    started_at: string | null;
    finished_at: string | null;
    attempt: number;
    cancel_requested: boolean;
    result?: unknown; // only sent to admins and the job's submitter
    error?: unknown; // only sent to admins and the job's submitter
}

export interface JobRecordPage {
    items: JobRecord[];
    next_cursor: string | null;
}

async function getJobPage(path: string, cursor?: string): Promise<JobRecordPage> {
    const params = new URLSearchParams({ limit: '25' });
    if (cursor) params.set('cursor', cursor);
    const response = await fetch(`${API_BASE}${path}?${params}`, { credentials: 'include' });
    await ensureOk(response, 'Could not read job history');
    return response.json();
}

export function getDatasetJobHistory(datasetId: number, cursor?: string): Promise<JobRecordPage> {
    return getJobPage(`/datasets/${datasetId}/jobs`, cursor);
}

// Jobs that belong to no dataset (scheduled maintenance); admin only.
export function getGlobalJobs(cursor?: string): Promise<JobRecordPage> {
    return getJobPage('/admin/jobs', cursor);
}

export async function cancelJob(jobId: string): Promise<JobRecord> {
    const response = await fetch(`${API_BASE}/jobs/${jobId}/cancel`, { method: 'POST', credentials: 'include' });
    await ensureOk(response, 'Could not cancel the job');
    return response.json();
}

// Dataset Jobs panel → Schedules (backend/routers/schedules.py).
export interface ScheduleRule {
    enabled: boolean;
    frequency: 'daily' | 'weekly';
    weekday: number | null; // weekly only; 0 = Sunday
    time: string; // 'HH:MM', wall-clock time in `timezone`
    timezone: string;
    min_new_samples: number | null; // null: no sample condition
    min_days_between: number | null; // null: no frequency condition
    window_minutes: number; // a due run starts only this long after its time, else it is skipped
}

export type ScheduleJobType = 'map_refit' | 'index_review';

export interface DatasetSchedule extends ScheduleRule {
    id: number;
    dataset_id: number;
    job_type: ScheduleJobType;
    target: string;
    next_run_at: string | null;
    last_run_at: string | null;
    last_result: { skipped?: string; points?: number } | null;
    last_check: { at: string; ran: boolean; reason: string } | null;
    last_job: {
        id: string;
        state: JobRecord['state'];
        message: string | null;
        created_at: string;
        started_at: string | null;
        finished_at: string | null;
    } | null;
    map: {
        model_id: string | null;
        refitted_at: string | null;
        points_at_refit: number;
        current_points: number;
        new_samples: number;
        refit_running: boolean;
    } | null; // null for an index review
}

export async function getSchedules(datasetId: number): Promise<{ items: DatasetSchedule[]; defaults: ScheduleRule; review_defaults?: ScheduleRule }> {
    const response = await fetch(`${API_BASE}/datasets/${datasetId}/schedules`, { credentials: 'include' });
    await ensureOk(response, 'Could not read schedules');
    return response.json();
}

export async function createSchedule(
    datasetId: number,
    target: string,
    rule: ScheduleRule,
    jobType: ScheduleJobType = 'map_refit',
): Promise<DatasetSchedule> {
    const response = await fetch(`${API_BASE}/datasets/${datasetId}/schedules`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(jobType === 'index_review' ? { job_type: jobType, ...rule } : { job_type: jobType, target, ...rule }),
    });
    await ensureOk(response, 'Could not add the schedule');
    return response.json();
}

export async function updateSchedule(datasetId: number, scheduleId: number, rule: ScheduleRule): Promise<DatasetSchedule> {
    const response = await fetch(`${API_BASE}/datasets/${datasetId}/schedules/${scheduleId}`, {
        method: 'PUT',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(rule),
    });
    await ensureOk(response, 'Could not save the schedule');
    return response.json();
}

export async function deleteSchedule(datasetId: number, scheduleId: number): Promise<void> {
    const response = await fetch(`${API_BASE}/datasets/${datasetId}/schedules/${scheduleId}`, {
        method: 'DELETE',
        credentials: 'include',
    });
    await ensureOk(response, 'Could not delete the schedule');
}

export async function runSchedule(datasetId: number, scheduleId: number): Promise<{ job_id: string; state: string; created: boolean }> {
    const response = await fetch(`${API_BASE}/datasets/${datasetId}/schedules/${scheduleId}/run`, {
        method: 'POST',
        credentials: 'include',
    });
    await ensureOk(response, 'Could not start the job');
    return response.json();
}


// Index advisor (backend/index_advisor.py; administrators only): Dataset Settings → Indexes and
// Settings → System status.
export interface AdvisorIndex {
    name: string;
    table: string;
    kind: 'core' | 'field' | 'ann' | 'array' | 'other';
    method: string;
    dataset_id: number | null;
    field: string | null;
    definition: string;
    size_bytes: number;
    scans: number;
    last_scan: string | null;
    observed_since: string;
    valid: boolean;
    needed: string[];
    orphan?: boolean; // a field index whose dataset no longer exists
    invalid?: boolean;
    idle_days?: number | null;
}

export interface AdvisorOverlap {
    redundant: AdvisorIndex;
    kept: AdvisorIndex;
    rule: 'duplicate' | 'prefix' | 'operators';
    reason: string;
}

export interface AdvisorUsage {
    uses: number;
    mean_ms: number;
    p50_ms: number | null;
    p95_ms: number | null;
    reference_ms: number | null;
    slower: boolean;
}

export interface AdvisorSuggestion {
    field: string;
    type: string;
    reason: 'slow' | 'index_missing';
    usages: Partial<Record<'filter' | 'sort' | 'facet' | 'histogram', AdvisorUsage>>;
    excess_seconds: number;
    build_seconds: number | null;
    pays_back: boolean | null;
}

export interface IndexReview {
    scope: 'dataset' | 'system';
    dataset_id: number | null;
    reviewed_at: string;
    window_days: number;
    idle_days_required: number; // an index must go this long unused to be listed (two review periods)
    stats_since: string;
    history_since: string; // the later of the statistics start and when the advisor began watching
    enough_history: boolean;
    possibly_unused: AdvisorIndex[];
    too_new: AdvisorIndex[];
    overlapping: AdvisorOverlap[];
    building: AdvisorIndex[];
    needed?: AdvisorIndex[];
    missing_core?: { name: string; table: string }[];
    telemetry_since?: string | null;
    suggested?: AdvisorSuggestion[];
    build_seconds?: number | null;
    skipped?: string;
    // Removals the review closed because their index is back (re-created another way, or a stopped removal)
    recreated?: { index_name: string; removed_at: string; note: string }[];
}

export interface ReviewScheduleRule {
    enabled: boolean;
    frequency: 'daily' | 'weekly';
    weekday: number | null;
    time: string;
    timezone: string;
}

export interface ReviewSchedule extends ReviewScheduleRule {
    id?: number;
    next_run_at: string | null;
    last_run_at: string | null;
}

// An index the advisor removed: removing (the drop job runs), removed (gone: Restore), restoring,
// stopped (the removal ended with the index still there: Remove again), or back (re-created another way).
export interface AdvisorRemoval {
    id: number;
    index_name: string;
    table: string;
    kind: AdvisorIndex['kind'];
    field: string | null;
    definition: string;
    size_bytes: number | null;
    scans: number | null;
    last_scan: string | null;
    observed_since: string | null;
    reason: string | null;
    removed_by: string | null;
    removed_at: string;
    dropped_at: string | null;
    state: 'removing' | 'removed' | 'restoring' | 'stopped' | 'back';
    restore_failed: string | null;
}

export interface IndexAdvisorState {
    removed?: AdvisorRemoval[];
    // Indexes with a removal or restore queued or running (a removal is recorded by its first step).
    pending_actions?: string[];
    review: IndexReview | null;
    review_job: { id: string; finished_at: string | null } | null;
    active_job: { id: string; state: 'queued' | 'running'; created_at: string } | null;
    last_failure: { id: string; state: string; finished_at: string | null; message: string | null } | null;
    schedule: ReviewSchedule | null;
    window_days: number;
    dataset?: { id: number; name: string };
}

function advisorPath(datasetId?: number) {
    return datasetId === undefined ? '/admin/index-advisor' : `/datasets/${datasetId}/index-advisor`;
}

export async function getIndexAdvisor(datasetId?: number): Promise<IndexAdvisorState> {
    const response = await fetch(`${API_BASE}${advisorPath(datasetId)}`, { credentials: 'include' });
    await ensureOk(response, 'Could not read the index review');
    return response.json();
}

export async function reviewIndexes(datasetId?: number): Promise<{ job_id: string; state: string }> {
    const response = await fetch(`${API_BASE}${advisorPath(datasetId)}/review?wait=false`, {
        method: 'POST',
        credentials: 'include',
    });
    await ensureOk(response, 'Could not start the index review');
    return response.json();
}

export async function removeIndex(
    name: string,
    definition: string,
    reason: 'unused' | 'overlap' | 'invalid',
): Promise<{ job_id: string; state: string }> {
    const response = await fetch(`${API_BASE}/admin/indexes/${encodeURIComponent(name)}/remove?wait=false`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ definition, reason }),
    });
    await ensureOk(response, 'Could not remove the index');
    return response.json();
}

export async function restoreIndex(name: string): Promise<{ job_id: string; state: string }> {
    const response = await fetch(`${API_BASE}/admin/indexes/${encodeURIComponent(name)}/restore?wait=false`, {
        method: 'POST',
        credentials: 'include',
    });
    await ensureOk(response, 'Could not restore the index');
    return response.json();
}

export async function saveSystemReviewSchedule(rule: ReviewScheduleRule): Promise<ReviewSchedule> {
    const response = await fetch(`${API_BASE}/admin/index-advisor/schedule`, {
        method: 'PUT',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(rule),
    });
    await ensureOk(response, 'Could not save the review schedule');
    return response.json();
}

export interface Sample {
    id: number;
    version_id?: number;
    filepath: string;
    media_type: string;
    tags: string[];
    // Per-sample JSONB blob: a handful of well-known string keys plus arbitrary
    // dynamic fields (annotations, heatmaps, per-field overlays) read by name at
    // runtime, so the open index stays `any`.
    metadata: {
        filename?: string;
        source_id?: string;
        thumbnail_path?: string;
        // eslint-disable-next-line @typescript-eslint/no-explicit-any -- dynamic sample JSONB; fields read by name at runtime
        [key: string]: any;
    };
    location?: {
        type: string;
        coordinates: [number, number];
    };
    distance?: number;
    created_at?: string;
    last_modified_at?: string;
}

export interface Filters {
    // Per-field draft filter values are genuinely heterogeneous (scalars, arrays,
    // {min,max} ranges, {_op} markers) and consumed via untyped index access in the
    // sidebar, which relies on `any` to bypass indexed-access narrowing.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any -- dynamic per-field draft-filter map; see comment
    [key: string]: any;
}


export interface User {
    id: number;
    username: string;
    email?: string;
    created_at: string;
}

export interface ViewStage {
    type: 'filter' | 'selection' | 'map_region' | 'similarity';
    description: string;
    filter: ComplexFilter; // Only the filter this stage ADDS; the global filter is the AND of all stages.
    // Heterogeneous per-stage bag
    // (count/source/collapse/config/dupGroupCollapse/groupRep/originalDescription),
    // read by name without narrowing by panels we can't widen the contract for.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any -- heterogeneous per-stage metadata bag read by name
    metadata?: any;
}

export interface View {
    id: number;
    dataset_id: number;
    user_id?: number;
    name: string;
    description?: string;
    slug: string;
    content: {
        filters: ComplexFilter; // Legacy, kept for backward compatibility and as the "compiled" filter
        viewStages?: ViewStage[]; // The history of how we got here
        sort?: string;
        order?: 'asc' | 'desc';
        made_on?: string; // The follower instance the view was made on (docs/FOLLOWERS.md)
    };
    is_public: boolean;
    shared_with_user_ids: number[];
    created_at: string;
    from_primary?: boolean; // On a mirror: this view is the copy of one on its primary
}

export interface ComplexFilter {
    // Logic Node
    logic?: 'AND' | 'OR' | 'NOT';
    filters?: ComplexFilter[];

    // Leaf Node
    field?: string;
    operator?: '=' | '!=' | '>' | '<' | '>=' | '<=' | 'in' | 'not in' | 'contains' | 'geo_polygon' | 'geo_polygon_exclude' | 'embedding_polygon' | 'embedding_polygon_exclude' | 'in_selection' | 'is_null' | 'is_not_null' | 'exists' | 'not_exists' | 'concept_select' | 'concept_exclude' | 'parent_matches' | 'has_matching_object' | 'cg_self';
    // Leaf value shape depends on the operator: scalar for comparisons, an array
    // for in/not-in, a ring of [lon,lat]/[x,y] points for geo_/embedding_polygon, a
    // string token for in_selection, an object {concept_id, op, threshold} for
    // concept_select/concept_exclude, null for is_null/exists/not_exists. Consumers
    // (query builders, stage editor) bind it straight into untyped form controls, so
    // the leaf value stays `any`.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any -- operator-dependent leaf value bound into untyped form controls
    value?: any;
    // embedding_polygon: the map (model) the polygon was drawn on; the server refuses it after a re-fit.
    model_id?: string | null;
}

export type DeclaredFieldType = 'unknown' | 'boolean' | 'number' | 'string' | 'datetime' | 'list' | 'object';

export type FacetProvenance = 'exact' | 'cached' | 'pending' | 'out_of_budget';

export interface SchemaField {
    declared_type?: DeclaredFieldType;
    origin?: 'metadata' | 'native_array' | 'system';
    values_complete?: boolean;
    values_truncated?: boolean;
    stats_pending?: boolean;
    name: string;
    // 'derived': an embedding / hash / location (derivedFieldNames), presence-only in the stage editor.
    type: 'unknown' | 'list' | 'object' | 'string' | 'number' | 'boolean' | 'enum' | 'array' | 'datetime' | 'filepath' | 'heatmap' | 'class_probabilities' | 'segmentation' | 'keypoints' | 'cuboids' | 'classification' | 'detections' | 'polygons' | 'derived';
    min?: number | string;
    max?: number | string;
    values?: string[];
    is_promoted?: boolean;
    // false: the field's name cannot be used in a query, so it keeps its dataset values for every filter.
    filterable?: boolean;
    display_type?: string | null;
    null_count?: number;  // samples with no value for this field (drives the "(no value)" filter)
    sortable?: boolean;  // false for native array columns (tags, ...): the grid sort can't order by them
    // Which sample set this field's facet (values or min/max) describes, for the active filter:
    // 'exact' = the filtered set; 'cached' = the whole dataset; 'pending' = the whole dataset until
    // the field is opened (lightning mode); 'out_of_budget' = the whole dataset, the filtered query
    // ran out of time.
    provenance?: FacetProvenance;
    // Per-field overlay display config (heatmaps): UI-set colormap/range/opacity.
    config?: { colormap?: string; range?: [number, number]; opacity?: number } | null;
}

export interface HistogramBucket {
    key: string | number | null;
    count: number;
    label?: string;
    range?: {
        min: number;
        max: number;
        min_label?: string | null;
        max_label?: string | null;
    };
}

export interface HistogramResponse {
    field: string;
    total: number;
    buckets: HistogramBucket[];
    bins?: number;
    missing?: number;
    // Categorical only: total distinct categories (the returned buckets may be a top-N subset) and
    // the sample count in categories beyond those returned — so the cap is shown, never silent.
    distinct_total?: number;
    other_count?: number;
    // Set only by getHistogramPreview: counts are of sampled rows, not of the whole set.
    approximate?: boolean;
    preview?: HistogramPreviewInfo;
}

export interface HistogramPreviewInfo {
    status: 'ok' | 'not_broad' | 'insufficient_sample' | 'timeout' | 'failed';
    method?: string;
    id_range?: [number, number];
    draws?: number;  // distinct sample ids drawn
    matched?: number;  // drawn rows that are in the dataset and match the filter
    estimated_matching_rows?: number;
}

const API_BASE = '/api';

// --- Sample-mutation signal --------------------------------------------------
// The grid virtualizes over a manual page cache that lives OUTSIDE React Query,
// so queryClient.invalidateQueries can't reach it. Any op that changes sample
// rows (bulk-tag, version revert, bulk-set) calls notifySamplesMutated so the
// grid drops its cache and refetches. datasetId scopes it to the affected grid.
export const SAMPLES_MUTATED_EVENT = 'fridge:samples-mutated';

// How many times the dataset's samples were signalled changed on this page. Part of every count's
// query key (useSampleCount), so after a change each count is asked again, once for all its viewers.
const samplesMutations = new Map<number, number>();

export function samplesMutationVersion(datasetId: number): number {
    return samplesMutations.get(datasetId) ?? 0;
}

export function notifySamplesMutated(datasetId: number) {
    if (typeof window === 'undefined') return;
    samplesMutations.set(datasetId, samplesMutationVersion(datasetId) + 1);
    window.dispatchEvent(new CustomEvent(SAMPLES_MUTATED_EVENT, { detail: { datasetId } }));
}

// --- Long actions ------------------------------------------------------------
// Endpoints whose work can take long (concept scoring, index builds, duplicate rebuilds) run a
// server job and answer within five seconds: the final JSON, or 202 {job_id, state}. A 202 is
// announced (the dataset's Jobs indicator refreshes at once) and then followed to the job's
// result, which has the same shape as the immediate answer.
export const JOB_SUBMITTED_EVENT = 'fridge:job-submitted';

export function notifyJobSubmitted(datasetId: number, jobId: string) {
    if (typeof window === 'undefined') return;
    window.dispatchEvent(new CustomEvent(JOB_SUBMITTED_EVENT, { detail: { datasetId, jobId } }));
}

function jobFailure(job: JobRecord, fallbackMsg: string): string {
    const error = job.error as { message?: unknown } | null | undefined;
    if (error && typeof error === 'object' && error.message) return String(error.message);
    return job.state === 'cancelled' ? `${fallbackMsg}: the job was cancelled` : fallbackMsg;
}

// Follows a job to its end. Leaving the page stops nothing: the job keeps running on the server.
export async function waitForJob<T>(jobId: string, fallbackMsg: string, intervalMs = 1000): Promise<T> {
    for (;;) {
        const job = await getJob(jobId);
        if (job.state === 'succeeded') return job.result as T;
        if (job.state === 'failed' || job.state === 'cancelled') {
            throw new ApiError(job.state === 'cancelled' ? 409 : 500, jobFailure(job, fallbackMsg));
        }
        await new Promise(resolve => setTimeout(resolve, intervalMs));
    }
}

// A query that filters by a concept whose definition changed answers 409 "Concept … is being
// rescored" until its scoring job finishes (backend require_concept_scores). Such a refusal is
// temporary: queries retry it every RESCORING_RETRY_MS, so every view recovers when the job ends.
export const RESCORING_RETRY_MS = 5000;

export function isRescoringRefusal(error: unknown): boolean {
    return error instanceof ApiError && error.status === 409 && /is being rescored/.test(error.message);
}

// The wait before retry number `attempt` (from 0): a rescore usually ends within minutes; a stopped
// one waits for Resume, so slow down then.
export function rescoringRetryPeriod(attempt: number): number {
    return attempt < 12 ? RESCORING_RETRY_MS : 6 * RESCORING_RETRY_MS;
}

const rescoringTurns = new Map<number, Promise<void>>();

// Resolves at the page's next retry turn: the next multiple of the retry period on the page's own clock
// (performance.now, so pages opened at different moments do not all retry at once). Every refused
// request that waits for it resumes in the same task, so the sidebar's fields refused at different
// moments retry together, in one facet request.
export function rescoringRetryTurn(attempt: number, signal?: AbortSignal): Promise<void> {
    const period = rescoringRetryPeriod(attempt);
    const now = performance.now();
    const at = (Math.floor(now / period) + 1) * period;
    let turn = rescoringTurns.get(at);
    if (!turn) {
        turn = new Promise<void>(resolve => setTimeout(() => {
            rescoringTurns.delete(at);
            resolve();
        }, at - now));
        rescoringTurns.set(at, turn);
    }
    if (!signal) return turn;
    const waited = turn;
    return new Promise<void>((resolve, reject) => {
        const onAbort = () => reject(signal.reason ?? new DOMException('Aborted', 'AbortError'));
        if (signal.aborted) return onAbort();
        signal.addEventListener('abort', onAbort, { once: true });
        void waited.then(() => {
            signal.removeEventListener('abort', onAbort);
            resolve();
        });
    });
}

async function jobResult<T>(res: Response, datasetId: number, fallbackMsg: string): Promise<T> {
    await ensureOk(res, fallbackMsg);
    if (res.status !== 202) return res.json();
    const { job_id: jobId } = await res.json();
    notifyJobSubmitted(datasetId, jobId);
    return waitForJob<T>(jobId, fallbackMsg);
}

// A lasso (embedding_polygon) drawn on a map that has since been re-fitted: the server refuses every
// request that carries it (409, code map_refitted). FilterContext drops such lassos when it hears this.
export const MAP_REFITTED_EVENT = 'fridge:map-refitted';
export interface MapRefitted { datasetId: number; field: string; modelId: string | null }

// Error that preserves the HTTP status so global 401 handling in providers.tsx
// can distinguish expired sessions from other failures.
export class ApiError extends Error {
    status: number;
    code?: string;
    constructor(status: number, message: string, body?: { code?: unknown; dataset_id?: unknown; field?: unknown; model_id?: unknown }) {
        super(message);
        this.name = 'ApiError';
        this.status = status;
        if (typeof body?.code === 'string') this.code = body.code;
        // Announced here, at the choke point every API error flows through, so a stale lasso is dropped
        // whichever panel's request met it first (grid, scatter, sidebar, a saved view's first load).
        if (status === 409 && body?.code === 'map_refitted' && typeof body.field === 'string'
            && typeof body.dataset_id === 'number' && typeof window !== 'undefined') {
            const detail: MapRefitted = {
                datasetId: body.dataset_id,
                field: body.field,
                modelId: typeof body.model_id === 'string' ? body.model_id : null,
            };
            window.dispatchEvent(new CustomEvent<MapRefitted>(MAP_REFITTED_EVENT, { detail }));
        }
        // A 401 from any API call means the session is gone. Announce it here — at
        // the single choke point every status-bearing API error flows through — so
        // the app logs out even when the caller swallows this error (e.g. a
        // background grid prefetch that does `.catch(() => {})`). Fires before the
        // throw propagates; providers.tsx listens and redirects to login.
        if (status === 401 && typeof window !== 'undefined') {
            window.dispatchEvent(new Event('fridge:unauthorized'));
        }
    }
}

// Throws an ApiError with the HTTP status. Tries to surface the backend's
// `detail` / `message` from a JSON body so users see the real reason (e.g.
// "Field 'foo' not found") instead of the generic fallback.
async function ensureOk(res: Response, fallbackMsg: string): Promise<void> {
    if (res.ok) return;
    let msg = fallbackMsg;
    let body;
    try {
        body = await res.json();
        if (typeof body?.detail === 'string') msg = body.detail;
        // Older map-writer responses nested the message inside detail.
        else if (typeof body?.detail?.message === 'string') {
            msg = body.detail.message;
            if (typeof body.code !== 'string' && typeof body.detail.code === 'string') {
                body = { ...body, code: body.detail.code };
            }
        }
        else if (Array.isArray(body?.detail)) {
            const details = body.detail.map((item: { msg?: unknown }) => item?.msg).filter((item: unknown) => typeof item === 'string');
            if (details.length) msg = details.join('; ');
        }
        else if (typeof body?.message === 'string') msg = body.message;
    } catch { }
    throw new ApiError(res.status, msg, body);
}

// --- Users ---
export async function createUser(username: string, email?: string): Promise<User> {
    const res = await fetch(`${API_BASE}/users`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, email })
    });
    await ensureOk(res, 'Failed to create user');
    return res.json();
}

export async function getUsers(): Promise<User[]> {
    const res = await fetch(`${API_BASE}/users`);
    await ensureOk(res, 'Failed to fetch users');
    return res.json();
}

// id+username only — unlike getUsers this is not admin-gated, so non-admin
// view owners can pick share targets (ShareViewModal).
export async function getUserNames(): Promise<Pick<User, 'id' | 'username'>[]> {
    const res = await fetch(`${API_BASE}/users/names`);
    await ensureOk(res, 'Failed to fetch users');
    return res.json();
}

// --- Views ---
export async function createView(datasetId: number, view: Partial<View> & { username?: string }): Promise<View> {
    const res = await fetch(`${API_BASE}/datasets/${datasetId}/views`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(view)
    });
    await ensureOk(res, 'Failed to create view');
    return res.json();
}

export async function getDatasetViews(datasetId: number): Promise<View[]> {
    const res = await fetch(`${API_BASE}/datasets/${datasetId}/views`);
    await ensureOk(res, 'Failed to fetch views');
    return res.json();
}

export async function getView(slug: string): Promise<View> {
    const res = await fetch(`${API_BASE}/views/${slug}`);
    await ensureOk(res, 'Failed to fetch view');
    return res.json();
}

export async function deleteView(id: number): Promise<void> {
    const res = await fetch(`${API_BASE}/views/${id}`, { method: 'DELETE' });
    await ensureOk(res, 'Failed to delete view');
}

export async function shareView(datasetId: number, viewId: number, isPublic: boolean, sharedWithUserIds: number[]): Promise<void> {
    const res = await fetch(`${API_BASE}/datasets/${datasetId}/views/${viewId}/share`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ is_public: isPublic, shared_with_user_ids: sharedWithUserIds })
    });
    await ensureOk(res, 'Failed to update view sharing');
}

export async function createDataset(data: { name: string; description?: string }): Promise<Dataset> {
    const res = await fetch(`${API_BASE}/datasets`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify(data),
    });
    await ensureOk(res, 'Failed to create dataset');
    return res.json();
}

export async function getDatasets(): Promise<Dataset[]> {
    const res = await fetch(`${API_BASE}/datasets`);
    await ensureOk(res, 'Failed to fetch datasets');
    return res.json();
}

export async function getDataset(id: number): Promise<Dataset> {
    const res = await fetch(`${API_BASE}/datasets/${id}`);
    await ensureOk(res, 'Failed to fetch dataset');
    return res.json();
}

export async function updateDataset(id: number, data: Partial<Dataset>): Promise<Dataset> {
    const res = await fetch(`${API_BASE}/datasets/${id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify(data)
    });
    await ensureOk(res, 'Failed to update dataset');
    return res.json();
}

export async function deleteDataset(id: number): Promise<{ job_id: string }> {
    const res = await fetch(`${API_BASE}/datasets/${id}`, {
        method: 'DELETE', credentials: 'include',
    });
    await ensureOk(res, 'Failed to delete dataset');
    return res.json();
}

export async function resumeDatasetDeletion(id: number): Promise<{ job_id: string }> {
    const res = await fetch(`${API_BASE}/datasets/${id}/deletion/resume`, {
        method: 'POST', credentials: 'include',
    });
    await ensureOk(res, 'Failed to resume deletion');
    return res.json();
}

export async function getJob(jobId: string): Promise<JobRecord> {
    const res = await fetch(`${API_BASE}/jobs/${jobId}`, { credentials: 'include' });
    await ensureOk(res, 'Could not read job status');
    return res.json();
}

export interface EmbeddingsResponse {
    points: Array<{ sample_id: number; x: number; y: number; [key: string]: unknown }>;
    total: number;
    sampled: boolean;
    /** Coverage ranks predate a re-projection (coords are current; sampling is approximate until a re-rank). */
    rank_stale?: boolean;
    /** The map's current model when it has scheduled re-fits (Jobs → Schedules); null otherwise. */
    map_state?: { model_id: string; refitted_at: string } | null;
}

// Parse the columnar binary scatter payload (see _embeddings_binary_response in the
// backend): 4-byte LE header length, JSON header, then int32 ids / float32 x / float32 y.
// Reading typed arrays straight off the buffer avoids JSON.parse of a multi-MB array.
function parseEmbeddingsBinary(buf: ArrayBuffer): EmbeddingsResponse {
    const dv = new DataView(buf);
    const headerLen = dv.getUint32(0, true);
    const header = JSON.parse(new TextDecoder().decode(new Uint8Array(buf, 4, headerLen)));
    const n: number = header.count;
    let off = 4 + headerLen;
    const ids = new Int32Array(buf, off, n); off += n * 4;
    const xs = new Float32Array(buf, off, n); off += n * 4;
    const ys = new Float32Array(buf, off, n);
    const points = new Array(n);
    for (let i = 0; i < n; i++) points[i] = { sample_id: ids[i], x: xs[i], y: ys[i] };
    return {
        points,
        total: header.total,
        sampled: header.sampled,
        rank_stale: header.rank_stale === true,
        map_state: header.map_state ?? null,
    };
}

export async function getEmbeddings(
    datasetId: number,
    field: string = "clip",
    filters?: ComplexFilter,
    includeFields?: string[],
    samplePct: number = 30,
): Promise<EmbeddingsResponse> {
    // Base coords (no include_fields) come back as binary; color-by/patch requests
    // need extra per-point fields, so they stay JSON. The server also falls back to
    // JSON for patch fields, so branch on the actual response content-type.
    const wantBin = !includeFields || includeFields.length === 0;
    const url = `${API_BASE}/datasets/${datasetId}/embeddings/query${wantBin ? '?format=bin' : ''}`;
    const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            field,
            filters,
            include_fields: includeFields,
            sample_pct: samplePct,
        })
    });
    await ensureOk(res, 'Failed to fetch embeddings');
    if ((res.headers.get('content-type') || '').includes('application/octet-stream')) {
        return parseEmbeddingsBinary(await res.arrayBuffer());
    }
    const data = await res.json();
    if (Array.isArray(data)) return { points: data, total: data.length, sampled: false };
    return data;
}

export async function lookupEmbeddingCoordinates(
    datasetId: number,
    field: string,
    sampleIds: number[],
    includeFields?: string[],
): Promise<{ points: Array<{ sample_id: number; x: number; y: number; [key: string]: unknown }> }> {
    const res = await fetch(`${API_BASE}/datasets/${datasetId}/embeddings/lookup`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ field, sample_ids: sampleIds, include_fields: includeFields || undefined }),
    });
    await ensureOk(res, 'Failed to lookup coordinates');
    return res.json();
}

export async function lookupPatchCoordinates(
    datasetId: number,
    patches: Array<{ sample_id: number; field_name: string }>,
): Promise<{ points: Array<{ sample_id: number; x: number; y: number; field_name: string; patch_position?: string }> }> {
    const res = await fetch(`${API_BASE}/datasets/${datasetId}/embeddings/lookup-patches`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ patches }),
    });
    await ensureOk(res, 'Failed to lookup patch coordinates');
    return res.json();
}

export async function selectEmbeddingRegion(
    datasetId: number,
    field: string,
    polygon: [number, number][],
    filters?: ComplexFilter,
): Promise<{ token: string | null; total: number; patches?: Array<{ sample_id: number; field_name: string }> }> {
    const res = await fetch(`${API_BASE}/datasets/${datasetId}/embeddings/select-region`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ field, filters, polygon }),
    });
    await ensureOk(res, 'Failed to select region');
    return res.json();
}

// Materialize the samples matching `filters` into a server-side selection token, so a large
// Select-All can be applied/tagged via the indexed in_selection filter without a giant id
// array in the browser or on the wire. Returns { token, count }.
export async function createSelectionToken(
    datasetId: number,
    filters?: ComplexFilter,
): Promise<{ token: string; count: number }> {
    const res = await fetch(`${API_BASE}/datasets/${datasetId}/selection_token`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({ filters }),
    });
    await ensureOk(res, 'Failed to create selection');
    return res.json();
}

export async function getMapPoints(
    datasetId: number,
    filters?: ComplexFilter,
    opts?: { zoom?: number; bbox?: [number, number, number, number]; points?: boolean },
) {
    const res = await fetch(`${API_BASE}/datasets/${datasetId}/map_points`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ filters, zoom: opts?.zoom, bbox: opts?.bbox, points: opts?.points }),
    });
    await ensureOk(res, 'Failed to fetch map points');
    // { mode: 'hybrid'|'points', items, capped }. The server needs the zoom, or points: true.
    return res.json();
}

// --- Filter query language (docs/FILTER_QUERY.md) ---

/** A query the dataset can't take: where, why, and what may have been meant. */
export class FilterQueryProblem extends Error {
    line: number;
    column: number;
    suggestions: string[];
    constructor(detail: { message: string; line: number; column: number; suggestions?: string[] }) {
        super(detail.message);
        this.name = 'FilterQueryProblem';
        this.line = detail.line;
        this.column = detail.column;
        this.suggestions = detail.suggestions ?? [];
    }
}

export interface ParsedFilterQuery {
    filters: ComplexFilter[];  // one per top-level AND part
    parts: (string | null)[];  // each written back as text
    reuse: (number | null)[];  // the index in `current` of an equal filter, to keep its chip
}

/** Text to filters, checked against the dataset's fields. Throws FilterQueryProblem for a mistake. */
export async function parseFilterQuery(datasetId: number, query: string, current: ComplexFilter[]): Promise<ParsedFilterQuery> {
    const res = await fetch(`${API_BASE}/datasets/${datasetId}/filter_query/parse`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query, current }),
    });
    if (res.status === 400) {
        const body = await res.clone().json().catch(() => null);
        const problem = body?.detail?.errors?.[0];
        if (problem && typeof problem.message === 'string') throw new FilterQueryProblem(problem);
    }
    await ensureOk(res, 'Failed to read the query');
    return res.json();
}

/** Each filter as text, or null where the language can't express it (concepts, regions, selections). */
export async function formatFilterQuery(datasetId: number, filters: ComplexFilter[]): Promise<{ parts: (string | null)[] }> {
    const res = await fetch(`${API_BASE}/datasets/${datasetId}/filter_query/format`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ filters }),
    });
    await ensureOk(res, 'Failed to write the filters as a query');
    return res.json();
}

// True data extent (min/max lon/lat) for the current filters — used by "Zoom to Data".
export async function getMapExtent(
    datasetId: number,
    filters?: ComplexFilter,
): Promise<{ w?: number; s?: number; e?: number; n?: number; count: number }> {
    const res = await fetch(`${API_BASE}/datasets/${datasetId}/map_extent`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ filters }),
    });
    await ensureOk(res, 'Failed to fetch map extent');
    return res.json();
}

export async function getEmbeddingFields(datasetId: number): Promise<string[]> {
    const res = await fetch(`${API_BASE}/datasets/${datasetId}/embeddings/fields`);
    await ensureOk(res, 'Failed to fetch embedding fields');
    return res.json();
}

// Derived-artifact coverage: how many samples have each embedding/hash field + a location.
// Used by the Concepts panel to know how many embedded samples a concept hasn't scored yet.
export interface DatasetCoverage {
    total_samples: number;
    total_objects?: number;  // object rows among total_samples (patch view)
    embeddings: Array<{ field: string; present: number; missing: number }>;
    hashes: Array<{ field: string; present: number; missing: number }>;
    location: { present: number; missing: number };
}
export async function getDatasetCoverage(datasetId: number): Promise<DatasetCoverage> {
    const res = await fetch(`${API_BASE}/datasets/${datasetId}/coverage`);
    await ensureOk(res, 'Failed to fetch dataset coverage');
    return res.json();
}

// Filter names of the derived fields /coverage reports: embeddings and hashes are rows in their
// own tables, not metadata keys, so they are `embeddings.<field>` / `hashes.<field>`; plus `location`.
// They take exists / not_exists like any field.
export function derivedFieldNames(coverage: DatasetCoverage): string[] {
    return [
        ...coverage.embeddings.map(e => `embeddings.${e.field}`),
        ...coverage.hashes.map(h => `hashes.${h.field}`),
        'location',
    ];
}

// The grid's coverage warning as one ordinary filter: `<field> not_exists` per gap, OR'ed, i.e. every
// sample behind the warning (the backend uses the predicate /coverage counts, so the counts match).
export function missingFieldsStage(fields: string[]): ViewStage {
    const rules: ComplexFilter[] = fields.map(field => ({ field, operator: 'not_exists', value: null }));
    return {
        type: 'filter',
        // = describeFilter() of this filter, so the label regenerates when the stage is edited.
        description: fields.map(field => `${field}: no value`).join(' or '),
        filter: rules.length === 1 ? rules[0] : { logic: 'OR', filters: rules },
    };
}

// A histogram request. POST with the filter in the body: a saved selection of a few thousand sample ids
// makes a filter too large for a URL, and the server answers 400 past its 64 KB request line.
function histogramRequest(field: string, filters?: ComplexFilter, bins?: number, asNumber?: boolean): RequestInit {
    return {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            field,
            ...(filters ? { filters } : {}),
            ...(bins ? { bins } : {}),
            ...(asNumber ? { as_number: true } : {}),
        }),
    };
}

// The exact histogram. `asNumber` bins a field declared as text numerically (declared text is
// grouped by value by default).
export async function getHistogram(datasetId: number, field: string, filters?: ComplexFilter, bins?: number, asNumber?: boolean): Promise<HistogramResponse> {
    const res = await fetch(`${API_BASE}/datasets/${datasetId}/histogram`, histogramRequest(field, filters, bins, asNumber));
    await ensureOk(res, 'Failed to fetch histogram');
    return res.json();
}

// An APPROXIMATE histogram of a random sample, for broad sets only. Display-only: never filter
// or select from it. Buckets come back only when `preview.status` is 'ok'.
export async function getHistogramPreview(datasetId: number, field: string, filters?: ComplexFilter, bins?: number, asNumber?: boolean): Promise<HistogramResponse> {
    const res = await fetch(`${API_BASE}/datasets/${datasetId}/histogram/preview`, histogramRequest(field, filters, bins, asNumber));
    await ensureOk(res, 'Failed to fetch histogram preview');
    return res.json();
}

export async function reduceEmbeddings(datasetId: number, field: string = "clip") {
    const res = await fetch(`${API_BASE}/datasets/${datasetId}/embeddings/reduce`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ field_name: field }),
    });
    await ensureOk(res, 'Failed to reduce embeddings');
    return res.json();
}

export async function getSampleStats(datasetId: number, sampleIds?: number[]): Promise<Record<string, unknown>> {
    let url = `${API_BASE}/datasets/${datasetId}/stats`;
    if (sampleIds && sampleIds.length > 0) {
        url += `?sample_ids=${sampleIds.join(',')}`;
    }
    const res = await fetch(url);
    await ensureOk(res, 'Failed to fetch sample stats');
    return res.json();
}

export async function getDatasetSchema(
    id: number, filters?: ComplexFilter, lightningMode: boolean = true, signal?: AbortSignal,
): Promise<SchemaField[]> {
    // Use fast field-stats endpoint
    // For PROMOTED fields: backend computes filtered min/max based on current filters
    // For NON-PROMOTED fields: backend returns cached global min/max
    // Use POST to handle large filter payloads
    const res = await fetch(`${API_BASE}/datasets/${id}/field-stats`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ filters, lightning_mode: lightningMode }),
        // A superseded sidebar request is aborted, so the server stops its work too.
        signal,
    });

    await ensureOk(res, 'Failed to fetch schema');
    // The lightning sidebar request's breadth decision, which the same filter's facet request reuses
    // instead of probing again.
    const breadth = res.headers.get('X-Filter-Breadth');
    if (breadth === 'broad' || breadth === 'selective') rememberBreadth(facetKey(id, filters), breadth);
    else if (lightningMode) knownBreadth.delete(facetKey(id, filters));  // undecided now: forget a stale answer
    return res.json();
}

const facetKey = (id: number, filters: ComplexFilter | undefined) => JSON.stringify([id, filters ?? null]);
const knownBreadth = new Map<string, 'broad' | 'selective'>();
function rememberBreadth(key: string, breadth: 'broad' | 'selective') {
    knownBreadth.delete(key);
    knownBreadth.set(key, breadth);
    if (knownBreadth.size > 100) knownBreadth.delete(knownBreadth.keys().next().value as string);  // only recent filters matter
}

// One field's filtered facet. Requests started together (every expanded sidebar field after a filter
// change or a reload) share one POST, so the backend runs its selectivity probe and stats read once
// for all of them (none at all when the sidebar request's breadth is known). The answer streams one
// line per field as it completes, so each field shows as soon as its own query is done. A line with
// an `error` fails that field alone. A field whose signal aborts stops waiting at once; the shared
// request is aborted once every field in it has.
interface FacetWaiter {
    field: string;
    signal?: AbortSignal;
    resolve: (fields: SchemaField[]) => void;
    reject: (error: unknown) => void;
    done: boolean;
}
const facetBatches = new Map<string, FacetWaiter[]>();

export function getFieldFacet(id: number, filters: ComplexFilter | undefined, field: string, signal?: AbortSignal): Promise<SchemaField[]> {
    return new Promise((resolve, reject) => {
        const key = JSON.stringify([id, filters ?? null]);
        let waiters = facetBatches.get(key);
        if (!waiters) {
            const batch: FacetWaiter[] = [];
            waiters = batch;
            facetBatches.set(key, batch);
            setTimeout(() => {
                facetBatches.delete(key);
                void sendFacetBatch(id, filters, batch);
            }, 0);
        }
        waiters.push({ field, signal, resolve, reject, done: false });
    });
}

// How long the batcher gathers streamed lines before settling their fields together: about one frame,
// so a burst of lines costs one render rather than one each.
const FACET_LINES_FRAME_MS = 16;

async function sendFacetBatch(id: number, filters: ComplexFilter | undefined, waiters: FacetWaiter[]) {
    const controller = new AbortController();
    const settle = (waiter: FacetWaiter, outcome: () => void) => {
        if (waiter.done) return;
        waiter.done = true;
        outcome();
    };
    let gathered: Array<() => void> = [];
    let frame: ReturnType<typeof setTimeout> | undefined;
    const flush = () => {
        if (frame !== undefined) clearTimeout(frame);
        frame = undefined;
        const answers = gathered;
        gathered = [];
        answers.forEach(answer => answer());
    };
    const answer = (field: string, outcome: (waiter: FacetWaiter) => void) => {
        gathered.push(() => {
            for (const waiter of waiters) if (waiter.field === field) settle(waiter, () => outcome(waiter));
        });
        frame ??= setTimeout(flush, FACET_LINES_FRAME_MS);
    };
    const listeners = waiters.map(waiter => {
        const onAbort = () => {
            settle(waiter, () => waiter.reject(waiter.signal?.reason ?? new DOMException('Aborted', 'AbortError')));
            if (waiters.every(w => w.done)) controller.abort();
        };
        if (waiter.signal?.aborted) onAbort();
        else waiter.signal?.addEventListener('abort', onAbort);
        return onAbort;
    });
    try {
        if (waiters.every(w => w.done)) return;  // every field was dropped before the request went out
        const fields = Array.from(new Set(waiters.filter(w => !w.done).map(w => w.field)));
        const res = await fetch(`${API_BASE}/datasets/${id}/field-stats`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ filters, lightning_mode: true, fields, stream: true, breadth: knownBreadth.get(facetKey(id, filters)) }),
            signal: controller.signal,
        });
        await ensureOk(res, 'Failed to fetch field values');
        let complete = false;
        await readLines(
            res,
            (entry: SchemaField & { error?: string; done?: boolean; refusal?: { status: number; detail: string; code?: string; dataset_id?: number; field?: string; model_id?: string | null } }) => {
                if (entry.done) complete = true;
                // Refused after the stream began (a concept being rescored, a lasso on a re-fitted map): the
                // same ApiError a 409 answer gives, so the query retries or the stale lasso is dropped.
                else if (entry.refusal) {
                    const refusal = entry.refusal;
                    answer(entry.name, waiter => waiter.reject(new ApiError(refusal.status, refusal.detail, refusal)));
                }
                else answer(entry.name, waiter => (entry.error ? waiter.reject(new Error(entry.error)) : waiter.resolve([entry])));
            },
            // A line that is not JSON fails the field it names, not the fields after it.
            line => {
                const name = fieldOfLine(line);
                if (name !== undefined) answer(name, waiter => waiter.reject(new Error("Could not read this field's filtered values")));
            },
        );
        flush();
        // Only a complete answer ends with its `done` line; anything else failed on the way.
        if (!complete) throw new Error('The filtered values stopped before they were complete');
        // A field a complete answer did not name (no longer in the dataset) keeps its sidebar values.
        for (const waiter of waiters) settle(waiter, () => waiter.resolve([]));
    } catch (error) {
        flush();  // what did arrive still counts
        for (const waiter of waiters) settle(waiter, () => waiter.reject(error));
        controller.abort();  // nobody waits for the rest: the server stops computing it
    } finally {
        waiters.forEach((waiter, index) => waiter.signal?.removeEventListener('abort', listeners[index]));
    }
}

// The field a line names, read from its start (`{"name": "...", ...}`), when the line is not JSON.
function fieldOfLine(line: string): string | undefined {
    const match = /^\s*\{\s*"name"\s*:\s*("(?:[^"\\]|\\.)*")/.exec(line);
    try {
        return match ? JSON.parse(match[1]) : undefined;
    } catch {
        return undefined;
    }
}

// Calls `onLine` with each JSON line of a streamed (NDJSON) response as soon as the line is complete,
// and `onBadLine` with a line that is not JSON.
async function readLines<T>(res: Response, onLine: (value: T) => void, onBadLine: (line: string) => void): Promise<void> {
    const take = (line: string) => {
        if (!line.trim()) return;
        let value: T;
        try {
            value = JSON.parse(line);
        } catch {
            onBadLine(line);
            return;
        }
        onLine(value);
    };
    if (!res.body) {
        for (const line of (await res.text()).split('\n')) take(line);
        return;
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let pending = '';
    for (;;) {
        const { done, value } = await reader.read();
        pending += done ? decoder.decode() : decoder.decode(value, { stream: true });
        let end = pending.indexOf('\n');
        while (end >= 0) {
            const line = pending.slice(0, end);
            pending = pending.slice(end + 1);
            take(line);
            end = pending.indexOf('\n');
        }
        if (done) break;
    }
    take(pending);
}

// Save per-field overlay display config (heatmap colormap / range / opacity). Empty
// object clears it (rendering falls back to the heatmap's metadata defaults).
export async function saveOverlayConfig(
    datasetId: number,
    field: string,
    cfg: { colormap?: string; range?: [number, number]; opacity?: number },
): Promise<void> {
    const res = await fetch(`${API_BASE}/datasets/${datasetId}/fields/${encodeURIComponent(field)}/overlay_config`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(cfg),
    });
    await ensureOk(res, 'Failed to save overlay config');
}

// The index builds in a server job; this resolves when it is ready (or rejects if it failed).
export async function promoteField(datasetId: number, fieldName: string): Promise<void> {
    const res = await fetch(`${API_BASE}/datasets/${datasetId}/fields/${fieldName}/promote`, {
        method: 'POST',
        credentials: 'include',
    });
    await jobResult<{ message: string }>(res, datasetId, 'Failed to promote field');
}

// `exhaustive: false` = the server's bounded value scan hit its time budget and `values` is
// only the cached preview — an empty list then means "could not search", NOT "no such value".
export type AutocompleteResult = { values: Array<{ value: string; count: number | null }>; exhaustive: boolean };

export async function autocompleteField(datasetId: number, fieldName: string, query: string, filters?: ComplexFilter, limit: number = 100, signal?: AbortSignal): Promise<AutocompleteResult> {
    const res = await fetch(`${API_BASE}/datasets/${datasetId}/fields/${fieldName}/autocomplete`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ q: query, limit, filters }),
        signal,
    });
    await ensureOk(res, 'Failed to search field values');
    return res.json();
}

// --- Hashes (exact duplicates) ---

// Hash fields available on a dataset (e.g. 'phash_8'), with row counts.
export async function getHashFields(datasetId: number): Promise<Array<{ field_name: string; count: number }>> {
    const res = await fetch(`${API_BASE}/datasets/${datasetId}/hashes/fields`);
    await ensureOk(res, 'Failed to fetch hash fields');
    return res.json();
}

// How many samples the "collapse duplicates" toggle hides for a hash_config
// (a hash field_name, or '_all'). 0 if not yet built.
// Rebuild the collapse-duplicates set for one config from the current hashes. Without it the set
// only refreshed when the hourly stats job ran — which nothing schedules on a dev box — so new
// duplicates stayed visible and "N hidden" was silently out of date.
export async function refreshDuplicates(datasetId: number, config: string): Promise<{ config: string; hidden: number }> {
    const res = await fetch(`${API_BASE}/datasets/${datasetId}/hashes/refresh-duplicates?config=${encodeURIComponent(config)}`, {
        method: 'POST',
        credentials: 'include',
    });
    return jobResult(res, datasetId, 'Failed to rebuild the duplicate set');
}

export async function getDuplicateCount(datasetId: number, config: string): Promise<{ config: string; hidden: number }> {
    const res = await fetch(`${API_BASE}/datasets/${datasetId}/hashes/duplicate-count?config=${encodeURIComponent(config)}`);
    await ensureOk(res, 'Failed to fetch duplicate count');
    return res.json();
}

// Embedding "concepts": a saved linear direction w → a calibrated [0,1]
// score per sample. Exclude/keep via the concept_select/concept_exclude filter operators.
export interface Concept {
    id: number;
    name: string;
    embedding_field: string;
    kind: string;
    bias: number;
    threshold: number;
    scored_count: number;
    auto_rescore: boolean; // hourly job auto-scores newly-streamed samples for this concept
    matched?: number;
    score_min?: number; // actual score range — the UI ranges the threshold slider to [min,max]
    score_max?: number;
    spec?: Record<string, unknown>;
    created_at?: string;
    updated_at?: string;
    // false between a definition change and the end of its scoring job: filters refuse the concept.
    scores_ready?: boolean;
    // The explicit scoring job that owns the concept, if any (listing only).
    score_job?: { id: string; state: JobRecord['state'] } | null;
}

export async function listConcepts(datasetId: number): Promise<Concept[]> {
    const res = await fetch(`${API_BASE}/datasets/${datasetId}/concepts`);
    await ensureOk(res, 'Failed to list concepts');
    return res.json();
}

// Create a concept from sample ids. kind='seed' averages their embeddings; kind='probe' fits
// a logistic boundary (positives = seed_ids, negatives = neg_ids or a random background).
export async function createConcept(
    datasetId: number,
    body: { name: string; seed_ids?: number[]; prompt?: string; kind?: string; neg_ids?: number[]; embedding_field?: string; threshold?: number },
): Promise<Concept> {
    const res = await fetch(`${API_BASE}/datasets/${datasetId}/concepts`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ kind: 'seed', ...body }),
    });
    return jobResult(res, datasetId, 'Failed to create concept');
}

// Count of samples passing `score <op> threshold` — index-fast, for live slider tuning.
export async function conceptPreview(
    datasetId: number,
    conceptId: number,
    op: string,
    threshold: number,
): Promise<{ concept_id: number; op: string; threshold: number; matched: number; total: number }> {
    const res = await fetch(
        `${API_BASE}/datasets/${datasetId}/concepts/${conceptId}/preview?op=${encodeURIComponent(op)}&threshold=${threshold}`,
    );
    await ensureOk(res, 'Failed to preview concept');
    return res.json();
}

// Samples straddling the cut (closest to the threshold on both sides), with score + which side
// — so the preview shifts as the slider moves and you can correct either way.
export interface BoundarySample {
    sample_id: number;
    score: number;
    cut: boolean; // true = currently in the excluded set
}
export async function conceptBoundary(
    datasetId: number,
    conceptId: number,
    op: string,
    threshold: number,
    limit = 8,
): Promise<BoundarySample[]> {
    const res = await fetch(
        `${API_BASE}/datasets/${datasetId}/concepts/${conceptId}/boundary?op=${encodeURIComponent(op)}&threshold=${threshold}&limit=${limit}`,
    );
    await ensureOk(res, 'Failed to fetch concept boundary');
    return res.json();
}

// Threshold of the score ~n samples past the current cut (server-computed via an indexed
// offset) — for stepping the slider by a fixed NUMBER of samples instead of a fixed value.
export async function conceptStep(
    datasetId: number,
    conceptId: number,
    threshold: number,
    direction: 'up' | 'down',
    n = 8,
): Promise<{ threshold: number }> {
    const res = await fetch(
        `${API_BASE}/datasets/${datasetId}/concepts/${conceptId}/step?threshold=${threshold}&direction=${direction}&n=${n}`,
    );
    await ensureOk(res, 'Failed to step threshold');
    return res.json();
}

// Active-learning refine: keep_ids → negatives ("don't exclude"), exclude_ids → positives
// ("exclude / more like this"). Refits the concept (becomes a probe) and rescores.
export async function refineConcept(
    datasetId: number,
    conceptId: number,
    body: { keep_ids?: number[]; exclude_ids?: number[]; threshold?: number; self_train?: boolean },
): Promise<Concept> {
    const res = await fetch(`${API_BASE}/datasets/${datasetId}/concepts/${conceptId}/refine`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    return jobResult(res, datasetId, 'Failed to refine concept');
}

// Overwrite-in-place: retune the saved threshold (no rescore) or rebuild w from new seeds.
export async function rebuildConcept(
    datasetId: number,
    conceptId: number,
    body: { seed_ids?: number[]; threshold?: number },
): Promise<Concept> {
    const res = await fetch(`${API_BASE}/datasets/${datasetId}/concepts/${conceptId}/rebuild`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    return jobResult(res, datasetId, 'Failed to rebuild concept');
}

// Re-score new (unscored) samples; full=true rebuilds all scores.
export async function rescoreConcept(
    datasetId: number,
    conceptId: number,
    full = false,
): Promise<{ concept_id: number; scored: number }> {
    const res = await fetch(`${API_BASE}/datasets/${datasetId}/concepts/${conceptId}/rescore?full=${full}`, {
        method: 'POST',
    });
    return jobResult(res, datasetId, 'Failed to rescore concept');
}

// Continue a stopped concept scoring job from its checkpoint (an administrator or its submitter).
export async function resumeConceptJob<T = unknown>(datasetId: number, jobId: string): Promise<T> {
    const response = await fetch(`${API_BASE}/jobs/${jobId}/resume`, { method: 'POST', credentials: 'include' });
    await ensureOk(response, 'Could not resume scoring');
    notifyJobSubmitted(datasetId, jobId);
    return waitForJob<T>(jobId, 'Scoring failed');
}

// Toggle whether the hourly maintenance job auto-scores newly-streamed samples for this
// concept (keeps a saved concept-exclude/select current as data streams in).
export async function setConceptAutoRescore(
    datasetId: number,
    conceptId: number,
    enabled: boolean,
): Promise<{ id: number; auto_rescore: boolean }> {
    const res = await fetch(`${API_BASE}/datasets/${datasetId}/concepts/${conceptId}/auto-rescore`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled }),
    });
    await ensureOk(res, 'Failed to update auto-rescore');
    return res.json();
}

export async function deleteConcept(datasetId: number, conceptId: number): Promise<void> {
    const res = await fetch(`${API_BASE}/datasets/${datasetId}/concepts/${conceptId}`, { method: 'DELETE' });
    await ensureOk(res, 'Failed to delete concept');
}

// Exact duplicates of a single sample. fieldName: a specific hash field,
// '_any' (match on >=1 of the sample's hash fields), or '_all' (every field).
export async function sampleDuplicates(datasetId: number, sampleId: number, fieldName: string = '_any'): Promise<{sample_ids: number[], field_name: string}> {
    const res = await fetch(`${API_BASE}/datasets/${datasetId}/hashes/sample-duplicates`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sample_id: sampleId, field_name: fieldName }),
    });
    await ensureOk(res, 'Duplicate lookup failed');
    return res.json();
}

export async function findDuplicates(datasetId: number, limit: number = 50, offset: number = 0, fieldName?: string, filters?: ComplexFilter): Promise<{groups: Array<{sample_ids: number[], size: number}>, total_groups: number, total_duplicate_samples?: number}> {
    const res = await fetch(`${API_BASE}/datasets/${datasetId}/hashes/duplicates`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ limit, offset, field_name: fieldName, filters: filters || undefined }),
    });
    await ensureOk(res, 'Duplicate detection failed');
    return res.json();
}

export async function getHashStats(datasetId: number, filters?: ComplexFilter): Promise<{total_samples: number, fields: Record<string, {samples_with_hashes: number, exact_duplicate_groups: number, exact_duplicate_samples: number}>}> {
    if (filters) {
        const res = await fetch(`${API_BASE}/datasets/${datasetId}/hashes/stats`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ filters }),
        });
        await ensureOk(res, 'Hash stats failed');
        return res.json();
    }
    const res = await fetch(`${API_BASE}/datasets/${datasetId}/hashes/stats`);
    await ensureOk(res, 'Hash stats failed');
    return res.json();
}

// A class-probability field's default rule (lib/class-rules.ts); null removes it.
export async function setClassRule(datasetId: number, fieldName: string, rule: ClassRule | null): Promise<void> {
    const res = await fetch(`${API_BASE}/datasets/${datasetId}/class-rules/${encodeURIComponent(fieldName)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({ rule }),
    });
    await ensureOk(res, 'Saving the rule failed');
}

export async function setFieldType(datasetId: number, fieldName: string, fieldType: DeclaredFieldType): Promise<void> {
    const res = await fetch(`${API_BASE}/datasets/${datasetId}/fields/${encodeURIComponent(fieldName)}/type`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({ field_type: fieldType }),
    });
    await ensureOk(res, 'Failed to change field type');
}

export async function setFieldDisplayType(datasetId: number, fieldName: string, displayType: string | null): Promise<void> {
    const res = await fetch(`${API_BASE}/datasets/${datasetId}/fields/${fieldName}/display-type`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({ display_type: displayType }),
    });
    await ensureOk(res, 'Failed to set display type');
}

export async function demoteField(datasetId: number, fieldName: string): Promise<void> {
    // The index is dropped online in a server job (DROP INDEX CONCURRENTLY): follow it to the end.
    const res = await fetch(`${API_BASE}/datasets/${datasetId}/fields/${fieldName}/demote`, {
        method: 'POST',
        credentials: 'include',
    });
    await jobResult<{ message: string }>(res, datasetId, 'Failed to demote field');
}

export async function getSamples(
    id: number,
    limit = 100,
    offset = 0,
    filters?: ComplexFilter,
    sort_by?: string,
    sort_order?: 'asc' | 'desc',
    skip_count?: boolean,
    slim?: boolean,
    fields?: string,
    orderByIds?: number[],
    orderByParent?: boolean,
): Promise<{ items: Sample[], total: number }> {
    const body: {
        limit: number;
        offset: number;
        filters?: ComplexFilter;
        sort_by?: string;
        sort_order?: 'asc' | 'desc';
        skip_count?: boolean;
        slim?: boolean;
        fields?: string;
        order_by_ids?: number[];
        order_by_parent?: boolean;
    } = {
        limit,
        offset
    };

    if (filters) {
        body.filters = filters;
    }

    if (sort_by) {
        body.sort_by = sort_by;
        if (sort_order) {
            body.sort_order = sort_order;
        }
    }

    if (skip_count) {
        body.skip_count = true;
    }

    // Slim path: fetch only the requested columns (e.g. id-only) instead of full
    // sample rows + COUNT(*). Required for any "fetch all matching ids" use.
    if (slim && fields) {
        body.slim = true;
        body.fields = fields;
    }

    // Rank order (similarity / patch search): server orders by position in this id list.
    if (orderByIds && orderByIds.length) {
        body.order_by_ids = orderByIds;
        // Cross-grain rank: the id list holds IMAGE ids and these are OBJECT rows, so order each
        // object by its parent image's position (an image/patch search ranking the object grid).
        if (orderByParent) {
            body.order_by_parent = true;
        }
    }

    const res = await fetch(`${API_BASE}/datasets/${id}/samples/query`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
    });

    await ensureOk(res, 'Failed to fetch samples');
    return res.json();
}

// --- Similarity Search ---
export async function searchSimilar(params: {
    datasetId: number;
    sampleId?: number;
    sampleIds?: number[];
    text?: string;  // natural-language prompt — CLIP-text-encoded server-side
    embeddingField: string;
    limit: number;
    filters?: ComplexFilter;
}): Promise<Sample[]> {
    const res = await fetch(`${API_BASE}/search`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            dataset_id: params.datasetId,
            sample_id: params.sampleId || undefined,
            sample_ids: params.sampleIds || undefined,
            text: params.text || undefined,
            field_name: params.embeddingField,
            limit: params.limit,
            filters: params.filters
        })
    });
    await ensureOk(res, 'Failed to search similar samples');
    return res.json();
}

// Reverse image search: rank the dataset by similarity to an uploaded image (base64,
// optionally a data: URL) or a remote image URL. The server CLIP-encodes the image into
// the field's space and runs the same KNN as searchSimilar. CLIP-family fields only.
export async function searchByImage(params: {
    datasetId: number;
    imageBase64?: string;
    imageUrl?: string;
    embeddingField: string;
    limit: number;
    filters?: ComplexFilter;
}): Promise<Sample[]> {
    const res = await fetch(`${API_BASE}/search/by-image`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            dataset_id: params.datasetId,
            image_base64: params.imageBase64 || undefined,
            image_url: params.imageUrl || undefined,
            field_name: params.embeddingField,
            limit: params.limit,
            filters: params.filters
        })
    });
    await ensureOk(res, 'Failed to search by image');
    return res.json();
}

// --- Patch Similarity Search ---

export interface PatchSearchResult {
    sample_id: number;
    patch_row: number;
    patch_col: number;
    distance: number;
    filepath: string;
    metadata: Record<string, unknown>;
    media_type: string;
    tags: string[];
    location?: Sample['location'];
}

// A matched patch tile from object→patch cross-search (find-unlabeled). Same shape as a
// patch hit, plus the per-hit scale (results can mix scales).
export interface ObjectToPatchResult extends PatchSearchResult {
    patch_scale: string | null;
}

// From a labeled object, find similar patch tiles NOT already covered by a labeled box —
// i.e. unlabeled look-alike regions ("find missing things").
export async function searchObjectToPatches(params: {
    datasetId: number;
    objectSampleId: number;
    baseField?: string;
    patchScale?: string;  // e.g. "4x4"; omit for all computed scales
    limit?: number;
    iouThreshold?: number;
    filters?: ComplexFilter;
}): Promise<ObjectToPatchResult[]> {
    const res = await fetch(`${API_BASE}/search/object-to-patches`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            dataset_id: params.datasetId,
            object_sample_id: params.objectSampleId,
            base_field: params.baseField ?? 'clip',
            patch_scale: params.patchScale,
            limit: params.limit ?? 50,
            iou_threshold: params.iouThreshold ?? 0.3,
            filters: params.filters,
        }),
    });
    await ensureOk(res, 'Find-unlabeled search failed');
    return res.json();
}

export async function getPatchScales(
    datasetId: number,
    fieldName: string = "clip",
): Promise<{ scale: string }[]> {
    const res = await fetch(`${API_BASE}/datasets/${datasetId}/embeddings/patch-scales?field_name=${fieldName}`);
    if (!res.ok) return [];
    return res.json();
}

export async function searchSimilarPatches(params: {
    datasetId: number;
    sampleId: number;
    fieldName: string;
    patchScale: string;
    patchRow: number;
    patchCol: number;
    limit: number;
    filters?: ComplexFilter;
}): Promise<PatchSearchResult[]> {
    const res = await fetch(`${API_BASE}/search/patches`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            dataset_id: params.datasetId,
            sample_id: params.sampleId,
            field_name: params.fieldName,
            patch_scale: params.patchScale,
            patch_row: params.patchRow,
            patch_col: params.patchCol,
            limit: params.limit,
            filters: params.filters,
        })
    });
    await ensureOk(res, 'Patch similarity search failed');
    return res.json();
}

// --- Storage Configs ---
export interface StorageConfig {
    id: number;
    name: string;
    storage_type: string;
    bucket_name: string;
    region: string;
    access_key_id?: string;
    endpoint_url?: string;
    is_public_bucket: boolean;
    is_default: boolean;
    bucket_patterns: string[];
    created_at: string;
    // false: serves every dataset. true: serves only dataset_ids.
    restricted: boolean;
    dataset_ids: number[];
}

export interface StorageConfigCreate {
    name: string;
    storage_type?: string;
    bucket_name: string;
    region?: string;
    access_key_id?: string;
    secret_access_key?: string;
    endpoint_url?: string;
    is_public_bucket?: boolean;
    is_default?: boolean;
    bucket_patterns?: string[];
    restricted?: boolean;
    dataset_ids?: number[];
}

export interface TestConnectionResult {
    success: boolean;
    message: string;
    bucket_location?: string;
    sample_objects?: string[];
}

export async function getStorageConfigs(): Promise<StorageConfig[]> {
    const res = await fetch(`${API_BASE}/storage/configs`, {
        credentials: 'include',
    });
    if (!res.ok) {
        const msg = res.status === 403 ? 'Admin access required' : 'Failed to fetch storage configs';
        throw new ApiError(res.status, msg);
    }
    return res.json();
}

export async function createStorageConfig(config: StorageConfigCreate): Promise<StorageConfig> {
    const res = await fetch(`${API_BASE}/storage/configs`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify(config),
    });
    await ensureOk(res, 'Failed to create storage config');
    return res.json();
}

export async function updateStorageConfig(id: number, config: Partial<StorageConfigCreate>): Promise<StorageConfig> {
    const res = await fetch(`${API_BASE}/storage/configs/${id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify(config),
    });
    await ensureOk(res, 'Failed to update storage config');
    return res.json();
}

export async function deleteStorageConfig(id: number): Promise<void> {
    const res = await fetch(`${API_BASE}/storage/configs/${id}`, {
        method: 'DELETE',
        credentials: 'include',
    });
    await ensureOk(res, 'Failed to delete storage config');
}

export async function testStorageConnection(id: number): Promise<TestConnectionResult> {
    const res = await fetch(`${API_BASE}/storage/configs/${id}/test`, {
        method: 'POST',
        credentials: 'include',
    });
    await ensureOk(res, 'Failed to test connection');
    return res.json();
}

// --- Sparse Sampling ---

export interface SparseSampleField {
    name: string;
    type: 'location' | 'embedding' | 'numeric';
    description: string;
}

export interface SparseSampleResult {
    sample_ids: number[];
    count: number;
    candidate_count: number;
    elapsed_seconds: number;
}

export async function getSparseSampleFields(datasetId: number): Promise<SparseSampleField[]> {
    const res = await fetch(`${API_BASE}/datasets/${datasetId}/sparse-sample/available-fields`, {
        credentials: 'include',
    });
    await ensureOk(res, 'Failed to get available fields');
    return res.json();
}

export async function sparseSample(
    datasetId: number,
    fields: string[],
    opts: { count?: number; percentage?: number; weights?: Record<string, number>; filters?: ComplexFilter }
): Promise<SparseSampleResult> {
    const res = await fetch(`${API_BASE}/datasets/${datasetId}/sparse-sample`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({
            fields,
            count: opts.count,
            percentage: opts.percentage,
            weights: opts.weights,
            filters: opts.filters,
        }),
    });
    await ensureOk(res, 'Sparse sample failed');
    return res.json();
}

// Settings → System status (admin only): GET /api/admin/status, backend/system_status.py.
export type DiskLevel = 'ok' | 'low' | 'paused' | 'unknown';

export interface CommitInfo {
    commit: string;
    subject: string;
    committed_at: string;
    backend_tree: string;
}

// The folder scripts/backup_local.py writes to (FRIDGE_BACKUP_DIR): its verified dumps and failure records.
export type BackupLevel = 'ok' | 'overdue' | 'failed' | 'none' | 'unreadable' | 'not_configured';

export interface BackupVersions {
    server_version: string | null;
    schema_version: string | null;
    migrations_applied: number | null;
}

export type BackupStatus =
    | { level: 'not_configured'; folder: null }
    | { level: 'unreadable'; folder: string; error: string }
    | {
          level: 'ok' | 'overdue' | 'failed' | 'none';
          folder: string;
          overdue_after_hours: number | null;
          latest:
              | (BackupVersions & {
                    file: string;
                    created_at: string;
                    size_bytes: number | null;
                    dump_seconds: number | null;
                    verify_seconds: number | null;
                    tables: number | null;
                })
              | null;
          last_failure: { failed_at: string; error: string } | null;
          kept: { count: number; total_bytes: number; oldest_created_at: string | null };
          unverified_dumps: number;
          disk: { total_bytes: number; free_bytes: number };
          current?: BackupVersions;
          same_schema?: boolean;
          same_postgres_major?: boolean;
          schedule?: BackupSchedule & { timing_changed_at: string; next_run_at: string | null };
          active_job?: { id: string; state: 'queued' | 'running'; created_at: string; started_at: string | null; cancel_requested: boolean } | null;
      };

// interval_hours null: no scheduled backups (an admin can still back up now).
export interface BackupSchedule {
    interval_hours: number | null;
    start_hour: number;
    keep: number;
}

export async function saveBackupSchedule(schedule: BackupSchedule): Promise<BackupSchedule> {
    const res = await fetch(`${API_BASE}/admin/backups/schedule`, {
        method: 'PUT',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(schedule),
    });
    await ensureOk(res, 'Could not save the backup schedule');
    return res.json();
}

/** Queues a backup job, or returns the one already queued or running (`started: false`). */
export async function runBackupNow(): Promise<{ job_id: string; state: string; started: boolean }> {
    const res = await fetch(`${API_BASE}/admin/backups/run`, { method: 'POST', credentials: 'include' });
    await ensureOk(res, 'Could not start a backup');
    return res.json();
}

export interface SystemStatus {
    backups?: BackupStatus;
    workers?:
        | { available: true; server: { online: number; last_seen: string | null }; compute: { online: number; last_seen: string | null } }
        | { available: false; error: string };
    checked_at: string;
    database_disk: {
        total_bytes: number | null;
        free_bytes: number | null;
        level: DiskLevel;
        low_below_bytes: number;
        ingest_paused_below_bytes: number;
    };
    cache_disks: {
        folders: { label: string; path: string }[];
        total_bytes: number;
        free_bytes: number;
        level: 'ok' | 'low';
    }[];
    database:
        | { reachable: true; size_bytes: number; pending_migrations: string[]; unknown_migrations: string[]; stale_functions: string[] }
        | { reachable: false; error: string };
    server: {
        version: string;
        started_at: string;
        running: CommitInfo | null;
        checkout: CommitInfo | null;
        restart_needed: boolean;
    };
}

export async function getSystemStatus(): Promise<SystemStatus> {
    const res = await fetch(`${API_BASE}/admin/status`, { credentials: 'include' });
    if (!res.ok) {
        // Pass the server's reason on: with the database down the request fails before the
        // status is collected, and "Failed to load" alone hides why.
        const detail = await res.json().then((b: { detail?: unknown }) => b?.detail, () => undefined);
        const msg = res.status === 403 ? 'Admin access required' : 'Failed to load system status';
        throw new ApiError(res.status, typeof detail === 'string' ? `${msg}: ${detail}` : msg);
    }
    return res.json();
}

export async function resumeBulkJob(jobId: string): Promise<{ job_id: string; state: string }> {
    const response = await fetch(`${API_BASE}/jobs/${jobId}/resume`, { method: 'POST', credentials: 'include' });
    await ensureOk(response, 'Could not resume the job');
    return response.json();
}

export async function discardBulkJob(jobId: string): Promise<{ job_id: string; state: string; discarded: boolean }> {
    const response = await fetch(`${API_BASE}/jobs/${jobId}/discard`, { method: 'POST', credentials: 'include' });
    await ensureOk(response, 'Could not discard the job');
    return response.json();
}
