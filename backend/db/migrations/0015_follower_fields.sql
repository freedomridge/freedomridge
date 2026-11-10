-- Patch scales a follower holds of a dataset it follows, computed there and not sent by the primary
-- (docs/FOLLOWERS.md): the follower reports them, so a patch search on such a scale can go to it. They go with the
-- subscription. On the follower, a mirror records when it last reported.
SET LOCAL lock_timeout = '2s';

CREATE TABLE follower_fields (
    follower_id INTEGER NOT NULL,
    dataset_id INTEGER NOT NULL,
    base_field TEXT NOT NULL,
    scale TEXT NOT NULL,
    dims INTEGER NOT NULL,
    indexed BOOLEAN NOT NULL,
    first_reported_at TIMESTAMPTZ NOT NULL DEFAULT now(),  -- providers are taken in this order
    reported_at TIMESTAMPTZ NOT NULL DEFAULT now(),  -- the latest report naming it
    PRIMARY KEY (follower_id, dataset_id, base_field, scale),
    FOREIGN KEY (follower_id, dataset_id) REFERENCES follower_datasets (follower_id, dataset_id) ON DELETE CASCADE
);
CREATE INDEX follower_fields_dataset ON follower_fields (dataset_id, base_field, scale);

ALTER TABLE mirrors ADD COLUMN fields_reported_at TIMESTAMP WITH TIME ZONE;
