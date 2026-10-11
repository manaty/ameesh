-- SPDX-License-Identifier: AGPL-3.0-only
-- 0051_turn_grace — délai de grâce du travail de fond d'un tour.
--
-- Constat du 2026-10-10 : la fin d'un tour tuait le travail lancé en fond
-- pendant le tour (groupe de processus du tour tué, conteneurs étiquetés du
-- tour supprimés), alors que les tours sont souvent clos avant la fin de ce
-- travail (borne du courrier, plafonds, rotations).
--
-- `turn_grace_seconds` : à la fin d'un tour normal, ce qui tourne encore
-- (processus du groupe du tour, conteneurs du tour sans lot) a ce délai pour
-- finir avant le nettoyage habituel. NULL = défaut de l'exécuteur
-- (`AMEESH_TURN_GRACE_SECONDS`, 20 min) ; 0 = nettoyage immédiat.
--
-- Posé par `ameesh set <agent> turn_grace_seconds=…`.

alter table agent_registry add column if not exists turn_grace_seconds bigint
    check (turn_grace_seconds is null or turn_grace_seconds >= 0);
