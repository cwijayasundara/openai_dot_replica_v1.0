-- A shared Slack channel (or other external conversation) belongs to one dot.
CREATE UNIQUE INDEX channel_bindings_external ON channel_bindings (channel, external_id);

-- Replies and approval cards waiting to be posted to an external channel.
-- Delivery is at least once: a crash between posting and marking repeats a post.
CREATE TABLE outbox (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    dot_id TEXT NOT NULL REFERENCES dots (dot_id),
    channel TEXT NOT NULL,
    kind TEXT NOT NULL,
    target JSONB NOT NULL,
    body JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    delivered_at TIMESTAMPTZ,
    parked_at TIMESTAMPTZ
);

CREATE INDEX outbox_pending ON outbox (channel, id) WHERE delivered_at IS NULL AND parked_at IS NULL;

-- Inbound events already accepted, so a redelivery is not queued twice.
CREATE TABLE channel_events (
    channel TEXT NOT NULL,
    event_key TEXT NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (channel, event_key)
);

-- Where an approval card was posted, and the status it last showed.
CREATE TABLE approval_posts (
    approval_id TEXT NOT NULL REFERENCES approvals (approval_id),
    channel TEXT NOT NULL,
    ref JSONB NOT NULL,
    shown_status TEXT NOT NULL,
    PRIMARY KEY (approval_id, channel)
);
