-- SPDX-License-Identifier: AGPL-3.0-only
-- 0465_work_packet_members — #2443 : lier chaque unité au paquet qui la compose.
--
-- Un paquet (§11 bis/§12) est composé en git : sa branche `integration/**`
-- porte des commits de fusion dont le trailer `ameesh-work: <lot>` nomme les
-- unités. Jusqu'ici cette appartenance ne vivait que dans un manifeste
-- (`reviews/**`) ou dans le corps du lot porteur : `work show` et
-- `sync-merges` ne pouvaient pas la retrouver sans lire ce manifeste.
--
-- Cette table la persiste au moment de la composition :
--   * `package_id`   : le lot porteur du paquet (branche `integration/**`) ;
--   * `work_item_id` : l'unité composée ;
--   * `base_sha` / `head_sha` : l'intervalle exact relu dans le dépôt ;
--   * `unit_sha`     : le commit de composition (première parente du paquet) ;
--   * `source_sha`   : son second parent (tête composée, résolution comprise) ;
--   * `frozen_sha`   : le gel effectif de l'unité au moment de la composition,
--                      vide si aucun ; il permet de refuser une preuve périmée
--                      après un nouveau gel.
--
-- Remplace, pour un paquet donné, l'ensemble des lignes à chaque
-- `ameesh work packet record` : une unité retirée du paquet disparaît. Une
-- unité peut appartenir à plusieurs paquets (recomposition) ; `recorded_at`
-- départage la lecture d'affichage.
--
-- Table interne, lue par l'exécuteur et les commandes de travail ; aucun
-- GRANT nouveau, elle reste hors du contrat des rôles superviseur (fermée
-- comme `work_landings`, #1945).

create table if not exists work_packet_members (
    package_id     bigint      not null references work_items(id) on delete cascade,
    work_item_id   bigint      not null references work_items(id) on delete cascade,
    package_branch text        not null,
    base_sha       text        not null,
    head_sha       text        not null,
    unit_sha       text        not null,
    source_sha     text        not null default '',
    frozen_sha     text        not null default '',
    recorded_by    text        not null,
    recorded_at    timestamptz not null default clock_timestamp(),
    primary key (package_id, work_item_id),
    check (package_id <> work_item_id),
    check (btrim(package_branch) <> ''),
    check (base_sha ~ '^[0-9a-f]{40,64}$'),
    check (head_sha ~ '^[0-9a-f]{40,64}$'),
    check (unit_sha ~ '^[0-9a-f]{40,64}$'),
    check (source_sha = '' or source_sha ~ '^[0-9a-f]{7,64}$'),
    check (frozen_sha = '' or frozen_sha ~ '^[0-9a-f]{7,64}$')
);

create index if not exists work_packet_members_unit_idx
    on work_packet_members (work_item_id, recorded_at desc);

comment on table work_packet_members is
  '#2443 : appartenance d''une unité à un paquet, écrite à la composition (lot porteur, commit de composition, gel effectif). Lue par work show et sync-merges sans lire le manifeste.';
