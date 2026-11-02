import asyncio
import json
import time

from backend.database import db

# Raw queries, not parse_filter_node, so both filter shapes are timed without changing the backend.


async def benchmark():
    await db.connect()

    # Find a dataset with samples having metadata
    row = await db.fetchrow("SELECT dataset_id FROM samples WHERE metadata != '{}'::jsonb LIMIT 1")
    if not row:
        print("No samples with metadata found in any dataset.")
        await db.disconnect()
        return

    dataset_id = row['dataset_id']
    print(f"Using dataset_id: {dataset_id}")

    # 1. Old way: ->>
    # Find a field that exists.
    keys = await db.fetch(
        "SELECT DISTINCT jsonb_object_keys(metadata) as k FROM samples WHERE dataset_id = $1 LIMIT 5",
        dataset_id,
    )
    print(f"Available keys: {[k['k'] for k in keys]}")

    if not keys:
        print("No metadata keys found. Cannot benchmark.")
        await db.disconnect()
        return

    target_key = keys[0]['k']
    # Get a value
    val_row = await db.fetchrow(
        f"SELECT metadata->>'{target_key}' as v FROM samples WHERE dataset_id = $1 AND metadata->>'{target_key}' IS NOT NULL LIMIT 1",  # noqa: E501
        dataset_id,
    )
    target_value = val_row['v']
    print(f"Benchmarking filter: {target_key} = {target_value}")

    # Warmup
    await db.fetchval(
        f"SELECT count(*) FROM samples WHERE dataset_id = $1 AND metadata->>'{target_key}' = $2",
        dataset_id,
        target_value,
    )

    start = time.time()
    for _ in range(5):
        await db.fetchval(
            f"SELECT count(*) FROM samples WHERE dataset_id = $1 AND metadata->>'{target_key}' = $2",
            dataset_id,
            target_value,
        )
    end = time.time()
    print(f"Old method (->>) avg time: {(end - start) / 5:.4f}s")

    # 2. New way: @>
    # Construct JSON for containment
    # Need to handle type. If it's a string in DB, we pass string.
    # But for @> we need a jsonb object.

    # Check type
    is_num = (
        await db.fetchval(
            f"SELECT jsonb_typeof(metadata->'{target_key}') FROM samples WHERE dataset_id = $1 LIMIT 1",
            dataset_id,
        )
        == 'number'
    )

    if is_num:
        json_val = json.dumps({target_key: float(target_value)})
    else:
        json_val = json.dumps({target_key: target_value})

    # Warmup
    await db.fetchval(
        "SELECT count(*) FROM samples WHERE dataset_id = $1 AND metadata @> $2::jsonb",
        dataset_id,
        json_val,
    )

    start = time.time()
    for _ in range(5):
        await db.fetchval(
            "SELECT count(*) FROM samples WHERE dataset_id = $1 AND metadata @> $2::jsonb",
            dataset_id,
            json_val,
        )
    end = time.time()
    print(f"New method (@>) avg time: {(end - start) / 5:.4f}s")

    await db.disconnect()


if __name__ == "__main__":
    asyncio.run(benchmark())
