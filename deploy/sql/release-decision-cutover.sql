-- SPDX-License-Identifier: AGPL-3.0-only
\set ON_ERROR_STOP on
\if :{?role}
\else
  \set role ameesh_release_controller
\endif
BEGIN;
SELECT set_config('ameesh.release_role', :'role', true) \g /dev/null
-- Controller stopped; additive step already installed.
\ir _revoke-release-mailbox.sql
\ir _contrat-release-decision.sql
\ir _contrat-mailbox-readers.sql
COMMIT;
