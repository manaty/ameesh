-- SPDX-License-Identifier: AGPL-3.0-only
-- 0061_host_measure — shared host measure (lot L159d, study L159 §1.4).
--
-- The readings of L31 gave the memory and the load of a host, not how much
-- its users suffer nor how much it could lend. New columns, NULL when the
-- host does not give them (no PSI, no guest slice, first reading of a
-- process) — no guess:
--   * `mem_total_bytes`: total memory (prudent lendable share);
--   * `psi_cpu_some`, `psi_mem_some`, `psi_mem_full`, `psi_io_some`: pressure
--     stall information of the host, 60 s averages, in percent;
--   * `app_cpu`, `guest_cpu`, `cpu_window_s`: CPU used by the application
--     (everything but the guest) and by the guest (`ameesh-guest.slice`,
--     `ameesh-agents.slice`), averaged since the previous reading;
--   * `guest_mem_bytes`: memory charged to the guest slices;
--   * `lendable_cpu`, `lendable_memory_bytes`, `lendable_disk_bytes`: capacity
--     the host could lend, computed on 7 days of readings, BEFORE the offer
--     of the Host fiche (`policy.lend`), which is applied on reading.
-- The threshold `max_cpu_pressure` stays declarative (`policy.resources`).

alter table host_resources add column if not exists mem_total_bytes bigint;
alter table host_resources add column if not exists psi_cpu_some double precision;
alter table host_resources add column if not exists psi_mem_some double precision;
alter table host_resources add column if not exists psi_mem_full double precision;
alter table host_resources add column if not exists psi_io_some double precision;
alter table host_resources add column if not exists app_cpu double precision;
alter table host_resources add column if not exists guest_cpu double precision;
alter table host_resources add column if not exists cpu_window_s double precision;
alter table host_resources add column if not exists guest_mem_bytes bigint;
alter table host_resources add column if not exists lendable_cpu double precision;
alter table host_resources add column if not exists lendable_memory_bytes bigint;
alter table host_resources add column if not exists lendable_disk_bytes bigint;
