-- SPDX-License-Identifier: AGPL-3.0-only
-- #6320 / manaty/ameesh#4882: additive A1 journal. No existing table changes.
-- Positions are allocated under the stream row lock, not a sequence: a late
-- commit cannot appear behind an already consumed pagination watermark.
CREATE TABLE repair_signal_streams (
    project text NOT NULL,
    environment text NOT NULL,
    source text NOT NULL CHECK (source IN ('thread', 'inbox', 'github_watch', 'alerts')),
    cursor text,
    position bigint NOT NULL DEFAULT 0 CHECK (position >= 0),
    PRIMARY KEY (project, environment, source)
);
CREATE TABLE repair_signals (
    id text PRIMARY KEY,
    project text NOT NULL,
    environment text NOT NULL,
    source text NOT NULL,
    native_id text NOT NULL,
    position bigint NOT NULL CHECK (position > 0),
    envelope jsonb NOT NULL CHECK (jsonb_typeof(envelope) = 'object'),
    outcome text NOT NULL DEFAULT 'pending'
        CHECK (outcome IN ('pending','lot','basket','decision','ignored','unknown','refused')),
    outcome_ref text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project, environment, source, native_id),
    UNIQUE (project, environment, source, position),
    FOREIGN KEY (project, environment, source)
        REFERENCES repair_signal_streams(project, environment, source)
);
CREATE INDEX repair_signals_report ON repair_signals (created_at, project, environment);
-- Invalid input is represented only by a caller-supplied metadata identity
-- digest and an enumerated reason. No rejected payload or error text is kept.
CREATE TABLE repair_signal_refusals (
    project text NOT NULL,
    environment text NOT NULL,
    source text NOT NULL,
    rejection_key text NOT NULL CHECK (rejection_key ~ '^[0-9a-f]{64}$'),
    reason text NOT NULL CHECK (reason IN ('invalid_envelope','unsupported_schema','source_error')),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project, environment, source, rejection_key)
);
