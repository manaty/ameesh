-- SPDX-License-Identifier: AGPL-3.0-only
-- #6473: advisory A3 receipts; historical fingerprint sources stay unchanged.
CREATE TABLE fingerprint_cause_observations (
    receipt_id text PRIMARY KEY,
    project text NOT NULL,
    environment text NOT NULL,
    source text NOT NULL,
    signal_id text NOT NULL,
    alias text NOT NULL,
    cause_id text NOT NULL,
    lot bigint REFERENCES work_items(id) ON DELETE SET NULL,
    record jsonb NOT NULL CHECK (jsonb_typeof(record) = 'object'),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX fingerprint_cause_exact ON fingerprint_cause_observations
    (project, environment, source, cause_id, lot) WHERE cause_id <> '';
CREATE INDEX fingerprint_cause_alias ON fingerprint_cause_observations
    (project, environment, source, alias);
