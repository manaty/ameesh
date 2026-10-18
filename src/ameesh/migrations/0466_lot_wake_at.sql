-- SPDX-License-Identifier: AGPL-3.0-only
-- #2466 : réveil programmé d'un lot, porté par l'exécuteur.
--
-- Une échéance (nulle = aucun réveil) que l'exécuteur consomme à l'échéance,
-- même quand le lot est retenu par une note `hold:` ou n'est pas actionnable
-- (`waiting_human`, `blocked`) : une séance owner planifiée ne dépend plus de
-- la mémoire d'un agent ni d'un timer propre à un hôte. Le réveil suit
-- l'assigné du lot ; il est consommé par un UPDATE conditionnel, donc une
-- seule fois, et le journal du lot garde la pose (`wake: …`) et le
-- déclenchement (`wake: … fired …`).
alter table work_items add column if not exists wake_at timestamptz;

create index if not exists work_items_wake_idx
    on work_items (wake_at) where wake_at is not null;
