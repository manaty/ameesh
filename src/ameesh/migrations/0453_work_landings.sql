-- SPDX-License-Identifier: AGPL-3.0-only
-- 0453_work_landings — attente d'atterrissage planifiée (#1945).
--
-- Un lot dont le travail est fini (verdict `ok`) peut attendre l'atterrissage
-- d'un VÉHICULE de paquet sans être stagnant : ni porte humaine, ni lot
-- ouvert à déclarer en dépendance (#1642), ni conflit à préparer (#160d).
-- Le pilote ou le composeur déclare ici le véhicule (un lot ouvert qui porte
-- la composition) et l'échéance FINIE de l'atterrissage.
--
-- La tête exacte et l'étape ne sont pas recopiées : elles se lisent à chaque
-- passe dans l'état du véhicule (`work_items.state`) et son dernier gel
-- (`work_item_milestones`, kind `frozen`) — un nouveau gel remplace donc la
-- tête affichée sans geste, et rien n'est lu dans git.
--
-- La ligne n'éteint rien à elle seule : `planned_waits` ne la lit que tant
-- que le véhicule est ouvert et que l'échéance (fin de journée, comme un
-- hold daté) n'est pas passée. Retrait (`ameesh work landing <lot> --clear`),
-- véhicule fermé/fusionné ou échéance dépassée rendent l'alerte stale au lot.
-- Une admission verbale, une vieille chaîne de transport ou un véhicule
-- inconnu ne masquent donc rien.

CREATE TABLE IF NOT EXISTS work_landings (
    work_item_id bigint PRIMARY KEY REFERENCES work_items(id) ON DELETE CASCADE,
    vehicle_id   bigint NOT NULL REFERENCES work_items(id) ON DELETE CASCADE,
    -- fin de journée incluse : l'attente court jusqu'au lendemain 00:00 local
    until_on     date NOT NULL,
    created_by   text NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (work_item_id <> vehicle_id)
);

CREATE INDEX IF NOT EXISTS work_landings_vehicle ON work_landings (vehicle_id);
