-- SPDX-License-Identifier: AGPL-3.0-only
-- #5925: operator-side queue; no cloud secret or command in the database.
CREATE TABLE vm_adapter_requests (
    host text NOT NULL,
    generation bigint NOT NULL,
    release_epoch bigint NOT NULL,
    state text NOT NULL CHECK (state IN ('draining','idle-drained','busy')),
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','done','obsolete')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    result text,
    PRIMARY KEY(host,generation,release_epoch,state)
);
CREATE INDEX vm_adapter_requests_pending_idx ON vm_adapter_requests(host,created_at)
    WHERE status='pending';
CREATE TABLE vm_adapter_observations (
    host text PRIMARY KEY,
    provider_state text NOT NULL,
    on_since timestamptz,
    off_since timestamptz,
    poweron_at timestamptz,
    wake_delay_s double precision,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
