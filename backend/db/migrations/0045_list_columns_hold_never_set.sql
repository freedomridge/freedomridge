-- A list field kept in a column of its own (POST /datasets/{id}/array-fields: a TEXT[] column of samples) can hold
-- "never set" apart from the empty list: its column loses its default, so a sample written without the field holds
-- no value for it (NULL) where it held the empty list. The columns are found, not named: every column
-- add_array_column made (sample_array_columns), the ones no dataset registers any more included, and never tags or
-- local_tags, whose default stays.
--
-- No row is read or written: dropping a default is a change of the catalog only. A row written before keeps what it
-- holds, the empty list among them (a row older than its column reads the column's stored missing value, which
-- dropping the default keeps). So no trigger fires and a follower sees no change. It takes ACCESS EXCLUSIVE on samples
-- once, for a few milliseconds whatever the table's size, and gives up after lock_timeout rather than queue behind a
-- long reader (the runner rolls the file back; run it again).
--
-- Re-runnable: run a second time it finds no column with a default left and changes nothing.
SET LOCAL lock_timeout = '2s';

DO $$
DECLARE
    dropped TEXT[];
BEGIN
    SELECT array_agg(format('ALTER COLUMN %I DROP DEFAULT', a.attname) ORDER BY a.attname)
    INTO dropped
    FROM sample_array_columns r
    JOIN pg_attribute a ON a.attrelid = 'samples'::regclass AND a.attname = r.column_name
        AND a.attnum > 0 AND NOT a.attisdropped
    WHERE r.column_name NOT IN ('tags', 'local_tags')
      AND a.atttypid = 'text[]'::regtype
      AND a.atthasdef;
    IF dropped IS NOT NULL THEN
        EXECUTE 'ALTER TABLE samples ' || array_to_string(dropped, ', ');
    END IF;
END $$;
