"""Restore identity, recovery budgets and availability under concurrent work."""

import asyncio
import logging
import os
import signal
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from backend import backups, concept_jobs, job_queue, job_runner, job_service, statistics
from backend.tests import test_field_registry_postgres as helpers
from backend.tests.test_concept_jobs import finish as finish_concept
from backend.tests.test_concept_maintenance import scores
from backend.tests.test_pending_work_visibility import prepare

registry_database = helpers.registry_database
pytestmark = helpers.pytestmark


def test_restore_between_mark_and_newest_writer_does_not_double_count(registry_database, monkeypatch):
    async def scenario(database):
        concept_id = await prepare(database, 'concept_score_count_deltas')
        restored = await database.fetchval('SELECT txid_current()+40')
        await database.execute('UPDATE concept_score_count_deltas SET transaction_id=$1', restored)
        # The target counter exceeds the dumped mark, but not the last writer in the dump.
        await database.execute(
            'ALTER TABLE transaction_high_water ADD COLUMN IF NOT EXISTS system_identifier BIGINT; '
            'UPDATE transaction_high_water SET system_identifier=0',
        )
        assert await job_queue.check_transaction_high_water(database.pool)
        original = concept_jobs._measure

        async def measure(pool, job, size):
            measured = await original(pool, job, size)
            if job['checkpoint'].get('count_phase'):
                for _ in range(80):
                    await database.fetchval('SELECT txid_current()')
            return measured

        monkeypatch.setattr(concept_jobs, '_measure', measure)
        job = await concept_jobs.request_scoring(database.pool, concept_id)
        await database.execute("""UPDATE jobs SET checkpoint='{"count_phase":true}' WHERE id=$1""", job['id'])
        await finish_concept(database, concept_id, job)
        assert await database.fetchval('SELECT scored_count FROM embedding_concepts WHERE id=$1', concept_id) == 1
        assert len(await scores(database, concept_id)) == 1
        assert not await job_queue.check_transaction_high_water(database.pool)

    registry_database(scenario)


def test_missing_mark_is_logged_repaired_and_normalized_once(registry_database, caplog):
    async def scenario(database):
        await database.execute(
            'DELETE FROM transaction_high_water; '
            'INSERT INTO dataset_stats_pending(dataset_id,block_id,transaction_id) VALUES(1,0,txid_current()+100000)',
        )
        with caplog.at_level(logging.WARNING):
            assert await job_queue.check_transaction_high_water(database.pool)
            assert not await job_queue.check_transaction_high_water(database.pool)
        assert sum('Missing transaction high-water mark' in record.message for record in caplog.records) == 1
        assert await database.fetchval('SELECT count(*) FROM transaction_high_water') == 1
        assert await database.fetchval('SELECT transaction_id FROM dataset_stats_pending') == 3
        assert not await database.fetchval('SELECT restored_work_pending()')

    registry_database(scenario)


def test_claim_does_not_wait_for_a_slow_marker_commit(registry_database):
    async def scenario(database):
        job = await job_queue.enqueue_job(database.pool, backups.KIND)
        async with database.pool.acquire() as holder, holder.transaction():
            await holder.fetchval('SELECT normalize_restored_work()')
            claim = await asyncio.wait_for(job_queue.claim_job(database.pool, 'backup'), 0.5)
            assert claim is not None and claim['id'] == job['id']
            # Still holding the singleton and advisory lock after a delayed COMMIT.
            await asyncio.sleep(0.15)

    registry_database(scenario)


def test_recovery_outlives_queue_statement_budget(registry_database):
    async def scenario(database):
        await database.execute(
            """INSERT INTO duplicate_pending VALUES(1,0,999999);
               UPDATE transaction_high_water SET transaction_id=txid_current()+1000000;
               CREATE FUNCTION slow_normalization() RETURNS TRIGGER AS $$
               BEGIN PERFORM pg_sleep(6); RETURN NEW; END;
               $$ LANGUAGE plpgsql;
               CREATE TRIGGER slow_normalization BEFORE INSERT ON duplicate_pending
               FOR EACH ROW EXECUTE FUNCTION slow_normalization()""",
        )
        assert await asyncio.wait_for(job_queue.check_transaction_high_water(database.pool), 15)
        assert await database.fetchval('SELECT transaction_id FROM duplicate_pending') == 3

    registry_database(scenario)


def test_ingest_lock_does_not_prevent_recovery_or_unrelated_claims(registry_database):
    async def scenario(database):
        backup = await job_queue.enqueue_job(database.pool, backups.KIND)
        await job_queue.enqueue_job(database.pool, statistics.KIND, dataset_id=1)
        await database.execute('UPDATE transaction_high_water SET transaction_id=txid_current()+1000000')
        async with database.pool.acquire() as writer:
            transaction = writer.transaction()
            await transaction.start()
            await writer.execute("INSERT INTO samples(dataset_id,filepath,media_type) VALUES(1,'ingest','image')")
            recovery = asyncio.create_task(job_queue.check_transaction_high_water(database.pool))
            try:
                await asyncio.sleep(0.2)
                assert not recovery.done()
                claim = await asyncio.wait_for(job_queue.claim_job(database.pool, 'backup'), 0.5)
                assert claim is not None and claim['id'] == backup['id']
                assert await job_queue.claim_job(database.pool, 'upkeep', kinds=[statistics.KIND]) is None
            finally:
                await transaction.commit()
                await asyncio.wait_for(recovery, 10)
        assert await database.fetchval('SELECT transaction_id FROM dataset_stats_pending') == 3
        assert await job_queue.claim_job(database.pool, 'upkeep', kinds=[statistics.KIND]) is not None

    registry_database(scenario)


def test_service_schedules_backups_while_recovery_waits(monkeypatch):
    async def scenario():
        loop = asyncio.get_running_loop()
        callbacks: dict = {}
        stopped = asyncio.Event()
        recovering = asyncio.Event()
        cancelled = asyncio.Event()

        async def fetchval(sql, *args, **kwargs):
            return len(args[0]) if args else False

        @asynccontextmanager
        async def local_runner(*args, **kwargs):
            yield SimpleNamespace(
                workers=[SimpleNamespace(pool=SimpleNamespace(fetchval=fetchval))],
                run=stopped.wait,
                stop=stopped.set,
            )

        async def recover(*args, **kwargs):
            recovering.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async def schedule_backups(pool):
            await recovering.wait()
            callbacks[signal.SIGTERM]()

        async def cleanup(*args):
            return None

        async def upkeep(*args):
            pytest.fail('Upkeep scheduled before recovery completed')

        monkeypatch.setattr(loop, 'add_signal_handler', lambda signum, callback: callbacks.update({signum: callback}))
        monkeypatch.setattr(loop, 'remove_signal_handler', lambda signum: None)
        monkeypatch.setattr(job_service, 'local_runner', local_runner)
        monkeypatch.setattr(job_service, 'schedule_cleanup', cleanup)
        monkeypatch.setattr(job_queue, 'check_transaction_high_water', recover)
        monkeypatch.setattr(backups, 'schedule_backups', schedule_backups)
        monkeypatch.setattr(statistics, 'schedule_statistics', upkeep)
        monkeypatch.setattr(concept_jobs, 'schedule_concepts', upkeep)
        await asyncio.wait_for(job_service.run_service('unused'), 1)
        assert cancelled.is_set()

    asyncio.run(scenario())


def test_smallest_runner_pool_can_claim_backups_during_recovery(registry_database, monkeypatch):
    async def scenario(database):
        monkeypatch.setenv('JOB_POOL_MAX_SIZE', '1')
        namespace = await database.fetchval('SELECT current_schema()')
        backup = await job_queue.enqueue_job(database.pool, backups.KIND)
        await database.execute('UPDATE transaction_high_water SET transaction_id=txid_current()+1000000')
        async with job_runner.local_runner(
            os.environ['FRIDGE_TEST_POSTGRES_DSN'],
            {backups.KIND: backups.handler()},
            slots=1,
            server_settings={'search_path': f'{namespace},public'},
        ) as runner:
            pool = runner.workers[0].pool
            async with database.pool.acquire() as writer:
                transaction = writer.transaction()
                await transaction.start()
                await writer.execute("INSERT INTO samples(dataset_id,filepath,media_type) VALUES(1,'ingest','image')")
                recovery = asyncio.create_task(job_queue.check_transaction_high_water(pool))
                try:
                    await asyncio.sleep(0.2)
                    assert not recovery.done()
                    claim = await asyncio.wait_for(job_queue.claim_job(pool, 'backup'), 2)
                    assert claim is not None and claim['id'] == backup['id']
                finally:
                    await transaction.commit()
                    await asyncio.wait_for(recovery, 10)

    registry_database(scenario)
