-- SPDX-License-Identifier: AGPL-3.0-only
-- 0429_ci_run_attempt — L161c, #901 : une exécution relancée est suivie.
--
-- `gh run rerun` (le geste courant sous saturation, quand un job est annulé)
-- garde le même `run_id` et incrémente `run_attempt` côté GitHub. Le relevé ne
-- connaissait que le `run_id` : la ligne `ci_runs` restait `cancelled`, le
-- `_follow` la croyait déjà suivie, aucun verdict n'était envoyé et
-- `ameesh ci status` restait figé sur l'attempt précédent.
--
-- `ci_runs.run_attempt` porte l'attempt suivi : la ligne conclue que GitHub dit
-- de nouveau `queued`/`in_progress` est rouverte (`running`, nouvel attempt) et
-- suivie jusqu'à son verdict. `ci_test_failures.run_attempt` dit de quel
-- attempt un échec est le témoin : un test qui échoue encore à la relance est
-- re-signalé (ligne remise à l'attempt courant, message rouvert), un test qui
-- échouait à l'attempt précédent et passe n'est plus imputé à l'exécution.
--
-- Additif : deux colonnes avec défaut, aucun retrait, aucune contrainte
-- resserrée ; le code d'avant continue d'écrire (les lignes restent attempt 1).

alter table ci_runs
    add column if not exists run_attempt integer not null default 1;

alter table ci_test_failures
    add column if not exists run_attempt integer not null default 1;
