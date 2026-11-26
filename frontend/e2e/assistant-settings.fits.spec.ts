import { test, expect, type Locator, type Page, type Route } from '@playwright/test';
import { API, adminLogin, apiFetch, deleteDatasetAndWait } from './api';
import { AUTH_STATE } from './helpers';
import { type Level, type TestUser, allSteps, createGroup, createSmallDataset, createUser, deleteGroup, openDataset, openSettings, share, signIn, stamp } from './access-helpers';

// The assistant's settings (docs/ASSISTANT.md, the UI contract of PR #497). An assistant reads a server's datasets as
// the person using it, only when an administrator allows it: Settings → Assistant (the MCP server switch, who may use
// it, whether new datasets start closed), a dataset's own switch in Dataset Settings (manage level), and each field's
// "Keep this field's values from the assistant" (edit level). Every change is sent at once; a control shows its new
// state once the server has answered.
// A guest can't use the MCP server (the contract "guests and the MCP server; a way from the Assistant page to a token",
// section 2): Roles has no Guests tick, a guest's row reads No and their own answer can't be given, whatever the
// settings store (roles.guest, a guest's own answer and a group with a guest in it are kept for the chat in the app, so
// these tests store them through the API), and while the MCP server is on its section links to making an MCP token.
// Read here: "the row's own-answer control" is the row's "Assistant for <name>" select, and its accessible description
// is exactly "Guests can't use the MCP server".
// The settings are the server's own, so this spec runs in the one-at-a-time project after every other spec
// (playwright.config.ts: *.fits.spec.ts), and puts them back as it found them.
test.use({ storageState: AUTH_STATE });

type Settings = {
    mcp_enabled: boolean;
    roles: { admin: boolean; power_user: boolean; guest: boolean };
    groups: Array<{ id: number; name: string }>;
    users: Array<{ id: number; username: string; allowed: boolean }>;
    new_datasets_closed: boolean;
};

const S = stamp();
const PREFIX = `as${S}_`;
let cookie = '';
let found: Settings;
const people: TestUser[] = [];
const groupIds: number[] = [];
const datasetIds: number[] = [];

const MCP_ON = 'An assistant such as Claude can connect to this server, as one of the people below, and read the datasets that person can open.';
const MCP_OFF = 'No assistant can connect to this server. Switch it on to let the people below use one.';
const OFF_NOTE = 'While the MCP server is off, nobody can use it, whatever this column says.';
const NEVER = 'A guest or a demo account never may.';
const GUEST_ROW = 'Guests can\'t use the MCP server';
const TO_A_TOKEN = 'To connect an AI client, create an MCP token. Each person who may use it makes their own, under Settings → API Tokens.';
const DATASETS_NOTE = 'A dataset\'s managers open or close it to the assistant in its Dataset Settings, and anyone who can edit it can keep a field\'s values from the assistant there. Datasets that exist already are not changed by this setting.';
const OPEN = 'Open: when an administrator lets them use the assistant (Settings → Assistant), it reads this dataset as the person using it.';
const CLOSED = 'Closed: the assistant leaves this dataset out for everyone, administrators too. People open it here as before.';
const NEEDS_MANAGE = 'This needs manage access to the dataset';
const NEEDS_EDIT = 'This needs edit access to the dataset';
const VIEW_ONLY = 'View only: changing these settings needs edit access to the dataset';
const KEEP = 'Keep this field\'s values from the assistant';
const FIELDS_INTRO = 'A field whose values are kept from the assistant still shows them here; the assistant learns what the field means and how many samples have it, never a value.';

test.beforeAll(async () => {
    cookie = await adminLogin();
    found = await apiFetch(cookie, '/admin/assistant');
});

test.afterAll(async () => {
    test.setTimeout(240_000);
    await allSteps([
        () => apiFetch(cookie, '/admin/assistant', {
            method: 'PATCH',
            body: JSON.stringify({ mcp_enabled: found.mcp_enabled, roles: found.roles, new_datasets_closed: found.new_datasets_closed }),
        }),
        ...datasetIds.map(id => () => deleteDatasetAndWait(cookie, id, 120_000)),
        ...groupIds.map(id => () => deleteGroup(cookie, id)),
        ...people.map(user => () => apiFetch(cookie, `/users/${user.id}`, { method: 'DELETE' })),
    ]);
});

async function person(name: string, role: 'power_user' | 'guest' = 'power_user') {
    const user = await createUser(cookie, `${PREFIX}${name}`, role);
    people.push(user);
    return user;
}

async function dataset(name: string) {
    const id = await createSmallDataset(cookie, `${PREFIX}${name}`);
    datasetIds.push(id);
    return id;
}

const settings = (): Promise<Settings> => apiFetch(cookie, '/admin/assistant');
const patchSettings = (changes: Partial<Omit<Settings, 'roles'>> & { roles?: Partial<Settings['roles']> }) => apiFetch(cookie, '/admin/assistant', { method: 'PATCH', body: JSON.stringify(changes) });

/** The changes the page sends (PATCH, PUT, DELETE) to a path starting with `prefix`: method, path and body. */
function writesTo(page: Page, prefix: string) {
    const sent: Array<[string, string, unknown]> = [];
    page.on('request', request => {
        const path = new URL(request.url()).pathname;
        if (['PATCH', 'PUT', 'DELETE'].includes(request.method()) && path.startsWith(prefix)) sent.push([request.method(), path, request.postDataJSON() ?? null]);
    });
    return sent;
}

/** A session's cookie for `user` (a fresh sign-in). */
async function sessionOf(user: TestUser): Promise<string> {
    const login = await fetch(`${API}/auth/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: user.username, password: user.password }),
    });
    expect(login.ok, `sign in as ${user.username}`).toBe(true);
    return `session_id=${(login.headers.get('set-cookie') || '').match(/session_id=([^;]+)/)?.[1]}`;
}

/** What GET /api/auth/me says to that person, signed in: whether they could use the MCP server now. */
const mcpFor = async (session: string): Promise<boolean> => (await apiFetch(session, '/auth/me')).assistant?.mcp;

async function openAssistant(page: Page) {
    await page.goto('/settings/assistant');
    await expect(page.getByRole('heading', { name: 'Assistant', level: 1 })).toBeVisible();
    await expect(page.getByRole('switch', { name: 'MCP server', exact: true })).toBeVisible();
}

const mcpSwitch = (page: Page) => page.getByRole('switch', { name: 'MCP server', exact: true });
const rolesGroup = (page: Page) => page.getByRole('group', { name: 'Roles', exact: true });
const roleBox = (page: Page, name: 'Administrators' | 'Power users') => rolesGroup(page).getByRole('checkbox', { name, exact: true });
const groupsBox = (page: Page) => page.getByRole('group', { name: 'Groups whose members may use it', exact: true });
const newClosedBox = (page: Page) => page.getByRole('region', { name: 'Datasets', exact: true }).getByRole('checkbox', { name: 'New datasets start closed to the assistant', exact: true });
const peopleTable = (page: Page) => page.getByRole('table', { name: 'People and the assistant', exact: true });
const answerOf = (page: Page, username: string) => page.getByRole('combobox', { name: `Assistant for ${username}`, exact: true });
const rowOf = (page: Page, username: string) => peopleTable(page).getByRole('row').filter({ has: page.getByText(username, { exact: true }) });
/** The row's "May use it" cell: the last one. */
const mayUse = (page: Page, username: string) => rowOf(page, username).getByRole('cell').last();

/** The answer `label` is the one selected. */
const expectAnswer = (page: Page, username: string, label: string) => expect(answerOf(page, username).getByRole('option', { name: label, exact: true })).toHaveJSProperty('selected', true);

test('Settings shows the Assistant card to administrators, whose page it opens; anyone else reaches neither the page\'s controls nor its API', async ({ page, browser }) => {
    await page.goto('/settings');
    const card = page.getByTestId('settings-assistant-card');
    await expect(card.getByRole('heading', { name: 'Assistant' })).toBeVisible();
    await card.click();
    await expect(page).toHaveURL(/\/settings\/assistant$/);
    await expect(page.getByRole('heading', { name: 'Assistant', level: 1 })).toBeVisible();
    await page.getByRole('link', { name: 'Back to Settings' }).click();
    await expect(page).toHaveURL(/\/settings$/);

    const before = await settings();
    for (const role of ['power_user', 'guest'] as const) {
        const user = await person(`reach_${role}`, role);
        const { context, page: theirs } = await signIn(browser, user);
        try {
            await theirs.goto('/settings');
            await expect(theirs.getByRole('heading').first()).toBeVisible();
            await expect(theirs.getByTestId('settings-assistant-card'), role).toHaveCount(0);
            await theirs.goto('/settings/assistant');
            await expect(theirs.getByRole('heading', { name: 'Assistant', level: 1 })).toBeVisible();
            await expect(theirs.getByText('Only administrators can change the assistant\'s settings.', { exact: true })).toBeVisible();
            await expect(theirs.getByRole('switch')).toHaveCount(0);
            await expect(theirs.getByRole('checkbox')).toHaveCount(0);
            await expect(theirs.getByRole('table')).toHaveCount(0);
            const read = await theirs.request.get('/api/admin/assistant');
            expect(read.status(), `${role}: GET`).toBe(403);
            expect(await read.text(), `${role}: learns nothing`).not.toContain('mcp_enabled');
            const write = await theirs.request.patch('/api/admin/assistant', { data: { mcp_enabled: true, new_datasets_closed: true } });
            expect(write.status(), `${role}: PATCH`).toBe(403);
            expect((await theirs.request.put(`/api/admin/assistant/users/${user.id}`, { data: { allowed: true } })).status(), `${role}: PUT a person`).toBe(403);
        } finally {
            await context.close();
        }
    }
    expect(await settings(), 'nothing changed').toEqual(before);

    // Signed out: the login page, and the API tells nothing.
    const context = await browser.newContext({ storageState: { cookies: [], origins: [] } });
    try {
        const visitor = await context.newPage();
        await visitor.goto('/settings/assistant');
        await expect(visitor).toHaveURL(/\/login/);
        const read = await visitor.request.get('/api/admin/assistant');
        expect(read.ok()).toBe(false);
        expect(await read.text()).not.toContain('mcp_enabled');
    } finally {
        await context.close();
    }
});

test('the MCP server switch: off as shipped; a click shows the new state once the server answers, the screen held meanwhile; it sends only that and survives a reload', async ({ page }) => {
    const sent = writesTo(page, '/api/admin/assistant');
    await openAssistant(page);
    const region = page.getByRole('region', { name: 'MCP server', exact: true });
    const mcp = mcpSwitch(page);
    await expect(mcp).toHaveAttribute('aria-checked', 'false');
    await expect(region.getByText('Off', { exact: true })).toBeVisible();
    await expect(mcp).toHaveAccessibleDescription(MCP_OFF);
    await expect(page.getByText(OFF_NOTE, { exact: true })).toBeVisible();

    // The server's answer held: the switch keeps its state and the screen's controls wait.
    let release!: () => void;
    const answered = new Promise<void>(resolve => { release = resolve; });
    await page.route('**/api/admin/assistant', async (route: Route) => {
        if (route.request().method() !== 'PATCH') return route.fallback();
        await answered;
        return route.fallback();
    });
    await mcp.click();
    await expect(mcp).toHaveAttribute('aria-checked', 'false');
    for (const control of [mcp, roleBox(page, 'Power users'), newClosedBox(page)]) await expect(control).toBeDisabled();
    release();
    await expect(mcp).toHaveAttribute('aria-checked', 'true');
    await page.unroute('**/api/admin/assistant');
    await expect(region.getByText('On', { exact: true })).toBeVisible();
    await expect(mcp).toHaveAccessibleDescription(MCP_ON);
    await expect(page.getByText(OFF_NOTE, { exact: true })).toHaveCount(0);
    await expect(roleBox(page, 'Power users')).toBeEnabled();
    expect(sent).toEqual([['PATCH', '/api/admin/assistant', { mcp_enabled: true }]]);

    await page.reload();
    await expect(mcpSwitch(page)).toHaveAttribute('aria-checked', 'true');
    expect((await settings()).mcp_enabled).toBe(true);

    await mcpSwitch(page).click();
    await expect(mcpSwitch(page)).toHaveAttribute('aria-checked', 'false');
    await expect(page.getByText(OFF_NOTE, { exact: true })).toBeVisible();
    expect(sent.at(-1)).toEqual(['PATCH', '/api/admin/assistant', { mcp_enabled: false }]);
});

test('the MCP server on: under its note, a sentence whose "create an MCP token" links to the tokens page with the MCP kind; shown as soon as it is switched on, gone while it is off', async ({ page }) => {
    await patchSettings({ mcp_enabled: false });
    await openAssistant(page);
    const region = page.getByRole('region', { name: 'MCP server', exact: true });
    const link = region.getByRole('link', { name: 'create an MCP token', exact: true });
    const anyLink = page.getByRole('link', { name: 'create an MCP token' });
    try {
        // Off (as shipped): neither the sentence nor the link.
        await expect(mcpSwitch(page)).toHaveAttribute('aria-checked', 'false');
        await expect(region).toContainText(MCP_OFF);
        await expect(region).not.toContainText(TO_A_TOKEN);
        await expect(anyLink).toHaveCount(0);

        // Switched on: both, without a reload, under the note.
        await mcpSwitch(page).click();
        await expect(mcpSwitch(page)).toHaveAttribute('aria-checked', 'true');
        await expect(region).toContainText(TO_A_TOKEN);
        await expect(link).toHaveAttribute('href', '/settings/tokens?kind=mcp');
        const text = (await region.innerText()).replace(/\s+/g, ' ');
        expect(text.indexOf(MCP_ON), 'the note').toBeGreaterThanOrEqual(0);
        expect(text.indexOf(MCP_ON), 'the sentence under the note').toBeLessThan(text.indexOf('To connect an AI client'));

        // Switched off: gone, without a reload.
        await mcpSwitch(page).click();
        await expect(mcpSwitch(page)).toHaveAttribute('aria-checked', 'false');
        await expect(region).not.toContainText(TO_A_TOKEN);
        await expect(anyLink).toHaveCount(0);

        // On again, and after a reload; the link opens the tokens page with the MCP kind asked for.
        await mcpSwitch(page).click();
        await expect(mcpSwitch(page)).toHaveAttribute('aria-checked', 'true');
        await page.reload();
        await expect(region).toContainText(TO_A_TOKEN);
        await link.click();
        await expect(page).toHaveURL(/\/settings\/tokens\?kind=mcp$/);
        await expect(page.getByRole('heading', { name: 'API Tokens', level: 1 })).toBeVisible();
    } finally {
        await patchSettings({ mcp_enabled: false });
    }
});

test('Roles: two ticks, Administrators and Power users, and no Guests tick, whatever roles.guest stores; administrators only as shipped; each sends only its role, and survives a reload', async ({ page }) => {
    const sent = writesTo(page, '/api/admin/assistant');
    try {
        // roles.guest stored true (kept for the chat in the app): still no tick for it, and nothing here changes it.
        await patchSettings({ roles: { ...found.roles, guest: true } });
        await openAssistant(page);
        await expect(rolesGroup(page).getByRole('checkbox')).toHaveCount(2);
        for (const [index, name] of ['Administrators', 'Power users'].entries()) {
            await expect(rolesGroup(page).getByRole('checkbox').nth(index)).toHaveAccessibleName(name);
        }
        await expect(page.getByRole('checkbox', { name: 'Guests' })).toHaveCount(0);
        await expect(rolesGroup(page)).not.toContainText('Guests');
        await expect(roleBox(page, 'Administrators')).toBeChecked();
        await expect(roleBox(page, 'Power users')).not.toBeChecked();
        const keys = { Administrators: 'admin', 'Power users': 'power_user' } as const;
        for (const [name, key] of Object.entries(keys) as Array<[keyof typeof keys, string]>) {
            const was = await roleBox(page, name).isChecked();
            await roleBox(page, name).click();
            await expect(roleBox(page, name)).toBeChecked({ checked: !was });
            expect(sent.at(-1), name).toEqual(['PATCH', '/api/admin/assistant', { roles: { [key]: !was } }]);
            await page.reload();
            await expect(roleBox(page, name)).toBeChecked({ checked: !was });
            expect((await settings()).roles[key as keyof Settings['roles']]).toBe(!was);
            await roleBox(page, name).click();
            await expect(roleBox(page, name)).toBeChecked({ checked: was });
            expect(sent.at(-1), name).toEqual(['PATCH', '/api/admin/assistant', { roles: { [key]: was } }]);
        }
        expect(sent).toHaveLength(4);
        expect((await settings()).roles.guest, 'roles.guest as stored').toBe(true);
    } finally {
        await patchSettings({ roles: found.roles });
    }
});

test('Who may use it says that a guest or a demo account never may', async ({ page }) => {
    await openAssistant(page);
    const who = page.getByRole('region', { name: 'Who may use it', exact: true });
    await expect(who).toContainText(NEVER);
    await expect(who).not.toContainText('A demo account never may.');
});

test('Groups: one checkbox per group, by name; a click lets its members in or takes it back; with none, the page says where to make them', async ({ page }) => {
    const b = await createGroup(cookie, `${PREFIX}grp_b`);
    const a = await createGroup(cookie, `${PREFIX}grp_a`);
    groupIds.push(a.id, b.id);
    const sent = writesTo(page, '/api/admin/assistant');
    await openAssistant(page);
    await expect(groupsBox(page).getByRole('checkbox', { name: b.name, exact: true })).toBeVisible();
    const listed = await groupsBox(page).innerText();
    expect(listed.indexOf(a.name), 'by name').toBeGreaterThanOrEqual(0);
    expect(listed.indexOf(a.name), 'by name').toBeLessThan(listed.indexOf(b.name));
    const box = groupsBox(page).getByRole('checkbox', { name: a.name, exact: true });
    await expect(box).not.toBeChecked();
    await box.click();
    await expect(box).toBeChecked();
    expect(sent.at(-1)?.slice(0, 2)).toEqual(['PUT', `/api/admin/assistant/groups/${a.id}`]);
    await page.reload();
    await expect(groupsBox(page).getByRole('checkbox', { name: a.name, exact: true })).toBeChecked();
    expect((await settings()).groups.map(group => group.id)).toContain(a.id);
    await groupsBox(page).getByRole('checkbox', { name: a.name, exact: true }).click();
    await expect(groupsBox(page).getByRole('checkbox', { name: a.name, exact: true })).not.toBeChecked();
    expect(sent.at(-1)?.slice(0, 2)).toEqual(['DELETE', `/api/admin/assistant/groups/${a.id}`]);
    expect((await settings()).groups.map(group => group.id)).not.toContain(a.id);

    // A server with no groups (answer faked).
    const real = await settings();
    await page.route('**/api/admin/assistant', (route: Route) => (route.request().method() === 'GET' ? route.fulfill({ json: { ...real, groups: [] } }) : route.fallback()));
    await page.route('**/api/groups*', (route: Route) => (route.request().method() === 'GET' ? route.fulfill({ json: [] }) : route.fallback()));
    await page.reload();
    await expect(page.getByText('No groups yet: make them under User Management.', { exact: true })).toBeVisible();
    await expect(page.getByRole('link', { name: 'User Management', exact: true })).toHaveAttribute('href', '/users');
});

test('People: active people by name, followers and demo guests left out; Find a person filters; "May use it" follows their own answer, then groups, then the role, as each one\'s auth/me says', async ({ page }) => {
    test.setTimeout(240_000);
    // Names of their own, so a filter on PP finds this test's people and nobody else (a follower's name allows only
    // lower-case letters, digits and hyphens).
    const PP = `pp${S}`;
    const make = async (name: string, role: 'power_user' | 'guest' = 'power_user') => {
        const user = await createUser(cookie, `${PP}_${name}`, role);
        people.push(user);
        return user;
    };
    const amy = await make('amy');
    const gil = await make('gil', 'guest');
    const pat = await make('pat');
    const group = await createGroup(cookie, `${PP}_crew`, [pat]);
    groupIds.push(group.id);
    const follower = await apiFetch(cookie, '/followers', { method: 'POST', body: JSON.stringify({ name: `${PP}-fol` }) });
    const demo = await fetch(`${API}/auth/demo-login`, { method: 'POST' });
    const demoSession = `session_id=${(demo.headers.get('set-cookie') || '').match(/session_id=([^;]+)/)?.[1]}`;
    const demoName: string = (await apiFetch(demoSession, '/auth/me')).username;
    const sessions = { amy: await sessionOf(amy), gil: await sessionOf(gil), pat: await sessionOf(pat) };
    try {
        await patchSettings({ mcp_enabled: true });
        const sent = writesTo(page, '/api/admin/assistant');
        await openAssistant(page);
        const table = peopleTable(page);
        await expect(table.getByRole('columnheader')).toHaveText(['Person', 'Role', 'Their own answer', 'May use it']);
        const find = page.getByRole('textbox', { name: 'Find a person', exact: true });

        // Only this test's people, found in any letter case, by name order; not the follower's account.
        await find.fill(PP.toUpperCase());
        await expect(table.getByRole('row').filter({ has: page.getByRole('cell') })).toHaveCount(3);
        const rows = await table.getByRole('row').allInnerTexts();
        const at = [amy, gil, pat].map(user => rows.findIndex(row => row.includes(user.username)));
        expect(at.every(index => index >= 0) && at[0] < at[1] && at[1] < at[2], `by username: ${at}`).toBe(true);
        await expect(table.getByText(`follower:${PP}-fol`)).toHaveCount(0);
        await expect(rowOf(page, gil.username)).toContainText('guest');
        await expect(rowOf(page, pat.username)).toContainText('power user');
        for (const user of [amy, gil, pat]) {
            await expect(answerOf(page, user.username).getByRole('option')).toHaveText(['Role and groups decide', 'May use it', 'May not use it']);
            await expectAnswer(page, user.username, 'Role and groups decide');
        }
        await find.fill('zz-nobody-here');
        await expect(table).toContainText('No person matches "zz-nobody-here".');
        await find.fill(demoName);
        await expect(table).toContainText(`No person matches "${demoName}".`);
        await find.fill(`follower:${PP}`);
        await expect(table).toContainText(`No person matches "follower:${PP}".`);
        await find.fill(PP);

        /** The row says `shown`, and that person's auth/me agrees. */
        async function check(user: TestUser, session: string, shown: 'Yes' | 'No', step: string) {
            await expect(mayUse(page, user.username), `${step}: ${user.username}'s row`).toHaveText(shown);
            await expect.poll(() => mcpFor(session), { message: `${step}: ${user.username}'s auth/me`, timeout: 15_000 }).toBe(shown === 'Yes');
        }
        await check(pat, sessions.pat, 'No', 'a power user, in no ticked group');
        await check(amy, sessions.amy, 'No', 'a power user, in no ticked group');
        await check(gil, sessions.gil, 'No', 'a guest');

        // A ticked group lets its members in.
        await groupsBox(page).getByRole('checkbox', { name: group.name, exact: true }).click();
        await expect(groupsBox(page).getByRole('checkbox', { name: group.name, exact: true })).toBeChecked();
        await check(pat, sessions.pat, 'Yes', 'in a ticked group');
        await check(amy, sessions.amy, 'No', 'not in it');

        // Their own answer wins over the group: May not use it.
        await answerOf(page, pat.username).selectOption({ label: 'May not use it' });
        await expectAnswer(page, pat.username, 'May not use it');
        expect(sent.at(-1)).toEqual(['PUT', `/api/admin/assistant/users/${pat.id}`, { allowed: false }]);
        await check(pat, sessions.pat, 'No', 'their own "May not use it" over the group');

        // And over role and groups the other way: May use it, the group unticked.
        await groupsBox(page).getByRole('checkbox', { name: group.name, exact: true }).click();
        await expect(groupsBox(page).getByRole('checkbox', { name: group.name, exact: true })).not.toBeChecked();
        await answerOf(page, pat.username).selectOption({ label: 'May use it' });
        await expectAnswer(page, pat.username, 'May use it');
        expect(sent.at(-1)).toEqual(['PUT', `/api/admin/assistant/users/${pat.id}`, { allowed: true }]);
        await check(pat, sessions.pat, 'Yes', 'their own "May use it"');
        await page.reload();
        await expectAnswer(page, pat.username, 'May use it');
        expect((await settings()).users.find(user => user.id === pat.id)?.allowed).toBe(true);

        // Back to role and groups: a power user, no group ticked.
        await answerOf(page, pat.username).selectOption({ label: 'Role and groups decide' });
        await expectAnswer(page, pat.username, 'Role and groups decide');
        expect(sent.at(-1)?.slice(0, 2)).toEqual(['DELETE', `/api/admin/assistant/users/${pat.id}`]);
        await check(pat, sessions.pat, 'No', 'role and groups decide');
        expect((await settings()).users.find(user => user.id === pat.id)).toBeUndefined();

        // The role decides when nothing else does; a guest never may, roles.guest stored true (through the API: the
        // page has no Guests tick), nor a demo guest.
        await roleBox(page, 'Power users').click();
        await expect(roleBox(page, 'Power users')).toBeChecked();
        await check(pat, sessions.pat, 'Yes', 'power users ticked');
        await check(amy, sessions.amy, 'Yes', 'power users ticked');
        await check(gil, sessions.gil, 'No', 'power users ticked');
        await patchSettings({ roles: { guest: true } });
        await page.reload();
        await find.fill(PP);
        await check(gil, sessions.gil, 'No', 'roles.guest stored true');
        await expect.poll(() => mcpFor(demoSession), { message: 'a demo guest, roles.guest stored true' }).toBe(false);
        await patchSettings({ roles: { guest: false } });
        await page.reload();
        await find.fill(PP);
        await roleBox(page, 'Power users').click();
        await expect(roleBox(page, 'Power users')).not.toBeChecked();
        await check(gil, sessions.gil, 'No', 'power users unticked');
        await check(amy, sessions.amy, 'No', 'power users unticked');

        // With the MCP server off, the column still says what the rule says, the note says it doesn't count, and
        // nobody may use it.
        await answerOf(page, amy.username).selectOption({ label: 'May use it' });
        await expectAnswer(page, amy.username, 'May use it');
        await mcpSwitch(page).click();
        await expect(mcpSwitch(page)).toHaveAttribute('aria-checked', 'false');
        await expect(mayUse(page, amy.username)).toHaveText('Yes');
        await expect(page.getByText(OFF_NOTE, { exact: true })).toBeVisible();
        await expect.poll(() => mcpFor(sessions.amy), { message: 'the server off', timeout: 15_000 }).toBe(false);
        await answerOf(page, amy.username).selectOption({ label: 'Role and groups decide' });
        await expectAnswer(page, amy.username, 'Role and groups decide');
    } finally {
        await patchSettings({ mcp_enabled: false, roles: found.roles });
        await apiFetch(cookie, `/followers/${follower.id}`, { method: 'DELETE' });
    }
});

test('People: a guest\'s row reads No and their own answer can\'t be given, saying why, whatever the settings store for them (roles.guest, a ticked group, their own "May use it"); everyone else\'s row is as before', async ({ page }) => {
    test.setTimeout(240_000);
    const GP = `gp${S}`;
    const make = async (name: string, role: 'power_user' | 'guest' = 'power_user') => {
        const user = await createUser(cookie, `${GP}_${name}`, role);
        people.push(user);
        return user;
    };
    const gail = await make('gail', 'guest');  // roles.guest alone
    const gus = await make('gus', 'guest');  // and their own answer, allowed: true
    const gwen = await make('gwen', 'guest');  // and a ticked group
    const pia = await make('pia');  // a power user with their own answer, allowed: true
    const group = await createGroup(cookie, `${GP}_crew`, [gwen]);
    groupIds.push(group.id);
    const guests: Array<[TestUser, string]> = [[gail, await sessionOf(gail)], [gus, await sessionOf(gus)], [gwen, await sessionOf(gwen)]];
    const piaSession = await sessionOf(pia);
    try {
        await patchSettings({ mcp_enabled: true, roles: { guest: true } });
        await apiFetch(cookie, `/admin/assistant/users/${gus.id}`, { method: 'PUT', body: JSON.stringify({ allowed: true }) });
        await apiFetch(cookie, `/admin/assistant/users/${pia.id}`, { method: 'PUT', body: JSON.stringify({ allowed: true }) });
        await apiFetch(cookie, `/admin/assistant/groups/${group.id}`, { method: 'PUT' });
        // Stored as sent: the settings keep their shape.
        const stored = await settings();
        expect(stored.roles.guest).toBe(true);
        expect(stored.users.find(user => user.id === gus.id)?.allowed).toBe(true);
        expect(stored.groups.map(each => each.id)).toContain(group.id);
        // The server is on as auth/me reads it (it reads the switch every few seconds): the power user may.
        await expect.poll(() => mcpFor(piaSession), { message: 'a power user, their own "May use it"', timeout: 15_000 }).toBe(true);

        const sent = writesTo(page, '/api/admin/assistant');
        for (const pass of ['opened', 'reloaded']) {
            if (pass === 'opened') await openAssistant(page);
            else await page.reload();
            await page.getByRole('textbox', { name: 'Find a person', exact: true }).fill(GP);
            await expect(peopleTable(page).getByRole('row').filter({ has: page.getByRole('cell') }), pass).toHaveCount(4);
            for (const [guest, session] of guests) {
                const step = `${pass}: ${guest.username}`;
                await expect(rowOf(page, guest.username), step).toContainText('guest');
                await expect(mayUse(page, guest.username), step).toHaveText('No');
                await expect(answerOf(page, guest.username), step).toBeDisabled();
                await expect(answerOf(page, guest.username), step).toHaveAccessibleDescription(GUEST_ROW);
                expect(await mcpFor(session), `${step}: auth/me`).toBe(false);
            }
            // Everyone else's row as before: a power user's own answer can be given, and the column follows it.
            await expect(mayUse(page, pia.username), pass).toHaveText('Yes');
            await expect(answerOf(page, pia.username), pass).toBeEnabled();
            await expectAnswer(page, pia.username, 'May use it');
            await expect(answerOf(page, pia.username), pass).not.toHaveAccessibleDescription(GUEST_ROW);
        }
        await answerOf(page, pia.username).selectOption({ label: 'May not use it' });
        await expectAnswer(page, pia.username, 'May not use it');
        await expect(mayUse(page, pia.username)).toHaveText('No');
        await expect.poll(() => mcpFor(piaSession), { message: 'a power user, their own "May not use it"', timeout: 15_000 }).toBe(false);
        expect(sent, 'only the power user\'s answer was sent').toEqual([['PUT', `/api/admin/assistant/users/${pia.id}`, { allowed: false }]]);
        // The guests' stored entries are still there, as stored.
        const after = await settings();
        expect(after.roles.guest).toBe(true);
        expect(after.users.find(user => user.id === gus.id)?.allowed).toBe(true);
        expect(after.groups.map(each => each.id)).toContain(group.id);
    } finally {
        await allSteps([
            () => patchSettings({ mcp_enabled: false, roles: found.roles }),
            () => apiFetch(cookie, `/admin/assistant/groups/${group.id}`, { method: 'DELETE' }),
        ]);
    }
});

test('New datasets start closed: off as shipped; ticked, a dataset created afterwards starts closed and the ones before stay open; it sends only that', async ({ page, browser }) => {
    const before = await dataset('before');
    const sent = writesTo(page, '/api/admin/assistant');
    await openAssistant(page);
    const box = newClosedBox(page);
    await expect(box).not.toBeChecked();
    await expect(page.getByRole('region', { name: 'Datasets', exact: true })).toContainText(DATASETS_NOTE);
    try {
        await box.click();
        await expect(box).toBeChecked();
        expect(sent).toEqual([['PATCH', '/api/admin/assistant', { new_datasets_closed: true }]]);
        await page.reload();
        await expect(newClosedBox(page)).toBeChecked();

        const made = await apiFetch(cookie, '/datasets', { method: 'POST', body: JSON.stringify({ name: `${PREFIX}after` }) });
        datasetIds.push(made.id);
        expect(made.assistant_closed, 'the answer to the new dataset').toBe(true);
        expect((await apiFetch(cookie, `/datasets/${made.id}`)).assistant_closed).toBe(true);
        expect((await apiFetch(cookie, `/datasets/${before}`)).assistant_closed, 'a dataset made before').toBe(false);
        const listed = (await apiFetch(cookie, '/datasets')) as Array<{ id: number; assistant_closed: boolean }>;
        expect(listed.filter(row => [made.id, before].includes(row.id)).map(row => [row.id, row.assistant_closed]).sort())
            .toEqual([[before, false], [made.id, true]].sort());

        // The new dataset's own switch says so.
        const { context, page: admin } = await signIn(browser);
        try {
            await openDataset(admin, made.id);
            const dialog = await openSettings(admin);
            await expect(dialog.getByRole('region', { name: 'Access' }).getByRole('switch', { name: 'The assistant may read this dataset', exact: true })).toHaveAttribute('aria-checked', 'false');
        } finally {
            await context.close();
        }
    } finally {
        if (await newClosedBox(page).isChecked()) await newClosedBox(page).click();
        await expect(newClosedBox(page)).not.toBeChecked();
    }
    expect(sent.at(-1)).toEqual(['PATCH', '/api/admin/assistant', { new_datasets_closed: false }]);
    const later = await apiFetch(cookie, '/datasets', { method: 'POST', body: JSON.stringify({ name: `${PREFIX}later` }) });
    datasetIds.push(later.id);
    expect(later.assistant_closed, 'made after it was unticked').toBe(false);
});

test('a refused change shows the server\'s words at the top and the next change clears them; a settings read that fails shows its message instead of the sections (answers faked)', async ({ page }) => {
    await openAssistant(page);
    await page.route('**/api/admin/assistant', (route: Route) => (route.request().method() === 'PATCH'
        ? route.fulfill({ status: 409, json: { detail: 'e2e: this change is refused' } })
        : route.fallback()));
    await mcpSwitch(page).click();
    await expect(page.getByRole('alert').filter({ hasText: 'e2e: this change is refused' })).toHaveText('e2e: this change is refused');
    await expect(mcpSwitch(page)).toHaveAttribute('aria-checked', 'false');
    await page.unroute('**/api/admin/assistant');
    await roleBox(page, 'Power users').click();
    await expect(roleBox(page, 'Power users')).toBeChecked();
    await expect(page.getByRole('alert').filter({ hasText: 'e2e:' })).toHaveCount(0);
    await roleBox(page, 'Power users').click();
    await expect(roleBox(page, 'Power users')).not.toBeChecked();

    await page.route('**/api/admin/assistant', (route: Route) => (route.request().method() === 'GET'
        ? route.fulfill({ status: 500, json: { detail: 'e2e: the settings could not be read' } })
        : route.fallback()));
    await page.reload();
    await expect(page.getByRole('alert').filter({ hasText: 'e2e: the settings could not be read' })).toBeVisible();
    await expect(mcpSwitch(page)).toHaveCount(0);
    await expect(page.getByRole('table')).toHaveCount(0);
});

/** The assistant switch in Dataset Settings → Access, on `page` (signed in as whoever). */
async function datasetSwitch(page: Page, id: number): Promise<{ dialog: Locator; toggle: Locator; access: Locator }> {
    await openDataset(page, id);
    const dialog = await openSettings(page);
    const access = dialog.getByRole('region', { name: 'Access' });
    return { dialog, access, toggle: access.getByRole('switch', { name: 'The assistant may read this dataset', exact: true }) };
}

test('Dataset Settings → Access: a manager closes and opens the dataset to the assistant at once, sending only that; below manage the switch shows the state, disabled, and says why', async ({ page, browser }) => {
    test.setTimeout(240_000);
    const id = await dataset('access');
    const below: Array<[Level, TestUser]> = [];
    for (const level of ['view', 'tag', 'edit'] as const) {
        const user = await person(`ds_${level}`);
        await share(cookie, id, { user_id: user.id }, level);
        below.push([level, user]);
    }
    const manager = await person('ds_manage');
    await share(cookie, id, { user_id: manager.id }, 'manage');

    // An administrator: open, closed with one click, and so after a reload.
    const sent = writesTo(page, `/api/datasets/${id}`);
    let { dialog, toggle } = await datasetSwitch(page, id);
    await expect(toggle).toHaveAttribute('aria-checked', 'true');
    await expect(toggle).toBeEnabled();
    await expect(toggle).toHaveAccessibleDescription(OPEN);
    await toggle.click();
    await expect(toggle).toHaveAttribute('aria-checked', 'false');
    await expect(toggle).toHaveAccessibleDescription(CLOSED);
    await expect(dialog).toBeVisible();
    expect(sent).toEqual([['PATCH', `/api/datasets/${id}`, { assistant_closed: true }]]);
    expect((await apiFetch(cookie, `/datasets/${id}`)).assistant_closed).toBe(true);
    await page.reload();
    ({ dialog, toggle } = await datasetSwitch(page, id));
    await expect(toggle).toHaveAttribute('aria-checked', 'false');

    // Below manage: the state shown, disabled, with the reason; then the other state.
    for (const [level, user] of below) {
        const { context, page: theirs } = await signIn(browser, user);
        try {
            const mine = await datasetSwitch(theirs, id);
            await expect(mine.toggle, level).toHaveAttribute('aria-checked', 'false');
            await expect(mine.toggle, level).toBeDisabled();
            await expect(mine.toggle, level).toHaveAccessibleDescription(NEEDS_MANAGE);
            await apiFetch(cookie, `/datasets/${id}`, { method: 'PATCH', body: JSON.stringify({ assistant_closed: false }) });
            await theirs.reload();
            const again = await datasetSwitch(theirs, id);
            await expect(again.toggle, level).toHaveAttribute('aria-checked', 'true');
            await expect(again.toggle, level).toBeDisabled();
            const refused = await theirs.request.patch(`/api/datasets/${id}`, { data: { assistant_closed: true } });
            expect(refused.status(), `${level}: the API refuses`).toBe(403);
            expect((await apiFetch(cookie, `/datasets/${id}`)).assistant_closed, `${level}: unchanged`).toBe(false);
            await apiFetch(cookie, `/datasets/${id}`, { method: 'PATCH', body: JSON.stringify({ assistant_closed: true }) });
        } finally {
            await context.close();
        }
    }

    // A manager opens it again.
    const { context, page: managers } = await signIn(browser, manager);
    try {
        const theirs = await datasetSwitch(managers, id);
        await expect(theirs.toggle).toBeEnabled();
        await expect(theirs.toggle).toHaveAccessibleDescription(CLOSED);
        await theirs.toggle.click();
        await expect(theirs.toggle).toHaveAttribute('aria-checked', 'true');
        await expect(theirs.toggle).toHaveAccessibleDescription(OPEN);
        expect((await apiFetch(cookie, `/datasets/${id}`)).assistant_closed).toBe(false);

        // A refusal shows the server's words under the switch, and the state stays.
        await managers.route(`**/api/datasets/${id}`, (route: Route) => (route.request().method() === 'PATCH'
            ? route.fulfill({ status: 403, json: { detail: 'e2e: closing is refused' } })
            : route.fallback()));
        await theirs.toggle.click();
        await expect(theirs.access.getByRole('alert')).toHaveText('e2e: closing is refused');
        await expect(theirs.toggle).toHaveAttribute('aria-checked', 'true');
    } finally {
        await context.close();
    }
});

/** Dataset Settings → Fields on `page`. */
async function fieldsTab(page: Page, id: number): Promise<Locator> {
    await openDataset(page, id);
    const dialog = await openSettings(page);
    await dialog.getByRole('button', { name: 'Fields', exact: true }).click();
    await expect(dialog.getByRole('group', { name: 'id', exact: true })).toBeVisible();
    return dialog;
}

const keepBox = (dialog: Locator, field: string) => dialog.getByRole('group', { name: field, exact: true }).getByRole('checkbox', { name: KEEP, exact: true });

type Field = { name: string; type?: string; description: string | null; unit: string | null; description_source: string | null; values_private?: boolean };
const fieldOf = async (id: number, name: string): Promise<Field> => ((await apiFetch(cookie, `/datasets/${id}/field-stats`, { method: 'POST', body: JSON.stringify({}) })) as Field[])
    .find(field => field.name === name)!;
const apart = ({ name, type, description, unit, description_source }: Field) => ({ name, type, description, unit, description_source });

test('Dataset Settings → Fields: from edit level any field\'s values can be kept from the assistant, sending only that and changing nothing else; below edit the box is disabled saying why', async ({ page, browser }) => {
    test.setTimeout(240_000);
    const id = await dataset('fields');
    await apiFetch(cookie, `/datasets/${id}/samples`, {
        method: 'POST',
        body: JSON.stringify({ samples: [{ filepath: `s3://e2e-assistant/${S}.jpg`, metadata: { brightness: 0.4, weather: 'sun' } }] }),
    });
    await apiFetch(cookie, `/datasets/${id}/refresh-stats`, { method: 'POST' });
    await expect.poll(async () => (await fieldOf(id, 'weather'))?.name, { timeout: 60_000 }).toBe('weather');
    await apiFetch(cookie, `/datasets/${id}/fields/brightness/description`, { method: 'PATCH', body: JSON.stringify({ description: 'Mean pixel value.', unit: 'px' }) });

    const sent = writesTo(page, `/api/datasets/${id}/fields/`);
    const dialog = await fieldsTab(page, id);
    await expect(dialog).toContainText(FIELDS_INTRO);
    for (const field of ['id', 'tags', 'media_type', 'idx', 'brightness', 'weather']) {
        await expect(keepBox(dialog, field), field).toBeEnabled();
        await expect(keepBox(dialog, field), field).not.toBeChecked();
    }

    // A metadata field with a description: only values_private is sent, and the rest of the field stays.
    const was = apart(await fieldOf(id, 'brightness'));
    await keepBox(dialog, 'brightness').click();
    await expect(keepBox(dialog, 'brightness')).toBeChecked();
    expect(sent).toEqual([['PATCH', `/api/datasets/${id}/fields/brightness/description`, { values_private: true }]]);
    const now = await fieldOf(id, 'brightness');
    expect(now.values_private).toBe(true);
    expect(apart(now), 'nothing else about the field changed').toEqual(was);
    await expect(dialog.getByRole('textbox', { name: 'Description of brightness', exact: true })).toHaveValue('Mean pixel value.');
    await expect(dialog.getByRole('textbox', { name: 'Unit of brightness', exact: true })).toHaveValue('px');

    // A built-in field too, and either one unticked again; kept over a reload.
    await keepBox(dialog, 'tags').click();
    await expect(keepBox(dialog, 'tags')).toBeChecked();
    expect(sent.at(-1)).toEqual(['PATCH', `/api/datasets/${id}/fields/tags/description`, { values_private: true }]);
    await page.reload();
    const reopened = await fieldsTab(page, id);
    await expect(keepBox(reopened, 'brightness')).toBeChecked();
    await expect(keepBox(reopened, 'tags')).toBeChecked();
    await keepBox(reopened, 'tags').click();
    await expect(keepBox(reopened, 'tags')).not.toBeChecked();
    expect(sent.at(-1)).toEqual(['PATCH', `/api/datasets/${id}/fields/tags/description`, { values_private: false }]);
    expect((await fieldOf(id, 'tags')).values_private).not.toBe(true);

    // A refusal shows the server's words under that box, and it stays as it was.
    await page.route(`**/api/datasets/${id}/fields/weather/description`, (route: Route) => route.fulfill({ status: 409, json: { detail: 'e2e: keeping is refused' } }));
    await keepBox(reopened, 'weather').click();
    await expect(reopened.getByRole('group', { name: 'weather', exact: true }).getByRole('alert')).toHaveText('e2e: keeping is refused');
    await expect(keepBox(reopened, 'weather')).not.toBeChecked();
    await page.unroute(`**/api/datasets/${id}/fields/weather/description`);

    // Each level.
    for (const level of ['view', 'tag', 'edit'] as const) {
        const user = await person(`fd_${level}`);
        await share(cookie, id, { user_id: user.id }, level);
        const { context, page: theirs } = await signIn(browser, user);
        try {
            const mine = await fieldsTab(theirs, id);
            if (level === 'edit') {
                await expect(keepBox(mine, 'weather')).toBeEnabled();
                await expect(keepBox(mine, 'weather')).not.toHaveAttribute('title', /./);
                await keepBox(mine, 'weather').click();
                await expect(keepBox(mine, 'weather')).toBeChecked();
                expect((await fieldOf(id, 'weather')).values_private).toBe(true);
            } else {
                for (const field of ['id', 'brightness', 'weather']) {
                    await expect(keepBox(mine, field), `${level}: ${field}`).toBeDisabled();
                    await expect.soft(keepBox(mine, field), `${level}: ${field}: the reason as the box's title`).toHaveAttribute('title', NEEDS_EDIT);
                }
                await expect(keepBox(mine, 'brightness'), `${level}: shows the state`).toBeChecked();
                await expect(mine).toContainText(VIEW_ONLY);
            }
        } finally {
            await context.close();
        }
    }
});

test('on a mirror, the Fields box works for the primary\'s fields too, changing nothing else, and the dataset\'s assistant switch is this server\'s own', async ({ page }) => {
    test.setTimeout(420_000);
    const source = await apiFetch(cookie, '/datasets', { method: 'POST', body: JSON.stringify({ name: `${PREFIX}primary` }) });
    await apiFetch(cookie, `/datasets/${source.id}/samples`, {
        method: 'POST',
        body: JSON.stringify({ samples: [{ filepath: `s3://e2e-assistant-mirror/${S}.jpg`, metadata: { digital_gain: 1.5 } }] }),
    });
    await apiFetch(cookie, `/datasets/${source.id}/refresh-stats`, { method: 'POST' });
    await expect.poll(async () => (await fieldOf(source.id, 'digital_gain'))?.name, { timeout: 60_000 }).toBe('digital_gain');
    const gain = 'Sensor gain applied before readout.';
    await apiFetch(cookie, `/datasets/${source.id}/fields/digital_gain/description`, { method: 'PATCH', body: JSON.stringify({ description: gain, unit: 'dB' }) });

    const follower = await apiFetch(cookie, '/followers', { method: 'POST', body: JSON.stringify({ name: `as${S}-mirror` }) });
    let linkId: number | undefined;
    let mirrorId: number | undefined;
    try {
        await apiFetch(cookie, `/followers/${follower.id}/datasets/${source.id}`, { method: 'PUT', body: JSON.stringify({ tags: 'both', views: 'primary' }) });
        linkId = (await apiFetch(cookie, '/primaries', { method: 'POST', body: JSON.stringify({ url: API, token: follower.token }) })).id;
        // The job runner syncs about once a minute.
        await expect.poll(async () => {
            const listed = await apiFetch(cookie, '/primaries');
            const link = listed.primaries.find((entry: { id: number }) => entry.id === linkId);
            const mirror = link?.mirrors?.find((entry: { origin_dataset_id: number; samples: number }) => entry.origin_dataset_id === source.id && entry.samples >= 1);
            mirrorId = mirror?.dataset_id;
            return mirrorId !== undefined;
        }, { timeout: 200_000, intervals: [3_000] }).toBe(true);
        const mirror = mirrorId!;
        await expect.poll(async () => (await fieldOf(mirror, 'digital_gain'))?.description, { timeout: 120_000, intervals: [3_000] }).toBe(gain);

        const sent = writesTo(page, `/api/datasets/${mirror}`);
        const dialog = await fieldsTab(page, mirror);
        const was = apart(await fieldOf(mirror, 'digital_gain'));
        await expect(keepBox(dialog, 'digital_gain')).toBeEnabled();
        await keepBox(dialog, 'digital_gain').click();
        await expect(keepBox(dialog, 'digital_gain')).toBeChecked();
        expect(sent).toEqual([['PATCH', `/api/datasets/${mirror}/fields/digital_gain/description`, { values_private: true }]]);
        const now = await fieldOf(mirror, 'digital_gain');
        expect(now.values_private).toBe(true);
        expect(apart(now), 'the primary\'s description and unit stay').toEqual(was);
        expect((await fieldOf(source.id, 'digital_gain')).values_private, 'the primary\'s own field is not changed').not.toBe(true);

        // The dataset's own switch, here too.
        await page.keyboard.press('Escape');
        const { toggle } = await datasetSwitch(page, mirror);
        await expect(toggle).toBeEnabled();
        await toggle.click();
        await expect(toggle).toHaveAttribute('aria-checked', 'false');
        expect((await apiFetch(cookie, `/datasets/${mirror}`)).assistant_closed).toBe(true);
        expect((await apiFetch(cookie, `/datasets/${source.id}`)).assistant_closed, 'the primary stays open').toBe(false);
    } finally {
        await allSteps([
            ...(linkId !== undefined ? [() => apiFetch(cookie, `/primaries/${linkId}`, { method: 'DELETE' })] : []),
            ...(mirrorId !== undefined ? [() => deleteDatasetAndWait(cookie, mirrorId!, 120_000)] : []),
            () => apiFetch(cookie, `/followers/${follower.id}`, { method: 'DELETE' }),
            () => deleteDatasetAndWait(cookie, source.id, 120_000),
        ]);
    }
});
