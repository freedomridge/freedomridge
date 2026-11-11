import type { ViewStage } from './api';
import { type ExpiredSelection, filterHasInSelection, isReadOnlyStage } from './filter-query';

// A link that opens a dataset: /datasets/<id>?<parameters>, each optional.
//   view=<slug>          a saved view, as choosing it in Saved Views does
//   q=<text>             the filter as query-language text (docs/FILTER_QUERY.md), one chip per top-level AND part
//   f=<version>.<data>   the filter stages, encoded, for chips the language can't write (map regions, lassos,
//                        selections, searches, concepts)
//   sort=<field>, dir=asc|desc   the grid's sort
//   sample=<id>          the sample whose view opens
// `view` wins over `f`, and `f` over `q`. The page removes the parameters once it has opened them. Copy link
// (datasetLink, viewLink) writes one of view, q or f, never two.
export const LINK_PARAMETERS = ['view', 'q', 'f', 'sort', 'dir', 'sample'] as const;

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

/** `href` without the link's parameters, as the path, query and hash history.replaceState takes. */
export function withoutLinkParameters(href: string): string {
    const url = new URL(href);
    for (const name of LINK_PARAMETERS) url.searchParams.delete(name);
    return `${url.pathname}${url.search}${url.hash}`;
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
 * they weren't asked for): `q=` when every stage may be text and has its part (empty with no stages: "no
 * filters"), else `f=` with every stage. Throws a
 * LinkError, saying why, for a stage the server forgets (a big Select All), for an expired selection's chip in the
 * bar (`expired`: the link couldn't hold the selection it names) and for a link over MAX_LINK_LENGTH.
 */
export async function datasetLink({ origin, datasetId, stages, expired = [], parts, sort, sample }: {
    origin: string;
    datasetId: number;
    stages: ViewStage[];
    expired?: ExpiredSelection[];
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
    if (mayBeText(stages) && parts?.length === stages.length && parts.every(part => part)) {
        params.set('q', joinQueryParts(parts as string[]));
    } else if (stages.length) {
        params.set('f', await encodeStages(stages));
    } else {
        // No filters is what the sender sees too: an empty query, so the reader's own saved filters and the
        // dataset's default view don't open in its place.
        params.set('q', '');
    }
    if (typeof sort?.by === 'string' && sort.by) {
        params.set('sort', sort.by);
        params.set('dir', sort.order === 'desc' ? 'desc' : 'asc');
    }
    if (sample !== undefined) params.set('sample', String(sample));
    const query = params.toString();
    const link = `${origin}/datasets/${datasetId}${query ? `?${query}` : ''}`;
    if (link.length > MAX_LINK_LENGTH) throw new LinkError(TOO_LONG_NOTICE);
    return link;
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
