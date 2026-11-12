-- When a saved view's filters last changed (views.updated_at): replaced in place ("Update this view": PATCH
-- /datasets/{id}/views/{view_id}/content), by a version restore, or on a mirror by the sync. The database stamps it
-- for every writer, when a view is made and when its content changes; its name, description and sharing don't
-- count, and no writer sets it. The app takes a default view's new filters by it, and an update names the value it
-- started from, so it can't overwrite filters changed since. NULL for a view unchanged since this was applied: no rows
-- change here.
SET LOCAL lock_timeout = '2s';

ALTER TABLE views ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE;

CREATE OR REPLACE FUNCTION stamp_view_updated_at() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'INSERT' OR NEW.content IS DISTINCT FROM OLD.content THEN
        NEW.updated_at := clock_timestamp();
    ELSE
        NEW.updated_at := OLD.updated_at;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS views_updated_at ON views;
CREATE TRIGGER views_updated_at BEFORE INSERT OR UPDATE ON views
    FOR EACH ROW EXECUTE FUNCTION stamp_view_updated_at();
