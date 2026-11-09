-- A follower's token belongs to one follower instance (docs/FOLLOWERS.md): the first one that uses it, by the
-- installation id that instance sends with each call. Two instances on one token would share one subscription, and
-- the faster one's acknowledgements would prune what the other hasn't read.
SET LOCAL lock_timeout = '2s';

ALTER TABLE followers ADD COLUMN instance TEXT;

-- This installation's id, made once. A dump carries it, so a follower restored or moved to another server is still
-- the same instance, while two installations differ.
CREATE TABLE installation (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    id UUID NOT NULL DEFAULT gen_random_uuid()
);
INSERT INTO installation DEFAULT VALUES;
