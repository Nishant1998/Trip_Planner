CREATE TABLE IF NOT EXISTS phase_two_control_checkpoints (
    run_id TEXT PRIMARY KEY REFERENCES planning_jobs(id) ON DELETE CASCADE,
    checkpoint JSONB NOT NULL,
    metadata JSONB NOT NULL
);
CREATE TABLE IF NOT EXISTS phase_two_collections (
    snapshot_id BIGINT NOT NULL REFERENCES confirmed_trip_snapshots(id) ON DELETE CASCADE,
    request_hash TEXT NOT NULL,
    payload JSONB NOT NULL,
    PRIMARY KEY (snapshot_id, request_hash)
);
