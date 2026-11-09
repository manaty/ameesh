-- SPDX-License-Identifier: AGPL-3.0-only
-- #6475 / manaty/ameesh#4980: U14 A11 retry journal, additive only.
-- No raw text enters here: origin/intent/cause are bounded tokens, episode is
-- a bounded identity, and the idempotency key is a caller-supplied 64-hex
-- digest.  The attempt counter is durable so it survives a holder handover.
CREATE TABLE repair_retry_attempts (
    project text NOT NULL,
    environment text NOT NULL,
    episode text NOT NULL CHECK (char_length(episode) BETWEEN 1 AND 200),
    cause text NOT NULL CHECK (cause ~ '^[a-z][a-z0-9_.-]{0,63}$'),
    attempts bigint NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    -- Series generation: a late completion of an older attempt must never
    -- reset the counter of a newer series (out-of-order settle, #6475 B2).
    generation bigint NOT NULL DEFAULT 0 CHECK (generation >= 0),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project, environment, episode, cause)
);

CREATE TABLE repair_retries (
    idempotency_key text PRIMARY KEY CHECK (idempotency_key ~ '^[0-9a-f]{64}$'),
    project text NOT NULL,
    environment text NOT NULL,
    origin text NOT NULL CHECK (origin IN ('operator','agent','automatic')),
    intent text NOT NULL CHECK (intent ~ '^[a-z][a-z0-9_.-]{0,63}$'),
    cause text NOT NULL CHECK (cause ~ '^[a-z][a-z0-9_.-]{0,63}$'),
    episode text NOT NULL CHECK (char_length(episode) BETWEEN 1 AND 200),
    attempt bigint NOT NULL CHECK (attempt >= 0),
    generation bigint NOT NULL DEFAULT 0 CHECK (generation >= 0),
    requested_at timestamptz NOT NULL DEFAULT now(),
    engaged_at timestamptz,
    completed_at timestamptz,
    failed_at timestamptz,
    unknown_at timestamptz,
    avoided_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    -- One outcome only, and an effect always comes after an engagement.
    CHECK (NOT (completed_at IS NOT NULL AND failed_at IS NOT NULL)),
    CHECK (NOT (completed_at IS NOT NULL AND unknown_at IS NOT NULL)),
    CHECK (NOT (failed_at IS NOT NULL AND unknown_at IS NOT NULL)),
    CHECK (engaged_at IS NOT NULL OR (completed_at IS NULL AND failed_at IS NULL
                                      AND unknown_at IS NULL)),
    -- An avoided retry is an idempotence proof, never an effect or an attempt.
    CHECK (avoided_at IS NULL OR (attempt = 0 AND engaged_at IS NULL
                                  AND completed_at IS NULL AND failed_at IS NULL
                                  AND unknown_at IS NULL))
);
CREATE INDEX repair_retries_report
    ON repair_retries (requested_at, project, environment);
