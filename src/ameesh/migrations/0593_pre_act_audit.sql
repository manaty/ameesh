-- SPDX-License-Identifier: AGPL-3.0-only
-- #7761: additive, shared across watcher hosts. Mail and consumption commit together.
CREATE TABLE pre_act_audit_cursors (
    project text NOT NULL,
    repo text NOT NULL,
    workflow text NOT NULL,
    activated_at double precision NOT NULL,
    scanned_through double precision NOT NULL,
    PRIMARY KEY (project, repo, workflow)
);
CREATE TABLE pre_act_audit_runs (
    repo text NOT NULL,
    run_id bigint NOT NULL,
    attempt integer NOT NULL,
    workflow text NOT NULL,
    pregeste_id text,
    reason text NOT NULL,
    PRIMARY KEY (repo, run_id, attempt)
);
-- A receipt is single-use across repositories, workflows and re-runs.
CREATE UNIQUE INDEX pre_act_audit_consumed ON pre_act_audit_runs (pregeste_id)
    WHERE reason = '';
