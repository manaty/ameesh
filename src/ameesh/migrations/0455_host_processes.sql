-- SPDX-License-Identifier: AGPL-3.0-only
-- 0455_host_processes — process and zombie counts in host resource readings
-- (#2022).
--
-- On 16/10 the bureau found by hand 5 900 zombies on the workstation, 11 000
-- on agents-1 and 6 200 on agents-2 (#1749: `git <defunct>` never waited by
-- the runners), close to exhausting the PIDs; no reading said it. Every
-- runner now counts, at each reading:
--   procs_total, procs_zombie   the processes of the host, and those in
--                               state Z (the same for every runner of a host);
--   runner_id                   the runner that published the reading (one
--                               runner per agent on a host);
--   runner_procs, runner_zombies  that runner's descendants, and the zombies
--                               among them (the runner that does not wait).
-- Null when the process table cannot be read (not Linux, no psutil).
-- Numbers and a runner id only; additive, a reading from before keeps nulls.
-- The supervisor role reads them (deploy/sql/role-superviseur.sql).

alter table host_resources add column if not exists procs_total integer;
alter table host_resources add column if not exists procs_zombie integer;
alter table host_resources add column if not exists runner_id text;
alter table host_resources add column if not exists runner_procs integer;
alter table host_resources add column if not exists runner_zombies integer;
