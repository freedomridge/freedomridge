"""Reclaim temporary accounts and sessions using the existing runner's schedules."""

DEMO_ACCOUNT_LOCK = 739128041


async def cleanup_accounts(pool) -> int:
    async with pool.acquire(timeout=5) as connection, connection.transaction():
        await connection.execute("SET LOCAL statement_timeout='30s'")
        await connection.execute("SET LOCAL lock_timeout='5s'")
        # The same lock guards admission, making the cap global across API workers.
        await connection.execute('SELECT pg_advisory_xact_lock($1)', DEMO_ACCOUNT_LOCK)
        ids = [
            row['id']
            for row in await connection.fetch(
                "SELECT id FROM users WHERE is_demo_account AND created_at < NOW()-INTERVAL '1 day' FOR UPDATE",
            )
        ]
        if ids:
            # Views use SET NULL, not CASCADE. Remove public legacy views as well as private ones.
            await connection.execute('DELETE FROM views WHERE user_id=ANY($1::int[])', ids)
            await connection.execute(
                'UPDATE views SET shared_with_user_ids=ARRAY('
                'SELECT unnest(shared_with_user_ids) EXCEPT SELECT unnest($1::int[])) '
                'WHERE shared_with_user_ids && $1::int[]',
                ids,
            )
            # sessions, api_tokens and dataset_access cascade; provenance references SET NULL.
            # Demo guests cannot author datasets, samples, storage configs, concepts or jobs.
            await connection.execute('DELETE FROM users WHERE id=ANY($1::int[])', ids)
        await connection.execute('DELETE FROM sessions WHERE expires_at <= NOW()')
        await connection.execute(
            "DELETE FROM demo_admissions WHERE user_id IS NULL AND created_at <= NOW()-INTERVAL '1 hour'",
        )
        return len(ids)


async def scheduled_cleanup(pool):
    return await cleanup_accounts(pool)
