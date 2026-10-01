CREATE INDEX audit_log_dot_id ON audit_log (dot_id, id);
CREATE INDEX audit_log_turn ON audit_log (dot_id, (detail->>'turn_id'), id);
