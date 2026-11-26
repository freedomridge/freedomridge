// Run with: node --test tests/session-end.test.cjs
/* eslint-disable @typescript-eslint/no-require-imports -- Transpile the real modules with the Node CommonJS loader. */
// A deactivated account's session is refused with a 403 "Account is inactive" (backend/routers/auth.py): the app treats
// it as the end of the session, as a 401, and any other 403 as a refusal of one request. And asking who is signed in is
// paced: a page's own failing requests ask at most once in 5 seconds; a navigation asks at once, every time.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const { test } = require('node:test');
const ts = require('typescript');
const React = require('react');

function load(relative, mocks = {}) {
    const filename = path.resolve(__dirname, '..', relative);
    const loaded = new Module(filename);
    loaded.paths = Module._nodeModulePaths(path.dirname(filename));
    const originalRequire = loaded.require.bind(loaded);
    loaded.require = name => Object.hasOwn(mocks, name) ? mocks[name] : originalRequire(name);
    loaded._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
        compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
        fileName: filename,
    }).outputText, filename);
    return loaded.exports;
}

function harness(t) {
    const slots = [];
    let cursor = 0;
    const timers = new Map();
    const window = new EventTarget();
    const oldWindow = global.window;
    global.window = window;
    t.after(() => { global.window = oldWindow; });
    t.mock.method(global, 'setTimeout', callback => { const key = {}; timers.set(key, callback); return key; });
    t.mock.method(global, 'clearTimeout', key => timers.delete(key));
    let answer = { status: 200, body: { id: 3, username: 'pat', role: 'power_user' } };
    t.mock.method(global, 'fetch', async () => new Response(JSON.stringify(answer.body), { status: answer.status }));
    const react = {
        ...React,
        useState(initial) {
            const i = cursor++;
            if (!(i in slots)) slots[i] = typeof initial === 'function' ? initial() : initial;
            return [slots[i], value => { slots[i] = typeof value === 'function' ? value(slots[i]) : value; }];
        },
        useRef(initial) { const i = cursor++; return slots[i] ??= { current: initial }; },
        useCallback(callback) { const i = cursor++; return slots[i] ??= callback; },
        useEffect() { cursor++; },
    };
    const { UserProvider } = load('contexts/UserContext.tsx', {
        react,
        'next/navigation': { useRouter: () => ({ push() {} }) },
        '@tanstack/react-query': { useQueryClient: () => ({}) },
    });
    const ended = t.mock.fn();
    window.addEventListener('fridge:unauthorized', ended);
    return {
        timers, ended,
        answer(status, body) { answer = { status, body }; },
        render() { cursor = 0; return UserProvider({ children: null }).props.value; },
    };
}

test("a deactivated account's 403 signs the open tab out and announces it, as a 401 does", async t => {
    const h = harness(t);
    await h.render().checkAuth();
    assert.equal(h.render().user.username, 'pat');
    h.answer(403, { detail: 'Account is inactive' });
    await h.render().checkAuth();
    assert.equal(h.render().user, null);
    assert.equal(h.render().isLoading, false);
    assert.equal(h.ended.mock.callCount(), 1);
    assert.equal(h.timers.size, 0);  // no quiet retry
});

test('a tab opened on a deactivated account stops loading instead of retrying for good', async t => {
    const h = harness(t);
    h.answer(403, { detail: 'Account is inactive' });
    await h.render().checkAuth();
    assert.equal(h.render().isLoading, false);
    assert.equal(h.render().user, null);
    assert.equal(h.ended.mock.callCount(), 1);
});

test('any other 403 keeps the person signed in and retries quietly', async t => {
    const h = harness(t);
    await h.render().checkAuth();
    h.answer(403, { detail: 'Insufficient permissions' });
    await h.render().checkAuth();
    assert.equal(h.render().user.username, 'pat');
    assert.equal(h.ended.mock.callCount(), 0);
    assert.equal(h.timers.size, 1);
});

test("an API call refused for a deactivated account announces the end of the session; another 403 doesn't", t => {
    const window = new EventTarget();
    const oldWindow = global.window;
    global.window = window;
    t.after(() => { global.window = oldWindow; });
    const ended = t.mock.fn();
    window.addEventListener('fridge:unauthorized', ended);
    const { ApiError } = load('lib/api.ts');
    new ApiError(403, 'This needs edit access to the dataset');
    new ApiError(403, 'Insufficient permissions');
    assert.equal(ended.mock.callCount(), 0);
    new ApiError(403, 'Account is inactive');
    assert.equal(ended.mock.callCount(), 1);
});

// A fake React for one component: its hooks keep their slots between renders, and its effects run when asked.
function fakeReact() {
    const slots = [];
    let cursor = 0;
    let pending = [];
    const react = {
        ...React,
        useState(initial) {
            const i = cursor++;
            if (!(i in slots)) slots[i] = typeof initial === 'function' ? initial() : initial;
            return [slots[i], value => { slots[i] = typeof value === 'function' ? value(slots[i]) : value; }];
        },
        useRef(initial) { const i = cursor++; return slots[i] ??= { current: initial }; },
        useCallback(callback) { const i = cursor++; return slots[i] ??= callback; },
        useEffect(effect, deps) {
            const i = cursor++;
            const previous = slots[i];
            if (previous && deps.every((value, index) => Object.is(value, previous.deps[index]))) return;
            pending.push(() => { previous?.cleanup?.(); slots[i] = { deps, cleanup: effect() }; });
        },
    };
    return {
        react,
        render(component, props) { cursor = 0; return component(props); },
        effects() { const due = pending; pending = []; due.forEach(effect => effect()); },
        unmount() { slots.forEach(slot => slot?.cleanup?.()); },
    };
}

// UserProvider (its session watchdog) and SessionCheck together, on a clock the test moves.
function pacingHarness(t) {
    let now = 1_000_000;
    t.mock.method(Date, 'now', () => now);
    const timers = new Map();
    t.mock.method(global, 'setTimeout', (callback, ms = 0) => { const key = {}; timers.set(key, { at: now + ms, callback }); return key; });
    t.mock.method(global, 'clearTimeout', key => timers.delete(key));
    t.mock.method(global, 'setInterval', () => 1);  // the watchdog's minute: not what these tests count
    t.mock.method(global, 'clearInterval', () => {});
    const old = { window: global.window, document: global.document };
    global.window = new EventTarget();
    global.document = Object.assign(new EventTarget(), { visibilityState: 'visible' });
    const asked = [];
    let reachable = true;
    t.mock.method(global, 'fetch', async () => {
        asked.push(now);
        if (!reachable) throw new TypeError('Failed to fetch');
        return new Response(JSON.stringify({ id: 3, username: 'pat', role: 'power_user' }), { status: 200 });
    });
    const provider = fakeReact();
    const check = fakeReact();
    t.after(() => {
        provider.unmount();
        check.unmount();
        global.window = old.window;
        global.document = old.document;
    });
    const { UserProvider } = load('contexts/UserContext.tsx', {
        react: provider.react,
        'next/navigation': { useRouter: () => ({ push() {} }) },
        '@tanstack/react-query': { useQueryClient: () => ({}) },
    });
    const subscribers = new Set();
    const cache = { subscribe(callback) { subscribers.add(callback); return () => subscribers.delete(callback); } };
    let value;
    let pathname = '/datasets/1';
    const { SessionCheck } = load('components/SessionCheck.tsx', {
        react: check.react,
        'next/navigation': { usePathname: () => pathname },
        '@tanstack/react-query': {
            useQueryClient: () => ({ getQueryCache: () => cache, getMutationCache: () => cache }),
            isCancelledError: error => error?.name === 'CancelledError',
        },
        '@/contexts/UserContext': { useUser: () => value },
    });
    const settle = async () => { for (let i = 0; i < 10; i++) await new Promise(resolve => setImmediate(resolve)); };
    const renderAll = async () => {
        value = provider.render(UserProvider, { children: null }).props.value;
        check.render(SessionCheck, {});
        provider.effects();
        check.effects();
        await settle();
    };
    // Moves the clock a second at a time, running the timers that come due.
    const advance = async ms => {
        for (let step = 0; step < ms; step += 1000) {
            now += 1000;
            for (const [key, timer] of [...timers].filter(([, due]) => due.at <= now)) {
                timers.delete(key);
                timer.callback();
                await settle();
            }
        }
    };
    return {
        asked,
        async signIn() {
            await renderAll();  // asks as it starts
            await renderAll();  // signed in: the watchdog and SessionCheck start listening
            asked.length = 0;
        },
        offline() { reachable = false; },
        async fail(error) {
            subscribers.forEach(callback => callback({ type: 'updated', action: { type: 'error', error } }));
            await settle();
        },
        async navigate(to) { pathname = to; await renderAll(); },
        advance,
    };
}

const gaps = asked => asked.slice(1).map((at, i) => at - asked[i]);

test("a page's own request failing every second asks who is signed in at most once in 5 seconds", async t => {
    const h = pacingHarness(t);
    await h.signIn();
    for (let second = 0; second < 120; second++) {
        await h.advance(1000);
        await h.fail(new Error('Failed to fetch jobs'));
    }
    assert.ok(h.asked.length >= 1, 'a failure asks');
    assert.ok(gaps(h.asked).every(gap => gap >= 5000), `asked at ${gaps(h.asked)} ms apart`);
});

test('with the server out of reach, failures add nothing to the quiet retry: still at most once in 5 seconds', async t => {
    const h = pacingHarness(t);
    await h.signIn();
    h.offline();
    for (let second = 0; second < 120; second++) {
        await h.advance(1000);
        for (let query = 0; query < 5; query++) await h.fail(new TypeError('Failed to fetch'));  // every query gives up
    }
    assert.ok(h.asked.length >= 2, 'the retry keeps asking');
    assert.ok(gaps(h.asked).every(gap => gap >= 5000), `asked at ${gaps(h.asked)} ms apart`);
});

test('a navigation inside the app asks at once, every time, even right after a failure asked', async t => {
    const h = pacingHarness(t);
    await h.signIn();
    await h.fail(new Error('Failed to fetch tokens'));
    assert.equal(h.asked.length, 1);
    await h.navigate('/');
    await h.navigate('/settings');
    await h.navigate('/settings/tokens');
    assert.equal(h.asked.length, 4);
    assert.ok(gaps(h.asked).every(gap => gap === 0));
});

test("an API call's error (it has a status) and a cancelled request don't ask", async t => {
    const h = pacingHarness(t);
    await h.signIn();
    await h.fail(Object.assign(new Error('Server error'), { status: 500 }));
    await h.fail(Object.assign(new Error('cancelled'), { name: 'CancelledError' }));
    await h.fail(Object.assign(new Error('aborted'), { name: 'AbortError' }));
    assert.equal(h.asked.length, 0);
});
