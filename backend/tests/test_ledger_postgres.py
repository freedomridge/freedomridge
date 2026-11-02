"""The migrations ledger and baseline objects, against a disposable PostgreSQL built from schema.sql."""

from backend import job_service, schema_check
from backend.tests import test_field_registry_postgres as helpers

registry_database = helpers.registry_database
pytestmark = helpers.pytestmark


def test_the_runner_claims_work_only_when_the_ledger_is_exactly_what_the_code_ships(registry_database):
    async def scenario(database):
        required = [schema_check.BASELINE, *schema_check.migration_files()]
        assert await database.fetchval(job_service.LEDGER_READY_SQL, required) is True
        await database.execute("INSERT INTO schema_migrations (filename) VALUES ('0099_newer.sql')")
        assert await database.fetchval(job_service.LEDGER_READY_SQL, required) is False
        await database.execute("DELETE FROM schema_migrations")
        assert await database.fetchval(job_service.LEDGER_READY_SQL, required) is False

    registry_database(scenario)


def test_deleting_a_storage_config_unlinks_its_datasets(registry_database):
    """schema.sql adds datasets.storage_config_id's foreign key in a DO block, ON DELETE SET NULL."""

    async def scenario(database):
        assert (
            await database.fetchval(
                """SELECT count(*) FROM pg_constraint
                   WHERE conrelid='datasets'::regclass AND conname='datasets_storage_config_id_fkey'
                     AND contype='f' AND confrelid='storage_configs'::regclass AND confdeltype='n'""",
            )
            == 1
        )

    registry_database(scenario)
