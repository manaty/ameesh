-- SPDX-License-Identifier: AGPL-3.0-only
-- 0614_lot_wake_replay — #8458 : le rejeu d'un réveil de lot non lancé survit
-- au redémarrage et reste par lot.
--
-- Constat (#8427, relecture de #8423) : `_settle_lot_wake` gardait le rejeu
-- en mémoire seulement, et l'effaçait sans comparer le lot ; un réveil compté
-- sur le lot A était oublié si le réveil du lot B partait d'abord, ou si
-- l'exécuteur redémarrait entre `note_wake` et le lancement.
--
-- Le compte `wakes` reste la mesure du délai doublé ; ces trois colonnes
-- portent la dette d'un réveil compté dont le tour n'a pas lancé son harnais :
-- `wake_pending_at` (l'instant du réveil, NULL quand la dette est close),
-- `wake_retries` (rejeux déjà réclamés, bornés par `LOT_WAKE_RETRY_MAX`) et
-- `wake_retry_reason` (dernière raison lisible). Toute la ligne est par
-- (agent, lot) : le rejeu d'un lot n'est ni effacé ni compté par un autre.
--
-- Additive et compatible N-1 : trois colonnes neuves avec défaut ou NULL, que
-- le code d'avant n'écrit ni ne lit. Une ligne existante n'a pas de dette.
-- Pendant un déploiement par vagues, un écrivain N-1 qui note un réveil avance
-- `last_wake_at` sans toucher `wake_pending_at` : la dette devient caduque
-- (`wake_pending_at < last_wake_at`), car ce tour-là a pris le réveil en
-- charge (B mty-amh-impl-11, N-1 sur ligne existante). Un nouveau réveil du
-- code N repose les deux instants ensemble.

alter table agent_lot_sessions
    add column if not exists wake_pending_at timestamptz;
alter table agent_lot_sessions
    add column if not exists wake_retries integer not null default 0;
alter table agent_lot_sessions
    add column if not exists wake_retry_reason text;

-- le lecteur ne demande que les lignes en dette, peu nombreuses
create index if not exists agent_lot_sessions_wake_pending
    on agent_lot_sessions (agent) where wake_pending_at is not null;

comment on column agent_lot_sessions.wake_pending_at is
    '#8458: instant of a counted lot wake whose turn has not launched its harness; replayed without being counted again. A later last_wake_at (writer N-1) supersedes it.';
comment on column agent_lot_sessions.wake_retries is
    '#8458: replay attempts already claimed for that pending wake; bounded by LOT_WAKE_RETRY_MAX, then the normal doubled delay resumes.';
comment on column agent_lot_sessions.wake_retry_reason is
    '#8458: why the last turn for that pending wake did not launch (host pressure, lease lost, setup refused…), for the journal.';
