-- SPDX-License-Identifier: AGPL-3.0-only
-- 0616_restore — #8422 : retrait des preuves de veille des runners CI.
--
-- Le code N-1 ne lit ni n'écrit ces colonnes. Revenir en N-1 avant ce retrait
-- quand le worker 0616 tourne encore : il nommerait des colonnes absentes et
-- refuserait ses ticks (le doute est sûr, mais aucun retour ne se prouverait).
-- Retirer ces colonnes ne change pas l'état des VM.
ALTER TABLE ci_runner_state
    DROP COLUMN IF EXISTS last_busy_at,
    DROP COLUMN IF EXISTS online;
