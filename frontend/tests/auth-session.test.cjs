// Run with: node --test tests/auth-session.test.cjs
/* eslint-disable @typescript-eslint/no-require-imports -- Transpile the real providers with the Node CommonJS loader. */
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
    const effects = [];
    const timers = new Map();
    const window = new EventTarget();
    window.location = { pathname: '/datasets/1', search: '?q=a', href: '/datasets/1?q=a' };
    const document = new EventTarget();
    document.visibilityState = 'visible';
    const oldWindow = global.window;
    const oldDocument = global.document;
    global.window = window;
    global.document = document;
    t.mock.method(global, 'setTimeout', callback => { const key = {}; timers.set(key, callback); return key; });
    t.mock.method(global, 'clearTimeout', key => timers.delete(key));
    t.mock.method(global, 'setInterval', () => 1);
    t.mock.method(global, 'clearInterval', () => {});
    let status = 200;
    const guest = { id: 7, username: 'demo-guest', role: 'guest' };
    t.mock.method(global, 'fetch', async () => {
        if (status === 'network') throw new TypeError('Failed to fetch');
        return new Response(JSON.stringify(guest), { status });
    });
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
            if (slots[i] && deps.every((value, index) => Object.is(value, slots[i].deps[index]))) return;
            slots[i]?.cleanup?.();
            slots[i] = { deps };
            effects.push(() => { slots[i].cleanup = effect(); });
        },
    };
    const cache = () => ({ subscribe: callback => { subscribers.push(callback); return () => {}; } });
    const subscribers = [];
    let options;
    const client = { clear: t.mock.fn(), getQueryCache: cache, getMutationCache: cache };
    const query = {
        useQueryClient: () => client,
        QueryClient: function (config) { options = config; return client; },
        QueryClientProvider: () => null,
    };
    const navigation = { useRouter: () => ({ push: t.mock.fn() }) };
    const { UserProvider } = load('contexts/UserContext.tsx', { react, 'next/navigation': navigation, '@tanstack/react-query': query });
    const api = load('lib/api.ts');
    const { Providers } = load('app/providers.tsx', {
        react, '@tanstack/react-query': query, '@/contexts/UserContext': { UserProvider },
        '@/contexts/ToastContext': { ToastProvider: () => null },
        '@/lib/api': api, '@/lib/query-notify': { coalesceQueryNotifications() {} }, '@/lib/links': load('lib/links.ts', { './filter-query': load('lib/filter-query.ts') }),
    });
    t.after(() => {
        slots.forEach(slot => slot?.cleanup?.());
        global.window = oldWindow;
        global.document = oldDocument;
    });
    return {
        guest, timers, window, client, subscribers, api,
        status(value) { status = value; },
        render() { cursor = 0; return UserProvider({ children: null }).props.value; },
        providers() { cursor = 0; Providers({ children: null }); return options; },
        effects() { effects.splice(0).forEach(effect => effect()); },
    };
}

for (const failure of [503, 500, 429, 403, 'network']) {
    test(`auth/me ${failure} keeps the guest and retries quietly`, async t => {
        const h = harness(t);
        await h.render().checkAuth();
        assert.deepEqual(h.render().user, h.guest);
        const expired = t.mock.fn();
        h.window.addEventListener('fridge:unauthorized', expired);
        h.status(failure);
        await h.render().checkAuth();
        assert.deepEqual(h.render().user, h.guest);
        assert.equal(h.render().isAuthenticated, true);
        assert.equal(h.timers.size, 1);
        assert.equal(expired.mock.callCount(), 0);
        h.status(200);
        [...h.timers.values()][0]();
        await h.render().checkAuth();
        assert.deepEqual(h.render().user, h.guest);
        assert.equal(h.timers.size, 0);
    });
}

test('an initial 503 keeps AuthGuard loading until a quiet retry identifies the guest', async t => {
    const h = harness(t);
    h.status(503);
    await h.render().checkAuth();
    assert.equal(h.render().isLoading, true);
    h.status(200);
    [...h.timers.values()][0]();
    await h.render().checkAuth();
    assert.equal(h.render().isLoading, false);
    assert.deepEqual(h.render().user, h.guest);
});

test('only a 401 clears the guest and announces an expired session', async t => {
    const h = harness(t);
    await h.render().checkAuth();
    const expired = t.mock.fn();
    h.window.addEventListener('fridge:unauthorized', expired);
    h.status(401);
    await h.render().checkAuth();
    assert.equal(h.render().user, null);
    assert.equal(h.render().isLoading, false);
    assert.equal(expired.mock.callCount(), 1);
    assert.equal(h.timers.size, 0);
});

test('the image-error watchdog preserves the guest on 503 and schedules a retry', async t => {
    const h = harness(t);
    h.render();
    h.effects();
    await h.render().checkAuth();
    h.render();
    h.effects();
    h.status(503);
    h.window.dispatchEvent(new Event('fridge:check-session'));
    await h.render().checkAuth();
    assert.deepEqual(h.render().user, h.guest);
    assert.equal(h.timers.size, 1);
});

test('API and provider errors redirect only for 401, and queries retry 429', t => {
    const h = harness(t);
    const options = h.providers();
    h.effects();
    for (const status of [503, 500, 429, 403]) {
        const error = new h.api.ApiError(status, 'Temporary error');
        h.subscribers.forEach(callback => callback({ type: 'updated', query: { state: { status: 'error', error } }, mutation: { state: { status: 'error', error } } }));
        assert.equal(h.window.location.href, '/datasets/1?q=a');
        assert.equal(h.client.clear.mock.callCount(), 0);
    }
    assert.equal(options.defaultOptions.queries.retry(0, new h.api.ApiError(429, 'Busy')), true);
    assert.equal(options.defaultOptions.queries.retry(3, new h.api.ApiError(429, 'Busy')), false);
    new h.api.ApiError(401, 'Expired');
    // The login page comes back to the page the session expired on.
    assert.equal(h.window.location.href, '/login?expired=true&next=%2Fdatasets%2F1%3Fq%3Da');
    assert.equal(h.client.clear.mock.callCount(), 1);
});
