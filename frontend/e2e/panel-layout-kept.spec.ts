import fs from 'fs';
import { test, expect, type Browser, type BrowserContext, type Page } from '@playwright/test';
import { AUTH_STATE, gotoDataset } from './helpers';
import { readState } from './api';

// A panel layout a person arranged is kept (the contract "three page fixes", section 1). A dataset page opened on
// the layout kept from the last visit, and shortly after the default layout was written back to
// localStorage["fridge-panel-layout-v2"], so the kept layout survived only one visit. Now the layout a person leaves a
// dataset page in is what the next visit opens on (after a reload, after navigating away and back, in a new tab of the
// same browser, any number of times), opening a page never writes over it, and a stored layout the page can't read
// opens the default layout without an error.
//
// Read here:
// - "The layout" is what the store holds today: the tree of panels (which panel, where) and each panel's share of its
//   group. Two layouts are the same when their trees, panel types and shares agree within one percentage point; the
//   panels' ids and a rewrite of the same shares (say re-rounded) don't count as a change. What the page shows is read
//   from the panels' boxes and compared with the boxes the stored tree gives, within 3% of the panel area.
// - A layout is seeded once per browser context (its storage state), never by an init script, which would put it back
//   on every load and hide a write over it. An init script records every write to the key (when, after the page
//   began loading, and what), only to say in a failure what was written.
// - "Nothing stored opens the default" pins today's default: Simple Filters 20%, Image Grid 50%, Embeddings 30%.
// - A stored value "the page can't read" (contract 1.3): not JSON (opens the default today), and JSON of the wrong
//   shape (crashes the page into "Something went wrong" today): null, an array, a number, an object that is no
//   panel, a group without children, a group whose children aren't a list, an older flat shape, a tree naming a panel
//   type the app doesn't know. No older shape is in the repository's history under this key, so "an older flat shape"
//   is taken to be a bare list of panels with no group around them. The test doesn't look at what stays stored then:
//   the unreadable value may stay until the person changes the layout.
// - Reloads come in threes: on main the first reload after a visit can still show the kept layout (the write over it
//   lands some 600 ms after the page starts loading), and the next ones show the default.
// - The contract's "the control that resets it, where there is one": the dataset page has no such control on main, so
//   nothing here tests one.
test.use({ storageState: AUTH_STATE });

const KEY = 'fridge-panel-layout-v2';

type LayoutNode =
    | { id?: string; type: 'leaf'; data: { panelType: string }; size?: number }
    | { id?: string; type: 'group'; direction: 'horizontal' | 'vertical'; children: LayoutNode[]; size?: number };

type Box = { type: string; x: number; y: number; w: number; h: number };

const DEFAULT: LayoutNode = {
    type: 'group',
    direction: 'horizontal',
    children: [
        { type: 'leaf', data: { panelType: 'filters' }, size: 20 },
        { type: 'leaf', data: { panelType: 'grid' }, size: 50 },
        { type: 'leaf', data: { panelType: 'embeddings' }, size: 30 },
    ],
};

// A layout unlike the default in every way the store records: the panels, where they are, and their sizes.
const NESTED: LayoutNode = {
    id: 'root',
    type: 'group',
    direction: 'horizontal',
    children: [
        { id: 'kept-grid', type: 'leaf', data: { panelType: 'grid' }, size: 62 },
        {
            id: 'kept-side',
            type: 'group',
            direction: 'vertical',
            size: 38,
            children: [
                { id: 'kept-filters', type: 'leaf', data: { panelType: 'filters' }, size: 65 },
                { id: 'kept-histogram', type: 'leaf', data: { panelType: 'histogram' }, size: 35 },
            ],
        },
    ],
};

// Three panels side by side like the default, but other panels in other places and other sizes.
const THREE_ACROSS: LayoutNode = {
    id: 'root',
    type: 'group',
    direction: 'horizontal',
    children: [
        { id: 'left', type: 'leaf', data: { panelType: 'grid' }, size: 45 },
        { id: 'center', type: 'leaf', data: { panelType: 'histogram' }, size: 25 },
        { id: 'right', type: 'leaf', data: { panelType: 'filters' }, size: 30 },
    ],
};

const SEEDED: Array<[string, LayoutNode]> = [['a nested layout', NESTED], ['three other panels across', THREE_ACROSS]];

function recordLayoutWrites(key: string) {
    const writes: Array<{ at: number; value: string | null }> = [];
    (window as unknown as { __layoutWrites: typeof writes }).__layoutWrites = writes;
    const setItem = Storage.prototype.setItem;
    const removeItem = Storage.prototype.removeItem;
    Storage.prototype.setItem = function (this: Storage, name: string, value: string) {
        if (name === key) writes.push({ at: Math.round(performance.now()), value: String(value) });
        return setItem.call(this, name, value);
    };
    Storage.prototype.removeItem = function (this: Storage, name: string) {
        if (name === key) writes.push({ at: Math.round(performance.now()), value: null });
        return removeItem.call(this, name);
    };
}

/** A browser signed in as the admin, with `stored` (a raw string) as the kept layout, or nothing stored. */
async function browserWith(browser: Browser, baseURL: string | undefined, stored?: string): Promise<BrowserContext> {
    const auth = JSON.parse(fs.readFileSync(AUTH_STATE, 'utf-8'));
    const origin = new URL(baseURL || 'http://localhost:3000').origin;
    const context = await browser.newContext({
        storageState: {
            cookies: auth.cookies,
            origins: stored === undefined ? [] : [{ origin, localStorage: [{ name: KEY, value: stored }] }],
        },
    });
    await context.addInitScript(recordLayoutWrites, KEY);
    return context;
}

/** The boxes a layout tree gives its panels, as fractions of the panel area, in the order the page lists them. */
function boxesOf(node: LayoutNode, x = 0, y = 0, w = 1, h = 1, out: Box[] = []): Box[] {
    if (node.type === 'leaf') {
        out.push({ type: node.data.panelType, x, y, w, h });
        return out;
    }
    const sizes = node.children.map(child => child.size ?? 100 / node.children.length);
    const total = sizes.reduce((a, b) => a + b, 0);
    let offset = 0;
    node.children.forEach((child, i) => {
        const share = sizes[i] / total;
        if (node.direction === 'horizontal') boxesOf(child, x + offset * w, y, share * w, h, out);
        else boxesOf(child, x, y + offset * h, w, share * h, out);
        offset += share;
    });
    return out;
}

/** The panels the page shows: each panel's type and box, as fractions of the area the panels fill. */
async function shownBoxes(page: Page): Promise<Box[]> {
    return page.evaluate(() => {
        const panels = Array.from(document.querySelectorAll<HTMLSelectElement>('[data-testid="panel-type-select"]')).map(select => {
            const root = select.closest('[data-testid^="panel-"]:not([data-testid="panel-type-select"])') ?? select;
            const r = root.getBoundingClientRect();
            return { type: select.value, x: r.left, y: r.top, w: r.width, h: r.height };
        });
        if (!panels.length) return [];
        const left = Math.min(...panels.map(p => p.x));
        const top = Math.min(...panels.map(p => p.y));
        const width = Math.max(...panels.map(p => p.x + p.w)) - left;
        const height = Math.max(...panels.map(p => p.y + p.h)) - top;
        const round = (v: number) => Math.round(v * 1000) / 1000;
        return panels.map(p => ({
            type: p.type,
            x: round((p.x - left) / width),
            y: round((p.y - top) / height),
            w: round(p.w / width),
            h: round(p.h / height),
        }));
    });
}

function describeBoxes(boxes: Box[]): string {
    const pct = (v: number) => Math.round(v * 100);
    return boxes.map(b => `${b.type} at (${pct(b.x)}%, ${pct(b.y)}%) ${pct(b.w)}% x ${pct(b.h)}%`).join('; ') || 'no panels';
}

function sameBoxes(shown: Box[], expected: Box[], tolerance = 0.03): boolean {
    return shown.length === expected.length && shown.every((box, i) => box.type === expected[i].type
        && Math.abs(box.x - expected[i].x) <= tolerance && Math.abs(box.y - expected[i].y) <= tolerance
        && Math.abs(box.w - expected[i].w) <= tolerance && Math.abs(box.h - expected[i].h) <= tolerance);
}

/** Wait until the page shows `layout` (soft: a test goes on to read what was stored either way). */
async function expectShown(page: Page, layout: LayoutNode, when: string, timeout = 15_000) {
    const expected = boxesOf(layout);
    let last: Box[] = [];
    await expect.soft.poll(async () => {
        last = await shownBoxes(page);
        return sameBoxes(last, expected);
    }, {
        timeout,
        message: `${when}: the page shows the layout ${describeBoxes(expected)}`,
    }).toBe(true);
    if (!sameBoxes(last, expected)) console.log(`[layout] ${when}: the page showed ${describeBoxes(last)}`);
}

/** null when `value` is the layout `expected` (see the opening comment), else what differs. */
function layoutDifference(value: unknown, expected: LayoutNode, at = 'the layout'): string | null {
    const node = value as Partial<LayoutNode & { children: unknown; data: { panelType?: unknown } }> | null;
    if (!node || typeof node !== 'object') return `${at} is ${JSON.stringify(value)}`;
    if (node.type !== expected.type) return `${at} is a ${String(node.type)}, not a ${expected.type}`;
    if (expected.type === 'leaf') {
        const panel = (node as { data?: { panelType?: unknown } }).data?.panelType;
        return panel === expected.data.panelType ? null : `${at} is ${String(panel)}, not ${expected.data.panelType}`;
    }
    const group = node as { direction?: unknown; children?: unknown };
    if (group.direction !== expected.direction) return `${at} is ${String(group.direction)}, not ${expected.direction}`;
    if (!Array.isArray(group.children) || group.children.length !== expected.children.length) {
        return `${at} has ${Array.isArray(group.children) ? group.children.length : 'no'} panels, not ${expected.children.length}`;
    }
    const children = group.children as Array<{ size?: unknown }>;
    const shares = (sizes: Array<number | undefined>) => {
        const filled = sizes.map(size => (typeof size === 'number' ? size : 100 / sizes.length));
        const total = filled.reduce((a, b) => a + b, 0);
        return filled.map(size => (size / total) * 100);
    };
    const got = shares(children.map(child => (typeof child.size === 'number' ? child.size : undefined)));
    const want = shares(expected.children.map(child => child.size));
    for (let i = 0; i < want.length; i++) {
        if (Math.abs(got[i] - want[i]) > 1) {
            return `${at}'s panel ${i + 1} has ${got[i].toFixed(1)}% of it, not ${want[i].toFixed(1)}%`;
        }
        const inner = layoutDifference(children[i], expected.children[i], `${at}'s panel ${i + 1}`);
        if (inner) return inner;
    }
    return null;
}

function storedDifference(raw: string | null, expected: LayoutNode): string | null {
    if (raw === null) return 'nothing is stored';
    let parsed: unknown;
    try {
        parsed = JSON.parse(raw);
    } catch {
        return `the stored value is not JSON: ${raw.slice(0, 80)}`;
    }
    return layoutDifference(parsed, expected);
}

const stored = (page: Page) => page.evaluate(key => localStorage.getItem(key), KEY);
const writes = (page: Page) => page.evaluate(
    () => (window as unknown as { __layoutWrites?: Array<{ at: number; value: string | null }> }).__layoutWrites ?? [],
);

/**
 * Read the stored layout every quarter second for `ms` and expect each read to be `expected` (soft). A failure says
 * when the stored value first differed and lists the writes to the key since the page began loading.
 */
async function expectStoredStays(page: Page, expected: LayoutNode, when: string, ms = 5_000) {
    const start = Date.now();
    let first: string | null = null;
    while (Date.now() - start <= ms) {
        const raw = await stored(page);
        const difference = storedDifference(raw, expected);
        if (difference) {
            first = `${Date.now() - start} ms into the ${ms} ms after the page settled, ${difference}`;
            break;
        }
        await page.waitForTimeout(250);
    }
    const log = (await writes(page)).map(write => `${write.at} ms after load: ${write.value === null ? 'removed' : write.value}`);
    expect.soft(first, `${when}: the stored layout stays the kept one. Writes to ${KEY}: ${log.join(' | ') || 'none'}`).toBeNull();
}

async function dragSeparator(page: Page, index: number, dx: number, dy: number) {
    const separator = page.locator('[role="separator"][data-separator]').nth(index);
    const box = await separator.boundingBox();
    if (!box) throw new Error(`separator ${index} has no box`);
    const x = box.x + box.width / 2;
    const y = box.y + box.height / 2;
    await page.mouse.move(x, y);
    await page.mouse.down();
    await page.mouse.move(x + dx, y + dy, { steps: 12 });
    await page.mouse.up();
}

/**
 * The stored layout once it shows the person's change (`changed`) and has then stayed the same for a second: a change
 * is saved shortly after it is made.
 */
async function settledStoredLayout(page: Page, changed: (layout: LayoutNode) => boolean): Promise<LayoutNode> {
    let previous: string | null = null;
    let raw: string | null = null;
    await expect.poll(async () => {
        previous = raw;
        raw = await stored(page);
        if (raw === null || raw !== previous) return false;
        try {
            return changed(JSON.parse(raw) as LayoutNode);
        } catch {
            return false;
        }
    }, { timeout: 15_000, intervals: [1_000], message: 'the person\'s change is stored' }).toBe(true);
    return JSON.parse(raw as unknown as string) as LayoutNode;
}

const datasetPath = () => `/datasets/${readState().datasetId}`;

async function reloadDataset(page: Page) {
    await page.reload();
    await expect(page.getByTestId('total-samples')).toBeVisible({ timeout: 20_000 });
}

/** Three reloads in a row: each shows `layout` and keeps it stored for a few seconds after it settled. */
async function expectKeptOverReloads(page: Page, layout: LayoutNode, label: string) {
    for (let i = 1; i <= 3; i++) {
        await reloadDataset(page);
        await expectShown(page, layout, `${label}, reload ${i}`);
        await expectStoredStays(page, layout, `${label}, reload ${i}`, i === 3 ? 5_000 : 3_000);
    }
}

test('pin: a browser with nothing stored opens the default layout', async ({ browser, baseURL }) => {
    const context = await browserWith(browser, baseURL);
    try {
        const page = await context.newPage();
        await gotoDataset(page);
        await expectShown(page, DEFAULT, 'nothing stored');
    } finally {
        await context.close();
    }
});

for (const [name, layout] of SEEDED) {
    test(`${name} kept from the last visit opens again, and stays stored, over three reloads`, async ({ browser, baseURL }) => {
        test.setTimeout(120_000);
        const context = await browserWith(browser, baseURL, JSON.stringify(layout));
        try {
            const page = await context.newPage();
            await gotoDataset(page);
            await expectShown(page, layout, 'first open');
            await expectStoredStays(page, layout, 'first open');
            await expectKeptOverReloads(page, layout, 'kept');
        } finally {
            await context.close();
        }
    });
}

test('the layout a person arranges is what the next visits open on, and it stays stored', async ({ browser, baseURL }) => {
    test.setTimeout(120_000);
    const context = await browserWith(browser, baseURL);
    try {
        const page = await context.newPage();
        await gotoDataset(page);
        await expectShown(page, DEFAULT, 'nothing stored');
        expect(test.info().errors, 'the default layout opened').toHaveLength(0);

        // As a person does: close the Embeddings panel, make Simple Filters a Histogram, widen it by dragging the divider.
        await page.getByTestId('panel-embeddings').getByTitle('Close Panel').click();
        await expect(page.getByTestId('panel-embeddings')).toHaveCount(0);
        await page.getByTestId('panel-filters').getByTestId('panel-type-select').selectOption('histogram');
        await expect(page.getByTestId('panel-histogram')).toBeVisible();
        const before = (await shownBoxes(page))[0].w;
        await dragSeparator(page, 0, 180, 0);
        await expect.poll(async () => (await shownBoxes(page))[0].w, { message: 'the drag widened the histogram' }).toBeGreaterThan(before + 0.05);
        // What is stored is what the person sees: a histogram, then the grid, the histogram as wide as it now is.
        const arranged = await settledStoredLayout(page, layout => {
            const boxes = boxesOf(layout);
            return boxes.map(box => box.type).join() === 'histogram,grid' && boxes[0].w > before + 0.05;
        });
        expect(sameBoxes(await shownBoxes(page), boxesOf(arranged)), `the stored ${describeBoxes(boxesOf(arranged))} is what is shown`).toBe(true);

        await expectKeptOverReloads(page, arranged, 'arranged');
    } finally {
        await context.close();
    }
});

test('a kept layout opens again after navigating away and back', async ({ browser, baseURL }) => {
    test.setTimeout(120_000);
    const context = await browserWith(browser, baseURL, JSON.stringify(NESTED));
    try {
        const page = await context.newPage();
        await gotoDataset(page);
        await expectShown(page, NESTED, 'first open');
        await expectStoredStays(page, NESTED, 'first open');

        // Away by the header's Settings link, back with the browser's Back.
        await page.getByRole('link', { name: 'Settings', exact: true }).click();
        await expect(page).toHaveURL(/\/settings$/);
        await page.goBack();
        await expect(page.getByTestId('total-samples')).toBeVisible({ timeout: 20_000 });
        await expectShown(page, NESTED, 'back from Settings by Back');
        await expectStoredStays(page, NESTED, 'back from Settings by Back');

        // Away and back again, twice, as whole page loads.
        for (const time of ['once', 'twice']) {
            await page.goto('/settings');
            await expect(page.getByRole('heading', { name: 'Settings', exact: true })).toBeVisible({ timeout: 20_000 });
            await page.goto(datasetPath());
            await expect(page.getByTestId('total-samples')).toBeVisible({ timeout: 20_000 });
            await expectShown(page, NESTED, `opened again from Settings ${time}`);
            await expectStoredStays(page, NESTED, `opened again from Settings ${time}`, 3_000);
        }
    } finally {
        await context.close();
    }
});

test('a kept layout opens in a new tab of the same browser, and stays stored', async ({ browser, baseURL }) => {
    test.setTimeout(120_000);
    const context = await browserWith(browser, baseURL, JSON.stringify(NESTED));
    try {
        const first = await context.newPage();
        await gotoDataset(first);
        await expectShown(first, NESTED, 'the first tab');
        await expectStoredStays(first, NESTED, 'the first tab');

        // Once the first tab has settled.
        const second = await context.newPage();
        await gotoDataset(second);
        await expectShown(second, NESTED, 'a new tab');
        await expectStoredStays(second, NESTED, 'a new tab');
        await expectShown(first, NESTED, 'the first tab, once the second opened');
        await expectKeptOverReloads(second, NESTED, 'the new tab');
    } finally {
        await context.close();
    }
});

test('a kept layout survives several quick reloads in a row', async ({ browser, baseURL }) => {
    test.setTimeout(120_000);
    const context = await browserWith(browser, baseURL, JSON.stringify(NESTED));
    try {
        const page = await context.newPage();
        await gotoDataset(page);
        await expectShown(page, NESTED, 'first open');
        for (let i = 1; i <= 4; i++) {
            await reloadDataset(page);
            await expectShown(page, NESTED, `reload ${i}`);
            await expectStoredStays(page, NESTED, `reload ${i}`, 1_500);
        }
        await expectStoredStays(page, NESTED, 'after the last reload');
    } finally {
        await context.close();
    }
});

test('the stored layout changes when the person changes the layout, and that change is kept', async ({ browser, baseURL }) => {
    test.setTimeout(120_000);
    const context = await browserWith(browser, baseURL, JSON.stringify(NESTED));
    try {
        const page = await context.newPage();
        await gotoDataset(page);
        await expectShown(page, NESTED, 'first open');
        await expectStoredStays(page, NESTED, 'before the person changes anything');

        // The person narrows the grid by dragging the divider between it and the side panels.
        await dragSeparator(page, 0, -200, 0);
        await expect.poll(async () => (await shownBoxes(page))[0]?.w, { message: 'the drag narrowed the grid' }).toBeLessThan(0.55);
        const changed = await settledStoredLayout(page, layout => {
            const boxes = boxesOf(layout);
            return boxes.map(box => box.type).join() === 'grid,filters,histogram' && boxes[0].w < 0.55;
        });
        expect(sameBoxes(await shownBoxes(page), boxesOf(changed)), `the stored ${describeBoxes(boxesOf(changed))} is what is shown`).toBe(true);

        await expectKeptOverReloads(page, changed, 'changed');
    } finally {
        await context.close();
    }
});

const ERROR_PAGE = /^(Something went wrong|Application error)$/;

const DAMAGED: Array<[string, string]> = [
    ['not JSON', '{"id":"root","type":"group",'],
    ['null', 'null'],
    ['an array', '[1,2,3]'],
    ['a number', '42'],
    ['an object that is no panel', '{"id":"root"}'],
    ['a group without children', '{"id":"root","type":"group","direction":"horizontal"}'],
    ['a group whose children are not a list', '{"id":"root","type":"group","direction":"horizontal","children":"grid"}'],
    [
        'an older flat shape (a list of panels, no group)',
        JSON.stringify([
            { id: 'left', type: 'leaf', data: { panelType: 'filters' }, size: 20 },
            { id: 'center', type: 'leaf', data: { panelType: 'grid' }, size: 80 },
        ]),
    ],
    [
        'a tree naming a panel type the app does not know',
        JSON.stringify({
            id: 'root',
            type: 'group',
            direction: 'horizontal',
            children: [
                { id: 'left', type: 'leaf', data: { panelType: 'telescope' }, size: 40 },
                { id: 'right', type: 'leaf', data: { panelType: 'grid' }, size: 60 },
            ],
        }),
    ],
];

for (const [name, raw] of DAMAGED) {
    test(`a stored layout that is ${name} opens the default layout without an error`, async ({ browser, baseURL }) => {
        const context = await browserWith(browser, baseURL, raw);
        try {
            const page = await context.newPage();
            const uncaught: string[] = [];
            page.on('pageerror', error => uncaught.push(error.message));
            await page.goto(datasetPath());
            // The page opens, or shows its error page.
            await expect(page.getByTestId('total-samples').or(page.getByText(ERROR_PAGE))).toBeVisible({ timeout: 20_000 });
            await expect.soft(page.getByText(ERROR_PAGE), 'no error page').toHaveCount(0, { timeout: 1_000 });
            await expectShown(page, DEFAULT, `stored ${name}`, 5_000);
            expect.soft(uncaught, 'no uncaught exception').toEqual([]);
        } finally {
            await context.close();
        }
    });
}
