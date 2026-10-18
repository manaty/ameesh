-- SPDX-License-Identifier: AGPL-3.0-only
-- #2466 : réveil programmé d'un lot, porté par l'exécuteur.
--
-- `wake_at` (nul = aucun réveil) : l'échéance d'un rendez-vous explicite
-- (séance owner, reprise d'un hold). L'exécuteur la délivre même quand le lot
-- est retenu par une note `hold:` ou n'est pas actionnable (`waiting_human`,
-- `blocked`) ; il ne dépend plus de la mémoire d'un agent ni d'un timer d'un
-- hôte. Le réveil suit l'assigné du lot.
--
-- `wake_claim_at` : le bail de livraison. L'exécuteur réclame l'échéance
-- (claim), ouvre le tour, puis la consomme une fois le harnais lancé. Un
-- claim non consommé (drainage, arrêt, panne avant lancement) expire et le
-- réveil est délivré de nouveau : jamais de rendez-vous perdu entre le choix
-- du tour et son lancement. Le journal du lot garde la pose (`wake: …`) et
-- chaque délivrance (`wake: … delivered …`).
alter table work_items add column if not exists wake_at timestamptz;
alter table work_items add column if not exists wake_claim_at timestamptz;

create index if not exists work_items_wake_idx
    on work_items (wake_at) where wake_at is not null;
