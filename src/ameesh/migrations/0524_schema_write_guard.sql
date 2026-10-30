-- SPDX-License-Identifier: AGPL-3.0-only
-- #4975: deploy this additive marker and its reader BEFORE the next
-- non-additive migration. Code predating this guard still needs stopping.
CREATE TABLE schema_non_additive_migrations (
    version integer PRIMARY KEY REFERENCES schema_migrations(version),
    name text NOT NULL,
    checksum text NOT NULL
);
