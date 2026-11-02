"""One-time restore recovery, including adversarial reproductions."""

import asyncio
import logging

import asyncpg
import pytest

from backend import backups, concept_jobs, duplicate_jobs, job_queue, statistics
from backend.tests import test_field_registry_postgres as helpers
from backend.tests.test_concept_jobs import finish as finish_concept
from backend.tests.test_concept_maintenance import concept, sample, score, scores
from backend.tests.test_pending_work_visibility import prepare, simulate_restore

registry_database = helpers.registry_database
pytestmark = helpers.pytestmark


async def burn_until(database, target):
    while (xid := await database.fetchval('SELECT txid_current()')) < target - 1:
        pass
    assert xid == target - 1


def test_live_writer_at_restored_id_remains_pending_after_publication(registry_database):
    async def scenario(database):
        await database.execute("INSERT INTO samples(dataset_id,filepath,media_type) VALUES(1,'old','image')")
        await database.execute('DELETE FROM dataset_stats_pending')
        restored = await database.fetchval('SELECT txid_current()+30')
        await database.execute(
            """INSERT INTO dataset_stats_pending(dataset_id,block_id,transaction_id,field_names)
               VALUES(1,0,$1,ARRAY['a']);""",
            restored,
        )
        await database.execute('INSERT INTO dataset_stats_blocks(dataset_id,block_id) VALUES(1,0)')
        await simulate_restore(database)
        first_snapshot = await database.fetchval('SELECT txid_current_snapshot()::text')
        await burn_until(database, restored)
        async with database.pool.acquire() as writer, database.pool.acquire() as publisher:
            transaction = writer.transaction()
            await transaction.start()
            try:
                assert await writer.fetchval('SELECT txid_current()') == restored
                await writer.execute(
                    """INSERT INTO samples(dataset_id,filepath,media_type,metadata)
                       VALUES(1,'live','image','{"b":7}')""",
                )

                async def publish():
                    async with publisher.transaction():
                        await statistics._publish_part(
                            publisher,
                            {'dataset_id': 1},
                            0,
                            0,
                            0,
                            {'snapshot': first_snapshot, 'count': 1, 'fields': []},
                            first_snapshot,
                        )

                publication = asyncio.create_task(publish())
                await asyncio.sleep(0.1)
                await transaction.commit()
                await asyncio.wait_for(publication, 5)
            finally:
                if writer.is_in_transaction():
                    await transaction.rollback()
        pending = await database.fetch('SELECT transaction_id,field_names FROM dataset_stats_pending')
        assert len(pending) == 1 and pending[0]['transaction_id'] == restored
        assert 'b' in pending[0]['field_names']

    registry_database(scenario)


def test_restored_delta_not_counted_twice_when_counter_passes_it(registry_database, monkeypatch):
    async def scenario(database):
        concept_id = await prepare(database, 'concept_score_count_deltas')
        restored = await database.fetchval('SELECT txid_current()+40')
        await database.execute('UPDATE concept_score_count_deltas SET transaction_id=$1', restored)
        await simulate_restore(database)
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
        assert await database.fetchval('SELECT count(*) FROM concept_score_count_deltas') == 0

    registry_database(scenario)


@pytest.mark.parametrize('change', ['ingest', 'vector'])
def test_restored_bootstrap_preserves_writes_after_its_scan(registry_database, monkeypatch, change):
    async def scenario(database):
        concept_id = await concept(database)
        sample_id = await sample(database, 'one')
        await finish_concept(database, concept_id)
        await database.execute("UPDATE embedding_concepts SET weight='[0,1]' WHERE id=$1", concept_id)
        job = await concept_jobs.request_scoring(database.pool, concept_id)
        future = await database.fetchval('SELECT txid_current()+100000')
        await database.execute(
            """UPDATE jobs SET barrier=jsonb_set(barrier,'{snapshot}',to_jsonb($2::text)),
               checkpoint='{"count_phase":true,"first_snapshot":"99999:99999:"}' WHERE id=$1""",
            job['id'],
            f'{future}:{future}:',
        )
        await database.execute('UPDATE transaction_high_water SET transaction_id=$1', future)
        assert await job_queue.check_transaction_high_water(database.pool)
        original = concept_jobs._measure
        written: list[int] = []

        async def measure(pool, current, size):
            if current['checkpoint'].get('count_phase') and not written:
                if change == 'ingest':
                    written.append(await sample(database, 'new', '[0.6,0.8]'))
                else:
                    await database.execute("UPDATE embeddings SET vector='[0.6,0.8]' WHERE sample_id=$1", sample_id)
                    written.append(sample_id)
                await concept_jobs.dispatch_changes(pool)
            return await original(pool, current, size)

        monkeypatch.setattr(concept_jobs, '_measure', measure)
        await finish_concept(database, concept_id, job)
        assert await database.fetchval('SELECT count(*) FROM concept_score_pending') > 0
        await finish_concept(database, concept_id)
        assert (await scores(database, concept_id))[written[0]] == pytest.approx(0.9)

    registry_database(scenario)


def test_restore_merges_keys_and_clears_manual_watermarks_once(registry_database, caplog):
    async def scenario(database):
        concept_id = await concept(database)
        await score(database, concept_id)
        await database.execute(
            """INSERT INTO dataset_stats_pending(dataset_id,block_id,transaction_id,field_names) VALUES
                 (1,0,99999,ARRAY['a']), (1,0,100000,ARRAY['b','a']),
                 (1,1,99999,NULL), (1,1,100000,ARRAY['b']),
                 (1,2,99999,ARRAY[]::text[]), (1,2,100000,ARRAY[]::text[]);
               INSERT INTO concept_embedding_pending VALUES(1,'clip',0,99999),(1,'clip',0,100000);
               INSERT INTO concept_score_pending VALUES(1,0,99999),(1,0,100000);
               INSERT INTO concept_score_count_deltas VALUES(1,99999,5),(1,100000,-2);
               INSERT INTO duplicate_pending VALUES(1,0,99999),(1,0,100000)""",
        )
        caplog.set_level(logging.WARNING, logger='backend.job_queue')
        await simulate_restore(database)
        fields = await database.fetch('SELECT field_names FROM dataset_stats_pending ORDER BY block_id')
        assert [row['field_names'] for row in fields] == [['a', 'b'], None, []]
        for table in ('concept_embedding_pending', 'concept_score_pending', 'duplicate_pending'):
            assert await database.fetchval(f'SELECT array_agg(transaction_id) FROM {table}') == [3]
        assert await database.fetchval('SELECT delta FROM concept_score_count_deltas') == 3
        assert await database.fetchval('SELECT score_manual_snapshot FROM embedding_concepts') is None
        assert await database.fetchval('SELECT score_manual_transaction FROM embedding_concepts') is None
        assert await database.fetchval('SELECT score_definition_transaction FROM embedding_concepts') == 3
        before = await database.fetch('SELECT *,ctid::text FROM dataset_stats_pending ORDER BY block_id')
        assert not await job_queue.check_transaction_high_water(database.pool)
        assert await database.fetch('SELECT *,ctid::text FROM dataset_stats_pending ORDER BY block_id') == before
        assert sum('restore recovery completed' in record.message for record in caplog.records) == 1
        await sample(database, 'after-restore')
        assert await score(database, concept_id) == 1

    registry_database(scenario)


def test_restored_definition_id_cannot_be_mistaken_for_current_writer(registry_database):
    async def scenario(database):
        concept_id = await concept(database)
        restored = await database.fetchval('SELECT txid_current()+20')
        await database.execute(
            'UPDATE embedding_concepts SET score_definition_transaction=$1 WHERE id=$2',
            restored,
            concept_id,
        )
        await simulate_restore(database)
        await burn_until(database, restored)
        await score(database, concept_id, full=True)
        assert await database.fetchval('SELECT score_initialized FROM embedding_concepts WHERE id=$1', concept_id)

    registry_database(scenario)


@pytest.mark.parametrize('counter_advance', [0, 100])
def test_normal_restart_and_higher_counter_leave_work_untouched(registry_database, counter_advance):
    async def scenario(database):
        concept_id = await concept(database)
        await sample(database, 'one')
        job = await concept_jobs.request_scoring(database.pool, concept_id)
        assert not await job_queue.check_transaction_high_water(database.pool)
        before = await database.fetchrow('SELECT *,ctid::text FROM jobs WHERE id=$1', job['id'])
        pending = await database.fetch('SELECT *,ctid::text FROM concept_score_pending')
        for _ in range(counter_advance):
            await database.fetchval('SELECT txid_current()')
        assert not await job_queue.check_transaction_high_water(database.pool)
        assert await database.fetchrow('SELECT *,ctid::text FROM jobs WHERE id=$1', job['id']) == before
        assert await database.fetch('SELECT *,ctid::text FROM concept_score_pending') == pending

    registry_database(scenario)


def test_simultaneous_claimers_only_normalize_once(registry_database):
    async def scenario(database):
        await database.execute('UPDATE transaction_high_water SET transaction_id=txid_current()+100000')
        results = await asyncio.gather(*(job_queue.check_transaction_high_water(database.pool) for _ in range(5)))
        assert results.count(True) == 1

    registry_database(scenario)


def test_restore_restarts_all_upkeep_barriers_and_fences_old_lease(registry_database):
    async def scenario(database):
        concept_id = await concept(database)
        await sample(database, 'before-dump')
        stats_job = await statistics.request_refresh(database.pool, 1, force=True)
        concept_job = await concept_jobs.request_scoring(database.pool, concept_id)
        duplicate_job = await duplicate_jobs.request_refresh(database.pool, 1, 'a')
        claim = await job_queue.claim_job(database.pool, 'old-worker', kinds=[statistics.KIND])
        assert claim is not None
        await database.execute(
            """UPDATE jobs SET checkpoint='{"first_snapshot":"99999:99999:","phase":"count"}',
               progress_done=20 WHERE state IN ('queued','running')""",
        )
        latest = await sample(database, 'before-recovery')
        await simulate_restore(database)
        snapshot = await database.fetchval('SELECT txid_current_snapshot()::text')
        for original in (stats_job, concept_job, duplicate_job):
            restarted = await database.fetchrow('SELECT * FROM jobs WHERE id=$1', original['id'])
            assert restarted['state'] == 'queued' and restarted['checkpoint'] == {}
            assert restarted['lease_token'] is None and restarted['started_at'] is None
            assert restarted['progress_done'] == 0
            assert restarted['barrier']['upper_id'] == latest
            assert int(restarted['barrier']['snapshot'].split(':')[1]) <= int(snapshot.split(':')[1])
        with pytest.raises(job_queue.LeaseLost):
            await job_queue.publish_step(database.pool, claim['id'], claim['lease_token'], finished=True)

    registry_database(scenario)


def test_failed_duplicate_bootstrap_cannot_resume_a_source_snapshot(registry_database):
    async def scenario(database):
        job = await duplicate_jobs.request_refresh(database.pool, 1, 'a')
        await database.execute(
            """UPDATE jobs SET state='failed',checkpoint='{"phase":"count"}' WHERE id=$1""",
            job['id'],
        )
        await simulate_restore(database)
        restarted = await duplicate_jobs.request_refresh(database.pool, 1, 'a')
        assert restarted['checkpoint'] == {} and restarted['barrier']['full']
        assert restarted['barrier']['snapshot'] != job['barrier']['snapshot']

    registry_database(scenario)


def test_failed_recovery_rolls_back_queues_and_mark(registry_database):
    async def scenario(database):
        await database.execute(
            """INSERT INTO dataset_stats_pending(dataset_id,block_id,transaction_id) VALUES(1,0,99999);
               INSERT INTO duplicate_pending VALUES(1,0,99999);
               UPDATE transaction_high_water SET transaction_id=txid_current()+100000;
               CREATE FUNCTION refuse_normalization() RETURNS TRIGGER AS $$
               BEGIN RAISE EXCEPTION 'test recovery failure'; END;
               $$ LANGUAGE plpgsql;
               CREATE TRIGGER refuse_normalization BEFORE INSERT ON duplicate_pending
               FOR EACH ROW EXECUTE FUNCTION refuse_normalization()""",
        )
        mark = await database.fetchval('SELECT transaction_id FROM transaction_high_water')
        with pytest.raises(asyncpg.RaiseError, match='test recovery failure'):
            await job_queue.check_transaction_high_water(database.pool)
        assert await database.fetchval('SELECT transaction_id FROM transaction_high_water') == mark
        assert await database.fetchval('SELECT transaction_id FROM dataset_stats_pending') == 99999
        assert await database.fetchval('SELECT transaction_id FROM duplicate_pending') == 99999
        await database.execute('DROP TRIGGER refuse_normalization ON duplicate_pending')
        assert await job_queue.check_transaction_high_water(database.pool)
        assert await database.fetchval('SELECT transaction_id FROM dataset_stats_pending') == 3

    registry_database(scenario)


def test_restore_drops_queued_work_of_a_dataset_being_deleted(registry_database):
    # A dump taken mid-deletion restores a sealed dataset with queued upkeep. Its guard refuses new
    # rows, so re-inserting them failed every recovery attempt and no upkeep could run anywhere.
    async def scenario(database):
        from backend.routers import datasets

        concept_id = await concept(database)
        await database.execute(
            f"""INSERT INTO dataset_stats_pending(dataset_id,block_id,transaction_id,field_names) VALUES
                 (1,0,99999,ARRAY['a']), (2,0,99999,ARRAY['b']);
               INSERT INTO concept_embedding_pending VALUES(1,'clip',0,99999);
               INSERT INTO concept_score_pending VALUES({int(concept_id)},0,99999);
               INSERT INTO concept_score_count_deltas VALUES({int(concept_id)},99999,5);
               INSERT INTO duplicate_pending VALUES(1,0,99999),(2,0,99999)""",
        )
        assert (await datasets.delete_dataset(1, helpers.USER))['state'] == 'queued'
        await simulate_restore(database)
        # The sealed dataset's rows are left for its deletion job; the other dataset's are merged.
        for table in ('dataset_stats_pending', 'duplicate_pending'):
            rows = await database.fetch(f'SELECT dataset_id,transaction_id FROM {table} ORDER BY 1')
            assert [tuple(row) for row in rows] == [(1, 99999), (2, 3)]
        for table in ('concept_embedding_pending', 'concept_score_pending', 'concept_score_count_deltas'):
            assert await database.fetchval(f'SELECT array_agg(transaction_id) FROM {table}') == [99999]

    registry_database(scenario)


@pytest.mark.parametrize('claimed_first', [False, True])
def test_restore_cancels_the_backup_its_dump_caught_mid_run(registry_database, claimed_first):
    # The runner makes the nightly dump, so every dump holds its own backup job mid-run. On a replacement
    # install that backup must not run again straight after recovery (a ~20-minute pg_dump).
    async def scenario(database):
        policies = {backups.KIND: backups.POLICY}
        job, _ = await backups.run_now(database.pool, None)
        claim = await job_queue.claim_job(database.pool, 'source', kinds=[backups.KIND], policies=policies)
        assert claim is not None and claim['id'] == job['id']
        # Its lease ran out long before the restore.
        await database.execute(
            "UPDATE jobs SET lease_expires_at=clock_timestamp()-interval '1 hour' WHERE id=$1",
            claim['id'],
        )
        await database.execute('UPDATE transaction_high_water SET transaction_id=txid_current()+100000')
        if claimed_first:
            # A slot can claim before recovery runs: its sweep requeues the backup, which then waits.
            assert await job_queue.claim_job(database.pool, 'target', kinds=[backups.KIND], policies=policies) is None
        assert await job_queue.check_transaction_high_water(database.pool)
        row = await database.fetchrow('SELECT * FROM jobs WHERE id=$1', claim['id'])
        assert (row['state'], row['lease_token']) == ('cancelled', None) and row['finished_at'] is not None
        assert row['error'] == {
            'code': 'restored_backup',
            'message': 'This backup was unfinished when database restore recovery ran; it was not resumed',
        }
        assert await job_queue.claim_job(database.pool, 'target', kinds=[backups.KIND], policies=policies) is None

    registry_database(scenario)


def test_restore_leaves_a_backup_started_during_recovery_running(registry_database):
    async def scenario(database):
        await database.execute('UPDATE transaction_high_water SET transaction_id=txid_current()+100000')
        job, _ = await backups.run_now(database.pool, None)
        claim = await job_queue.claim_job(database.pool, 'target', kinds=[backups.KIND])
        assert claim is not None and claim['id'] == job['id']
        assert await job_queue.check_transaction_high_water(database.pool)
        row = await database.fetchrow('SELECT state,lease_token FROM jobs WHERE id=$1', job['id'])
        assert (row['state'], row['lease_token']) == ('running', claim['lease_token'])

    registry_database(scenario)


def test_restore_leaves_a_backup_that_never_started_queued(registry_database):
    async def scenario(database):
        job, _ = await backups.run_now(database.pool, None)
        await simulate_restore(database)
        row = await database.fetchrow('SELECT state,error FROM jobs WHERE id=$1', job['id'])
        assert (row['state'], row['error']) == ('queued', None)
        claim = await job_queue.claim_job(database.pool, 'target', kinds=[backups.KIND])
        assert claim is not None and claim['id'] == job['id']

    registry_database(scenario)
