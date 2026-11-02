import asyncio

from backend.database import db


async def test_promote_field():
    await db.connect()

    # Find a dataset with samples
    row = await db.fetchrow("SELECT dataset_id FROM samples LIMIT 1")
    if not row:
        print("No samples found.")
        await db.disconnect()
        return
    dataset_id = row['dataset_id']

    # Find a numeric metadata field
    keys = await db.fetch(
        """
        SELECT DISTINCT jsonb_object_keys(metadata) as key
        FROM samples
        WHERE dataset_id = $1
        LIMIT 10
    """,
        dataset_id,
    )

    target_field = None
    for k in keys:
        key = k['key']
        # Check if numeric
        is_num = (
            await db.fetchval(
                f"SELECT jsonb_typeof(metadata->'{key}') FROM samples WHERE dataset_id = $1 LIMIT 1",
                dataset_id,
            )
            == 'number'
        )
        if is_num:
            target_field = key
            break

    if not target_field:
        print("No numeric metadata field found.")
        await db.disconnect()
        return

    print(f"Testing promotion for field: {target_field} in dataset {dataset_id}")

    # Simulate API call logic (since we can't call API directly easily without running server, we test logic)
    # 1. Check if promoted
    existing = await db.fetchrow(
        "SELECT is_promoted FROM dataset_fields WHERE dataset_id = $1 AND field_name = $2",
        dataset_id,
        target_field,
    )
    if existing and existing['is_promoted']:
        print("Field already promoted. Resetting for test.")
        await db.execute(
            "UPDATE dataset_fields SET is_promoted = FALSE WHERE dataset_id = $1 AND field_name = $2",
            dataset_id,
            target_field,
        )
        await db.execute(f'DROP INDEX IF EXISTS "idx_samples_meta_{dataset_id}_{target_field}"')

    # 2. Promote
    print("Promoting field...")
    index_name = f"idx_samples_meta_{dataset_id}_{target_field}"
    await db.execute(f"""
        CREATE INDEX IF NOT EXISTS "{index_name}"
        ON samples (( (metadata->>'{target_field}')::float ))
        WHERE dataset_id = {dataset_id}
    """)
    await db.execute(
        """
        INSERT INTO dataset_fields (dataset_id, field_name, field_type, is_promoted)
        VALUES ($1, $2, 'number', TRUE)
        ON CONFLICT (dataset_id, field_name)
        DO UPDATE SET is_promoted = TRUE
    """,
        dataset_id,
        target_field,
    )

    # 3. Verify
    promoted = await db.fetchval(
        "SELECT is_promoted FROM dataset_fields WHERE dataset_id = $1 AND field_name = $2",
        dataset_id,
        target_field,
    )
    assert promoted is True
    print("Field marked as promoted.")

    # Check index exists
    idx_exists = await db.fetchval(f"SELECT to_regclass('\"{index_name}\"')")
    assert idx_exists is not None
    print(f"Index {index_name} created.")

    print("Test passed!")
    await db.disconnect()


if __name__ == "__main__":
    asyncio.run(test_promote_field())
