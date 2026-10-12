-- SPDX-License-Identifier: AGPL-3.0-only
-- L385: explicit prerequisites between execution lots. No inferred prose.
CREATE TABLE IF NOT EXISTS work_item_dependencies (
    work_item_id bigint NOT NULL REFERENCES work_items(id) ON DELETE CASCADE,
    blocker_id bigint NOT NULL REFERENCES work_items(id) ON DELETE CASCADE,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (work_item_id, blocker_id),
    CHECK (work_item_id <> blocker_id)
);
CREATE INDEX IF NOT EXISTS work_item_dependencies_blocker
    ON work_item_dependencies(blocker_id);
