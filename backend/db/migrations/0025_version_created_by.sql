-- Who made a version (docs/SDK.md, Dataset Versioning): the username of whoever created it, or for history brought
-- in from another system the author it had there, as given. A name, not a user id: it is a record of who it was
-- then, and an author in another system is no user here. NULL: unknown, as for every version made before this
-- release, and for history brought in without an author.
--
-- imported_by: the username of whoever brought a version in, set when the request stated its date or its author.
-- Those are a claim about another system; this is who made the request here. NULL: not brought in.
SET LOCAL lock_timeout = '2s';

ALTER TABLE dataset_versions ADD COLUMN created_by TEXT;
ALTER TABLE dataset_versions ADD COLUMN imported_by TEXT;
