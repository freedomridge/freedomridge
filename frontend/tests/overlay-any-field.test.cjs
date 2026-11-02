/* eslint-disable @typescript-eslint/no-require-imports -- Node regression tests transpile local TSX. */
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
Module._resolveFilename = function (name, ...args) {
    return resolve.call(this, name.startsWith('@/') ? path.join(root, name.slice(2)) : name, ...args);
};
for (const ext of ['.ts', '.tsx']) require.extensions[ext] = (module, filename) => {
    module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
        compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
        fileName: filename,
    }).outputText, filename);
};
const { parseAnnotations } = require('../lib/labels.ts');
const { AnnotationOverlay } = require('../components/annotations/AnnotationOverlay.tsx');
const { PolygonRenderer } = require('../components/PolygonRenderer.tsx');
const { generateColor } = require('../lib/annotation-utils.ts');
const metadata = {
    vehicles: [{ label: 'car', bbox: [0.1, 0.1, 0.2, 0.2] }],
    pedestrians: [{ label: 'person', bbox: [0.5, 0.1, 0.2, 0.2] }],
    lanes: [{ label: 'lane', paths: [[[0, 0], [0.2, 0.2]], [[0.6, 0.6], [1, 1]]], closed: false }],
};
function render(activeOverlayFields, config, data = metadata) {
    // Start at the post-image-load state; SSR itself does not run image events/effects.
    const useState = React.useState;
    React.useState = initial => useState(
        initial?.width === 0 ? { width: 100, height: 100 }
            : Array.isArray(initial) && initial.length === 0 ? parseAnnotations(data) : initial);
    try {
        return renderToStaticMarkup(React.createElement(AnnotationOverlay, {
            imageSrc: '/fixture.png', metadata: data, activeOverlayFields, segmentationConfig: config,
        }));
    } finally { React.useState = useState; }
}
test('each field and its .label alias select only that field', () => {
    for (const field of Object.keys(metadata)) for (const key of [field, `${field}.label`]) {
        const html = render([key]);
        for (const [name, labels] of Object.entries(metadata)) {
            assert.equal(html.includes(name === 'lanes' ? 'data-label="lane"' : `>${labels[0].label}<`), name === field, key);
        }
    }
    const html = render(['vehicles', 'pedestrians', 'lanes']);
    assert.match(html, />car</);
    assert.match(html, />person</);
    assert.match(html, /data-label="lane"/);
    assert.doesNotMatch(render([]), />car<|>person<|data-label="lane"/);
});
test('legacy field and .label keys work without enabling unrelated fields', () => {
    const data = { ...metadata, detections: [{ ...metadata.vehicles[0], label: 'legacy' }], polygons: [{ ...metadata.lanes[0], label: 'old-lane' }] };
    for (const key of ['detections', 'detections.label', 'polygons', 'polygons.label']) {
        const html = render([key], undefined, data);
        assert.doesNotMatch(html, />car<|>person<|data-label="lane"/);
        assert.match(html, key.startsWith('detections') ? />legacy</ : /data-label="old-lane"/);
    }
});
test('colours prefer the field, accept its legacy .label key, and fall back to label colours', () => {
    const fields = {
        vehicles: { valueColors: { car: '#123456' } },
        'vehicles.label': { valueColors: { car: '#abcdef' } },
        pedestrians: { valueColors: { person: '#654321' } },
        detections: { valueColors: { car: '#eeeeee' } },
    };
    const html = render(['vehicles', 'pedestrians'], { color_scheme: { fields } });
    assert.match(html, /stroke="#123456"/);
    assert.match(html, /stroke="#654321"/);
    assert.doesNotMatch(html, /#eeeeee|#abcdef/);
    delete fields.vehicles;
    assert.match(render(['vehicles'], { color_scheme: { fields } }), /stroke="#abcdef"/);
    delete fields['vehicles.label'];
    assert.match(render(['vehicles'], { color_scheme: { fields } }), /stroke="#eeeeee"/);
    delete fields.detections;
    assert.ok(render(['vehicles'], { color_scheme: { fields } }).includes(`stroke="${generateColor('car')}"`));
    fields.detections = { valueColors: { car: '#eeeeee' } };
    assert.match(render(['detections.label'], { color_scheme: { fields } }, { detections: metadata.vehicles }), /stroke="#eeeeee"/);
});
test('polygon colours and mask target names use the actual field with default target fallback', () => {
    const config = { color_scheme: { fields: { lanes: { valueColors: { lane: '#123abc' } } } },
        mask_targets: { lanes: { lane: 'road marking' }, polygons: { lane: 'wrong' } },
        default_mask_targets: { lane: 'default marking' } };
    assert.match(render(['lanes'], config), /stroke="#123abc"/);
    assert.match(render(['lanes'], config), /data-label="road marking"/);
    delete config.mask_targets.lanes;
    assert.match(render(['lanes'], config), /data-label="default marking"/);
});
test('every open path is drawn, even with an area label, without closing or filling it', () => {
    const html = renderToStaticMarkup(React.createElement(PolygonRenderer, {
        polygon: { ...metadata.lanes[0], label: 'area/open' }, imageWidth: 100, imageHeight: 100,
    }));
    assert.equal((html.match(/<polyline /g) || []).length, 2);
    assert.match(html, /points="0,0 20,20"/);
    assert.match(html, /points="60,60 100,100"/);
    assert.equal((html.match(/fill="none"/g) || []).length, 2);
    assert.doesNotMatch(html, /<path|Z/);
});

test('toggling either saved alias removes every handle for just that field', () => {
    const { toggleOverlayField, isOverlayActive } = require('../lib/labels.ts');
    const active = ['vehicles.label', 'vehicles', 'pedestrians', 'lanes.label'];
    assert.deepEqual(toggleOverlayField(active, 'vehicles', metadata), ['pedestrians', 'lanes.label']);
    assert.deepEqual(toggleOverlayField(active, 'vehicles.label', metadata), ['pedestrians', 'lanes.label']);
    assert.ok(isOverlayActive('lanes', active, metadata));
    assert.deepEqual(toggleOverlayField([], 'detections.label'), ['detections']);
});

test('colour editor writes only the selected field and retains saved settings', () => {
    const { SegmentationSettingsPanel } = require('../components/SegmentationSettingsPanel.tsx');
    const config = { color_scheme: { fields: { detections: { valueColors: { car: '#111111' } } } } };
    const props = { config, labelFields: Object.entries(metadata).map(([name, labels]) => ({ name, labels: labels.map(l => l.label) })) };
    const markup = renderToStaticMarkup(React.createElement(SegmentationSettingsPanel, { ...props, onChange() {} }));
    for (const name of Object.keys(metadata)) assert.ok(markup.includes(`data-testid="overlay-colors-${name}"`));
    let saved;
    const useState = React.useState;
    React.useState = value => [value, () => {}];
    let tree;
    try { tree = SegmentationSettingsPanel({ ...props, onChange: next => { saved = next; } }); }
    finally { React.useState = useState; }
    function find(node, title) {
        if (!node) return undefined;
        if (Array.isArray(node)) return node.map(n => find(n, title)).find(Boolean);
        if (node.props?.title === title) return node;
        return find(node.props?.children, title);
    }
    find(tree, 'vehicles').props.onColorChange('car', '#abcdef');
    assert.equal(saved.color_scheme.fields.vehicles.valueColors.car, '#abcdef');
    assert.deepEqual(saved.color_scheme.fields.detections, config.color_scheme.fields.detections);
    assert.equal(saved.color_scheme.fields.pedestrians, undefined);
});

test('saved polygon colours and global opacity fallback preserve field overrides', () => {
    const { getOverlayColorConfig } = require('../lib/labels.ts');
    const config = { color_scheme: { opacity: 0.2, fields: {
        polygons: { colorByAttribute: 'label', valueColors: { lane: '#c0ffee' }, opacity: 0.8 },
    } } };
    assert.equal(getOverlayColorConfig(config, 'lanes').opacity, 0.2);
    assert.equal(getOverlayColorConfig(config, 'polygons').opacity, 0.8);
    assert.match(render(['polygons.label'], config, { polygons: metadata.lanes }), /stroke="#c0ffee"/);
    const html = renderToStaticMarkup(React.createElement(PolygonRenderer, {
        polygon: { ...metadata.lanes[0], closed: true }, imageWidth: 100, imageHeight: 100,
    }));
    assert.match(html, /fill-rule="evenodd"/);
    assert.match(html, /d="M0,0 L20,20 Z M60,60 L100,100 Z"/);
});

test('registered dotted scalar names retain their toggle, export and promotion identity', () => {
    const { overlayFieldName, isDerivedLabelField, toggleOverlayField } = require('../lib/labels.ts');
    const schema = [
        { name: 'vehicles', type: 'detections', origin: 'metadata' },
        { name: 'cam.label', type: 'enum', origin: 'metadata' },
        { name: 'gps.lat', type: 'number', origin: 'metadata' },
        { name: 'keywords', type: 'list', origin: 'metadata' },
    ];
    for (const name of ['cam.label', 'gps.lat', 'keywords.label']) {
        assert.equal(overlayFieldName(name, schema), name);
        assert.equal(isDerivedLabelField(name, schema), false);
        assert.deepEqual(toggleOverlayField([], name, schema), [name]);
    }
    assert.equal(overlayFieldName('vehicles.label', schema), 'vehicles');
    assert.equal(overlayFieldName('vehicles.label', [...schema,
        { name: 'vehicles.label', type: 'enum', origin: 'metadata' }]), 'vehicles.label');
    assert.equal(overlayFieldName('cam.label', { 'cam.label': 'front' }), 'cam.label');
    // Consumers must use the same identity check (their modal wrappers require browser contexts).
    for (const file of ['export-dialog', 'filter-sidebar', 'dataset-grid']) {
        assert.match(fs.readFileSync(path.join(root, `components/${file}.tsx`), 'utf8'), /isDerivedLabelField\(/);
    }
});

test('only object/array boxes, cuboid exclusion and keypoint precedence match discovery', () => {
    const { detectLabelType } = require('../lib/labels.ts');
    for (const value of [['bbox'], ['label'], [null], [3]]) {
        assert.equal(detectLabelType(value), null);
    }
    assert.equal(detectLabelType([{ _type: 'cuboid', bbox: [], points: [], label: '3d' }]), 'cuboids');
    for (const bbox of [null, 3, true, 'bbox']) {
        assert.equal(detectLabelType([{ bbox, label: 'class' }]), 'classifications');
    }
    assert.equal(detectLabelType([{ bbox: {}, _type: 'keypoints', points: [] }]), 'detections');
    assert.equal(detectLabelType([{ _type: 'keypoints', points: [] }]), 'keypoints');
    assert.equal(detectLabelType({ type: 'heatmap', label: 'hot', maskPath: 'm' }), 'heatmap');
});

test('legacy area polygons fill only when closed is not explicitly false', () => {
    for (const closed of [undefined, true, false]) {
        const html = renderToStaticMarkup(React.createElement(PolygonRenderer, {
            polygon: { label: 'area/road', points: [[0, 0], [1, 0], [1, 1]], closed },
            imageWidth: 100, imageHeight: 100,
        }));
        assert.equal(html.includes('<path'), closed !== false);
        assert.equal(html.includes('<polyline'), closed === false);
    }
});

test('mask target labels participate in settings choices with an empty schema', () => {
    const { segmentationLabelChoices } = require('../lib/labels.ts');
    const config = { mask_targets: { customMask: { 1: 'road', 2: 'car' }, unsetMask: null } };
    assert.deepEqual(segmentationLabelChoices([], config), ['road', 'car']);
    assert.deepEqual(segmentationLabelChoices(['road'], config), ['road', 'car']);
    assert.match(fs.readFileSync(path.join(root, 'components/dataset-settings-modal.tsx'), 'utf8'), /segmentationLabelChoices\(/);
});

test('histogram eye shows the selected alias and its click disables the overlay', () => {
    const { toggleOverlayField } = require('../lib/labels.ts');
    const load = Module._load;
    Module._load = function (name, ...args) {
        if (name === '@tanstack/react-query') return { useQuery: () => ({ data: { buckets: [], total: 0 } }) };
        if (name === '@/contexts/FilterContext') return { useFilters: () => ({ simpleFilters: {}, setSimpleFilters() {}, setFilterMode() {} }) };
        return load.call(this, name, ...args);
    };
    let HistogramPanel;
    try { ({ HistogramPanel } = require('../components/histogram-panel.tsx')); }
    finally { Module._load = load; }
    const schema = [{ name: 'vehicles', type: 'detections', origin: 'metadata' }, { name: 'vehicles.label', type: 'enum' }];
    let active = ['vehicles'];
    const saved = { useState: React.useState, useEffect: React.useEffect, useMemo: React.useMemo, useCallback: React.useCallback };
    React.useState = initial => [initial === 'id' ? 'vehicles.label' : initial, () => {}];
    React.useEffect = () => {};
    React.useMemo = fn => fn();
    React.useCallback = fn => fn;
    function find(node) {
        if (!node) return undefined;
        if (Array.isArray(node)) return node.map(find).find(Boolean);
        if (node.type === 'button' && /from image|on image/.test(node.props.title)) return node;
        return find(node.props?.children);
    }
    try {
        const props = { datasetId: 1, schema, globalFilters: {}, activeOverlayFields: active,
            onToggleOverlay: field => { active = toggleOverlayField(active, field, schema); } };
        const eye = find(HistogramPanel(props));
        assert.equal(eye.props.title, 'Hide from image');
        assert.equal(eye.props['aria-pressed'], true);
        eye.props.onClick();
        assert.deepEqual(active, []);
        const off = find(HistogramPanel({ ...props, activeOverlayFields: active }));
        assert.equal(off.props.title, 'Show on image');
    } finally { Object.assign(React, saved); }
});

test('unlabelled polylines render their segments without crashing', () => {
    for (const label of [undefined, null]) {
        const data = { lanes: [{ label, points: [[0, 0], [1, 1], [1, 0]] }] };
        const html = render(['lanes'], undefined, data);
        assert.match(html, /<polyline /);
        assert.match(html, /data-label=""/);
        assert.match(html, /fill="none"/);
    }
});

test('unlabelled detections render without crashing', () => {
    assert.match(render(['vehicles'], undefined, { vehicles: [{ bbox: [0, 0, 1, 1] }] }), /<rect /);
});

test('classification lists retain badges and field.label overlay aliases', () => {
    const { overlayFieldName, toggleOverlayField } = require('../lib/labels.ts');
    for (const field of ['ground_truth', 'predictions', 'classes']) {
        const data = { [field]: [{ label: 'dog' }] };
        for (const handle of [field, `${field}.label`]) {
            assert.match(render([handle], undefined, data), />dog</);
            assert.equal(overlayFieldName(handle, data), field);
        }
        const schema = [{ name: field, type: 'classification', origin: 'metadata' },
            { name: `${field}.label`, type: 'enum', origin: 'system' }];
        assert.deepEqual(toggleOverlayField([], `${field}.label`, schema), [field]);
    }
});
