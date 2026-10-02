-- What the learning loop knows about a memory edit beyond its diff: the file,
-- the rationale, the base it was made against, and later the replay results.
ALTER TABLE memory_versions ADD COLUMN detail JSONB NOT NULL DEFAULT '{}';
CREATE INDEX episodes_dot_id ON episodes (dot_id, id);
