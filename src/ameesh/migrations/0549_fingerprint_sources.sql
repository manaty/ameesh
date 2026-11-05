-- SPDX-License-Identifier: AGPL-3.0-only
-- #5968: a project may read several failure-fingerprint sources.
--
-- §16 used to declare one source per project (production only), so the cursor
-- table `fingerprint_watch_state` is keyed by `project`: a staging export
-- could not be read without sharing (and skipping) the production cursor.
-- This migration adds a **new** table keyed by `(project, environment)`; it
-- never alters `fingerprint_watch_state`, whose `project` primary key is the
-- N-1 barrier (#5946): the ameesh 1.39.0 still in service during a rolling
-- deployment keeps reading and writing the old table with `ON CONFLICT
-- (project)`.  The pass mirrors its production row into the old table, so both
-- directions of a takeover keep a usable cursor (a replay is idempotent: the
-- fingerprint primitive notes duplicates instead of opening a second lot).
--
-- Only addition and a seed from the old cursor: no row of the old table is
-- modified.
CREATE TABLE IF NOT EXISTS fingerprint_source_state (
    project      text NOT NULL,
    environment  text NOT NULL DEFAULT 'production'
                 CHECK (environment IN ('production', 'staging')),
    url          text NOT NULL,
    cursor       text,
    last_read_at timestamptz,
    last_error   text NOT NULL DEFAULT '',
    updated_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project, environment)
);

-- The project's historical cursor is its production source.  An empty URL is
-- a row that was never read; it has no cursor to carry.
INSERT INTO fingerprint_source_state
    (project, environment, url, cursor, last_read_at, last_error, updated_at)
SELECT project, 'production', url, cursor, last_read_at, last_error, updated_at
FROM fingerprint_watch_state
WHERE url <> ''
ON CONFLICT (project, environment) DO NOTHING;
