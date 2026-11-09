-- On-demand compute workers (docs/ON_DEMAND_WORKERS.md): when a compute job has waited with no worker free to take
-- it, the job runner starts a cloud machine that runs the worker and powers off once the worker is idle. Off until
-- an admin turns it on; the limits are times, set here.
SET LOCAL lock_timeout = '2s';

CREATE TABLE compute_launcher (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    region TEXT NOT NULL DEFAULT '',
    instance_type TEXT NOT NULL DEFAULT 'r7i.2xlarge',
    subnet_id TEXT,                                      -- NULL: the region's default VPC
    security_group_ids TEXT[] NOT NULL DEFAULT '{}',     -- empty: the VPC's default security group
    server_url TEXT NOT NULL DEFAULT '',                 -- the API address the machines reach this server at
    image TEXT,                                          -- NULL: this version's ghcr.io/freedomridge/backend
    access_key_id TEXT,                                  -- NULL: the server's own AWS credentials (its role)
    secret_access_key_encrypted TEXT,                    -- with STORAGE_ENCRYPTION_KEY
    wait_minutes INTEGER NOT NULL DEFAULT 5 CHECK (wait_minutes >= 1),
    idle_minutes INTEGER NOT NULL DEFAULT 10 CHECK (idle_minutes >= 1),
    max_machines INTEGER NOT NULL DEFAULT 1 CHECK (max_machines >= 1),
    max_hours_per_machine DOUBLE PRECISION NOT NULL DEFAULT 6 CHECK (max_hours_per_machine > 0),
    monthly_hours DOUBLE PRECISION NOT NULL DEFAULT 40 CHECK (monthly_hours > 0),
    last_error TEXT,
    last_error_at TIMESTAMPTZ,
    updated_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO compute_launcher DEFAULT VALUES;

CREATE TABLE compute_launches (
    id SERIAL PRIMARY KEY,
    job_id UUID REFERENCES jobs(id) ON DELETE SET NULL,  -- the job it was started for
    instance_id TEXT UNIQUE,                             -- NULL until AWS answers
    region TEXT NOT NULL,
    instance_type TEXT NOT NULL,
    market TEXT NOT NULL CHECK (market IN ('spot', 'on-demand')),
    worker_name TEXT NOT NULL,                           -- the name its worker registers with
    token_id INTEGER REFERENCES api_tokens(id) ON DELETE SET NULL,
    state TEXT NOT NULL DEFAULT 'starting' CHECK (state IN ('starting', 'running', 'ended')),
    launched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    connected_at TIMESTAMPTZ,
    ended_at TIMESTAMPTZ,
    end_reason TEXT
);
CREATE INDEX compute_launches_live ON compute_launches (launched_at) WHERE state <> 'ended';
