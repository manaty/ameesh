-- U6 observation: ordered alert pages and indexed case-insensitive issue proof.
-- Keep historical event bodies out of the index.
CREATE INDEX IF NOT EXISTS ameesh_events_alert_project_id_idx
    ON ameesh_events (project, id)
    WHERE type = 'alert.raised';
CREATE INDEX IF NOT EXISTS work_items_app_issue_fold_id_idx
    ON work_items (app, lower(issue_ref), id);
