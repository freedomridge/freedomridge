# Creating a 1M Sample Test Dataset

This directory contains a script to generate a large-scale test dataset for performance testing.

## Quick Start

```bash
# Generate 1M sample dataset (takes ~30-60 minutes depending on machine)
python scripts/scale_tests/create_1m_test_dataset.py

# Or customize the size
python scripts/scale_tests/create_1m_test_dataset.py --target 500000 --base-limit 5000
```

## What It Does

The `create_1m_test_dataset.py` script:

1. **Loads base samples** from BDD100K dataset (default: 10,000 samples)
2. **Duplicates with variations** to reach target count (default: 1,000,000)
3. **Generates fake embeddings** (512-dim normalized vectors) for each sample
4. **Ingests in batches** for memory efficiency (1,000 samples per batch)

## Arguments

- `--target N` - Target number of samples (default: 1,000,000)
- `--base-limit N` - Base samples to load from BDD100K (default: 10,000)
- `--username` - API username (default: admin)
- `--password` - API password (FRIDGE_PASSWORD or terminal prompt)

## How Duplication Works

Each base sample is duplicated with variations:

- **Unique filepaths**: filepaths are unique per dataset, so each copy is stored as
  `scale-test/copy-NNNN/<original path>`; the real image is in the `image` media field,
  which the grid and modal show
- **Metadata variations**: Different timestamps, slightly varied speeds
- **Fake embeddings**: Deterministic random 512-dim unit vectors, sent with each sample
- **Tracking**: Each duplicate has a `duplicate_id` field

Example: 10,000 base samples × 100 duplicates = 1,000,000 samples

## Performance Expectations

**Ingestion time estimates:**
- 100K samples: ~5 minutes
- 500K samples: ~20 minutes
- 1M samples: ~30-60 minutes

**Database size:**
- 1M samples: ~5-10 GB (depending on metadata)
- 1M embeddings: ~2 GB (512-dim float vectors)

## Testing With The Dataset

Once created, use this dataset to test:

### 1. Concurrent User Load
Run multiple clients querying simultaneously:
```python
# In separate terminals
for i in {1..20}; do
  curl "http://localhost:8000/api/datasets/X/samples?limit=50&offset=0" &
done
```

### 2. Pagination Performance
```bash
# Test different offsets
curl "http://localhost:8000/api/datasets/X/samples?limit=50&offset=0"
curl "http://localhost:8000/api/datasets/X/samples?limit=50&offset=10000"
curl "http://localhost:8000/api/datasets/X/samples?limit=50&offset=500000"
curl "http://localhost:8000/api/datasets/X/samples?limit=50&offset=999000"
```

### 3. Vector Search Performance
```bash
# Search with embeddings
curl -X POST http://localhost:8000/api/search \
  -H "Content-Type: application/json" \
  -d '{
    "dataset_id": X,
    "sample_id": 1,
    "field_name": "clip",
    "limit": 50
  }'
```

### 4. Complex Filter Performance
Test the query builder with:
- Large date ranges
- Multiple AND/OR conditions
- Metadata field queries
- Geospatial queries (if location data present)

## Cleanup

To remove the test dataset:
```bash
# Via API
curl -X DELETE http://localhost:8000/api/datasets/X

# Or via UI
# Navigate to dataset settings and click "Delete Dataset"
```

## Notes

- **Fake embeddings** are random but normalized unit vectors (like real CLIP embeddings)
- **Reuses images** from BDD100K (filepaths point to same image files)
- **Variations** make each sample unique in the database
- **Batch processing** prevents memory issues during ingestion
