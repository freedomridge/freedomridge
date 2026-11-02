# Panels & Filters Architecture

How panels work, how filters flow, and how to add new panels.

## Core Files

| File | Purpose |
|------|---------|
| `frontend/components/panel-content.tsx` | `PanelType` union, panel frame (title, dropdown, split/close) |
| `frontend/components/panel-layout.tsx` | Recursive tree layout, split/close/resize, localStorage persistence |
| `frontend/app/datasets/[id]/page.tsx` | `renderPanel` switch — instantiates panels with props |
| `frontend/contexts/FilterContext.tsx` | Filter state: globalFilters, stagedFilters, viewStages, selections |
| `frontend/lib/filter-utils.ts` | Conversion helpers (simple → complex, stage builders) |
| `frontend/lib/api.ts` | `ComplexFilter`, `ViewStage`, `SchemaField` types, fetch functions |

## Filter Model

### ComplexFilter Type

All filters are trees of `ComplexFilter` nodes:

```typescript
interface ComplexFilter {
  // Logic node (branch)
  logic?: 'AND' | 'OR' | 'NOT';
  filters?: ComplexFilter[];
  // Leaf node (condition)
  field?: string;
  operator?: '=' | '!=' | '>' | '<' | '>=' | '<=' | 'in' | 'not in' | 'contains' | 'geo_polygon' | 'is_null';
  value?: any;
}
```

### Three Filter Levels

```
┌─────────────────────────────────────────────────────────────────┐
│  globalFilters                                                  │
│  = AND of all applied viewStages                                │
│  Used by: embeddings, map, duplicates, histogram, sparse sample │
├─────────────────────────────────────────────────────────────────┤
│  stagedFilters                                                  │
│  = globalFilters + draft sidebar filters + transient selections │
│  Used by: image grid ONLY                                       │
├─────────────────────────────────────────────────────────────────┤
│  Draft state (simpleFilters / advancedFilters)                  │
│  = sidebar edits not yet applied                                │
│  Used by: filter sidebar preview                                │
└─────────────────────────────────────────────────────────────────┘
```

**globalFilters** — The applied view. Computed from `viewStages` (an array of `ViewStage` objects). All panels except the grid query with this. Empty = full dataset.

**stagedFilters** — globalFilters + in-progress edits. Includes draft sidebar filters AND transient selections (brushedIds, selectedIds, currentPolygon). Only the grid uses this, giving real-time preview as the user interacts.

**Draft state** — Sidebar filter edits (`simpleFilters` or `advancedFilters`). Not applied until user clicks Apply. Preserved on Reset.

### Key Rule

**Panels query with `globalFilters`. The grid queries with `stagedFilters`.** Never pass `stagedFilters` to non-grid panels.

### View Stages

Each applied filter is a `ViewStage` stored in the `viewStages` array:

```typescript
interface ViewStage {
  type: 'filter' | 'selection' | 'map_region' | 'similarity';
  description: string;      // Shown in query builder bar chip
  filter: ComplexFilter;     // The SQL condition this stage adds
  metadata?: any;            // Optional context (count, source)
}
```

Stage types and how they're created:

| Type | Created by | Filter shape |
|------|-----------|-------------|
| `filter` | Sidebar Apply | `{ field, operator, value }` or `{ logic: 'AND', filters: [...] }` |
| `selection` | Embeddings brush, map click, grid select | `{ field: 'id', operator: 'in', value: [ids] }` |
| `map_region` | Map polygon draw | `{ field: 'location', operator: 'geo_polygon', value: [coords] }` |
| `similarity` | Similarity search | `{ field: 'id', operator: 'in', value: [ids] }` |

`globalFilters` = AND of all stage filters. Stages appear as chips in the `QueryBuilderBar`. Each chip is editable (opens `FilterStageEditor`) and removable.

### Selection State

Two selection channels exist, both flow into `stagedFilters`:

| State | Set by | Purpose |
|-------|--------|---------|
| `brushedIds` | Embeddings lasso, duplicates panel, sparse sample | Bulk selection from a panel interaction |
| `selectedIds` | Grid checkboxes, map point clicks | Individual sample toggles |

When `applyFilters()` runs, these get merged into a `selection` stage and promoted to `globalFilters`.

### Apply / Reset / Clear

- **`applyFilters()`** — Converts draft state + selections into viewStages. Clears draft state. All panels now reflect the new view.
- **`resetFilters()`** — Clears all viewStages (globalFilters becomes empty). Does NOT clear sidebar drafts.
- **`clearSelection()`** — Clears `selectedIds` only. Preserves brushedIds and other state.

## Panel Layout

Panels are organized as a recursive binary tree:

- **Leaf node** = single panel with a `PanelType`
- **Group node** = horizontal or vertical split with children

Users split panels via header buttons (split right, split down). Sizes persist to localStorage per dataset. Default layout: Filters (20%) | Grid (50%) | Embeddings (30%).

## Adding a New Panel

### Step 1: Register the Panel Type

**`frontend/components/panel-content.tsx`** — Add to the union and dropdown:

```typescript
// Line 4: Add to union
export type PanelType = '...' | 'my-panel';

// Line 26-32: Add dropdown option
<option value="my-panel">My Panel</option>
```

### Step 2: Create the Panel Component

**`frontend/components/my-panel.tsx`**:

```typescript
interface MyPanelProps {
  datasetId: number;
  filters: ComplexFilter;              // Always globalFilters
  onSelection?: (ids: number[]) => void;  // If panel can select samples
  // Add schema, selectedIds, etc. as needed
}

export function MyPanel({ datasetId, filters, onSelection }: MyPanelProps) {
  // Query data with globalFilters
  const { data } = useQuery({
    queryKey: ['my-data', datasetId, filters],
    queryFn: () => fetchMyData(datasetId, filters),
    staleTime: 30_000,
  });

  // If your panel selects samples, call onSelection
  const handleSelect = (ids: number[]) => {
    onSelection?.(ids);  // Flows to setBrushedIds → stagedFilters → grid updates
  };

  return <div>...</div>;
}
```

### Step 3: Wire into Page

**`frontend/app/datasets/[id]/page.tsx`**:

```typescript
// Import
import { MyPanel } from '@/components/my-panel';

// In renderPanel switch:
case 'my-panel':
  return (
    <div className="h-full w-full overflow-hidden bg-zinc-900">
      <MyPanel
        datasetId={datasetId!}
        filters={globalFilters}
        onSelection={(ids) => {
          setSelectionSource('My Panel');
          setBrushedIds(ids);
        }}
      />
    </div>
  );
```

### Step 4 (if needed): Add Backend Endpoint

If your panel needs custom data, add a router in `backend/routers/`:

```python
# backend/routers/my_feature.py
from fastapi import APIRouter, Depends
from ..access import require_dataset_access
from ..database import db
from .datasets import parse_filter_node, get_array_columns

# main.py has no global login check. This dependency returns 401 without a login and
# 403 when the user may not see the dataset; every dataset route needs it.
router = APIRouter(dependencies=[Depends(require_dataset_access)])

@router.post("/datasets/{dataset_id}/my-endpoint")
async def my_endpoint(dataset_id: int, request: MyRequest):
    conditions = ["s.dataset_id = $1"]
    args = [dataset_id]
    arg_idx = 2

    if request.filters:
        sql, arg_idx = parse_filter_node(request.filters, args, arg_idx, field_mapping={'id': 's.id'})
        if sql and sql != "TRUE":
            conditions.append(sql)

    where_clause = " WHERE " + " AND ".join(conditions)
    rows = await db.fetch(f"SELECT ... FROM samples s {where_clause}", *args)
    return {...}
```

`require_dataset_access` reads the dataset ID from the `{dataset_id}` (or `{id}`) path
parameter. An endpoint that changes data also needs a role check, as the real routers
do: add `user: dict = Depends(require_role("power_user"))` (from `.auth`) to its
parameters. For a `POST` that only reads, like this one, also add its function name
(`my_endpoint`) to `_READ_ONLY_DATASET_POSTS` in `backend/access.py`; otherwise it
returns 409 while the dataset is being deleted.

Register in `backend/main.py`:
```python
from .routers import ..., my_feature
app.include_router(my_feature.router, prefix="/api")
```

Add the API function in `frontend/lib/api.ts`:
```typescript
export async function fetchMyData(datasetId: number, filters?: ComplexFilter) {
  const res = await fetch(`${API_BASE}/datasets/${datasetId}/my-endpoint`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify({ filters }),
  });
  if (!res.ok) throw new Error('Failed');
  return res.json();
}
```

## Common Panel Props Reference

| Prop | Type | Source | Used by |
|------|------|--------|---------|
| `datasetId` | `number` | URL params | All panels |
| `filters` / `globalFilters` | `ComplexFilter` | FilterContext | All panels (for queries) |
| `schema` | `SchemaField[]` | React Query cache | Panels with field pickers |
| `selectedIds` | `number[]` | Page state | Grid, embeddings, duplicates |
| `onSelection` | `(ids: number[]) => void` | Page callback → `setBrushedIds` | Panels that select samples |
| `onSampleClick` | `(sample: Sample) => void` | Page callback → `setSelectedSample` | Panels that open sample modal |
| `onToggleSelection` | `(id: number) => void` | Page callback | Panels with per-sample checkboxes |

## Existing Panels

| Panel | Data source | Selection mode | Key behavior |
|-------|------------|----------------|-------------|
| **Grid** | `stagedFilters` | Checkbox per sample | Real-time preview of drafts |
| **Filters** | Draft state | N/A | Edits simpleFilters/advancedFilters |
| **Embeddings** | `globalFilters` | Lasso brush | Scatter plot, grid-stratified sampling |
| **Map** | `globalFilters` | Polygon draw, point click | PostGIS geo queries |
| **Histogram** | `globalFilters` | Bar click → draft filter | Field value distribution |
| **Duplicates** | `globalFilters` | Group selection, per-image toggle | Hash-based duplicate groups |
| **Sparse Sample** | `globalFilters` | Bulk selection on result | FPS diversity sampling |

### Histogram panel

`frontend/components/histogram-panel.tsx`, backed by `GET /api/datasets/{id}/histogram` (exact) and
`GET /api/datasets/{id}/histogram/preview` (approximate).

**Declared text is grouped by value (since 2026-10-20).** A metadata field whose registry type is
`string` gets one bar per value (top 50 plus an "others" count; `top=0` returns all), even when its
values look numeric, such as serial numbers. Earlier, the endpoint first scanned the field to see
whether at least half of its values were numbers and, if so, binned them as numbers. That scan is
skipped now, which is most of the speed-up for text fields such as `country`. The panel's
**Treat as number** switch (shown only for declared-text fields) sends `as_number=true` and gets
the old interpretation back: the same detection, bins and labels. A field whose values are mostly
not numbers (or a single number) still falls back to categories, and the panel says so. An explicit `bins` is also a
numeric request, so the SDK's `histogram_values()` is unchanged. Fields declared as numbers,
datetimes, booleans, objects or lists, undeclared fields and native columns behave as before.
One exception keeps main's behaviour: a time-named field (`*_at`, `*_date`) declared as text whose
values are mostly ISO dates (checked on a bounded sample of up to 1,000 values through the metadata
index) keeps its time bins; non-date text in such a field is grouped by value.

**Broad histograms show an approximate preview first.** An exact histogram of most of a
multi-million-sample dataset takes seconds to tens of seconds. While it computes, the panel asks the
preview endpoint for the same histogram over a random sample: 20,000 ids drawn uniformly from the
dataset's id range, looked up by primary key, filtered, then histogrammed. The preview is shown only when
the filtered set is estimated above 100,000 samples (the sidebar's fast-mode "broad" threshold)
and at least 1,000 sampled rows match. Smaller sets are the fast exact case. The preview has a
2-second budget; if it misses that, the panel just waits for the exact result.

The preview is never presented as exact. It carries an "Approximate preview — not exact" banner,
shows estimated shares (≈%) instead of counts, notes that small categories may be missing and that
numeric bin ranges come from the sample, and its bars are not buttons. Filtering is done only from
the exact histogram. On large datasets, a sample missed a country with four samples. The exact request is always
sent and replaces the preview when it arrives; if it fails, the panel offers Retry.
