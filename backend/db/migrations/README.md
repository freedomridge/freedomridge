# Migrations

The files here are the schema changes since version 2.14.0, named `0045_short_name.sql`,
`0046_...`, and applied in order to existing databases by `scripts/apply_migrations.sh`.
`backend/db/schema.sql` is the full schema for new databases: it records `baseline_2.14.0.sql` (the
2.14.0 schema) and every file here in its `schema_migrations` ledger.

3.0.0 squashed the migrations up to 2.14.0 into that baseline. A 2.14.0 database records instead
the rows listed in `backend/db/ledger_2.14.0.txt` (the 1.0.0 baseline and the 44 migrations since),
which the runner replaces with `baseline_2.14.0.sql`. An older database is upgraded to 2.14.0 first:
[docs/UPGRADING.md](../../../docs/UPGRADING.md#from-a-release-before-2140).

How to write one: [CONTRIBUTING.md](../../../CONTRIBUTING.md#database-migrations).
