-- SPDX-License-Identifier: AGPL-3.0-only
-- 0424_ci_run_queue_time — the wait for a runner, apart from the run (#747).
--
-- On 13/10 a run of main read as "psql hung 2 h 20" was 1 h 24 waiting for a
-- runner and 18 min of tests: `ci_runs` only had the first sighting and the
-- conclusion, so queue and run time could not be told apart. `created_at` is
-- GitHub's creation of the run; `started_at` is the first relevé that saw it
-- `in_progress` (precise to one `ameesh notify` pass). A `running` row
-- without `started_at` is still waiting: `ameesh ci status` shows the wait,
-- and the `ci_queue_starved` alert fires past its threshold.
alter table ci_runs add column if not exists created_at timestamptz;
alter table ci_runs add column if not exists started_at timestamptz;
