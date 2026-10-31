-- #5045: durable pause state and lookup of later announcements during retention.
-- Additive; the same index serves exact type/object reads of the event log.
CREATE INDEX IF NOT EXISTS ameesh_events_type_object_id_idx
    ON ameesh_events (type, object, id DESC);
