-- A version records the dataset's public saved views (docs/SDK.md, Dataset Versioning): each one's name,
-- description and filters, so a view can be opened on the version it belonged to, and a view added, removed or
-- given other filters counts as a change since the version. NULL: not recorded, as for every version made
-- before this release and for a hash-only one; nothing is said about its views.
SET LOCAL lock_timeout = '2s';

ALTER TABLE dataset_versions ADD COLUMN saved_views JSONB;
