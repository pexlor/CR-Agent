CREATE TABLE IF NOT EXISTS review_publications (
    task_id TEXT PRIMARY KEY,
    publication_id TEXT NOT NULL UNIQUE,
    snapshot_id TEXT NOT NULL,
    snapshot_version INTEGER NOT NULL,
    plan_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_publication_items (
    publication_key TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('line','summary')),
    state TEXT NOT NULL CHECK (state IN ('pending','succeeded','failed','unknown')),
    remote_id TEXT,
    remote_url TEXT,
    error_code TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    fencing_token INTEGER NOT NULL DEFAULT 0,
    UNIQUE(task_id, ordinal)
);
CREATE TABLE IF NOT EXISTS review_publication_leases (
    task_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    fencing_token INTEGER NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_publication_fences (
    task_id TEXT PRIMARY KEY,
    last_token INTEGER NOT NULL
);
