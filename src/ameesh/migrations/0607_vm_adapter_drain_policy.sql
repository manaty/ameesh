-- SPDX-License-Identifier: AGPL-3.0-only
-- Public timing only; the private enrollment and cloud credentials stay local.
CREATE TABLE vm_adapter_drain_policy (
    host text PRIMARY KEY,
    idle_after_s double precision NOT NULL CHECK (idle_after_s > 0),
    min_on_s double precision NOT NULL CHECK (min_on_s > 0),
    min_off_s double precision NOT NULL CHECK (min_off_s > 0),
    wake_delay_s double precision NOT NULL CHECK (wake_delay_s > 0),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
