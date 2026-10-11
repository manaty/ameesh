-- SPDX-License-Identifier: AGPL-3.0-only
-- 0053_work_item_migration — numéro de migration réservé par un lot (L122).
--
-- Le 2026-10-10, quatre lots développés en parallèle ont pris les mêmes
-- numéros (0048 à 0050) : trois renumérotations et deux conflits en cascade.
-- Un lot qui ajoute une migration réserve désormais son numéro
-- (`ameesh work migration <lot>`) avant d'écrire le fichier : le prochain
-- numéro libre, au-delà des fichiers connus ET des numéros déjà réservés.
-- L'index unique garantit qu'un numéro n'est réservé que par un seul lot.

alter table work_items add column if not exists migration integer
    check (migration is null or migration between 1 and 9999);
create unique index if not exists work_items_migration_idx on work_items (migration)
    where migration is not null;
