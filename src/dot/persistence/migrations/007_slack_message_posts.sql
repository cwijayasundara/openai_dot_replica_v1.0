-- Which AI message each posted reply came from, so a Slack shortcut on it can file a correction.
CREATE TABLE message_posts (
    channel TEXT NOT NULL,
    conversation TEXT NOT NULL,
    ts TEXT NOT NULL,
    dot_id TEXT NOT NULL REFERENCES dots (dot_id) ON DELETE CASCADE,
    message_id TEXT NOT NULL,
    PRIMARY KEY (channel, conversation, ts)
);
