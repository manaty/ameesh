-- SPDX-License-Identifier: AGPL-3.0-only
-- 0048_pid_started_at — heure de démarrage du PID lié en secondes epoch
-- (lot L63, couche plateforme, étude « portabilité Mac et Windows »).
--
-- `session_bindings.pid_start` (0036) stocke des TOPS D'HORLOGE depuis le
-- démarrage de l'hôte (champ 22 de /proc/<pid>/stat) : une mesure propre à
-- Linux. La couche plateforme (psutil, ou /proc en repli) rend sur tous les
-- OS des secondes epoch, qui ne se comparent pas aux tops : sans nouvelle
-- colonne, toute liaison existante serait vue comme « PID réutilisé ».
--
--   * `pid_started_at` (double precision, secondes epoch) est la seule
--     colonne écrite désormais (`ameesh mail bind --pid`, re-liaison) ;
--   * `pid_start` reste LUE pour les liaisons antérieures : convertie en
--     epoch (démarrage de l'hôte + tops / CLK_TCK, tolérance d'une seconde),
--     sous Linux seulement — ailleurs la liaison n'est pas reconnue
--     (fail-closed). Une re-liaison la remet à NULL. Elle sera supprimée
--     quand plus aucune liaison active ne la portera.
--
-- Rien n'est recopié ici : la conversion dépend du démarrage de l'hôte qui a
-- mesuré, que la base ne connaît pas.

alter table session_bindings add column if not exists pid_started_at double precision;

comment on column session_bindings.pid_started_at is
  'L63 (0048) : heure de démarrage du PID lié, secondes epoch ; NULL = non contrôlée (ou ancienne pid_start).';
comment on column session_bindings.pid_start is
  'L46 (0036), remplacée par pid_started_at (0048) : tops d''horloge Linux, encore lue par conversion ; plus écrite.';
