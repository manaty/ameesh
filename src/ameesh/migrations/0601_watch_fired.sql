-- SPDX-License-Identifier: AGPL-3.0-only
-- 0601_watch_fired — #7933 : le battement d'un tir de veille, même sans envoi.
--
-- `watch send` ne dépose rien dans plusieurs cas sains : lecture inchangée et
-- tour récent, changement gardé en attente, note passive encore non lue,
-- compte rendu identique non dû. Mesurer la muette sur `last_sent_at` seul
-- alertait donc sur des veilles qui tiraient. Le tir écrit désormais
-- `last_fired_at` (et le motif du saut) dans la transaction qui porte déjà le
-- fencing du bail ; `watch_silent` mesure le plus récent des deux.
--
-- `last_attempt_at` sépare la cadence d'envoi du battement : l'unité d'un
-- rôle élu tique toutes les 60 s pour voir une relève, mais ne relit la veille
-- qu'une fois `every_s` écoulé. Un tir « pas encore dû » ne bat que
-- `last_fired_at` ; une tentative réelle (envoi ou saut sain) avance aussi
-- `last_attempt_at`, pour ne pas repartir en lecture à chaque battement.
--
-- Additive seulement : trois colonnes nullables, aucune donnée réécrite. Une
-- ligne existante garde `last_fired_at` NULL jusqu'au premier tir.

alter table agent_watches add column if not exists last_fired_at timestamptz;
alter table agent_watches add column if not exists last_fired_reason text;
alter table agent_watches add column if not exists last_attempt_at timestamptz;

comment on column agent_watches.last_fired_at is
    '#7933: when the watch last fired (sent, deliberately skipped, or heartbeat of a not-due tick), under the same role-lease fencing as the message; NULL = never fired since this migration.';
comment on column agent_watches.last_fired_reason is
    '#7933: why that firing sent nothing (not-due, recent-turn, change-pending, unread-note, report-reminder), or sent when it deposited the instruction.';
comment on column agent_watches.last_attempt_at is
    '#7933: when the watch last evaluated its reading (sent or healthy skip); the cadence gate uses it, never the 60 s liveness heartbeat.';
