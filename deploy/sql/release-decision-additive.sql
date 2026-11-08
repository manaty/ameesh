-- SPDX-License-Identifier: AGPL-3.0-only
\set ON_ERROR_STOP on
\if :{?role}
\else
  \set role ameesh_release_controller
\endif
BEGIN;
SELECT set_config('ameesh.release_role', :'role', true) \g /dev/null
-- Step 0: existing controller required; preserves its SELECT.
\ir _release-decision.sql
COMMIT;
