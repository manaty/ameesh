-- SPDX-License-Identifier: AGPL-3.0-only
-- 0616_ci_runner_veille — #8422 (issue manaty/ameesh#6640), tranche 5 de #8029.
--
-- The VM adapter stops the CI VMs (`ameesh-ci-1..3`) at rest and wakes them
-- on a queued job. #8031 already measured the wake queue and the labels of
-- every registered runner. Two facts were missing to prove a stop and a
-- return without the host reading and the probe agent a CI VM does not have:
--
-- * `online`: the runner is currently connected. `busy` alone could not tell
--   an idle online runner from a runner of a stopped VM: an offline runner
--   is never busy. The adapter requires every registered runner of the host
--   online in a reading made after the power-on to prove the return.
-- * `last_busy_at`: the last scan that saw this runner busy. `busy_since`
--   dates the current busy state and is cleared when the runner becomes idle
--   (0426); the stop proof needs "no runner busy since idle_after_s", so the
--   last busy instant must survive the idle transition.
--
-- Both are written by the only writer of `ci_runner_state`, `ci.Watcher`
-- (`observe_runners`), and read by the VM adapter. Additive and N-1
-- compatible: an older watcher does not name them and leaves the defaults
-- (`online=false`, `last_busy_at=NULL`), which are doubt, never a proof.
ALTER TABLE ci_runner_state
    ADD COLUMN online boolean NOT NULL DEFAULT false,
    ADD COLUMN last_busy_at timestamptz;

-- Existing rows: the current busy instant is the only truthful seed. An idle
-- runner's last busy is unknown (NULL); the adapter then waits for a busy
-- scan or uses the VM's own `on_since` observation.
UPDATE ci_runner_state
   SET last_busy_at = busy_since
 WHERE last_busy_at IS NULL AND busy AND busy_since IS NOT NULL;
