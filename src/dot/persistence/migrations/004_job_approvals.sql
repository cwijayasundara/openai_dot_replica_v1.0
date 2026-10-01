-- The review a paused job waits on. Its approval cards share this run_ref.
ALTER TABLE jobs ADD COLUMN pending_run_ref TEXT;
