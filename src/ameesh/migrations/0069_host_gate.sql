-- SPDX-License-Identifier: AGPL-3.0-only
-- 0069_host_gate — the host gate in host resource readings (L159f).
--
-- A lending host's executor obeys the state its capacity guard writes
-- (`ameesh-host-state/1`, docs/GARDE-DE-CAPACITE.md). Each reading records
-- the state the executor obeyed when it was taken, so that relocation
-- (0028 §5) never sends an agent to a host that is withdrawing:
--   * `gate_state`: `available`, `draining` or `stopped` (a stale or
--     unreadable state already counted as its fallback); null when the
--     executor has no gate (classic host);
--   * `gate_seq`: the `seq` of that state (-1 for a fallback);
--   * `gate_reason`: the guard's reason, free text, bounded by the executor.
-- The supervisor role reads all three (deploy/sql/role-superviseur.sql).

alter table host_resources add column if not exists gate_state text;
alter table host_resources add column if not exists gate_seq bigint;
alter table host_resources add column if not exists gate_reason text;
