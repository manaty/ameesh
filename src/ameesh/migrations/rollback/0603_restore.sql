-- SPDX-License-Identifier: AGPL-3.0-only
-- #8031 : retour arrière de 0603 (réveil d'une VM CI sur un job en file).
--
-- À appliquer AVANT de redéployer le code N-1 : le lecteur de réveil et le
-- veilleur nomment la colonne et la table ; sans elles la garde de la CI
-- échoue à chaque passage. Script manuel, non rejoué par l'applicateur.
set local lock_timeout = '5s';

drop index if exists ci_queue_wait_seen_idx;
drop table if exists ci_queue_wait;
drop table if exists ci_queue_wait_scan;
alter table ci_queue_state
    drop column if exists wait_seen_at;
alter table ci_runner_state
    drop column if exists labels;
