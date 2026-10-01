CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    slack_user_id TEXT UNIQUE,
    web_subject TEXT UNIQUE
);

CREATE TABLE dots (
    dot_id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL REFERENCES users (user_id),
    pack_name TEXT NOT NULL,
    pack_version TEXT NOT NULL,
    thread_id TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    status TEXT NOT NULL
);

CREATE TABLE inbox (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    dot_id TEXT NOT NULL REFERENCES dots (dot_id),
    source TEXT NOT NULL,
    payload JSONB NOT NULL,
    profile TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    claimed_at TIMESTAMPTZ,
    done_at TIMESTAMPTZ,
    error TEXT
);

CREATE INDEX inbox_pending ON inbox (dot_id, created_at) WHERE done_at IS NULL;

CREATE TABLE jobs (
    job_id TEXT PRIMARY KEY,
    dot_id TEXT NOT NULL REFERENCES dots (dot_id),
    subagent TEXT NOT NULL,
    instructions TEXT NOT NULL,
    status TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    updates JSONB NOT NULL DEFAULT '[]',
    result_ref TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ
);

CREATE INDEX jobs_dot_status ON jobs (dot_id, status);

CREATE TABLE approvals (
    approval_id TEXT PRIMARY KEY,
    dot_id TEXT NOT NULL REFERENCES dots (dot_id),
    run_ref TEXT NOT NULL,
    tool TEXT NOT NULL,
    args JSONB NOT NULL,
    status TEXT NOT NULL,
    decided_by TEXT,
    decided_at TIMESTAMPTZ,
    edit JSONB
);

CREATE TABLE audit_log (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    dot_id TEXT NOT NULL REFERENCES dots (dot_id),
    at TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor TEXT NOT NULL,
    kind TEXT NOT NULL,
    tool TEXT,
    effect TEXT,
    decision TEXT,
    verdict JSONB,
    detail JSONB
);

CREATE OR REPLACE FUNCTION audit_log_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only';
END;
$$;

CREATE TRIGGER audit_log_no_update
    BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_immutable();

CREATE TABLE findings (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    dot_id TEXT NOT NULL REFERENCES dots (dot_id),
    schedule TEXT NOT NULL,
    title TEXT NOT NULL,
    evidence JSONB NOT NULL,
    score DOUBLE PRECISION NOT NULL,
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE episodes (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    dot_id TEXT NOT NULL REFERENCES dots (dot_id),
    at TIMESTAMPTZ NOT NULL DEFAULT now(),
    task TEXT NOT NULL,
    proposal JSONB NOT NULL,
    human_action TEXT NOT NULL,
    outcome JSONB NOT NULL
);

CREATE TABLE memory_versions (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    dot_id TEXT NOT NULL REFERENCES dots (dot_id),
    at TIMESTAMPTZ NOT NULL DEFAULT now(),
    diff TEXT NOT NULL,
    episodes INTEGER[] NOT NULL,
    status TEXT NOT NULL
);

CREATE TABLE channel_bindings (
    dot_id TEXT NOT NULL REFERENCES dots (dot_id),
    channel TEXT NOT NULL,
    external_id TEXT NOT NULL,
    PRIMARY KEY (dot_id, channel)
);
