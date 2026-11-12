-- An embeddings map's layout id: it changes each time the map is laid out again, so a lasso (a polygon in the map's
-- coordinates) can tell that it was drawn on an earlier layout. No rows are written: a map without one is still in
-- the layout it had when this was applied, the one every lasso saved before it was drawn on.
SET LOCAL lock_timeout = '2s';

-- One row per map that has been laid out since: a whole-image vector field, or a patch scale (clip_patch_4x4) for all
-- its tiles. bump_map_layout gives it a new id. A mirror's copy of a primary map takes the primary's id once its feed
-- has passed every transaction the primary's read of it could see (pending_*, follower_sync.write_layouts).
CREATE TABLE IF NOT EXISTS embedding_layouts (
    dataset_id INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    field_name TEXT NOT NULL,
    layout_id UUID,  -- NULL, as no row: the layout the map had at the upgrade
    -- A mirror only: the primary's newer id, taken once the feed has passed pending_txid, the last transaction the
    -- primary's read of it could see.
    pending_layout_id UUID,
    pending_txid BIGINT,
    PRIMARY KEY (dataset_id, field_name),
    CHECK ((pending_layout_id IS NULL) = (pending_txid IS NULL))
);

-- A new layout of a map, in the transaction that writes it. Its writers call it: POST /embeddings/reduce, a map
-- re-fit's publish, and a coordinates upload without vector versions that moved a point already on the map or added
-- points to a whole-image map whose point count was 0. So do the deletes that make the map another one: a delete of
-- every vector of its field (Dataset.delete_embedding_field), and a mirror's sync replacing a map of its own with the
-- primary's. Placing new samples, a mirror's copy and other deletes never do. p_field is the map, or one of a patch
-- scale's tiles (clip_patch_4x4_r0c0), whose scale is the map. A followed dataset logs a 'maps' change, so its
-- followers read the new id with the map's state.
CREATE OR REPLACE FUNCTION bump_map_layout(p_dataset_id INTEGER, p_field TEXT) RETURNS UUID AS $$
DECLARE
    made UUID := gen_random_uuid();
BEGIN
    INSERT INTO embedding_layouts (dataset_id, field_name, layout_id)
    VALUES (p_dataset_id, regexp_replace(p_field, '(_patch_\d+x\d+)_r\d+c\d+$', '\1'), made)
    ON CONFLICT (dataset_id, field_name) DO UPDATE SET layout_id = EXCLUDED.layout_id;
    IF EXISTS (SELECT 1 FROM datasets WHERE id = p_dataset_id AND followed) THEN
        INSERT INTO dataset_changes (dataset_id, kind) VALUES (p_dataset_id, 'maps');
    END IF;
    RETURN made;
END;
$$ LANGUAGE plpgsql;

-- A mirror's ids of its primary's maps are written by the sync, like the map tables beside it.
CREATE TRIGGER follower_truncate_guard BEFORE TRUNCATE ON embedding_layouts
    FOR EACH STATEMENT EXECUTE FUNCTION refuse_followed_truncate();
