-- On-demand compute workers default to an Arm (Graviton) machine, r7g.2xlarge: the same 8 vCPUs and 64 GiB as
-- r7i.2xlarge for less. The machine's image follows the instance type's architecture (compute_launcher.py).
SET LOCAL lock_timeout = '2s';

ALTER TABLE compute_launcher ALTER COLUMN instance_type SET DEFAULT 'r7g.2xlarge';
-- A launcher never turned on and never used holds no choice of its own: it takes the new default.
UPDATE compute_launcher SET instance_type = 'r7g.2xlarge'
WHERE instance_type = 'r7i.2xlarge' AND NOT enabled AND NOT EXISTS (SELECT 1 FROM compute_launches);
