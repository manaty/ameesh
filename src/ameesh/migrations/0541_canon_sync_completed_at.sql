-- #5653: timestamp of the last COMPLETED canon sync of a host.
-- `periodic_sync` reuses a completed result only while it is recent
-- (`SYNC_REUSE_MAX_INTERVALS` x `canon_sync_interval`): a runner restarted
-- after a long stop must run a fresh sync instead of settling the host gate
-- on a result of arbitrary age.
ALTER TABLE canon_state
    ADD COLUMN IF NOT EXISTS sync_completed_at timestamptz;

-- Existing completed results get the best conservative proxy available: the
-- timestamp of the claim that produced them, but only when that claim is still
-- the current one (a newer pending claim must not make an old result look
-- fresh); otherwise the last `ok` record, which precedes completion. A recent
-- result stays reusable across the deploy (the staging restart sequence would
-- otherwise pay one more full sync); an old one keeps its old age and is
-- refused. A row with no usable timestamp stays NULL and is not reused until
-- the next complete sync publishes it.
UPDATE canon_state
   SET sync_completed_at = coalesce(
         CASE WHEN sync_token = sync_completed_token
              THEN sync_claimed_at END,
         last_good_at, checked_at)
 WHERE sync_completed_token IS NOT NULL
   AND sync_completed_at IS NULL;
