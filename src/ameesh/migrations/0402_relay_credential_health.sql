-- SPDX-License-Identifier: AGPL-3.0-only
-- #317: relay failures are recorded by the server, separately from CLI turns.
alter table credential_health drop constraint credential_health_source_check;
alter table credential_health add constraint credential_health_source_check
    check (source in ('sentinel', 'turn', 'manual', 'relay'));
