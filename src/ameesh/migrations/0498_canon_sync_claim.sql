-- #3963: short compare-and-swap per host/canon, no lock held across Git.
ALTER TABLE canon_state
    ADD COLUMN IF NOT EXISTS sync_claimed_at timestamptz,
    ADD COLUMN IF NOT EXISTS sync_token text,
    ADD COLUMN IF NOT EXISTS sync_completed_token text,
    ADD COLUMN IF NOT EXISTS sync_signature text;
