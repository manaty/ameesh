-- SPDX-License-Identifier: AGPL-3.0-only
-- 0615_standby_exit_state — #8459 (issue manaty/ameesh#6668).
--
-- #8420 runs `mode-veille.sh entrer` before halting a replica and `sortir`
-- once every replica is back. `entrer` empties the hub's
-- `synchronous_standby_names`; a failed `sortir` leaves it empty: no COMMIT
-- blocks (safe) but synchronous durability stays degraded with no deadline
-- unless the debt is durable.
--
-- * `standby_entered_at`: standby mode may be active, `sortir` is owed. The
--   VM adapter writes it under the tick's transaction BEFORE the external
--   `entrer`, like the halt intent, so a failed or killed probe still records
--   that the list may be empty. A successful `sortir` clears it.
-- * `standby_exit_failed_at`: first failed `sortir` of the current debt. The
--   adapter keeps replaying `sortir` at every tick while either column is
--   set, and `host_release.alerts` raises `host_release_standby_exit_failed`
--   from it (a stable "since" for the episode). Cleared by a successful
--   `sortir`, together with `standby_entered_at`.
--
-- Additive and N-1 compatible: an older writer never names these columns and
-- leaves them alone; the older reader ignores them. Only the VM adapter and
-- the alert projection read them.
ALTER TABLE host_lifecycle
    ADD COLUMN standby_entered_at timestamptz,
    ADD COLUMN standby_exit_failed_at timestamptz;
