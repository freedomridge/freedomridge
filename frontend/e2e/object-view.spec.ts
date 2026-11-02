import { test, expect } from '@playwright/test';
import { AUTH_STATE } from './helpers';

// The object/patch VIEW: objects are media_type='object' rows in the SAME dataset as
// their parent image. The grid "Objects" toggle flips the grain — count, cells, and the
// embedding scatter all switch to the object grain, while staying one dataset.
//
// Validates against the real open-images-v7 dataset (image rows + re-homed object rows).
// Skips cleanly if that dataset isn't present (so CI, which has no such dataset, is green).
test.use({ storageState: AUTH_STATE });

test('object view: grain toggle flips the grid between images and objects', async ({ page }) => {
    const datasets = await (await page.request.get('/api/datasets')).json();
    const ds = datasets.find((d: { name: string }) => d.name === 'open-images-v7');
    test.skip(!ds, 'open-images-v7 dataset not present');

    await page.goto(`/datasets/${ds.id}`);

    // Default = image grain: objects are excluded from the image grid. Wait for the grid's
    // first fetch to settle on a NON-zero count (it shows "0 images" momentarily on mount).
    const count = page.getByTestId('grid-view-count');
    await expect(count).toHaveText(/[1-9][\d,]*\s+image/, { timeout: 20_000 });
    const imageCount = parseInt((await count.textContent())!.replace(/[^0-9]/g, ''), 10);

    // Toggle into the object view.
    await page.getByTestId('grain-toggle').getByRole('button', { name: 'Objects' }).click();

    // Count switches to objects, and there are more objects than images (multiple boxes/img).
    // The grid's count refetch lags the label switch, so poll for the object count.
    await expect.poll(async () => {
        const t = (await count.textContent()) || '';
        return /object/.test(t) ? parseInt(t.replace(/[^0-9]/g, ''), 10) : 0;
    }, { timeout: 20_000 }).toBeGreaterThan(imageCount);

    // Object cells render (the crop images load).
    await expect(page.locator('img').first()).toBeVisible({ timeout: 20_000 });

    // Toggle back to images — count returns.
    await page.getByTestId('grain-toggle').getByRole('button', { name: 'Images' }).click();
    await expect(count).toHaveText(new RegExp(`${imageCount.toLocaleString()} image`), { timeout: 20_000 });
});

test('cross-grain link: a filter on images drives the OBJECT grid to those images\' objects', async ({ page }) => {
    const datasets = await (await page.request.get('/api/datasets')).json();
    const ds = datasets.find((d: { name: string }) => d.name === 'open-images-v7');
    test.skip(!ds, 'open-images-v7 dataset not present');

    // Grab 3 image ids to filter by.
    const res = await page.request.post(`/api/datasets/${ds.id}/samples/query`, {
        data: {
            filters: { logic: 'AND', filters: [{ field: 'media_type', operator: '!=', value: 'object' }] },
            limit: 3,
            offset: 0,
        },
    });
    const imgIds = (await res.json()).items.map((s: { id: number }) => s.id);
    expect(imgIds.length).toBe(3);

    // Seed a persisted view-stage filter (id IN those 3 images) so the page loads already
    // filtered — exercising the SAME stagedFilters -> withGrain path the sidebar / query
    // builder / lasso feed. (Not a grid checkbox selection: those don't enter stagedFilters.)
    await page.addInitScript(([dsId, ids]: [number, number[]]) => {
        localStorage.setItem(`fridge-filters-${dsId}`, JSON.stringify({
            viewStages: [{
                type: 'filter',
                description: 'crossgrain-test',
                filter: { logic: 'AND', filters: [{ field: 'id', operator: 'in', value: ids }] },
            }],
            filterMode: 'simple', simpleFilters: {}, advancedFilters: { logic: 'AND', filters: [] },
            brushedIds: [], selectedIds: [], currentPolygon: null, selectionSource: 'Manual',
        }));
    }, [ds.id, imgIds] as [number, number[]]);

    await page.goto(`/datasets/${ds.id}`);
    const count = page.getByTestId('grid-view-count');

    // Image grain: the filter narrows the grid to exactly the 3 images.
    await expect(count).toHaveText(/(^|\s)3 images/, { timeout: 20_000 });

    // Toggle to object grain — the SAME filter now resolves to those 3 images' OBJECTS via the
    // cross-grain link: non-zero (the bug was an EMPTY object grid) and far below the full
    // object count (proving it's their objects, not all of them).
    await page.getByTestId('grain-toggle').getByRole('button', { name: 'Objects' }).click();
    await expect.poll(async () => {
        const t = (await count.textContent()) || '';
        return /object/.test(t) ? parseInt(t.replace(/[^0-9]/g, ''), 10) : -1;
    }, { timeout: 20_000 }).toBeGreaterThan(0);
    const objCount = parseInt((await count.textContent())!.replace(/[^0-9]/g, ''), 10);
    expect(objCount).toBeLessThan(2000); // 3 images' objects, not all ~11k
});

test('object view: select images -> pivot to their objects (breadcrumb)', async ({ page }) => {
    const datasets = await (await page.request.get('/api/datasets')).json();
    const ds = datasets.find((d: { name: string }) => d.name === 'open-images-v7');
    test.skip(!ds, 'open-images-v7 dataset not present');

    await page.goto(`/datasets/${ds.id}`);
    const count = page.getByTestId('grid-view-count');
    await expect(count).toHaveText(/[1-9][\d,]*\s+image/, { timeout: 20_000 });
    const imageCount = parseInt((await count.textContent())!.replace(/[^0-9]/g, ''), 10);

    // Select all images, then pivot to their objects.
    await page.getByTestId('grid-select-all').click();
    await page.getByRole('button', { name: /Show Objects/ }).click();

    // Now in object grain, scoped to those images, with a breadcrumb naming the count.
    await expect(count).toHaveText(/[1-9][\d,]*\s+object/, { timeout: 20_000 });
    await expect(page.getByRole('button', { name: new RegExp(`Objects of ${imageCount.toLocaleString()} image`) }))
        .toBeVisible({ timeout: 20_000 });
});

test('grain toggle KEEPS the clip_img embeddings selection (it cross-links to objects)', async ({ page }) => {
    const datasets = await (await page.request.get('/api/datasets')).json();
    const ds = datasets.find((d: { name: string }) => d.name === 'open-images-v7');
    test.skip(!ds, 'open-images-v7 dataset not present');

    await page.goto(`/datasets/${ds.id}`);
    const count = page.getByTestId('grid-view-count');
    await expect(count).toHaveText(/[1-9][\d,]*\s+image/, { timeout: 20_000 });

    // Make a clip_img (image-grain) scatter: pick the field, wait for points.
    const fieldSelect = page.locator('select').filter({ has: page.locator('option[value="clip_img"]') });
    await fieldSelect.selectOption('clip_img');
    await expect(page.getByTestId('embeddings-point-count')).toBeVisible({ timeout: 20_000 });

    // Lasso the left half of the scatter -> an "Embeddings region" selection.
    const canvas = page.getByTestId('embeddings-canvas');
    const box = await canvas.boundingBox();
    if (!box) throw new Error('embeddings canvas has no bounding box');
    const x = (fx: number) => box.x + box.width * fx;
    const y = (fy: number) => box.y + box.height * fy;
    await page.mouse.move(x(0.03), y(0.03));
    await page.keyboard.down('Shift');
    await page.mouse.down();
    await page.mouse.move(x(0.6), y(0.03), { steps: 8 });
    await page.mouse.move(x(0.6), y(0.97), { steps: 8 });
    await page.mouse.move(x(0.03), y(0.97), { steps: 8 });
    await page.mouse.move(x(0.03), y(0.03), { steps: 8 });
    await page.mouse.up();
    await page.keyboard.up('Shift');

    // The region selection is active (chip in the shared filter bar).
    await expect(page.getByText('Embeddings region')).toBeVisible({ timeout: 10_000 });

    // Toggle the grid to objects. THE BUG: this used to clear the region selection.
    await page.getByTestId('grain-toggle').getByRole('button', { name: 'Objects' }).click();

    // The region MUST persist (regression guard) ...
    await expect(page.getByText('Embeddings region')).toBeVisible();
    // ... and it must drive the object grid via the cross-grain link: a non-empty object
    // count (the clip_img region resolves to those images' objects, not an empty grid).
    await expect.poll(async () => {
        const t = (await count.textContent()) || '';
        return /object/.test(t) ? parseInt(t.replace(/[^0-9]/g, ''), 10) : 0;
    }, { timeout: 20_000 }).toBeGreaterThan(0);
});

test('object similarity ranks the cross-grain image grid by each image\'s best object', async ({ page }) => {
    const datasets = await (await page.request.get('/api/datasets')).json();
    const ds = datasets.find((d: { name: string }) => d.name === 'open-images-v7');
    test.skip(!ds, 'open-images-v7 dataset not present');

    // Run a real clip_obj similarity search from a seed object (what "Find Similar" does).
    const seed = (await (await page.request.post(`/api/datasets/${ds.id}/samples/query`, {
        data: { filters: { logic: 'AND', filters: [{ field: 'media_type', operator: '=', value: 'object' }] }, limit: 1 },
    })).json()).items[0];
    const results = await (await page.request.post('/api/search', {
        data: { dataset_id: ds.id, sample_id: seed.id, field_name: 'clip_obj', limit: 20 },
    })).json();
    const objIds: number[] = results.map((s: { id: number }) => s.id);

    // The expected image order: dedupe parent_ids in rank order — each image's best object decides
    // its rank. This is exactly what handleSimilaritySearch stores as parentRankIds.
    const seen = new Set<number>();
    const parentRankIds: number[] = [];
    const parentBoxes: Record<number, [number, number, number, number]> = {};
    for (const s of results as Array<{ metadata?: { parent_id?: number | string; crop_bbox?: number[] } }>) {
        const pid = Number(s.metadata?.parent_id);
        if (Number.isFinite(pid) && !seen.has(pid)) {
            seen.add(pid);
            parentRankIds.push(pid);
            const cb = s.metadata?.crop_bbox;
            if (Array.isArray(cb) && cb.length === 4) parentBoxes[pid] = cb as [number, number, number, number];
        }
    }
    expect(parentRankIds.length).toBeGreaterThan(3);

    // Seed the SAME similarity view-stage the search produces (object ids in the filter +
    // parentRankIds + parentBoxes in metadata), then load in image grain — the cross-grain grid.
    await page.addInitScript(([dsId, ids, parents, boxes]: [number, number[], number[], Record<number, number[]>]) => {
        localStorage.setItem(`fridge-filters-${dsId}`, JSON.stringify({
            viewStages: [{
                type: 'similarity',
                description: `Similar objects (${ids.length})`,
                filter: { logic: 'AND', filters: [{ field: 'id', operator: 'in', value: ids }] },
                metadata: { parentRankIds: parents, parentBoxes: boxes },
            }],
            filterMode: 'simple', simpleFilters: {}, advancedFilters: { logic: 'AND', filters: [] },
            brushedIds: [], selectedIds: [], currentPolygon: null, selectionSource: 'Manual',
        }));
    }, [ds.id, objIds, parentRankIds, parentBoxes] as [number, number[], number[], Record<number, number[]>]);

    await page.goto(`/datasets/${ds.id}`);
    // Image grain (default): the cross-grain grid shows the parent images, ranked.
    await expect(page.getByTestId('grid-view-count')).toHaveText(/[1-9][\d,]*\s+image/, { timeout: 20_000 });
    const cells = page.getByTestId('grid-cell');
    await expect(cells.first().locator('img').first()).toBeVisible({ timeout: 20_000 });

    // The matching object's box is outlined on each parent image (SVG rect from AnnotationOverlay).
    await expect(cells.first().locator('svg rect').first()).toBeVisible({ timeout: 20_000 });

    // Read the first few cells' sample ids (from the media URL) — they must match the head of
    // parentRankIds, i.e. the image with the top-ranked object comes first. The bug was the
    // image grid falling back to default (id) order because the rank list held OBJECT ids.
    const n = Math.min(4, parentRankIds.length);
    const got: number[] = [];
    for (let i = 0; i < n; i++) {
        const src = await cells.nth(i).locator('img').first().getAttribute('src');
        const m = src?.match(/\/samples\/(\d+)\/media/);
        got.push(m ? Number(m[1]) : -1);
    }
    expect(got).toEqual(parentRankIds.slice(0, n));
});

test('image similarity ranks the cross-grain OBJECT grid by each object\'s parent image', async ({ page }) => {
    const datasets = await (await page.request.get('/api/datasets')).json();
    const ds = datasets.find((d: { name: string }) => d.name === 'open-images-v7');
    test.skip(!ds, 'open-images-v7 dataset not present');

    // A clip_img (image-grain) similarity search — ranks images. The OBJECT grid must inherit it:
    // each object ranked by its parent image's position (the mirror of the object->image case).
    const seedImg = (await (await page.request.post(`/api/datasets/${ds.id}/samples/query`, {
        data: { filters: { logic: 'AND', filters: [{ field: 'media_type', operator: '!=', value: 'object' }] }, limit: 1 },
    })).json()).items[0].id;
    const results = await (await page.request.post('/api/search', {
        data: { dataset_id: ds.id, sample_id: seedImg, field_name: 'clip_img', limit: 15 },
    })).json();
    const imgIds: number[] = results.map((s: { id: number }) => s.id);
    const rankPos = new Map<number, number>(imgIds.map((id, i) => [id, i]));

    // Seed the image-similarity stage (image ids, NO parentRankIds -> not an object search).
    await page.addInitScript(([dsId, ids]: [number, number[]]) => {
        localStorage.setItem(`fridge-filters-${dsId}`, JSON.stringify({
            viewStages: [{
                type: 'similarity',
                description: 'Similarity Search',
                filter: { logic: 'AND', filters: [{ field: 'id', operator: 'in', value: ids }] },
            }],
            filterMode: 'simple', simpleFilters: {}, advancedFilters: { logic: 'AND', filters: [] },
            brushedIds: [], selectedIds: [], currentPolygon: null, selectionSource: 'Manual',
        }));
    }, [ds.id, imgIds] as [number, number[]]);

    await page.goto(`/datasets/${ds.id}`);
    const count = page.getByTestId('grid-view-count');
    await expect(count).toHaveText(/[1-9][\d,]*\s+image/, { timeout: 20_000 });

    // Toggle to the cross-grain object grid.
    await page.getByTestId('grain-toggle').getByRole('button', { name: 'Objects' }).click();
    await expect.poll(async () => {
        const t = (await count.textContent()) || '';
        return /object/.test(t) ? parseInt(t.replace(/[^0-9]/g, ''), 10) : 0;
    }, { timeout: 20_000 }).toBeGreaterThan(0);

    // Read the first object cells (their object ids from the crop media URL).
    const cells = page.getByTestId('grid-cell');
    await expect(cells.first().locator('img').first()).toBeVisible({ timeout: 20_000 });
    const k = 10;
    const objIds: number[] = [];
    for (let i = 0; i < k; i++) {
        const src = await cells.nth(i).locator('img').first().getAttribute('src');
        const m = src?.match(/\/samples\/(\d+)\/media/);
        if (m) objIds.push(Number(m[1]));
    }
    // Look up each object's parent image, then its rank position.
    const q = await (await page.request.post(`/api/datasets/${ds.id}/samples/query`, {
        data: { filters: { logic: 'AND', filters: [{ field: 'id', operator: 'in', value: objIds }] }, limit: k },
    })).json();
    const parentOf = new Map<number, number>(
        q.items.map((it: { id: number; metadata?: { parent_id?: number | string } }) => [it.id, Number(it.metadata?.parent_id)]),
    );
    const positions = objIds.map(id => rankPos.get(parentOf.get(id) ?? -1) ?? 9999);

    // Objects must be grouped by their parent image's rank (non-decreasing) — the bug was default
    // order, since object rows' own ids never appear in the IMAGE rank list. Span >1 image rank to
    // prove it's genuinely cross-image-ordered, not all from one image.
    for (let i = 0; i < positions.length - 1; i++) {
        expect(positions[i]).toBeLessThanOrEqual(positions[i + 1]);
    }
    expect(Math.max(...positions)).toBeGreaterThan(0);
});

test('object view: "Show Images" pivots selected objects to their parent images', async ({ page }) => {
    const datasets = await (await page.request.get('/api/datasets')).json();
    const ds = datasets.find((d: { name: string }) => d.name === 'open-images-v7');
    test.skip(!ds, 'open-images-v7 dataset not present');

    await page.goto(`/datasets/${ds.id}`);
    const count = page.getByTestId('grid-view-count');
    await expect(count).toHaveText(/[1-9][\d,]*\s+image/, { timeout: 20_000 });

    // Flip to objects and select one.
    await page.getByTestId('grain-toggle').getByRole('button', { name: 'Objects' }).click();
    await expect.poll(async () => {
        const t = (await count.textContent()) || '';
        return /object/.test(t) ? parseInt(t.replace(/[^0-9]/g, ''), 10) : 0;
    }, { timeout: 20_000 }).toBeGreaterThan(0);
    const firstCell = page.getByTestId('grid-cell').first();
    await firstCell.scrollIntoViewIfNeeded();
    await firstCell.hover();
    await firstCell.getByTestId('cell-checkbox').click();
    await expect(page.getByText(/1 selected/).first()).toBeVisible({ timeout: 10_000 });

    // Show Images — the mirror of Show Objects: flip to image grain showing the parent(s).
    await page.getByRole('button', { name: /Show Images/ }).click();
    await expect(count).toHaveText(/[1-9][\d,]*\s+image/, { timeout: 20_000 });
    const n = parseInt((await count.textContent())!.replace(/[^0-9]/g, ''), 10);
    expect(n).toBeGreaterThanOrEqual(1);
    expect(n).toBeLessThan(2000); // the selected object's parent image(s), not all images
});
