# Migrations

The files here are the schema changes since version 1.0.0, named `0001_short_name.sql`,
`0002_...`, and applied in order to existing databases by `scripts/apply_migrations.sh`.
`backend/db/schema.sql` is the full schema for new databases: it records `0000_baseline.sql` (the
1.0.0 schema) and every file here in its `schema_migrations` ledger.

How to write one: [CONTRIBUTING.md](../../../CONTRIBUTING.md#database-migrations).
