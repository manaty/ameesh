-- SPDX-License-Identifier: AGPL-3.0-only
-- #4148 (B of #3167): the host lifecycle emits busy/draining/idle-drained to
-- an external adapter, in a strict order.
--
-- * `release_state`/`hook_state` separate what ameesh has decided (drain the
--   host) from what the adapter has acknowledged. A hook failure keeps the
--   previous acknowledged state (the host is not made evictable) while claims
--   stay closed.
-- * `claims_open` closes claims during `returning` too: a host woken by the
--   `busy` hook must not take a lease before the adapter has answered. It is
--   set by `drain` and reopened only by the `busy` transition (or by a manual
--   `undrain` without a hook). `ameesh_host_claims_open` reads it.
-- * `host_lifecycle_transitions` journals every successful transition per
--   (host, generation, state): a replay calls no hook twice, and the journal
--   is the evidence an operator reads.
-- * `host_release_locks` serialises the release scanner of one host across
--   the many runners of that host; `next_scan_at` spaces the expensive idle
--   projection without changing the cheap transition cadence.
-- * run counts are NOT declared by the adapter (owner decision §3.3): they
--   come from A's host measure (#4147). B treats A's placeholder source as
--   unknown and records on the scanner lock why a host was not drained.

ALTER TABLE host_lifecycle
    ADD COLUMN release_state text NOT NULL DEFAULT 'busy'
        CHECK (release_state IN ('busy', 'draining', 'idle-drained')),
    -- the last state the adapter acknowledged; NULL: no hook answered yet
    ADD COLUMN hook_state text
        CHECK (hook_state IN ('busy', 'draining', 'idle-drained')),
    ADD COLUMN claims_open boolean NOT NULL DEFAULT true,
    -- #4148: invalidates in-flight adapter calls when a return is requested;
    -- a transition started under an older epoch can never be recorded.
    ADD COLUMN release_epoch bigint NOT NULL DEFAULT 1,
    ADD COLUMN release_generation bigint,
    -- true: ameesh drained the host itself (burst idle), false: an operator did
    ADD COLUMN automatic boolean NOT NULL DEFAULT false,
    ADD COLUMN released_at timestamptz,
    ADD COLUMN release_error text,
    ADD COLUMN release_error_state text,
    ADD COLUMN release_error_at timestamptz,
    -- first failure of the current episode: a stable alert identity
    ADD COLUMN release_error_since timestamptz,
    ADD COLUMN hook_attempt_at timestamptz,
    ADD COLUMN hook_attempt_state text,
    ADD COLUMN hook_attempt_generation bigint,
    ADD COLUMN hook_attempt_epoch bigint;
    -- run counts are read from A's host measure (#4147), never persisted here.

UPDATE host_lifecycle
   SET release_generation = generation,
       release_state = CASE WHEN mode = 'draining' THEN 'draining' ELSE 'busy' END,
       -- A withdrawal in progress already closes claims through `mode`; the
       -- durable flag is false so a return cannot reopen them by accident.
       claims_open = (mode <> 'draining'),
       hook_state = CASE WHEN mode = 'draining' THEN NULL ELSE 'busy' END;

CREATE TABLE host_lifecycle_transitions (
    host text NOT NULL REFERENCES host_lifecycle(host) ON DELETE CASCADE,
    generation bigint NOT NULL,
    release_epoch bigint NOT NULL DEFAULT 1,
    state text NOT NULL CHECK (state IN ('busy', 'draining', 'idle-drained')),
    actor text NOT NULL,
    detail jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (host, generation, release_epoch, state)
);

CREATE TABLE host_release_locks (
    -- no foreign key: the scanner must serialise before the first control row
    -- of a host exists (auto-drain of a never-drained burst host).
    host text PRIMARY KEY,
    owner text NOT NULL,
    expires_at timestamptz NOT NULL,
    next_scan_at timestamptz,
    -- #4148: why the last scan did not drain (unknown runs from A's measure),
    -- kept here so the alert pass does not recompute the idle projection.
    blocked_reason text,
    blocked_source text,
    blocked_since timestamptz,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- `returning` keeps claims closed until the `busy` hook answered (#4148 B).
-- The release fence of L232b is unchanged: a live fence still closes every
-- claim except the enrolled probe.
CREATE OR REPLACE FUNCTION ameesh_host_claims_open(wanted_host text)
RETURNS boolean LANGUAGE plpgsql VOLATILE AS $$
BEGIN
    RETURN NOT EXISTS (SELECT 1 FROM host_lifecycle
                       WHERE host = wanted_host
                         AND (mode IN ('draining', 'lost') OR NOT claims_open))
       AND NOT EXISTS (SELECT 1 FROM host_release_fences WHERE host = wanted_host);
END $$;

CREATE OR REPLACE FUNCTION ameesh_host_gate_state(wanted_host text, observed text)
RETURNS text LANGUAGE plpgsql VOLATILE AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM host_lifecycle
               WHERE host = wanted_host AND mode = 'lost') THEN
        RETURN 'lost';
    END IF;
    IF NOT ameesh_host_claims_open(wanted_host) THEN
        RETURN 'draining';
    END IF;
    RETURN observed;
END $$;
