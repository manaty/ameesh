-- SPDX-License-Identifier: AGPL-3.0-only
-- #7477: immutable composition receipts, distinct from current membership.
CREATE TABLE work_packet_snapshots (
    id bigserial PRIMARY KEY,
    repository text NOT NULL,
    target text NOT NULL,
    package_id bigint NOT NULL REFERENCES work_items(id),
    branch text NOT NULL,
    head_sha text NOT NULL CHECK (head_sha ~ '^[0-9a-f]{40,64}$'),
    base_sha text NOT NULL CHECK (base_sha ~ '^[0-9a-f]{40,64}$'),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    recorded_by text NOT NULL,
    lease_owner text NOT NULL,
    lease_epoch bigint NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (repository, target, package_id, head_sha)
);
CREATE INDEX work_packet_snapshots_lookup
    ON work_packet_snapshots(repository, branch, head_sha);
