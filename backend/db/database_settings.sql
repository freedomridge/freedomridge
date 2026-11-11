-- The database's own settings, set again by scripts/apply_migrations.sh on every run: a backup (pg_dump without
-- --create) doesn't carry them, so a restored database would lose them. schema.sql sets the same on a fresh install.
--
-- Each statement planned for its own values, in every session: a generic plan, which a prepared statement switches to
-- after five runs and foreign-key checks use too, plans and locks every partition of embeddings and their indexes. The
-- app also sets it on its own connections (database.PLANNING), which covers a database it may not alter.
DO $$
BEGIN
    EXECUTE format('ALTER DATABASE %I SET plan_cache_mode = force_custom_plan', current_database());
EXCEPTION WHEN insufficient_privilege THEN
    RAISE NOTICE 'plan_cache_mode not set on the database (%); the app sets it on its own connections', SQLERRM;
END $$;
