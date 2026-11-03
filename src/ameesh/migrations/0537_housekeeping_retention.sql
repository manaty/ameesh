-- SPDX-License-Identifier: AGPL-3.0-only
-- #5588 : éviter le parcours intégral du journal à chaque purge de rétention.
CREATE INDEX IF NOT EXISTS housekeeping_log_at_idx ON housekeeping_log (at);
