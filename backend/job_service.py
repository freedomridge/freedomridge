"""Co-located runner for independently scheduled, bounded maintenance jobs."""

import asyncio
import logging
import math
import os
import signal
from datetime import datetime, timedelta

from . import (
    auth_cleanup,
    backups,
    bulk_jobs,
    concept_jobs,
    dataset_deletion,
    dataset_schedules,
    duplicate_jobs,
    index_actions,
    index_advisor,
    index_jobs,
    job_queue,
    map_refit,
    schema_check,
    statistics,
)
from .database import DATABASE_URL
from .job_queue import JobPolicy
from .job_runner import StepResult, local_runner
from .job_tasks import FailureBackoff

logger = logging.getLogger(__name__)
# True when schema_migrations holds exactly the filenames in $1 (filename is its primary key).
LEDGER_READY_SQL = (
    'SELECT count(*) = cardinality($1::text[]) '
    'AND count(*) FILTER (WHERE filename = ANY($1::text[])) = cardinality($1::text[]) '
    'FROM schema_migrations'
)
SELECTION_CLEANUP = 'selection_cleanup'
PRUNE_BATCH_SIZE = 1000


def runner_placements() -> tuple[str, ...]:
    """JOB_RUNNER_PLACEMENTS: the work this runner takes, 'server' and 'compute' (the default, one machine), or
    only 'server' when compute workers (python -m fridge.worker) do the compute work, such as map re-fits' fits."""
    placements = tuple(
        part.strip() for part in os.getenv('JOB_RUNNER_PLACEMENTS', 'server,compute').split(',') if part.strip()
    )
    if not placements or set(placements) - {'server', 'compute'} or 'server' not in placements:
        raise ValueError("JOB_RUNNER_PLACEMENTS is 'server,compute' or 'server'")
    return placements


def positive_seconds(name, default):
    value = float(os.getenv(name, str(default)))
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f'{name} must be finite and positive')
    return value


def selection_cleanup(pool):
    async def measure(job):
        if job['lane'] != 'maintenance':
            raise ValueError('Selection cleanup belongs to the maintenance lane')
        cutoff = job['created_at'] - timedelta(hours=2)
        after = job['checkpoint'].get('after_selection')
        args = [cutoff]
        where = 'created_at<$1'
        if after:
            args.extend([datetime.fromisoformat(after[0]), after[1]])
            where += ' AND (created_at,token)>($2,$3)'
        async with pool.acquire(timeout=1) as connection:
            candidate = await connection.fetchrow(
                f'SELECT token,created_at FROM region_selections WHERE {where} ORDER BY created_at,token LIMIT 1',
                *args,
                timeout=5,
            )
            pinned = candidate and await connection.fetchval(
                'SELECT EXISTS(SELECT 1 FROM bulk_selection_pins WHERE token=$1)',
                candidate['token'],
                timeout=5,
            )
        if candidate is None:
            return await _release_discarded_targets(pool, job)
        if pinned:
            # A selection is inserted in one transaction with one timestamp. Seek past
            # the entire pinned group rather than inspecting its millions of IDs.
            return StepResult(
                checkpoint={
                    **job['checkpoint'],
                    'after_selection': [candidate['created_at'].isoformat(), candidate['token']],
                },
            )

        async def publish(connection):
            await connection.execute("LOCK TABLE bulk_selection_pins IN SHARE ROW EXCLUSIVE MODE")
            await connection.execute(
                """DELETE FROM region_selections WHERE ctid IN (
                       SELECT ctid FROM region_selections WHERE token=$1 AND created_at<$2
                       AND NOT EXISTS (SELECT 1 FROM bulk_selection_pins p WHERE p.token=region_selections.token)
                       ORDER BY sample_id LIMIT $3 FOR UPDATE)""",
                candidate['token'],
                cutoff,
                PRUNE_BATCH_SIZE,
            )

        return StepResult(
            checkpoint={**job['checkpoint'], 'batches': job['checkpoint'].get('batches', 0) + 1},
            message='Pruning expired transient selections',
            publish=publish,
        )

    return measure


async def _release_discarded_targets(pool, job):
    """Discarded bulk jobs never run again; drop what remains of their targets in bounded units."""
    async with pool.acquire(timeout=1) as connection:
        owner = await connection.fetchval(
            """SELECT id FROM jobs WHERE kind=$1 AND state IN ('failed','cancelled')
               AND COALESCE(result,'{}'::jsonb) ? 'discarded'
               AND EXISTS (SELECT 1 FROM bulk_job_targets t WHERE t.job_id=jobs.id)
               ORDER BY created_at,id LIMIT 1""",
            bulk_jobs.KIND,
            timeout=5,
        )
    if owner is None:
        return await _cleanup_statistics_history(pool, job)

    async def publish(connection):
        await connection.execute(
            """DELETE FROM bulk_job_targets WHERE job_id=$1 AND sample_id IN (
                   SELECT sample_id FROM bulk_job_targets WHERE job_id=$1 ORDER BY sample_id LIMIT $2)""",
            owner,
            PRUNE_BATCH_SIZE,
        )

    return StepResult(
        checkpoint={**job['checkpoint'], 'target_batches': job['checkpoint'].get('target_batches', 0) + 1},
        message='Releasing targets of discarded bulk jobs',
        publish=publish,
    )


async def _cleanup_statistics_history(pool, job):
    cutoff = job['created_at'] - timedelta(days=1)
    async with pool.acquire(timeout=1) as connection:
        expired = await connection.fetch(
            """SELECT id FROM jobs
               WHERE kind IN ('statistics','concept_scoring','duplicate_maintenance')
                 AND dedupe_owner='system' AND state='succeeded'
                 AND NOT barrier @> '{"full":true}' AND finished_at<$1
               ORDER BY finished_at,id LIMIT $2""",
            cutoff,
            PRUNE_BATCH_SIZE,
            timeout=5,
        )
    if not expired:
        return StepResult(
            finished=True,
            result={
                'expired_before': (job['created_at'] - timedelta(hours=2)).isoformat(),
                'statistics_expired_before': cutoff.isoformat(),
            },
            message='Cleanup complete',
        )

    async def publish(connection):
        await connection.execute(
            """DELETE FROM jobs WHERE id=ANY($1::uuid[])
               AND kind IN ('statistics','concept_scoring','duplicate_maintenance')
                 AND dedupe_owner='system' AND state='succeeded'
               AND NOT barrier @> '{"full":true}' AND finished_at<$2""",
            [row['id'] for row in expired],
            cutoff,
        )

    return StepResult(
        checkpoint={**job['checkpoint'], 'statistics_batches': job['checkpoint'].get('statistics_batches', 0) + 1},
        message='Pruning successful automatic maintenance history',
        publish=publish,
    )


async def schedule_cleanup(pool, interval_seconds=300):
    """One active cleanup across all runners; cadence survives scheduler restarts."""
    async with job_queue._transaction(pool) as connection:
        locked = await connection.fetchval(
            "SELECT pg_try_advisory_xact_lock(hashtextextended('schedule:selections',0))",
        )
        if not locked:
            return None
        latest = await connection.fetchrow(
            """SELECT state,created_at>clock_timestamp()-$2::float8*interval '1 second' AS recent
               FROM jobs WHERE kind=$1 ORDER BY created_at DESC LIMIT 1""",
            SELECTION_CLEANUP,
            interval_seconds,
        )
        if latest is not None and (latest['state'] in ('queued', 'running') or latest['recent']):
            return None
        return await job_queue.enqueue_job(
            connection,
            SELECTION_CLEANUP,
            lane='maintenance',
            dedupe_key='scheduled:selection-cleanup',
        )


async def run_service(dsn=DATABASE_URL):
    interval = positive_seconds('JOB_SELECTION_CLEANUP_SECONDS', 300)
    stats_interval = positive_seconds('JOB_STATISTICS_SECONDS', 5)
    concept_interval = positive_seconds('JOB_CONCEPT_SECONDS', 60)
    shutdown_seconds = positive_seconds('JOB_SHUTDOWN_SECONDS', 20)
    placements = runner_placements()
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stopping.set)

    async def handle_cleanup(job):
        return await selection_cleanup(pool)(job)

    async def handle_statistics(job):
        return await statistics.handler(pool)(job)

    async def handle_concepts(job):
        return await concept_jobs.handler(pool)(job)

    async def handle_deletion(job):
        return await dataset_deletion.handler(pool)(job)

    async def handle_bulk(job):
        return await bulk_jobs.handler(pool)(job)

    async def handle_duplicates(job):
        return await duplicate_jobs.handler(pool)(job)

    async def handle_explicit_concepts(job):
        return await concept_jobs.explicit_handler(pool)(job)

    async def handle_field_index(job):
        return await index_jobs.field_handler(pool)(job)

    async def handle_ann_index(job):
        return await index_jobs.ann_handler(pool)(job)

    async def handle_map_refit(job):
        return await map_refit.handler(pool, fits_here='compute' in placements)(job)

    async def handle_index_review(job):
        return await index_advisor.handler(pool)(job)

    async def handle_index_drop(job):
        return await index_actions.drop_handler(pool)(job)

    async def handle_index_restore(job):
        return await index_actions.restore_handler(pool)(job)

    dispatch_after = 0

    async def dispatch_concepts():
        nonlocal dispatch_after
        dispatch_after = await concept_jobs.dispatch_changes(pool, after_dataset=dispatch_after)

    handlers = {
        SELECTION_CLEANUP: handle_cleanup,
        statistics.KIND: handle_statistics,
        concept_jobs.KIND: handle_concepts,
        duplicate_jobs.KIND: handle_duplicates,
        backups.KIND: backups.handler(),
        dataset_deletion.KIND: handle_deletion,
        bulk_jobs.KIND: handle_bulk,
        concept_jobs.EXPLICIT_KIND: handle_explicit_concepts,
        index_jobs.FIELD_KIND: handle_field_index,
        index_jobs.ANN_KIND: handle_ann_index,
        map_refit.KIND: handle_map_refit,
        index_advisor.KIND: handle_index_review,
        index_actions.DROP_KIND: handle_index_drop,
        index_actions.RESTORE_KIND: handle_index_restore,
    }
    try:
        async with local_runner(
            dsn,
            handlers,
            placements=placements,
            policies={
                SELECTION_CLEANUP: JobPolicy(max_unit_seconds=15),
                statistics.KIND: statistics.POLICY,
                concept_jobs.KIND: concept_jobs.POLICY,
                duplicate_jobs.KIND: duplicate_jobs.POLICY,
                backups.KIND: backups.POLICY,
                dataset_deletion.KIND: dataset_deletion.POLICY,
                bulk_jobs.KIND: bulk_jobs.POLICY,
                concept_jobs.EXPLICIT_KIND: concept_jobs.EXPLICIT_POLICY,
                index_jobs.FIELD_KIND: index_jobs.FIELD_POLICY,
                index_jobs.ANN_KIND: index_jobs.ANN_POLICY,
                map_refit.KIND: map_refit.POLICY,
                index_advisor.KIND: index_advisor.POLICY,
                index_actions.DROP_KIND: index_actions.POLICY,
                index_actions.RESTORE_KIND: index_actions.POLICY,
            },
        ) as runner:
            pool = runner.workers[0].pool
            readiness = FailureBackoff(logger, 'Runner schema readiness check', 5)
            waiting_for_migration = False
            # Claim nothing until the ledger is exactly the baseline plus every migration this code ships:
            # a missing one is pending, an extra one means the database is newer than this code.
            required_migrations = [schema_check.BASELINE, *schema_check.migration_files()]
            while not stopping.is_set():
                delay = 5.0
                try:
                    ready = await pool.fetchval(LEDGER_READY_SQL, required_migrations, timeout=5)
                    readiness.recovered()
                    if ready:
                        if waiting_for_migration:
                            logger.info('Runner migrations are ready; starting the runner')
                        break
                    if not waiting_for_migration:
                        logger.warning(
                            'Waiting for the database to record exactly the migrations %s (some are pending, or it '
                            'is newer than this code); runner is not claiming work',
                            required_migrations,
                        )
                    waiting_for_migration = True
                except Exception as error:
                    waiting_for_migration = False
                    delay = readiness.failed(error)
                try:
                    await asyncio.wait_for(stopping.wait(), delay)
                except (TimeoutError, asyncio.TimeoutError):
                    pass
            if stopping.is_set():
                return
            execution = asyncio.create_task(runner.run())
            stop_requested = asyncio.create_task(stopping.wait())
            recovery_ready = asyncio.Event()

            async def recover():
                retry = FailureBackoff(logger, 'Restore recovery', 5)
                while not stopping.is_set():
                    recovery_ready.clear()
                    delay = 5.0
                    try:
                        await job_queue.check_transaction_high_water(pool, refresh=True)
                        if not await pool.fetchval('SELECT restored_work_pending()', timeout=5):
                            recovery_ready.set()
                        else:
                            recovery_ready.clear()
                        retry.recovered()
                    except Exception as error:
                        recovery_ready.clear()
                        delay = retry.failed(error)
                    await asyncio.wait((stop_requested,), timeout=delay)

            recovery = asyncio.create_task(recover())
            schedules = [
                (lambda: auth_cleanup.scheduled_cleanup(pool), 300, 'Demo account and session cleanup', False),
                (lambda: schedule_cleanup(pool, interval), min(interval, 5), 'Selection cleanup', False),
                (lambda: statistics.schedule_statistics(pool), stats_interval, 'Statistics', True),
                (dispatch_concepts, 1, 'Concept work delivery', True),
                (lambda: concept_jobs.schedule_concepts(pool), concept_interval, 'Concepts', True),
                (lambda: backups.schedule_backups(pool), 60, 'Backups', False),
                (lambda: duplicate_jobs.schedule_duplicates(pool), 5, 'Duplicates', True),
                (lambda: index_jobs.sweep_invalid_indexes(pool), 60, 'Invalid index cleanup', False),
                (lambda: dataset_schedules.schedule_dataset_jobs(pool), 60, 'Dataset schedules', False),
                (lambda: map_refit.sweep(pool), 600, 'Map artifact cleanup', False),
                (lambda: map_refit.heal_unranked_maps(pool), 600, 'Unranked map heal', False),
                (lambda: map_refit.expire_unclaimed_fits(pool), 600, 'Unclaimed map fit expiry', False),
            ]
            retries = [FailureBackoff(logger, f'{name} scheduling', period) for _, period, name, _ in schedules]
            next_runs = [0.0] * len(schedules)
            try:
                logger.info(
                    'Job runner started with %s slots; scheduled kinds: %s',
                    len(runner.workers),
                    ', '.join(handlers),
                )
                while not stopping.is_set():
                    if execution.done():
                        await execution
                        raise RuntimeError('Runner exited before shutdown')
                    for index, (schedule, period, _, needs_recovery) in enumerate(schedules):
                        if loop.time() < next_runs[index]:
                            continue
                        if needs_recovery and not recovery_ready.is_set():
                            next_runs[index] = loop.time() + 1
                            continue
                        try:
                            await schedule()
                            retries[index].recovered()
                            delay = period
                        except Exception as error:
                            delay = retries[index].failed(error)
                        next_runs[index] = loop.time() + delay
                    delay = max(0, min(next_runs) - loop.time())
                    await asyncio.wait(
                        (execution, stop_requested),
                        timeout=delay,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
            finally:
                runner.stop()
                stop_requested.cancel()
                recovery.cancel()
                try:
                    await asyncio.wait_for(asyncio.shield(execution), shutdown_seconds)
                except (asyncio.TimeoutError, TimeoutError):
                    logger.warning('Shutdown grace expired; cancelling measurements, preserving committed checkpoints')
                    execution.cancel()
                finally:
                    await asyncio.gather(execution, stop_requested, recovery, return_exceptions=True)
    finally:
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(signum)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    asyncio.run(run_service())
