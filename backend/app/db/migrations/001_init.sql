CREATE TABLE IF NOT EXISTS users (
    id BIGSERIAL PRIMARY KEY,
    email VARCHAR(255) UNIQUE NOT NULL,
    username VARCHAR(100),
    name VARCHAR(150),
    picture TEXT,
    phone_number VARCHAR(30),
    is_phone_verified BOOLEAN NOT NULL DEFAULT FALSE,
    dob DATE,
    gender VARCHAR(30),
    citizenship VARCHAR(100),
    spending_tier VARCHAR(30) NOT NULL DEFAULT 'standard',
    role VARCHAR(20) NOT NULL DEFAULT 'user',
    auth_provider VARCHAR(30) NOT NULL DEFAULT 'google',
    auth_provider_id VARCHAR(255) UNIQUE,
    home_city VARCHAR(150),
    preferences JSONB NOT NULL DEFAULT '{}'::jsonb,
    profile_full TEXT NOT NULL DEFAULT '',
    profile_compressed TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'utc'), 'YYYY-MM-DD"T"HH24:MI:SS"Z"')),
    updated_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'utc'), 'YYYY-MM-DD"T"HH24:MI:SS"Z"'))
);

CREATE TABLE IF NOT EXISTS trips (
    id BIGSERIAL PRIMARY KEY,
    owner_id BIGINT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    slug VARCHAR(100) UNIQUE,
    trip_name TEXT,
    trip_description TEXT,
    cover_image_url TEXT,
    status TEXT NOT NULL DEFAULT 'draft',
    origin_city TEXT,
    destination_type TEXT,
    destination_items TEXT,
    start_date TEXT,
    end_date TEXT,
    number_of_days INTEGER,
    travelers_adults INTEGER NOT NULL DEFAULT 1,
    travelers_children INTEGER NOT NULL DEFAULT 0,
    travelers_seniors INTEGER NOT NULL DEFAULT 0,
    budget_currency TEXT NOT NULL DEFAULT 'INR',
    budget_amount INTEGER,
    trip_scope TEXT,
    spending_tier TEXT,
    travel_style TEXT,
    trip_purpose TEXT,
    factors JSONB NOT NULL DEFAULT '[]'::jsonb,
    agent_state TEXT,
    created_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'utc'), 'YYYY-MM-DD"T"HH24:MI:SS"Z"')),
    updated_at TEXT NOT NULL DEFAULT (to_char((now() AT TIME ZONE 'utc'), 'YYYY-MM-DD"T"HH24:MI:SS"Z"'))
);

CREATE INDEX IF NOT EXISTS idx_trips_owner ON trips(owner_id);
CREATE INDEX IF NOT EXISTS idx_trips_owner_updated ON trips(owner_id, id DESC);
