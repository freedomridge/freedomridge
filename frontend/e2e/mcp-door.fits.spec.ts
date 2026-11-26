import { test, expect, type Browser, type BrowserContext, type Page, type Route } from '@playwright/test';
import { API, adminLogin, apiFetch, deleteDatasetAndWait } from './api';
import { AUTH_STATE } from './helpers';
import { type TestUser, allSteps, createGroup, createSmallDataset, createUser, deleteGroup, signIn, stamp } from './access-helpers';

// The MCP door (docs/ASSISTANT.md "The MCP server", the UI contract of PR F). Outside AI clients reach the assistant's
// tools at /mcp with an MCP token, which opens /mcp and nothing else. The tokens page offers that kind only to someone
// who could use the MCP server right now; creating one shows how to connect a client and where the command leaves the
// token; the list marks MCP tokens, and they end like any token. Settings → Assistant shows administrators the recent
// tool calls: who, through which door, which tool and dataset, how long, how it ended, never what was asked.
// The assistant's settings and its call log are the server's own, so this spec runs in the one-at-a-time project after
// every other spec (playwright.config.ts: *.fits.spec.ts), and puts the settings back as it found them.
//
// Requests to /mcp follow the 2026-07-28 revision's Streamable HTTP binding
// (modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http): a POST with
// Accept: application/json, text/event-stream, MCP-Protocol-Version and Mcp-Method headers (Mcp-Name for tools/call),
// and the protocol version, client info and capabilities in the body's _meta; no initialize, no session.
test.use({ storageState: AUTH_STATE, permissions: ['clipboard-read', 'clipboard-write'] });

const S = stamp();
const PREFIX = `md${S}_`;
let cookie = '';
let found: { mcp_enabled: boolean; roles: Record<string, boolean>; new_datasets_closed: boolean };
const people: TestUser[] = [];
const groupIds: number[] = [];
const datasetIds: number[] = [];
const tokenIds: number[] = [];

const KIND_TEXT = {
    full: 'Scripts and the SDK: everything your role can do',
    worker: 'A compute worker: only runs jobs for this server',
    mcp: 'An AI client (MCP): only reads, through the assistant',
};
const HELP = 'For an AI client such as Claude Code: it opens only the MCP server, which reads as you through the assistant\'s tools, and no other part of the API.';
const NOTE = 'The command leaves the token in your shell\'s history, and Claude Code keeps it in its configuration in plain text. To keep it out of both, put the server in a project\'s .mcp.json by hand, with the header "Authorization": "Bearer ${FRIDGE_MCP_TOKEN}", and set FRIDGE_MCP_TOKEN in your environment.';
const CALLS_LINE = 'The tool calls made through the assistant in the last 30 days, newest first: who, which tool and dataset, how long each took and how it ended. What was asked and what was answered are not kept.';
const RESULT_TEXT: Record<string, string> = {
    not_allowed: 'Not allowed', refused: 'Refused', not_found: 'Not found', invalid: 'Invalid arguments', busy: 'Busy',
    timeout: 'Took too long', failed: 'Server failed', unknown_tool: 'Unknown tool',
};

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
        ...tokenIds.map(id => () => apiFetch(cookie, `/tokens/${id}`, { method: 'DELETE' }).catch(() => undefined)),
        ...datasetIds.map(id => () => deleteDatasetAndWait(cookie, id, 120_000)),
        ...groupIds.map(id => () => deleteGroup(cookie, id)),
        ...people.map(user => () => apiFetch(cookie, `/users/${user.id}`, { method: 'DELETE' }).catch(() => undefined)),
    ]);
});

const patchSettings = (changes: Record<string, unknown>) => apiFetch(cookie, '/admin/assistant', { method: 'PATCH', body: JSON.stringify(changes) });
const answerFor = (user: TestUser, allowed: boolean | null) => (allowed === null
    ? apiFetch(cookie, `/admin/assistant/users/${user.id}`, { method: 'DELETE' })
    : apiFetch(cookie, `/admin/assistant/users/${user.id}`, { method: 'PUT', body: JSON.stringify({ allowed }) }));

async function person(name: string, role: 'power_user' | 'guest' = 'power_user') {
    const user = await createUser(cookie, `${PREFIX}${name}`, role);
    people.push(user);
    return user;
}

/** A session cookie for `user`. */
async function sessionOf(user: TestUser): Promise<string> {
    const login = await fetch(`${API}/auth/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: user.username, password: user.password }),
    });
    expect(login.ok, `sign in as ${user.username}`).toBe(true);
    return `session_id=${(login.headers.get('set-cookie') || '').match(/session_id=([^;]+)/)?.[1]}`;
}

/** Whether GET /api/auth/me says this session's person could use the MCP server now. */
const mcpFor = async (session: string): Promise<boolean> => (await apiFetch(session, '/auth/me')).assistant?.mcp;

/** An MCP (or other) token made through the API for the session's person. */
async function tokenOf(session: string, scope: 'mcp' | 'full', name = `${PREFIX}${scope}-${Math.random().toString(36).slice(2, 8)}`) {
    const made = await apiFetch(session, '/tokens', { method: 'POST', body: JSON.stringify({ name, scope }) });
    tokenIds.push(made.id);
    return made as { id: number; name: string; token: string; scope: string };
}

const MCP_VERSION = '2026-07-28';
const META = {
    'io.modelcontextprotocol/protocolVersion': MCP_VERSION,
    'io.modelcontextprotocol/clientInfo': { name: 'freedomridge-e2e', version: '1.0.0' },
    'io.modelcontextprotocol/clientCapabilities': {},
};
let rpcId = 0;

type ToolResult = {
    isError: boolean;
    structuredContent: { datasets?: Array<{ id: number }>; dataset?: { id: number }; error?: { code: string } };
};
type RpcAnswer = {
    result?: Partial<ToolResult> & { capabilities?: { tools?: unknown }; tools?: Array<{ name: string; annotations?: { readOnlyHint?: boolean } }> };
    error?: { code: number; message: string };
};

/** One JSON-RPC request to /mcp through `origin`, as a 2026-07-28 client sends it; the answer as JSON (or SSE). */
async function mcpRequest(origin: string, token: string | null, method: string, params: Record<string, unknown> = {}, extra: Record<string, string> = {}) {
    const headers: Record<string, string> = {
        'Content-Type': 'application/json', Accept: 'application/json, text/event-stream', 'MCP-Protocol-Version': MCP_VERSION, 'Mcp-Method': method, ...extra,
    };
    if (method === 'tools/call') headers['Mcp-Name'] = String(params.name);
    if (token) headers.Authorization = `Bearer ${token}`;
    rpcId += 1;
    const res = await fetch(`${origin}/mcp`, { method: 'POST', headers, body: JSON.stringify({ jsonrpc: '2.0', id: rpcId, method, params: { ...params, _meta: META } }) });
    const text = await res.text();
    const sse = (res.headers.get('content-type') ?? '').includes('text/event-stream');
    const body = sse ? (text.split('\n').filter(line => line.startsWith('data:')).map(line => line.slice(5).trim()).at(-1) ?? 'null') : text;
    let json: RpcAnswer | null = null;
    try {
        json = JSON.parse(body || 'null');
    } catch {
        json = null;
    }
    return { status: res.status, headers: res.headers, json, text };
}

/** A tool call through /mcp; the tool result. */
async function callTool(origin: string, token: string, name: string, args: Record<string, unknown>) {
    const answer = await mcpRequest(origin, token, 'tools/call', { name, arguments: args });
    expect(answer.status, `${name}: ${answer.text.slice(0, 200)}`).toBe(200);
    return answer.json!.result as ToolResult;
}

/** The kinds the tokens page offers in "Used by" (option values), freshly loaded. */
async function kindsOffered(page: Page): Promise<string[]> {
    await page.goto('/settings/tokens');
    await expect(page.getByRole('heading', { name: 'API Tokens', level: 1 })).toBeVisible();
    await page.getByRole('button', { name: 'Create New Token', exact: true }).click();
    return page.getByRole('combobox', { name: 'Used by', exact: true }).getByRole('option').evaluateAll(options => options.map(option => (option as HTMLOptionElement).value));
}

const OFFERED = ['full', 'worker', 'mcp'];
const NOT_OFFERED = ['full', 'worker'];

test('the MCP kind is offered only to someone who could use the MCP server now: by role, group and their own answer either way, never with the server off', async ({ page, browser }) => {
    test.setTimeout(300_000);
    const admin = cookie;
    const pat = await person('pat');
    const patSession = await sessionOf(pat);
    const group = await createGroup(cookie, `${PREFIX}crew`, [pat]);
    groupIds.push(group.id);
    let context: BrowserContext | undefined;
    try {
        // Off: nobody, administrators included.
        await patchSettings({ mcp_enabled: false });
        await expect.poll(() => mcpFor(admin), { timeout: 15_000 }).toBe(false);
        expect(await kindsOffered(page), 'the server off: an administrator').toEqual(NOT_OFFERED);
        await expect(page.getByRole('combobox', { name: 'Used by', exact: true }).getByRole('option')).toHaveText([KIND_TEXT.full, KIND_TEXT.worker]);

        // On: administrators by their role, with the kind's text.
        await patchSettings({ mcp_enabled: true });
        await expect.poll(() => mcpFor(admin), { timeout: 15_000 }).toBe(true);
        expect(await kindsOffered(page), 'an administrator').toEqual(OFFERED);
        await expect(page.getByRole('combobox', { name: 'Used by', exact: true }).getByRole('option')).toHaveText([KIND_TEXT.full, KIND_TEXT.worker, KIND_TEXT.mcp]);
        const kept = await tokenOf(admin, 'mcp');

        // A power user, through each line of the rule; checked after a reload, as auth/me says.
        ({ context } = await signIn(browser, pat));
        const theirs = await context.newPage();
        const expectFor = async (offered: boolean, step: string) => {
            await expect.poll(() => mcpFor(patSession), { message: `${step}: auth/me`, timeout: 15_000 }).toBe(offered);
            expect(await kindsOffered(theirs), step).toEqual(offered ? OFFERED : NOT_OFFERED);
        };
        await expectFor(false, 'a power user, by role');
        await patchSettings({ roles: { power_user: true } });
        await expectFor(true, 'power users ticked');
        await patchSettings({ roles: { power_user: false } });
        await expectFor(false, 'power users unticked');
        await apiFetch(cookie, `/admin/assistant/groups/${group.id}`, { method: 'PUT' });
        await expectFor(true, 'in a ticked group');
        await answerFor(pat, false);
        await expectFor(false, 'their own "May not use it" over the group');
        await apiFetch(cookie, `/admin/assistant/groups/${group.id}`, { method: 'DELETE' });
        await answerFor(pat, true);
        await expectFor(true, 'their own "May use it" over role and groups');

        // The server switched off: nobody, whatever they may do; MCP tokens made before stay listed, marked.
        await patchSettings({ mcp_enabled: false });
        await expectFor(false, 'the server off, their own "May use it"');
        await expect.poll(() => mcpFor(admin), { timeout: 15_000 }).toBe(false);
        expect(await kindsOffered(page), 'the server off: an administrator').toEqual(NOT_OFFERED);
        await expect(page.getByRole('heading', { level: 3, name: new RegExp(`^${kept.name}`) }).getByTestId('token-mcp-badge')).toHaveText('MCP');
        await answerFor(pat, null);
    } finally {
        await context?.close();
        await patchSettings({ mcp_enabled: false, roles: found.roles });
    }
});

test('guests make no tokens of any kind, and a demo guest may never use the MCP server', async ({ browser }) => {
    test.setTimeout(120_000);
    const gil = await person('gil', 'guest');
    const gilSession = await sessionOf(gil);
    const demo = await fetch(`${API}/auth/demo-login`, { method: 'POST' });
    const demoSession = `session_id=${(demo.headers.get('set-cookie') || '').match(/session_id=([^;]+)/)?.[1]}`;
    try {
        await patchSettings({ mcp_enabled: true, roles: { guest: true } });
        await expect.poll(() => mcpFor(gilSession), { message: 'a guest, guests ticked', timeout: 15_000 }).toBe(true);
        await expect.poll(() => mcpFor(demoSession), { message: 'a demo guest, guests ticked', timeout: 15_000 }).toBe(false);
        const { context, page } = await signIn(browser, gil);
        try {
            await page.goto('/settings/tokens');
            await expect(page.getByText(/^Guest users cannot create API tokens\./)).toBeVisible();
            await expect(page.getByRole('combobox', { name: 'Used by', exact: true })).toHaveCount(0);
            await expect(page.getByRole('button', { name: 'Create New Token', exact: true })).toHaveCount(0);
        } finally {
            await context.close();
        }
    } finally {
        await patchSettings({ mcp_enabled: false, roles: found.roles });
    }
});

test('an MCP token is shown once, with the address, the header, the Claude Code command and where it leaves the token; the copy buttons copy them; the list marks it; Revoke ends it', async ({ page }) => {
    test.setTimeout(180_000);
    await patchSettings({ mcp_enabled: true });
    try {
        await expect.poll(() => mcpFor(cookie), { timeout: 15_000 }).toBe(true);
        await page.goto('/settings/tokens');
        await expect(page.getByRole('heading', { name: 'API Tokens', level: 1 })).toBeVisible();
        await page.getByRole('button', { name: 'Create New Token', exact: true }).click();
        const create = page.getByRole('button', { name: 'Create Token', exact: true });
        await expect(create).toBeDisabled();
        const name = `${PREFIX}laptop`;
        await page.getByPlaceholder('e.g., \'my-laptop\', \'ci-server\', \'data-pipeline\'').fill(name);
        await expect(create).toBeEnabled();

        // The kind's one line of help, only for that kind.
        const kind = page.getByRole('combobox', { name: 'Used by', exact: true });
        await expect(kind).toHaveAttribute('data-testid', 'token-scope');
        await kind.selectOption('mcp');
        await expect(page.getByText(HELP, { exact: true })).toBeVisible();
        await expect(kind).toHaveAccessibleDescription(HELP);
        await kind.selectOption('full');
        await expect(page.getByText(HELP, { exact: true })).toHaveCount(0);
        await kind.selectOption('mcp');

        const made = page.waitForResponse(response => response.request().method() === 'POST' && new URL(response.url()).pathname === '/api/tokens');
        await create.click();
        const answer = await made;
        expect(answer.request().postDataJSON()).toMatchObject({ name, scope: 'mcp' });
        const { token, id } = await answer.json();
        tokenIds.push(id);
        expect(token).toMatch(/^\S{43}$/);
        await expect(page.getByText('Token Created!')).toBeVisible();
        await expect(page.getByText(token, { exact: true })).toBeVisible();
        await expect(page.getByText(/^Usage:/)).toHaveCount(0);

        // How to connect a client.
        const origin = new URL(page.url()).origin;
        const command = `claude mcp add --transport http freedomridge ${origin}/mcp --header "Authorization: Bearer ${token}"`;
        const region = page.getByRole('region', { name: 'Connect an AI client', exact: true });
        await expect(region.getByRole('heading', { name: 'Connect an AI client', level: 4 })).toBeVisible();
        await expect(region.getByRole('term')).toHaveText(['Address', 'Header to send with every request', 'Claude Code']);
        await expect(region.getByRole('definition')).toHaveText([`${origin}/mcp`, `Authorization: Bearer ${token}`, command]);
        for (const [index, value] of [`${origin}/mcp`, `Authorization: Bearer ${token}`, command].entries()) {
            await expect(region.getByRole('definition').nth(index).getByRole('code')).toHaveText(value);
        }
        await expect(region.getByText(NOTE, { exact: true })).toBeVisible();
        const copyCommand = region.getByRole('button', { name: 'Copy Claude Code command', exact: true });
        await expect(copyCommand).toHaveAccessibleDescription(NOTE);
        await page.getByRole('button', { name: 'Copy token', exact: true }).click();
        await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toBe(token);
        await copyCommand.click();
        await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toBe(command);

        // The list marks it; the API calls its kind mcp.
        const heading = page.getByRole('heading', { level: 3, name: new RegExp(`^${name}`) });
        await expect(heading.getByTestId('token-mcp-badge')).toHaveText('MCP');
        const listed = ((await apiFetch(cookie, '/tokens')) as Array<{ id: number; scope: string }>).find(row => row.id === id);
        expect(listed?.scope).toBe('mcp');

        // Shown once.
        await page.reload();
        await expect(page.getByRole('heading', { name: 'API Tokens', level: 1 })).toBeVisible();
        await expect(heading).toBeVisible();
        await expect(page.getByText(token)).toHaveCount(0);
        await expect(page.getByText('Token Created!')).toHaveCount(0);
        expect(await page.content()).not.toContain(token);

        // An ordinary token, for contrast: its usage line, no connect section, no badge.
        await page.getByRole('button', { name: 'Create New Token', exact: true }).click();
        await page.getByPlaceholder('e.g., \'my-laptop\', \'ci-server\', \'data-pipeline\'').fill(`${PREFIX}script`);
        const madeFull = page.waitForResponse(response => response.request().method() === 'POST' && new URL(response.url()).pathname === '/api/tokens');
        await page.getByRole('button', { name: 'Create Token', exact: true }).click();
        tokenIds.push((await (await madeFull).json()).id);
        await expect(page.getByText('Usage: Authorization: Bearer {token}')).toBeVisible();
        await expect(page.getByRole('region', { name: 'Connect an AI client' })).toHaveCount(0);
        await expect(page.getByRole('heading', { level: 3, name: new RegExp(`^${PREFIX}script`) }).getByTestId('token-mcp-badge')).toHaveCount(0);

        // Revoke ends it, as for any token: /mcp refuses it afterwards.
        expect((await mcpRequest(origin, token, 'tools/list')).status, 'before revoking').toBe(200);
        await page.reload();
        await expect(heading).toBeVisible();
        const names = (await page.getByRole('heading', { level: 3 }).allInnerTexts()).map(text => text.trim());
        const index = names.findIndex(text => text.startsWith(name));
        expect(index).toBeGreaterThanOrEqual(0);
        const confirm = new Promise<string>(resolve => page.once('dialog', dialog => {
            resolve(dialog.message());
            void dialog.accept();
        }));
        await page.getByRole('button', { name: 'Revoke', exact: true }).nth(index).click();
        expect(await confirm).toBe(`Revoke token "${name}"? This cannot be undone.`);
        await expect.poll(async () => ((await apiFetch(cookie, '/tokens')) as Array<{ id: number; is_active: boolean }>).find(row => row.id === id)?.is_active).toBe(false);
        const after = await mcpRequest(origin, token, 'tools/list');
        expect(after.status, 'after revoking').toBe(401);
    } finally {
        await patchSettings({ mcp_enabled: false });
    }
});

test('the token does what the page says: /mcp discovers, lists and calls the tools with it, every other route refuses it as an unknown token, and /mcp takes no ordinary token or session', async ({ page, baseURL }) => {
    test.setTimeout(120_000);
    const origin = baseURL!;
    await patchSettings({ mcp_enabled: true });
    const id = await createSmallDataset(cookie, `${PREFIX}door`);
    datasetIds.push(id);
    try {
        await expect.poll(() => mcpFor(cookie), { timeout: 15_000 }).toBe(true);
        const mcp = await tokenOf(cookie, 'mcp');
        const full = await tokenOf(cookie, 'full');

        const discovered = await mcpRequest(origin, mcp.token, 'server/discover');
        expect(discovered.status, discovered.text.slice(0, 300)).toBe(200);
        expect(discovered.json?.result?.capabilities?.tools, 'discovery names the tools capability').toBeTruthy();
        const listed = await mcpRequest(origin, mcp.token, 'tools/list');
        expect(listed.status).toBe(200);
        const tools = listed.json!.result!.tools!;
        expect(tools.map(tool => tool.name)).toEqual(expect.arrayContaining(['list_datasets', 'describe_dataset']));
        expect(tools.every(tool => tool.annotations?.readOnlyHint === true), 'every tool marked read-only').toBe(true);

        const named = await callTool(origin, mcp.token, 'list_datasets', { search: `${PREFIX}door` });
        expect(named.isError).toBe(false);
        expect(named.structuredContent.datasets!.map(row => row.id)).toEqual([id]);
        const described = await callTool(origin, mcp.token, 'describe_dataset', { dataset: id });
        expect(described.isError).toBe(false);
        expect(described.structuredContent.dataset!.id).toBe(id);
        const missing = await callTool(origin, mcp.token, 'describe_dataset', { dataset: 999_999_999 });
        expect(missing.isError).toBe(true);
        expect(missing.structuredContent.error!.code).toBe('not_found');

        // Anywhere else, the MCP token gets what a made-up token gets: the same status and words, reads included.
        for (const path of ['/api/auth/me', '/api/datasets', `/api/datasets/${id}`, '/api/tokens']) {
            const withMcp = await fetch(`${origin}${path}`, { headers: { Authorization: `Bearer ${mcp.token}` } });
            const madeUp = await fetch(`${origin}${path}`, { headers: { Authorization: `Bearer e2e-no-such-token-${S}` } });
            expect([withMcp.status, await withMcp.text()], path).toEqual([madeUp.status, await madeUp.text()]);
            expect(withMcp.status, path).toBe(401);
        }

        // /mcp takes an MCP token and nothing else: an ordinary token is told where one is made; a session counts for
        // nothing.
        const ordinary = await mcpRequest(origin, full.token, 'tools/list');
        expect(ordinary.status).toBe(401);
        expect(ordinary.headers.get('www-authenticate')).toBe('Bearer');
        expect(ordinary.json?.error?.message).toMatch(/MCP token/);
        const session = await page.request.post('/mcp', {
            headers: { 'Content-Type': 'application/json', Accept: 'application/json, text/event-stream', 'MCP-Protocol-Version': MCP_VERSION, 'Mcp-Method': 'tools/list' },
            data: { jsonrpc: '2.0', id: 1, method: 'tools/list', params: { _meta: META } },
        });
        expect(session.status(), 'a signed-in session and no token').toBe(401);
        const none = await mcpRequest(origin, null, 'tools/list');
        expect(none.status, 'no token').toBe(401);
    } finally {
        await patchSettings({ mcp_enabled: false });
    }
});

test('deactivating a person ends their MCP tokens with the rest, and the message counts them', async ({ page, baseURL }) => {
    test.setTimeout(180_000);
    const origin = baseURL!;
    await patchSettings({ mcp_enabled: true });
    try {
        const cases: Array<[string, Array<'mcp' | 'full'>, RegExp]> = [
            ['two', ['mcp', 'mcp', 'full'], /^Account deactivated, and 3 API tokens are revoked, including 2 MCP tokens\. AI clients using them can no longer connect\.$/],
            ['one', ['mcp'], /^Account deactivated, .*including 1 MCP token\. An AI client using it can no longer connect\.$/],
            ['none', ['full'], /^Account deactivated(?!.*MCP).*$/],
        ];
        for (const [label, scopes, message] of cases) {
            const user = await person(`deact_${label}`);
            await answerFor(user, true);
            const session = await sessionOf(user);
            await expect.poll(() => mcpFor(session), { timeout: 15_000 }).toBe(true);
            const tokens = [];
            for (const scope of scopes) tokens.push(await tokenOf(session, scope));
            const mcpTokens = tokens.filter(token => token.scope === 'mcp');
            for (const token of mcpTokens) expect((await mcpRequest(origin, token.token, 'tools/list')).status, `${label}: before`).toBe(200);

            await page.goto('/users');
            await page.getByRole('row').filter({ hasText: user.username }).getByTitle('Edit User').click();
            const dialog = page.getByRole('dialog');
            await dialog.getByLabel('Active Account').uncheck();
            await page.getByTestId('edit-user-save').click();
            await expect(page.getByText(/^Account deactivated/), label).toHaveText(message);
            for (const token of mcpTokens) expect((await mcpRequest(origin, token.token, 'tools/list')).status, `${label}: after`).toBe(401);
            const rows = (await apiFetch(cookie, `/users`)) as Array<{ id: number; is_active: boolean }>;
            expect(rows.find(row => row.id === user.id)?.is_active, label).toBe(false);
        }
    } finally {
        await patchSettings({ mcp_enabled: false });
    }
});

/** What the Took column shows for `ms`. */
const took = (ms: number) => (ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`);
type Call = { id: number; user_id: number | null; username: string | null; door: string; tool: string; dataset_id: number | null; started_at: string; duration_ms: number; ok: boolean; error_code: string | null };

async function openCalls(page: Page) {
    await page.goto('/settings/assistant');
    const region = page.getByRole('region', { name: 'Recent calls', exact: true });
    await expect(region.getByRole('heading', { name: 'Recent calls', level: 2 })).toBeVisible();
    return region;
}

test('Recent calls: who, door, tool, dataset, how long and how it ended, newest first, never what was asked; a deleted person; Refresh, Older and Newer', async ({ page, baseURL }) => {
    test.setTimeout(300_000);
    const origin = baseURL!;
    const planted = `ZEBRA-${S}-PLANTED`;
    await patchSettings({ mcp_enabled: true });
    const open = await createSmallDataset(cookie, `${PREFIX}calls-open`);
    const closed = await createSmallDataset(cookie, `${PREFIX}calls-closed`);
    datasetIds.push(open, closed);
    await apiFetch(cookie, `/datasets/${closed}`, { method: 'PATCH', body: JSON.stringify({ assistant_closed: true }) });
    const dee = await person('dee');
    try {
        await answerFor(dee, true);
        const deeSession = await sessionOf(dee);
        await expect.poll(() => mcpFor(deeSession), { timeout: 15_000 }).toBe(true);
        await expect.poll(() => mcpFor(cookie), { timeout: 15_000 }).toBe(true);
        const mine = await tokenOf(cookie, 'mcp');
        const deeToken = await tokenOf(deeSession, 'mcp');

        // Five calls, each carrying the planted text in its arguments.
        const made: Array<[string, string, number | null, string]> = [];  // person, tool, dataset, result
        await callTool(origin, deeToken.token, 'list_datasets', { search: planted });
        made.push(['Deleted user', 'list_datasets', null, 'OK']);
        await callTool(origin, mine.token, 'list_datasets', { search: planted });
        made.push(['admin', 'list_datasets', null, 'OK']);
        await callTool(origin, mine.token, 'describe_dataset', { dataset: open, search: planted });
        made.push(['admin', 'describe_dataset', open, 'OK']);
        expect((await callTool(origin, mine.token, 'describe_dataset', { dataset: closed, search: planted })).isError).toBe(true);
        made.push(['admin', 'describe_dataset', closed, 'Refused']);
        expect((await callTool(origin, mine.token, 'describe_dataset', { dataset: 999_999_999, search: planted })).isError).toBe(true);
        made.push(['admin', 'describe_dataset', 999_999_999, 'Not found']);
        // The person who made the first call is deleted; the row stays, as a deleted user's.
        await apiFetch(cookie, `/users/${dee.id}`, { method: 'DELETE' });

        const log = await apiFetch(cookie, '/admin/assistant/calls?limit=50&offset=0') as { calls: Call[]; total: number; next_offset: number | null };
        expect(JSON.stringify(log)).not.toContain(planted);
        const region = await openCalls(page);
        await expect(region.getByText(CALLS_LINE, { exact: true })).toBeVisible();
        const table = region.getByRole('table', { name: 'Recent assistant calls', exact: true });
        await expect(table.getByRole('columnheader')).toHaveText(['When', 'Person', 'Door', 'Tool', 'Dataset', 'Took', 'Result']);
        const rows = table.getByRole('row').filter({ has: page.getByRole('cell') });
        await expect(rows).toHaveCount(Math.min(50, log.total));
        await expect(region.getByText(`Calls 1–${Math.min(50, log.total)} of ${log.total}`, { exact: true })).toBeVisible();

        // Newest first: the five calls, last made on top, each column as the log has it.
        const newest = [...made].reverse();
        for (const [index, [who, tool, dataset, result]] of newest.entries()) {
            const call = log.calls[index];
            expect([call.tool, call.dataset_id], `the log's row ${index}`).toEqual([tool, dataset]);
            const when = await page.evaluate(at => new Date(at).toLocaleString(), call.started_at);
            await expect(rows.nth(index).getByRole('cell'), `row ${index}`).toHaveText([when, who, 'MCP', tool, dataset === null ? '—' : String(dataset), took(call.duration_ms), result]);
            expect(result === 'OK' ? null : RESULT_TEXT[call.error_code!], `row ${index}: the code's words`).toBe(result === 'OK' ? null : result);
        }
        expect(await page.content(), 'what was asked appears nowhere').not.toContain(planted);

        // Refresh reads again, disabled while reading; the next call shows on top.
        await callTool(origin, mine.token, 'list_datasets', { search: planted });
        let release!: () => void;
        const held = new Promise<void>(resolve => { release = resolve; });
        await page.route('**/api/admin/assistant/calls*', async (route: Route) => {
            await held;
            await route.fallback();
        });
        const refresh = region.getByRole('button', { name: 'Refresh', exact: true });
        await refresh.click();
        await expect(refresh).toBeDisabled();
        release();
        await expect(refresh).toBeEnabled();
        await page.unroute('**/api/admin/assistant/calls*');
        await expect(rows.first().getByRole('cell').nth(3)).toHaveText('list_datasets');
        await expect(region.getByText(`Calls 1–${Math.min(50, log.total + 1)} of ${log.total + 1}`, { exact: true })).toBeVisible();

        // More than a page: 50 a page, Older and Newer.
        const total = log.total + 1;
        const needed = Math.max(0, 53 - total);
        for (let i = 0; i < needed; i += 1) await callTool(origin, mine.token, 'list_datasets', { search: planted, limit: 1 });
        const all = total + needed;
        await page.reload();
        const older = region.getByRole('button', { name: 'Older', exact: true });
        const newer = region.getByRole('button', { name: 'Newer', exact: true });
        await expect(region.getByText(`Calls 1–50 of ${all}`, { exact: true })).toBeVisible();
        await expect(rows).toHaveCount(50);
        await expect(newer).toBeDisabled();
        await expect(older).toBeEnabled();
        await older.click();
        const last = Math.min(100, all);
        await expect(region.getByText(`Calls 51–${last} of ${all}`, { exact: true })).toBeVisible();
        await expect(rows).toHaveCount(last - 50);
        await expect(newer).toBeEnabled();
        if (all <= 100) await expect(older).toBeDisabled();
        expect(await page.content()).not.toContain(planted);
        await newer.click();
        await expect(region.getByText(`Calls 1–50 of ${all}`, { exact: true })).toBeVisible();
        await expect(newer).toBeDisabled();
    } finally {
        await patchSettings({ mcp_enabled: false });
    }
});

test('Recent calls with none, while reading and when the read fails (answers faked); someone who isn\'t an administrator has neither the table nor its API', async ({ page, browser }) => {
    await page.route('**/api/admin/assistant/calls*', (route: Route) => route.fulfill({ json: { calls: [], total: 0, offset: 0, next_offset: null } }));
    let region = await openCalls(page);
    await expect(region.getByText('No calls in the last 30 days.', { exact: true })).toBeVisible();
    await page.unroute('**/api/admin/assistant/calls*');

    let release!: () => void;
    const held = new Promise<void>(resolve => { release = resolve; });
    await page.route('**/api/admin/assistant/calls*', async (route: Route) => {
        await held;
        await route.fulfill({ json: { calls: [], total: 0, offset: 0, next_offset: null } });
    });
    await page.goto('/settings/assistant');
    region = page.getByRole('region', { name: 'Recent calls', exact: true });
    await expect(region.getByText('Loading the recent calls…', { exact: true })).toBeVisible();
    release();
    await expect(region.getByText('No calls in the last 30 days.', { exact: true })).toBeVisible();
    await page.unroute('**/api/admin/assistant/calls*');

    await page.route('**/api/admin/assistant/calls*', (route: Route) => route.fulfill({ status: 500, json: { detail: 'e2e: the calls could not be read' } }));
    region = await openCalls(page);
    await expect(region.getByRole('alert').filter({ hasText: 'e2e: the calls could not be read' })).toBeVisible();
    await page.unroute('**/api/admin/assistant/calls*');

    await expectNoCallsFor(browser, await person('notadmin'));
});

async function expectNoCallsFor(browser: Browser, user: TestUser) {
    const { context, page } = await signIn(browser, user);
    try {
        await page.goto('/settings/assistant');
        await expect(page.getByText('Only administrators can change the assistant\'s settings.', { exact: true })).toBeVisible();
        await expect(page.getByRole('region', { name: 'Recent calls' })).toHaveCount(0);
        await expect(page.getByRole('table', { name: 'Recent assistant calls' })).toHaveCount(0);
        const read = await page.request.get('/api/admin/assistant/calls?limit=50&offset=0');
        expect(read.status()).toBe(403);
        expect(await read.text()).not.toContain('"calls"');
    } finally {
        await context.close();
    }
    const visitor = await browser.newContext({ storageState: { cookies: [], origins: [] } });
    try {
        const read = await visitor.request.get('/api/admin/assistant/calls?limit=50&offset=0');
        expect(read.ok()).toBe(false);
        expect(await read.text()).not.toContain('"calls"');
    } finally {
        await visitor.close();
    }
}
