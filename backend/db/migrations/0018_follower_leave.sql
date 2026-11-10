-- A follower that stops following tells its primary (docs/FOLLOWERS.md): its subscriptions move to
-- follower_departures, with their options, so the primary stops logging for it and prunes its log, and they come
-- back if it returns. A tag first made from a follower records who made it there (display only).
SET LOCAL lock_timeout = '2s';

ALTER TABLE followers ADD COLUMN left_at TIMESTAMP WITH TIME ZONE;

CREATE TABLE follower_departures (
    follower_id INTEGER NOT NULL REFERENCES followers(id) ON DELETE CASCADE,
    dataset_id INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    tags TEXT NOT NULL CHECK (tags IN ('primary', 'both')),
    views TEXT NOT NULL CHECK (views IN ('primary', 'both')),
    images BOOLEAN NOT NULL,
    left_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (follower_id, dataset_id)
);
CREATE INDEX follower_departures_dataset ON follower_departures (dataset_id);

ALTER TABLE dataset_tags ADD COLUMN created_by_name TEXT;
