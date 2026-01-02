-- 1. USERS TABLE
CREATE TABLE IF NOT EXISTS users (
    id                  BIGSERIAL PRIMARY KEY,
    email               VARCHAR(255) UNIQUE NOT NULL,
    username            VARCHAR(100),
    name                VARCHAR(150),
    picture             TEXT,
    phone_number        VARCHAR(30),
    is_phone_verified   BOOLEAN NOT NULL DEFAULT FALSE,
    dob                 DATE,
    gender              VARCHAR(30),
    citizenship         VARCHAR(100),
    spending_tier       VARCHAR(30) NOT NULL DEFAULT 'standard',
    role                VARCHAR(20) NOT NULL DEFAULT 'user',
    auth_provider       VARCHAR(30) NOT NULL DEFAULT 'google',
    auth_provider_id    VARCHAR(255) UNIQUE,
    home_city           VARCHAR(150),
    preferences         JSONB NOT NULL DEFAULT '{}'::jsonb,
    profile_full        TEXT NOT NULL DEFAULT '',
    profile_compressed  TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'utc'), 'YYYY-MM-DD"T"HH24:MI:SS"Z"')),
    updated_at          TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'utc'), 'YYYY-MM-DD"T"HH24:MI:SS"Z"'))
);

-- 2. USER SESSIONS TABLE
CREATE TABLE IF NOT EXISTS user_sessions (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    session_token_hash  VARCHAR(255) UNIQUE NOT NULL,
    user_agent          TEXT,
    ip_address          INET,
    expires_at          TIMESTAMPTZ NOT NULL,
    created_at          TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'utc'), 'YYYY-MM-DD"T"HH24:MI:SS"Z"'))
);

-- 3. TRIPS TABLE
CREATE TABLE IF NOT EXISTS trips (
    id                  BIGSERIAL PRIMARY KEY,
    owner_id            BIGINT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    slug                VARCHAR(100) UNIQUE,
    trip_name           TEXT,
    trip_description    TEXT,
    cover_image_url     TEXT,
    status              TEXT NOT NULL DEFAULT 'draft',
    origin_city         TEXT,
    destination_type    TEXT,
    destination_items   TEXT,
    start_date          TEXT,
    end_date            TEXT,
    number_of_days      INTEGER,
    travelers_adults    INTEGER NOT NULL DEFAULT 1,
    travelers_children  INTEGER NOT NULL DEFAULT 0,
    travelers_seniors   INTEGER NOT NULL DEFAULT 0,
    budget_currency     TEXT NOT NULL DEFAULT 'INR',
    budget_amount       INTEGER,
    trip_scope          TEXT,
    spending_tier       TEXT,
    travel_style        TEXT,
    trip_purpose        TEXT,
    factors             JSONB NOT NULL DEFAULT '[]'::jsonb,
    agent_state         TEXT,
    created_at          TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'utc'), 'YYYY-MM-DD"T"HH24:MI:SS"Z"')),
    updated_at          TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'utc'), 'YYYY-MM-DD"T"HH24:MI:SS"Z"'))
);

-- 4. MESSAGES TABLE
CREATE TABLE IF NOT EXISTS messages (
    id                  BIGSERIAL PRIMARY KEY,
    trip_id             BIGINT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    sender_id           BIGINT REFERENCES users(id) ON DELETE SET NULL,
    role                TEXT NOT NULL,
    content             TEXT NOT NULL,
    meta                JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at          TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'utc'), 'YYYY-MM-DD"T"HH24:MI:SS"Z"'))
);

-- 5. PLANNING ARTIFACTS TABLE
CREATE TABLE IF NOT EXISTS planning_artifacts (
    id                  BIGSERIAL PRIMARY KEY,
    trip_id             BIGINT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    artifact_type       TEXT NOT NULL,
    data                JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_stale            BOOLEAN NOT NULL DEFAULT FALSE,
    is_current          BOOLEAN NOT NULL DEFAULT TRUE,
    version             INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'utc'), 'YYYY-MM-DD"T"HH24:MI:SS"Z"'))
);

-- 6. CONFIRMED TRIP SNAPSHOTS TABLE
CREATE TABLE IF NOT EXISTS confirmed_trip_snapshots (
    id                  BIGSERIAL PRIMARY KEY,
    trip_id             BIGINT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    version             INTEGER NOT NULL DEFAULT 1,
    fingerprint         TEXT NOT NULL,
    payload             JSONB NOT NULL,
    is_current          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'utc'), 'YYYY-MM-DD"T"HH24:MI:SS"Z"')),
    UNIQUE (trip_id, version)
);

-- 7. PLANNING JOBS TABLE
CREATE TABLE IF NOT EXISTS planning_jobs (
    id                  TEXT PRIMARY KEY,
    trip_id             BIGINT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    snapshot_id         BIGINT REFERENCES confirmed_trip_snapshots(id) ON DELETE RESTRICT,
    status              TEXT NOT NULL,
    progress_percent    INTEGER NOT NULL DEFAULT 0,
    current_step        TEXT NOT NULL DEFAULT 'Initializing',
    attempt_count       INTEGER NOT NULL DEFAULT 0,
    progress            JSONB,
    result              JSONB,
    failure             JSONB,
    created_at          TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'utc'), 'YYYY-MM-DD"T"HH24:MI:SS"Z"')),
    updated_at          TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'utc'), 'YYYY-MM-DD"T"HH24:MI:SS"Z"'))
);

-- 8. TRIP PLANS TABLE
CREATE TABLE IF NOT EXISTS trip_plans (
    id                  TEXT PRIMARY KEY,
    trip_id             BIGINT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    job_id              TEXT REFERENCES planning_jobs(id) ON DELETE SET NULL,
    version             INTEGER NOT NULL DEFAULT 1,
    delivery_status     TEXT NOT NULL,
    plan_json           JSONB NOT NULL DEFAULT '{}'::jsonb,
    markdown_summary    TEXT NOT NULL DEFAULT '',
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'utc'), 'YYYY-MM-DD"T"HH24:MI:SS"Z"')),
    UNIQUE (trip_id, version)
);

-- 9. LLM USAGE TABLE
CREATE TABLE IF NOT EXISTS llm_usage (
    id                  BIGSERIAL PRIMARY KEY,
    user_id             BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    trip_id             BIGINT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    phase               TEXT NOT NULL,
    agent_name          TEXT NOT NULL,
    model               TEXT NOT NULL,
    requests            BIGINT NOT NULL DEFAULT 0,
    input_tokens        BIGINT NOT NULL DEFAULT 0,
    output_tokens       BIGINT NOT NULL DEFAULT 0,
    cached_input_tokens BIGINT NOT NULL DEFAULT 0,
    cache_write_tokens  BIGINT NOT NULL DEFAULT 0,
    reasoning_tokens    BIGINT NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'utc'), 'YYYY-MM-DD"T"HH24:MI:SS"Z"')),
    updated_at          TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'utc'), 'YYYY-MM-DD"T"HH24:MI:SS"Z"')),
    CONSTRAINT uq_llm_usage_aggregate UNIQUE (user_id, trip_id, phase, agent_name, model)
);

-- 10. TOOL USAGE TABLE
CREATE TABLE IF NOT EXISTS tool_usage (
    id                  BIGSERIAL PRIMARY KEY,
    user_id             BIGINT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    trip_id             BIGINT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    phase               TEXT NOT NULL,
    agent_name          TEXT NOT NULL,
    provider            TEXT NOT NULL,
    tool_name           TEXT NOT NULL,
    calls               BIGINT NOT NULL DEFAULT 0,
    failed_calls        BIGINT NOT NULL DEFAULT 0,
    duration_ms         BIGINT NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'utc'), 'YYYY-MM-DD"T"HH24:MI:SS"Z"')),
    updated_at          TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'utc'), 'YYYY-MM-DD"T"HH24:MI:SS"Z"')),
    CONSTRAINT uq_tool_usage_aggregate UNIQUE (user_id, trip_id, phase, agent_name, provider, tool_name)
);

-- IDEMPOTENT COLUMN ADDITIONS (Guarantees backward and forward schema compatibility)
ALTER TABLE users ADD COLUMN IF NOT EXISTS name VARCHAR(150);
ALTER TABLE users ADD COLUMN IF NOT EXISTS username VARCHAR(100);
ALTER TABLE users ADD COLUMN IF NOT EXISTS email VARCHAR(255);
ALTER TABLE users ADD COLUMN IF NOT EXISTS picture TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS phone_number VARCHAR(30);
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_phone_verified BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS dob DATE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS gender VARCHAR(30);
ALTER TABLE users ADD COLUMN IF NOT EXISTS citizenship VARCHAR(100);
ALTER TABLE users ADD COLUMN IF NOT EXISTS spending_tier VARCHAR(30) NOT NULL DEFAULT 'standard';
ALTER TABLE users ADD COLUMN IF NOT EXISTS role VARCHAR(20) NOT NULL DEFAULT 'user';
ALTER TABLE users ADD COLUMN IF NOT EXISTS auth_provider VARCHAR(30) NOT NULL DEFAULT 'google';
ALTER TABLE users ADD COLUMN IF NOT EXISTS auth_provider_id VARCHAR(255);
ALTER TABLE users ADD COLUMN IF NOT EXISTS home_city VARCHAR(150);
ALTER TABLE users ADD COLUMN IF NOT EXISTS preferences JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE users ADD COLUMN IF NOT EXISTS profile_full TEXT NOT NULL DEFAULT '';
ALTER TABLE users ADD COLUMN IF NOT EXISTS profile_compressed TEXT NOT NULL DEFAULT '';

ALTER TABLE trips ADD COLUMN IF NOT EXISTS owner_id BIGINT;
ALTER TABLE trips ADD COLUMN IF NOT EXISTS slug VARCHAR(100);
ALTER TABLE trips ADD COLUMN IF NOT EXISTS trip_name TEXT;
ALTER TABLE trips ADD COLUMN IF NOT EXISTS trip_description TEXT;
ALTER TABLE trips ADD COLUMN IF NOT EXISTS cover_image_url TEXT;
ALTER TABLE trips ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'draft';
ALTER TABLE trips ADD COLUMN IF NOT EXISTS origin_city TEXT;
ALTER TABLE trips ADD COLUMN IF NOT EXISTS destination_type TEXT;
ALTER TABLE trips ADD COLUMN IF NOT EXISTS destination_items TEXT;
ALTER TABLE trips ADD COLUMN IF NOT EXISTS start_date TEXT;
ALTER TABLE trips ADD COLUMN IF NOT EXISTS end_date TEXT;
ALTER TABLE trips ADD COLUMN IF NOT EXISTS number_of_days INTEGER;
ALTER TABLE trips ADD COLUMN IF NOT EXISTS travelers_adults INTEGER NOT NULL DEFAULT 1;
ALTER TABLE trips ADD COLUMN IF NOT EXISTS travelers_children INTEGER NOT NULL DEFAULT 0;
ALTER TABLE trips ADD COLUMN IF NOT EXISTS travelers_seniors INTEGER NOT NULL DEFAULT 0;
ALTER TABLE trips ADD COLUMN IF NOT EXISTS budget_currency TEXT NOT NULL DEFAULT 'INR';
ALTER TABLE trips ADD COLUMN IF NOT EXISTS budget_amount INTEGER;
ALTER TABLE trips ADD COLUMN IF NOT EXISTS trip_scope TEXT;
ALTER TABLE trips ADD COLUMN IF NOT EXISTS spending_tier TEXT;
ALTER TABLE trips ADD COLUMN IF NOT EXISTS travel_style TEXT;
ALTER TABLE trips ADD COLUMN IF NOT EXISTS trip_purpose TEXT;
ALTER TABLE trips ADD COLUMN IF NOT EXISTS factors JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE trips ADD COLUMN IF NOT EXISTS agent_state TEXT;

ALTER TABLE planning_jobs ADD COLUMN IF NOT EXISTS progress_percent INTEGER NOT NULL DEFAULT 0;
ALTER TABLE planning_jobs ADD COLUMN IF NOT EXISTS current_step TEXT NOT NULL DEFAULT 'Initializing';

ALTER TABLE trip_plans ADD COLUMN IF NOT EXISTS job_id TEXT;
ALTER TABLE trip_plans ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE trip_plans ADD COLUMN IF NOT EXISTS markdown_summary TEXT NOT NULL DEFAULT '';
ALTER TABLE trip_plans DROP COLUMN IF EXISTS quality_score;

-- PERFORMANCE INDEXES
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_auth_pid ON users(auth_provider_id);
CREATE INDEX IF NOT EXISTS idx_trips_owner ON trips(owner_id);
CREATE INDEX IF NOT EXISTS idx_trips_owner_updated ON trips(owner_id, id DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_trips_slug ON trips(slug);
CREATE INDEX IF NOT EXISTS idx_messages_trip ON messages(trip_id);
CREATE INDEX IF NOT EXISTS idx_messages_trip_id ON messages(trip_id, id ASC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_confirmed_trip_snapshots_current ON confirmed_trip_snapshots(trip_id) WHERE is_current = TRUE;
CREATE INDEX IF NOT EXISTS idx_planning_jobs_trip ON planning_jobs(trip_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_active_planning_job_per_snapshot
    ON planning_jobs(snapshot_id) WHERE status IN ('queued', 'running');
CREATE UNIQUE INDEX IF NOT EXISTS idx_active_plan_per_trip ON trip_plans(trip_id) WHERE is_active = TRUE;
