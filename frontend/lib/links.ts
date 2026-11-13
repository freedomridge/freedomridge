import type { ViewStage } from './api';
import { type ExpiredSelection, filterHasInSelection, isReadOnlyStage } from './filter-query';
import { type SourceView, sameStages } from './source-view';

// A link that opens a dataset: /datasets/<id>?<parameters>, each optional.
//   view=<slug>          a saved view, as choosing it in Saved Views does
//   q=<text>             the filter as query-language text (docs/FILTER_QUERY.md), one chip per top-level AND part
//   f=<version>.<data>   the filter stages, encoded, for chips the language can't write (map regions, lassos,
//                        selections, searches, concepts)
//   sort=<field>, dir=asc|desc   the grid's sort
//   sample=<id>          the sample whose view opens
// `view` wins over `f`, and `f` over `q`. The dataset page's address is such a link to what it shows (FilterContext
// writes it on each history entry). Copy link (datasetLink, viewLink) and the address write one of view, q or f,
// never two.

export interface DatasetLink {
    view?: string;
    q?: string;
    f?: string;
    sort?: string;
    dir?: 'asc' | 'desc';
    sample?: string;
}

/** The link in a page's query string, or null when it has none of the parameters. */
export function readLink(params: { get(name: string): string | null }): DatasetLink | null {
    const link: DatasetLink = {};
    for (const name of ['view', 'q', 'f', 'sort', 'sample'] as const) {
        const value = params.get(name);
        if (value !== null) link[name] = value;
    }
    const dir = params.get('dir');
    if (dir !== null) link.dir = dir === 'desc' ? 'desc' : 'asc';
    return Object.keys(link).length ? link : null;
}

/** Whether the link sets the filters (a view, a query or encoded stages). A sample or a sort alone doesn't. */
export function linkSetsFilters(link: DatasetLink | null): boolean {
    return !!link && (link.view !== undefined || link.f !== undefined || link.q !== undefined);
}

// --- Opening the dataset page ---

/** The grids' sort an address opens with; `by` unset is the default order. */
export interface LinkSort {
    by: string | undefined;
    order: 'asc' | 'desc';
}

/**
 * How the dataset page opens (FilterContext), decided at its first render:
 * - `entry`: on one of its own history entries, reopened by a reload or by Back or Forward into a page that was
 *   unloaded. The entry is the page's (`_fridge`) and records the address it holds (`address`, as the browser shows
 *   it), which is the address shown (`shown`) and the one the router opened (`opened`). While a navigation inside the
 *   app has yet to write its own entry, history.state and the address shown are still the previous page's.
 * - `link`: else on the link in the address, when it has one.
 * - neither: on the filters the browser saved for the dataset.
 * `sort` is the grids' sort the address gives: an entry's, which its address always says (none: the default order,
 * not the sort another tab chose since), or a link's when it names one.
 */
export function openingOf(state: unknown, shown: string, opened: string): {
    entry: Record<string, unknown> | null;
    link: DatasetLink | null;
    sort?: LinkSort;
} {
    const params = new URL(opened, 'http://x').searchParams;
    const sort = { by: params.get('sort') || undefined, order: params.get('dir') === 'desc' ? 'desc' as const : 'asc' as const };
    if (isObject(state) && state._fridge === true && typeof state.address === 'string' && state.address === shown
        && sameAddress(shown, opened)) {
        return { entry: state, link: null, sort };
    }
    return { entry: null, link: readLink(params), sort: params.has('sort') ? sort : undefined };
}

/** `address` (a path and query) with `sort` in place of the sort it names: its filters as they are. */
export function withSort(address: string, sort: { by?: string; order?: 'asc' | 'desc' }): string {
    const url = new URL(address, 'http://x');
    url.searchParams.delete('sort');
    url.searchParams.delete('dir');
    setSort(url.searchParams, sort);
    return `${url.pathname}${url.search}`;
}

/** `address` (a path and query) with the sample whose view is open (null: none), last, as Copy link in the view writes it. */
export function withSample(address: string, sample: number | null): string {
    if (sample === null) return address;
    const url = new URL(address, 'http://x');
    url.searchParams.set('sample', String(sample));
    return `${url.pathname}${url.search}`;
}

// Two addresses (path and query) name the same page and parameters, however each is encoded (`+` or `%20`).
function sameAddress(a: string, b: string): boolean {
    const one = new URL(a, 'http://x');
    const two = new URL(b, 'http://x');
    return one.pathname === two.pathname && one.searchParams.toString() === two.searchParams.toString();
}

// --- f=: the stages encoded ---
// Version 1 is the JSON {"stages": [...]} (each stage as local storage keeps a chip: type, description, filter,
// metadata), deflated (raw), in base64url. A change to that shape that old links can't be read with either keeps
// reading version 1 or takes version 2 on purpose (tests/link-fixtures.json holds links that must keep opening).
export const LINK_VERSION = 1;
// A short link can't inflate to more than this in the reader's tab.
export const MAX_INFLATED_BYTES = 1024 * 1024;

export const UNKNOWN_VERSION_NOTICE = "This link was made by a different version of Freedom Ridge and can't be opened here.";
export const BROKEN_LINK_NOTICE = "This link's filters can't be read. It may have been cut short when it was copied.";
export const OVERSIZED_LINK_NOTICE = "This link's filters are too large to open.";

/** A link this build can't open, or can't make; the message says why, for the reader. */
export class LinkError extends Error {}

/** The stages as an `f=` value. */
export async function encodeStages(stages: ViewStage[]): Promise<string> {
    const json = new TextEncoder().encode(JSON.stringify({ stages }));
    const deflated = new Blob([json]).stream().pipeThrough(new CompressionStream('deflate-raw'));
    return `${LINK_VERSION}.${toBase64Url(new Uint8Array(await new Response(deflated).arrayBuffer()))}`;
}

/** The stages of an `f=` value. Throws LinkError for another version, a damaged value or one over the bound. */
export async function decodeStages(value: string): Promise<ViewStage[]> {
    const dot = value.indexOf('.');
    if (dot < 0 || value.slice(0, dot) !== String(LINK_VERSION)) throw new LinkError(UNKNOWN_VERSION_NOTICE);
    let payload: unknown;
    try {
        payload = JSON.parse(await inflate(fromBase64Url(value.slice(dot + 1))));
    } catch (error) {
        if (error instanceof LinkError) throw error;
        throw new LinkError(BROKEN_LINK_NOTICE);
    }
    const stages = isObject(payload) ? payload.stages : undefined;
    if (!Array.isArray(stages) || !stages.every(isStage)) throw new LinkError(BROKEN_LINK_NOTICE);
    return stages;
}

function isObject(value: unknown): value is Record<string, unknown> {
    return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isStage(value: unknown): value is ViewStage {
    return isObject(value)
        && typeof value.type === 'string'
        && typeof value.description === 'string'
        && isObject(value.filter)
        && (value.metadata === undefined || isObject(value.metadata));
}

function toBase64Url(bytes: Uint8Array): string {
    let binary = '';
    for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
    return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function fromBase64Url(text: string): Uint8Array<ArrayBuffer> {
    if (!/^[A-Za-z0-9_-]*$/.test(text)) throw new LinkError(BROKEN_LINK_NOTICE);
    const base64 = text.replace(/-/g, '+').replace(/_/g, '/');
    const binary = atob(base64 + '='.repeat((4 - (base64.length % 4)) % 4));
    return Uint8Array.from(binary, (char) => char.charCodeAt(0));
}

// Inflated as it streams, stopping once it passes the bound, so a link can't expand to hundreds of megabytes.
async function inflate(bytes: Uint8Array<ArrayBuffer>): Promise<string> {
    const reader = new Blob([bytes]).stream().pipeThrough(new DecompressionStream('deflate-raw')).getReader();
    const chunks: Uint8Array[] = [];
    let size = 0;
    for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        size += value.byteLength;
        if (size > MAX_INFLATED_BYTES) {
            void reader.cancel().catch(() => {});
            throw new LinkError(OVERSIZED_LINK_NOTICE);
        }
        chunks.push(value);
    }
    const joined = new Uint8Array(size);
    let at = 0;
    for (const chunk of chunks) {
        joined.set(chunk, at);
        at += chunk.byteLength;
    }
    return new TextDecoder('utf-8', { fatal: true }).decode(joined);
}

// --- Copy link: a link to what the page shows ---

// Proxies and servers commonly cap a request line at 8 KB.
export const MAX_LINK_LENGTH = 6000;
export const TOO_LONG_NOTICE = "Can't copy a link: the selection is too large for a link. Save a view and copy its link instead.";

/**
 * Whether the stages may go in `q=`: every one an ordinary filter, which a filter card can show as text. Picked
 * samples and searches (id lists, a search's in rank order), map regions, lassos, concepts and duplicate collapses
 * go in `f=`, as their chips.
 */
export function mayBeText(stages: ViewStage[]): boolean {
    return stages.length > 0 && !stages.some(isReadOnlyStage);
}

/** Text parts joined as one query, each part one chip (POST filter_query/format brackets a part that needs it). */
export function joinQueryParts(parts: string[]): string {
    return parts.join(' AND ');
}

/**
 * The link to a dataset on its applied stages, its grid's sort (`by` unset is the default order) and optionally a
 * sample. `parts` holds each stage's filter as query text, null for one the language can't write (or null when
 * they weren't asked for): `view=` while the stages are exactly those of the public saved view they came from
 * (`source`; a private one wouldn't open for others), else `q=` when every stage may be text and has its part
 * (empty with no stages: "no filters"), else `f=` with every stage. Throws a
 * LinkError, saying why, for a stage the server forgets (a big Select All), for an expired selection's chip in the
 * bar (`expired`: the link couldn't hold the selection it names) and for a link over MAX_LINK_LENGTH.
 */
export async function datasetLink({ origin, datasetId, stages, expired = [], source = null, parts, sort, sample }: {
    origin: string;
    datasetId: number;
    stages: ViewStage[];
    expired?: ExpiredSelection[];
    source?: SourceView | null;
    parts: (string | null)[] | null;
    sort?: { by?: string; order?: 'asc' | 'desc' };
    sample?: number;
}): Promise<string> {
    const temporary = stages.filter(stage => filterHasInSelection(stage.filter));
    if (temporary.length) {
        const names = temporary.map(stage => `“${stage.description}”`).join(', ');
        throw new LinkError(`Can't copy a link: ${names} ${temporary.length === 1 ? 'is a temporary selection' : 'are temporary selections'}, `
            + 'kept for 2 hours only. Save a view and copy its link instead.');
    }
    if (expired.length) {
        const names = expired.map(selection => `“${selection.description}”`).join(', ');
        throw new LinkError(`Can't copy a link: ${names} ${expired.length === 1 ? 'is an expired selection' : 'are expired selections'}. `
            + `Select the samples again, or remove ${expired.length === 1 ? 'it' : 'them'}.`);
    }
    const params = new URLSearchParams();  // `+` in a value is written %2B: a raw + reads as a space
    if (source?.isPublic && sameStages(stages, source.stages)) {
        params.set('view', source.slug);
    } else if (mayBeText(stages) && parts?.length === stages.length && parts.every(part => part)) {
        params.set('q', joinQueryParts(parts as string[]));
    } else if (stages.length) {
        params.set('f', await encodeStages(stages));
    } else {
        // No filters is what the sender sees too: an empty query, so the reader's own saved filters and the
        // dataset's default view don't open in its place.
        params.set('q', '');
    }
    setSort(params, sort);
    if (sample !== undefined) params.set('sample', String(sample));
    const query = params.toString();
    const link = `${origin}/datasets/${datasetId}${query ? `?${query}` : ''}`;
    if (link.length > MAX_LINK_LENGTH) throw new LinkError(TOO_LONG_NOTICE);
    return link;
}

function setSort(params: URLSearchParams, sort: { by?: string; order?: 'asc' | 'desc' } | undefined) {
    if (typeof sort?.by === 'string' && sort.by) {
        params.set('sort', sort.by);
        params.set('dir', sort.order === 'desc' ? 'desc' : 'asc');
    }
}

/**
 * The dataset page's address for a state (FilterContext puts it on the state's history entry): its link, as a path and
 * query. A state no link can hold (datasetLink refuses it: a temporary or expired selection, over MAX_LINK_LENGTH)
 * gets an address with no filters, only the sort, so the address never names filters that aren't on screen. Opened
 * elsewhere it opens the dataset as it would without a link; a reload of its entry still restores the entry.
 */
export async function datasetAddress(link: Omit<Parameters<typeof datasetLink>[0], 'sample'>): Promise<string> {
    try {
        return (await datasetLink(link)).slice(link.origin.length);
    } catch (error) {
        if (!(error instanceof LinkError)) throw error;
        const params = new URLSearchParams();
        setSort(params, link.sort);
        const query = params.toString();
        return `/datasets/${link.datasetId}${query ? `?${query}` : ''}`;
    }
}

/** The link to a saved view. It opens only for people who can see the view. */
export function viewLink(origin: string, datasetId: number, slug: string): string {
    return `${origin}/datasets/${datasetId}?${new URLSearchParams({ view: slug })}`;
}

type Say = (message: string, type: 'success' | 'warning' | 'info', duration?: number) => void;

/**
 * Puts the link on the clipboard and says so with `copied`; a link that can't be made says why, and copies nothing.
 * Safari takes a write only in the click that asked for it, not after an await, so the write starts at once with
 * the link to follow (a ClipboardItem holding a promise); writeText is the fallback; and when the browser refuses
 * both, the link is shown to copy by hand.
 */
export async function copyLink(link: Promise<string>, copied: string, say: Say): Promise<void> {
    let done = false;
    if (typeof ClipboardItem !== 'undefined' && navigator.clipboard?.write) {
        try {
            const blob = link.then(text => new Blob([text], { type: 'text/plain' }));
            // Handled here too: when the browser refuses the write before reading the item, nothing else ever reads
            // `blob`, and a link that can't be made then rejected it unhandled (the awaits below cover `link` only).
            blob.catch(() => {});
            await navigator.clipboard.write([new ClipboardItem({ 'text/plain': blob })]);
            done = true;
        } catch {
            // Refused, or the link couldn't be made: told below.
        }
    }
    let text: string;
    try {
        text = await link;
    } catch (error) {
        say(error instanceof LinkError ? error.message : `Couldn't make the link: ${(error as Error).message}`, 'warning', 10000);
        return;
    }
    if (!done) {
        try {
            await navigator.clipboard.writeText(text);
            done = true;
        } catch {
            // Shown below.
        }
    }
    if (done) say(copied, 'success', 5000);
    else say(`This browser didn't let the page copy. The link: ${text}`, 'info', 0);
}

// --- Login: back to the page asked for ---

/** The login page, set to come back to `here` (a page of this app) after signing in. */
export function loginPath(here: { pathname: string; search: string }, expired = false): string {
    const params = new URLSearchParams();
    if (expired) params.set('expired', 'true');
    const page = `${here.pathname}${here.search}`;
    if (here.pathname !== '/login' && page !== '/') params.set('next', page);
    const query = params.toString();
    return query ? `/login?${query}` : '/login';
}

/**
 * Where to go after signing in: `next` when it is a page of this app, else `/`. Checked as the browser reads it
 * (`/\evil.example` is `//evil.example` to a browser, and tabs and newlines are dropped), not as a string.
 */
export function safeNext(next: string | null | undefined, origin: string): string {
    if (!next) return '/';
    try {
        const url = new URL(next, origin);
        return url.origin === origin ? `${url.pathname}${url.search}${url.hash}` : '/';
    } catch {
        return '/';
    }
}
