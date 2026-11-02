// Run with node --test tests/bulk-jobs-refresh.test.cjs
/* eslint-disable @typescript-eslint/no-require-imports -- Uses the Node CommonJS loader to transpile local TS/TSX. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const { test } = require('node:test');
const ts = require('typescript');
const root = path.resolve(__dirname, '..');
const configPath = path.join(root, 'tsconfig.json');
const config = ts.readConfigFile(configPath, ts.sys.readFile);
if (config.error) throw new Error(ts.flattenDiagnosticMessageText(config.error.messageText, '\n'));
const parsedConfig = ts.parseJsonConfigFileContent(config.config, ts.sys, root);
if (parsedConfig.errors.length) throw new Error(ts.formatDiagnosticsWithColorAndContext(parsedConfig.errors, {
    getCanonicalFileName: file => file, getCurrentDirectory: () => root, getNewLine: () => '\n',
}));
const originalResolve = Module._resolveFilename;
Module._resolveFilename = function (specifier, parent, ...args) {
    const resolved = ts.resolveModuleName(specifier, parent?.filename || __filename, parsedConfig.options, ts.sys).resolvedModule;
    if (resolved && !resolved.isExternalLibraryImport && !resolved.resolvedFileName.endsWith('.d.ts')) {
        specifier = resolved.resolvedFileName;
    }
    return originalResolve.call(this, specifier, parent, ...args);
};
const refs = [];
let cursor = 0;
let history = { items: [{ id: 'old', kind: 'bulk_mutation', state: 'succeeded', finished_at: 'old' }] };
let active = { running: [], resuming: [], waiting: [] };
const calls = { invalidations: [], notifications: [], historyOptions: [] };
const fakes = {
    react: { useEffect: fn => fn(), useRef: value => refs[cursor++] ?? (refs[cursor - 1] = { current: value }), useState: value => [value, () => {}] },
    '@tanstack/react-query': {
        keepPreviousData: {},
        useQueryClient: () => ({ invalidateQueries: options => calls.invalidations.push(options) }),
        useQuery: options => {
            if (options.queryKey[0] === 'dataset-job-history') {
                calls.historyOptions.push(options);
                return { data: history };
            }
            return { data: active, isSuccess: true };
        },
        useMutation: () => ({}),
    },
    'lucide-react': { Activity: () => null },
    '@/hooks/useSampleCount': { SAMPLE_COUNT_KEY: 'samples-count' },
    '@/contexts/ToastContext': { useToast: () => ({ addToast() {} }) },
    '@/lib/api': { ApiError: class {}, notifySamplesMutated: id => calls.notifications.push(id) },
    'react/jsx-runtime': { jsx: () => null, jsxs: () => null, Fragment: 'f' },
};
const originalLoad = Module._load;
Module._load = function (request, ...args) {
    return Object.hasOwn(fakes, request) ? fakes[request] : originalLoad.call(this, request, ...args);
};
for (const extension of ['.ts', '.tsx']) {
    require.extensions[extension] = (module, filename) => {
        module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
            compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true }, fileName: filename,
        }).outputText, filename);
    };
}
const { DatasetJobs } = require(path.join(root, 'components/dataset-jobs.tsx'));
function render(id = 7) { cursor = 0; DatasetJobs({ datasetId: id }); }

test('the loader resolves tsconfig aliases for hooks, libraries and TSX components', () => {
    for (const file of ['hooks/useSampleCount.ts', 'lib/api.ts', 'components/dataset-jobs.tsx']) {
        const specifier = '@/' + file.replace(/\.tsx?$/, '');
        assert.equal(require.resolve(specifier), path.join(root, file));
    }
    assert.equal(require('@/hooks/useSampleCount').SAMPLE_COUNT_KEY, 'samples-count');
});

test('history initializes quietly, refreshes new completions once, and only invalidates this dataset', () => {
    render();
    assert.deepEqual(calls.notifications, []);
    assert.deepEqual(calls.invalidations, []);
    assert.equal(calls.historyOptions.at(-1).refetchInterval, 30000); // External jobs still refresh.
    history = { items: [...history.items, { id: 'fast', kind: 'bulk_mutation', state: 'succeeded', finished_at: 'new' }] };
    render();
    assert.deepEqual(calls.notifications, [7]);
    const { predicate } = calls.invalidations.at(-1);
    assert.equal(predicate({ queryKey: ['samples', 7] }), true);
    assert.equal(predicate({ queryKey: ['field-stats', 7] }), true);
    assert.equal(predicate({ queryKey: ['samples', 17, 7] }), false);
    assert.equal(predicate({ queryKey: ['dataset-active-jobs', 7] }), false);
    render();
    assert.deepEqual(calls.notifications, [7]);
    active = { running: [{ id: 'running', kind: 'bulk_mutation', progress: { done: 0 } }], resuming: [], waiting: [] };
    render();
    assert.equal(calls.historyOptions.at(-1).refetchInterval, 5000);
    active = { running: [], resuming: [], waiting: [] };
    render();
    assert.deepEqual(calls.invalidations.at(-1), { queryKey: ['dataset-job-history', 7] });
    assert.equal(calls.historyOptions.at(-1).refetchInterval, 30000); // External jobs still refresh.
    refs.length = 0;
    render();
    assert.deepEqual(calls.notifications, [7]); // Remount does not refresh past results.
    render(17);
    assert.deepEqual(calls.notifications, [7]);
});
