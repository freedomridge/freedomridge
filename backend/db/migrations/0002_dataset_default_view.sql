-- A dataset's default view: the saved view the app opens the dataset on, and goes back to when the
-- filters are cleared. Only the app uses it; the API and the SDK return every sample as before.
-- Deleting the view leaves the dataset without a default view.
SET LOCAL lock_timeout = '2s';

ALTER TABLE datasets ADD COLUMN default_view_id INTEGER
    CONSTRAINT datasets_default_view_id_fkey REFERENCES views(id) ON DELETE SET NULL;
