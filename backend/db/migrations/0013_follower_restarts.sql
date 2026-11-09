-- A primary's identity is its cluster's system identifier and its database's oid (docs/FOLLOWERS.md): a restore
-- with dropdb and createdb keeps the first but not the second. Each subscription records the identity its
-- start_txid belongs to, so a restored database restarts it instead of waiting on a txid from another database.
SET LOCAL lock_timeout = '2s';

ALTER TABLE follower_datasets ADD COLUMN start_identity TEXT;
-- The subscriptions that exist belong to this database and keep their place, unless their start is above this
-- database's txids: then it came from another cluster (restored or moved before this release), and the column stays
-- NULL so the follower's next config call starts it again.
UPDATE follower_datasets
SET start_identity = (SELECT system_identifier::text FROM pg_control_system())
                     || ':' || (SELECT oid::text FROM pg_database WHERE datname = current_database())
WHERE start_txid IS NOT NULL AND start_txid <= txid_snapshot_xmax(txid_current_snapshot());

-- A mirror that fails waits before its next try, longer after each failure in a row, and starts again on success.
ALTER TABLE mirrors ADD COLUMN failures INTEGER NOT NULL DEFAULT 0;
ALTER TABLE mirrors ADD COLUMN retry_after TIMESTAMP WITH TIME ZONE;
