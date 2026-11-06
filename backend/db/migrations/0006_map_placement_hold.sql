-- A full layout written over time (the SDK's compute_visualization: a fit of minutes, then batches of
-- coordinates) holds placement of new samples on that map until it ends, so none is placed from the old layout
-- and kept on the new one. The SDK renews the hold while it works; a script that dies stops renewing, and
-- placement resumes once the hold lapses.
SET LOCAL lock_timeout = '2s';

ALTER TABLE embedding_viz_meta ADD COLUMN placement_held_until TIMESTAMPTZ;
