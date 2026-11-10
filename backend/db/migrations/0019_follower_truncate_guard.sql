-- TRUNCATE fires none of the triggers that log a followed dataset's changes for its followers, or that keep a
-- mirror read-only: a follower would keep rows the primary no longer has, and a mirror could be emptied. Nothing
-- in the application truncates these tables, so the statement is refused while any dataset here is followed or is
-- a mirror. Every table with a follower-log trigger or a mirror guard has it
-- (backend/tests/test_follower_change_log.py checks the catalog).
SET LOCAL lock_timeout = '2s';

CREATE OR REPLACE FUNCTION refuse_followed_truncate() RETURNS trigger AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM datasets WHERE followed) OR EXISTS (SELECT 1 FROM mirrors) THEN
        RAISE EXCEPTION 'TRUNCATE % is refused while a dataset here is followed or is a mirror: it would skip the change log followers read and a mirror''s protection. Delete the rows instead (followers see each delete), or stop sharing the followed datasets and delete the mirrors first',
            TG_TABLE_NAME USING ERRCODE = '55000';
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER follower_truncate_guard BEFORE TRUNCATE ON samples
    FOR EACH STATEMENT EXECUTE FUNCTION refuse_followed_truncate();
CREATE TRIGGER follower_truncate_guard BEFORE TRUNCATE ON embeddings
    FOR EACH STATEMENT EXECUTE FUNCTION refuse_followed_truncate();
CREATE TRIGGER follower_truncate_guard BEFORE TRUNCATE ON sample_hashes
    FOR EACH STATEMENT EXECUTE FUNCTION refuse_followed_truncate();
CREATE TRIGGER follower_truncate_guard BEFORE TRUNCATE ON embedding_coordinates
    FOR EACH STATEMENT EXECUTE FUNCTION refuse_followed_truncate();
CREATE TRIGGER follower_truncate_guard BEFORE TRUNCATE ON dataset_fields
    FOR EACH STATEMENT EXECUTE FUNCTION refuse_followed_truncate();
CREATE TRIGGER follower_truncate_guard BEFORE TRUNCATE ON views
    FOR EACH STATEMENT EXECUTE FUNCTION refuse_followed_truncate();
CREATE TRIGGER follower_truncate_guard BEFORE TRUNCATE ON embedding_viz_meta
    FOR EACH STATEMENT EXECUTE FUNCTION refuse_followed_truncate();
CREATE TRIGGER follower_truncate_guard BEFORE TRUNCATE ON datasets
    FOR EACH STATEMENT EXECUTE FUNCTION refuse_followed_truncate();
