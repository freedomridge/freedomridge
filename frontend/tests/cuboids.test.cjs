// Run with node --test tests/*.test.cjs; no browser or live services.
/* eslint-disable @typescript-eslint/no-require-imports -- Existing CommonJS TS test loader pattern. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const { test } = require('node:test');
const ts = require('typescript');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const root = path.resolve(__dirname, '..');
const resolve = Module._resolveFilename;
Module._resolveFilename = function (specifier, ...args) {
    return resolve.call(this, specifier.startsWith('@/') ? path.join(root, specifier.slice(2)) : specifier, ...args);
};
for (const ext of ['.ts', '.tsx']) require.extensions[ext] = (module, filename) => {
    module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
        compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true, target: ts.ScriptTarget.ES2017 },
        fileName: filename,
    }).outputText, filename);
};

const { detectLabelType } = require('../lib/labels.ts');
const { projectCuboid, isCalibration } = require('../lib/geometry3d.ts');
const { CuboidRenderer } = require('../components/annotations/CuboidRenderer.tsx');
const { AnnotationRenderer } = require('../components/annotations/AnnotationRenderer.tsx');
const fixture = require('../e2e/fixtures/cuboid-projection.json');
const calibration = { type: 'calibration', ...fixture.calibration };
const cuboids = fixture.cases.map(c => c.cuboid);

function close(actual, expected, scale = 1) {
    if (Array.isArray(expected)) {
        assert.equal(actual.length, expected.length);
        expected.forEach((value, i) => close(actual[i], value, scale));
    } else if (expected === null) assert.equal(actual, null);
    else assert.ok(Math.abs(actual - expected * scale) < 1e-8, `${actual} != ${expected * scale}`);
}

test('cuboid marker wins over the classification heuristic', () => {
    assert.equal(detectLabelType(cuboids), 'cuboids');
    assert.equal(detectLabelType(cuboids[0]), null); // cuboids must be lists
    assert.equal(detectLabelType([{ label: 'Car' }]), 'classifications');
    assert.equal(detectLabelType({ label: 'scene' }), 'classification');
});

test('Python/TypeScript projection parity, including homogeneous near-plane clipping', () => {
    for (const c of fixture.cases) {
        const projected = projectCuboid(c.cuboid, calibration);
        close(projected.edges, c.edges);
        close(projected.extent, c.extent);
        close(projected.distance, c.distance);
    }
    assert.equal(projectCuboid(cuboids[1], calibration).edges.length, 8);
});

test('512 pixel thumbnails scale from calibration image_size, not the loaded image', () => {
    for (const c of fixture.cases) {
        const projected = projectCuboid(c.cuboid, calibration, 512);
        close(projected.edges, c.edges, 512 / 1242);
        close(projected.extent, c.extent, 512 / 1242);
    }
});

test('behind-camera and outside-image cuboids have no visible extent', () => {
    const behind = projectCuboid({ ...cuboids[0], center: [0, 0, -10] }, calibration);
    assert.deepEqual(behind.edges, []);
    assert.equal(behind.extent, null);
    assert.equal(projectCuboid({ ...cuboids[0], center: [1000, 0, 10] }, calibration).extent, null);
});

test('missing calibration or transforms do not crash the overlay', () => {
    assert.equal(isCalibration({}), false);
    assert.equal(isCalibration({ ...calibration, P: [NaN] }), false);
    assert.equal(projectCuboid(cuboids[2], { ...calibration, transforms: {} }), null);
    assert.equal(projectCuboid({ ...cuboids[0], center: [NaN, 0, 1] }, calibration), null);
    assert.equal(renderToStaticMarkup(React.createElement(AnnotationRenderer, {
        type: 'cuboids', data: cuboids, imageSize: { width: 512, height: 155 },
    })), '');
});

test('wireframes retain clipped edges, heading, tooltip, hidden and selected state', () => {
    const render = props => renderToStaticMarkup(React.createElement('svg', null,
        React.createElement(CuboidRenderer, { cuboids, calibration,
            imageSize: { width: 1242, height: 375 }, ...props })));
    const all = render({ selectedIndex: 1 });
    assert.equal((all.match(/data-cuboid-id=/g) || []).length, 3);
    assert.equal((all.match(/data-cuboid-edge=/g) || []).length, 32);
    assert.match(all, /data-cuboid-heading=/);
    assert.match(all, /data-cuboid-id="near" data-selected="true"/);
    assert.match(all, /Car · 14.0 m/);
    assert.match(all, /clipPath/); // Clip to the image even inside a square, letterboxed grid cell.
    assert.doesNotMatch(all, /NaN|Infinity/);
    const hidden = render({ hiddenIndices: new Set([1]) });
    assert.doesNotMatch(hidden, /data-cuboid-id="near"/);
    assert.equal((hidden.match(/data-cuboid-edge=/g) || []).length, 24);
});

test('Overlays lists cuboid fields with visibility and presence controls', () => {
    const { QueryClient, QueryClientProvider } = require('@tanstack/react-query');
    require('../contexts/ToastContext.tsx').useToast = () => ({ addToast() {} });
    require('../contexts/UserContext.tsx').useUser = () => ({ user: { role: 'admin' } });
    const { FilterSidebar } = require('../components/filter-sidebar.tsx');
    const client = new QueryClient({ defaultOptions: { queries: { gcTime: Infinity } } });
    try {
        const html = renderToStaticMarkup(React.createElement(QueryClientProvider, { client },
            React.createElement(FilterSidebar, { datasetId: 1, schema: [{ name: 'ground_truth_3d', type: 'cuboids' }],
                filters: {}, onChange() {}, activeOverlayFields: ['ground_truth_3d'], onToggleOverlay() {} })));
        assert.match(html, /Overlays/);
        assert.match(html, /title="ground_truth_3d"/);
        assert.match(html, /Hide from image/);
        assert.match(html, /aria-label="Toggle ground_truth_3d overlay"/);
        assert.match(html, /aria-pressed="true"/);
        assert.match(html, /Filter by presence of &quot;ground_truth_3d&quot;/);
    } finally { client.clear(); }
});

// A tiny hook runner exercises event/effect updates without a DOM or Playwright.
function hooks(run) {
    const saved = Object.fromEntries(['useState', 'useMemo', 'useCallback', 'useRef', 'useId', 'useEffect'].map(k => [k, React[k]]));
    const slots = [];
    let cursor = 0, effects = [];
    const changed = (old, deps) => !old || !deps || deps.some((v, i) => !Object.is(v, old[i]));
    React.useState = initial => {
        const i = cursor++;
        if (!(i in slots)) slots[i] = typeof initial === 'function' ? initial() : initial;
        return [slots[i], next => { slots[i] = typeof next === 'function' ? next(slots[i]) : next; }];
    };
    React.useMemo = (fn, deps) => {
        const i = cursor++;
        if (changed(slots[i]?.deps, deps)) slots[i] = { deps, value: fn() };
        return slots[i].value;
    };
    React.useCallback = (fn, deps) => React.useMemo(() => fn, deps);
    React.useRef = value => React.useMemo(() => ({ current: value }), []);
    React.useId = () => React.useMemo(() => 'clip', []);
    React.useEffect = (fn, deps) => {
        const i = cursor++;
        if (changed(slots[i], deps)) { effects.push(fn); slots[i] = deps; }
    };
    try { return run((Component, props) => {
        cursor = 0; effects = [];
        const tree = (Component.type || Component)(props);
        effects.forEach(fn => fn());
        return tree;
    }); } finally { Object.assign(React, saved); }
}
function nodes(tree, predicate) {
    if (!tree) return [];
    if (Array.isArray(tree)) return tree.flatMap(n => nodes(n, predicate));
    return [...(predicate(tree) ? [tree] : []), ...nodes(tree.props?.children, predicate)];
}
const rendererProps = { cuboids, calibration, imageSize: { width: 512, height: 155 } };
const boxNodes = tree => nodes(tree, n => n.props?.['data-cuboid-id'] !== undefined);

test('cuboid E2E uses the shared API helper without port assertions', () => {
    const spec = fs.readFileSync(path.join(root, 'e2e/cuboids.spec.ts'), 'utf8');
    assert.match(spec, /import .*apiFetch.* from '\.\/api'/);
    assert.doesNotMatch(spec, /8075|\.port\)/);
});

test('missing, null and non-string labels use the empty-string colour fallback', () => {
    const { generateColor } = require('../lib/annotation-utils.ts');
    for (const label of [undefined, null, 5, {}, '']) {
        const html = renderToStaticMarkup(React.createElement(CuboidRenderer, {
            ...rendererProps, cuboids: [{ ...cuboids[0], label }],
        }));
        assert.match(html, /data-cuboid-edge=/);
        assert.ok(html.includes(`stroke="${generateColor('')}"`));
    }
});

test('near-plane hit targets follow edges and grid clicks bubble', () => hooks(render => {
    let stopped = false, selected;
    const event = { stopPropagation() { stopped = true; } };
    const tree = render(CuboidRenderer, rendererProps);
    const near = boxNodes(tree).find(n => n.props['data-cuboid-id'] === 'near');
    assert.equal(nodes(near, n => n.type === 'polygon').length, 0);
    const hits = nodes(near, n => n.props?.pointerEvents === 'stroke');
    assert.equal(hits.length, fixture.cases[1].edges.length);
    for (const hit of hits) assert.equal(hit.props.vectorEffect, 'non-scaling-stroke');
    near.props.onClick(event);
    assert.equal(stopped, false);
    const modal = render(CuboidRenderer, { ...rendererProps, onSelect: index => { selected = index; } });
    boxNodes(modal)[1].props.onClick(event);
    assert.equal(stopped, true);
    assert.equal(selected, 1);
}));

test('hover rerenders reuse projections; width, calibration and boxes invalidate them', () => {
    const geometry = require('../lib/geometry3d.ts');
    const original = geometry.projectCuboid;
    let calls = 0;
    geometry.projectCuboid = (...args) => { calls++; return original(...args); };
    try { hooks(render => {
        render(CuboidRenderer, rendererProps);
        assert.equal(calls, 3);
        render(CuboidRenderer, { ...rendererProps, selectedIndex: 1, onHover() {} });
        assert.equal(calls, 3);
        const props = { ...rendererProps, imageSize: { width: 1242, height: 375 } };
        render(CuboidRenderer, props);
        assert.equal(calls, 6);
        props.calibration = { ...calibration };
        render(CuboidRenderer, props);
        assert.equal(calls, 9);
        props.cuboids = [...cuboids];
        render(CuboidRenderer, props);
        assert.equal(calls, 12);
    }); } finally { geometry.projectCuboid = original; }
});

test('hover position uses CSS pixels and has only one tooltip', () => hooks(render => {
    let hover;
    const tree = render(CuboidRenderer, { ...rendererProps, onHover: (...args) => { hover = args; } });
    const event = { clientX: 190, clientY: 95, currentTarget: {
        ownerSVGElement: { getBoundingClientRect: () => ({ left: 100, top: 50 }) },
    } };
    boxNodes(tree)[0].props.onMouseMove(event);
    assert.equal(hover[1], 90);
    assert.equal(hover[2], 45);
    assert.equal(hover[3], event);
    assert.equal(nodes(tree, n => n.type === 'title').length, 0);
}));

test('actual cuboid SVG obeys per-field toggle and showLabels gates', () => hooks(render => {
    const { AnnotationOverlay } = require('../components/annotations/AnnotationOverlay.tsx');
    const props = { imageSrc: '/fixture.png', metadata: { gt: cuboids, predictions: cuboids, calibration },
        activeOverlayFields: ['predictions'] };
    let tree = render(AnnotationOverlay, props);
    nodes(tree, n => n.type === 'img')[0].props.onLoad({ currentTarget: { naturalWidth: 1242, naturalHeight: 375 } });
    tree = render(AnnotationOverlay, props);
    const rendered = t => nodes(t, n => n.type === AnnotationRenderer && n.props.type === 'cuboids');
    assert.deepEqual(rendered(tree).map(n => n.key), ['predictions']);
    assert.equal(rendered(render(AnnotationOverlay, { ...props, activeOverlayFields: [] })).length, 0);
    assert.equal(rendered(render(AnnotationOverlay, { ...props, showLabels: false })).length, 0);
    assert.equal(rendered(render(AnnotationOverlay, { ...props, activeOverlayFields: undefined })).length, 2);
    assert.equal(rendered(tree)[0].props.onSelect, undefined); // grid clicks must bubble through the wrapper too
}));

test('modal highlights the requested field and nonzero index, including sibling navigation', () => {
    const { SampleModal } = require('../components/sample-modal.tsx');
    const { AnnotationOverlay } = require('../components/annotations/AnnotationOverlay.tsx');
    const window = global.window;
    global.window = { addEventListener() {}, removeEventListener() {} };
    try { hooks(render => {
        const props = { sample: { id: 7, filepath: 'parent.png', metadata: {
            gt: [cuboids[0]], predictions: [cuboids[1], cuboids[0]],
        } }, activeOverlayFields: [], onClose() {}, highlightCuboidId: 'front', highlightCuboidField: 'predictions' };
        render(SampleModal, props);
        const overlay = () => nodes(render(SampleModal, props), n => n.type === AnnotationOverlay)[0].props;
        assert.deepEqual(overlay().selectedAnnotation, { field: 'predictions', index: 1 });
        assert.deepEqual(overlay().activeOverlayFields, ['predictions']);
        props.highlightCuboidField = 'gt';
        render(SampleModal, props);
        assert.deepEqual(overlay().selectedAnnotation, { field: 'gt', index: 0 });
        props.highlightCuboidField = 'absent';
        render(SampleModal, props);
        assert.equal(overlay().selectedAnnotation, null);
    }); } finally { global.window = window; }
    const page = fs.readFileSync(path.join(root, 'app/datasets/[id]/page.tsx'), 'utf8');
    assert.match(page, /sample\.metadata\??\.cuboid_field/);
    assert.match(page, /highlightCuboidField=\{highlightCuboidField\}/);
});

test('duplicate producer ids and edge/heading indices have unique React keys', () => hooks(render => {
    const tree = render(CuboidRenderer, { ...rendererProps, cuboids: [cuboids[0], cuboids[0]] });
    const boxes = boxNodes(tree);
    assert.equal(new Set(boxes.map(n => n.key)).size, 2);
    for (const box of boxes) {
        const lines = nodes(box, n => n.type === 'line');
        assert.equal(new Set(lines.map(n => n.key)).size, lines.length);
    }
}));

test('modal field toggle exposes its name and pressed state without duplicate controls', () => hooks(render => {
    const { SampleModal } = require('../components/sample-modal.tsx');
    const oldWindow = global.window;
    global.window = { addEventListener() {}, removeEventListener() {} };
    try {
        const props = { sample: { id: 1, metadata: { gt: cuboids } }, activeOverlayFields: ['gt'], onClose() {} };
        const modal = render(SampleModal, props);
        const viewer = nodes(modal, n => n.type?.name === 'PropertyViewer')[0];
        const tree = viewer.type({ ...viewer.props, data: props.sample.metadata, prefix: 'metadata' });
        const toggles = nodes(tree, n => n.type === 'button' && n.props['aria-label'] === 'Toggle gt overlay');
        assert.equal(toggles.length, 1);
        assert.equal(toggles[0].props['aria-pressed'], true);
    } finally { global.window = oldWindow; }
}));
