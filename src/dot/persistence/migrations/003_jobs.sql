-- profile: the capability profile the job was started under; its result
-- returns to the inbox under the same profile.
-- origin: where the request came from (channel, inbox ids, the user's
-- instruction) so the result goes back there and the Guardian scopes to it.
ALTER TABLE jobs
    ADD COLUMN profile TEXT NOT NULL DEFAULT 'chat',
    ADD COLUMN origin JSONB NOT NULL DEFAULT '{}',
    ADD COLUMN error TEXT,
    ADD COLUMN started_at TIMESTAMPTZ;

-- The default only backfills existing rows. New jobs must name their profile.
ALTER TABLE jobs ALTER COLUMN profile DROP DEFAULT;

CREATE INDEX jobs_open ON jobs (created_at, job_id) WHERE status IN ('queued', 'running');
