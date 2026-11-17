-- SPDX-License-Identifier: AGPL-3.0-only
-- 0589_guest_jobs — live offloaded jobs in host resource readings (#7687).
--
-- A lending host (`policy.lend`, L159b) runs guest jobs offloaded from other
-- hosts, outside its own turns and leases. `runs_interface: none` cannot
-- declare them absent (#5905); the new `guest` interface needs their count
-- in the host measure (#4147), read by `resources.guest_jobs`: the live
-- `ameesh-offload-shell` processes of the guest, plus its populated
-- `ameesh-job-*.slice` cgroups (an orphaned job whose shell died).
--
-- Null when the process table (or part of it: `hidepid`, a container's own
-- namespace) or a part of the cgroup tree is unreadable: the release keeps
-- the runs unknown, never a zero by default. The shell
-- marker covers the job's whole life (before its first slice, whatever the
-- executor mode), so no age of an empty slice is guessed. Additive; a
-- reading from before keeps null.
-- The supervisor role reads it (deploy/sql/role-superviseur.sql).

alter table host_resources add column if not exists guest_jobs integer;
