# Freedom Ridge Code Style & Conventions

## Naming

### Database
- Tables: `snake_case` plural (`samples`, `dataset_versions`, `sample_array_columns`)
- Columns: `snake_case` (`dataset_id`, `field_name`, `created_at`)
- Indexes: `idx_{table}_{columns}` (`idx_samples_tags_gin`, `idx_samples_meta_1_source_id`)
- Dynamic array columns: lowercase, alphanumeric + underscores (`inferred_tags`, `categories`)

### Tags & Array Fields
- Category tags: `CATEGORY:SUBCATEGORY` (`ANIMAL:DOG`, `VEHICLE:TRUCK`, `SIGN:STOP`)
- Process tags: `PROCESS:STATUS` (`PROCESS:NOT_HELPFUL`, `PROCESS:WRONG_LABEL`)
- Quality tags: `IMAGE_QUALITY:ISSUE` (`IMAGE_QUALITY:BLURRY`, `IMAGE_QUALITY:BLOCKED`)
- Person tags: `name_descriptor` (`reviewer_batch_one`, `reviewer_batch_two`)
- Review tags: `REVIEW:REJECTED`, `CLUSTER:REJECTED`

### API Endpoints
- REST conventions: GET for reads, POST for creates/queries, PATCH for updates, DELETE
- Query endpoints use POST to handle large filter payloads
- Field naming: `snake_case` in JSON (`field_name`, `sample_ids`, `skip_count`)

### Frontend
- Components: PascalCase files and exports (`EmbeddingsPanel`, `DatasetGrid`)
- React Query keys: kebab-case strings (`'embedding-fields'`, `'grid-samples'`, `'map-points'`)
- State: camelCase (`samplePct`, `selectedIds`, `colorField`)

## Backend Patterns

### Filter Parsing
All filters go through `parse_filter_node()`. Never write custom SQL for filtering — extend the parser instead. Array fields are detected via `_get_array_columns_sync()`.

### Database Access
- Use `db.fetch/fetchrow/fetchval` for simple queries
- Use `async with db.pool.acquire() as conn:` for transactions or queries needing timeout
- Connection pool: 10-50 connections, 60s command timeout
- JSONB codec auto-encodes/decodes dicts

### Schema Deduplication
The schema endpoint has multiple code paths that can add fields. ALWAYS deduplicate at the return point:
```python
seen = set()
deduped = []
for field in schema:
    if field['name'] not in seen:
        seen.add(field['name'])
        deduped.append(field)
return deduped
```

### SQL Safety for Dynamic Column Names
Column names can't be parameterized in SQL. Use:
1. Regex validation: `^[a-z][a-z0-9_]{0,62}$`
2. Reserved name blocklist check
3. Registry table verification (`sample_array_columns`)

## Frontend Patterns

### Query Caching
Always set `staleTime` and `refetchOnWindowFocus: false`:
```tsx
useQuery({
    queryKey: ['key', datasetId, filters],
    queryFn: () => fetchData(...),
    staleTime: 30_000,
    refetchOnWindowFocus: false,
});
```

### Grid Navigation
Use a ref (not state) to track loaded samples for modal navigation. State causes re-renders on every infinite scroll page load:
```tsx
const gridSamplesRef = useRef<Sample[]>([]);
// Grid writes to ref directly — no re-renders
if (gridSamplesRef) gridSamplesRef.current = allSamples;
```

### Canvas Performance
For 500K+ points, batch by color:
```tsx
const colorBuckets = new Map<string, number[]>();
// Single pass: compute color, push coordinates
// One ctx.fill() per color bucket with ctx.rect()
```
Never use `ctx.arc()` per point at this scale.

## Git Conventions

### Commit Messages
```
feat: short description
fix: short description
perf: short description
docs: short description
revert: short description
```
Credit co-authors when applicable.

### Before Commits
- Never use `git reset --hard` without explicit permission
- Never use `git add -A` — add specific files
- Verify syntax of all changed files before committing
- Use your own Git author name and email.
