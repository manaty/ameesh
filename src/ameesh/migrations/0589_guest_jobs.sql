-- SPDX-License-Identifier: AGPL-3.0-only
-- 0589_guest_jobs — live offloaded jobs in host resource readings (#7687).
--
-- A lending host (`policy.lend`, L159b) runs guest jobs offloaded from other
-- hosts, outside its own turns and leases. `runs_interface: none` cannot
-- declare them absent (#5905); the new `guest` interface needs their count
-- in the host measure (#4147): the number of `ameesh-job-*.slice` cgroups of
-- the offload shell that are populated, read by `resources.guest_jobs`.
--
-- Null when the cgroup tree is unreadable, or when an empty job slice is
-- young enough to be a job between two phases (`resources.GUEST_JOB_EMPTY_S`):
-- the release keeps the runs unknown, never a zero by default. An empty slice
-- older than that is the leftover of a job whose shell died, and ignored.
-- Additive; a reading from before keeps null.
-- The supervisor role reads it (deploy/sql/role-superviseur.sql).

alter table host_resources add column if not exists guest_jobs integer;
