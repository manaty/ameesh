-- SPDX-License-Identifier: AGPL-3.0-only
-- #5932: an asynchronous adapter wait is not a failure.
--
-- The VM adapter hook queues a durable request and answers 75 (EX_TEMPFAIL)
-- while its separate operator worker has not completed it. The scanner must
-- not acknowledge the state (the resource is not released yet), must not
-- reopen claims, and must not record `release_error`: the wait is normal.
--
-- `hook_wait_at`/`hook_wait_state` keep that explicit wait on the control
-- row. `hook_wait_generation`/`hook_wait_epoch` fence it to the wave that
-- queued it: a reader must only trust the wait while both still match the
-- live control. A generation/epoch change or any completed/failed transition
-- clears it. The fence also covers an N-1 writer during a rolling deploy:
-- that writer changes the epoch/generation without knowing the new columns,
-- so the persisted wait stops matching and the new reader ignores it instead
-- of masking a real loss (#5690 compatibility).
-- `hook_state` and `release_state` stay untouched until the worker journals
-- the real completion, so the acknowledged state is never advanced by a
-- request that is merely queued.
ALTER TABLE host_lifecycle
    ADD COLUMN hook_wait_at timestamptz,
    ADD COLUMN hook_wait_state text
        CHECK (hook_wait_state IN ('busy', 'draining', 'idle-drained')),
    ADD COLUMN hook_wait_generation bigint,
    ADD COLUMN hook_wait_epoch bigint;
