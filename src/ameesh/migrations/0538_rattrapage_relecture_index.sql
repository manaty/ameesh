-- SPDX-License-Identifier: AGPL-3.0-only
-- 0538_rattrapage_relecture_index — #5582 : le rattrapage de relecture lit le
-- journal par lot au lieu de le parcourir.
--
-- Constat (diagnostic #5579, 2026-11-03) : `review_lot.catch_up` →
-- `WorkItems.items_missing_ref`, appelé à la fin de chaque relevé `plan_git`,
-- coûtait 3,7 s par appel et 234 s sur une fenêtre de 102 s (2,3 cœurs en
-- continu ; 31 383 appels depuis le 27/10). Le plan partait du balayage des
-- jalons `merged` depuis la borne d'activation (2 729 lignes au central) et
-- évaluait `agent_mesh_merged_annulled` sur chacun, puis parcourait le journal
-- deux fois par lot (marque terminale, compteur de tentatives) : aucun index
-- ne servait `split_part(note, ' ', 1)` ni `(work_item_id, state, created_at)`.
--
-- La sélection matérialise désormais les barrières bon marché (état,
-- anti-jointure du lot de relecture, marque terminale) avant le jalon de
-- fusion et le compteur. Ces index servent les lectures restantes, une
-- lecture par lot :
--   * (work_item_id, split_part(note, ' ', 1)) : marque terminale et compteur
--     de tentatives, dont le premier mot de la note est
--     `review-after-merge:…:#<id>` ;
--   * (work_item_id, state, created_at) : `agent_mesh_merged_annulled`, qui
--     cherche les corrections `build` postérieures au jalon de fusion ;
--   * work_items (issue_ref) : sans lui, l'anti-jointure du lot de relecture
--     hache `issue_ref` en balayant les 5 453 lignes (larges) de `work_items`
--     — 30 ms au central. Avec, une fois les barrières bon marché passées,
--     elle se sonde par candidat (100 lectures d'index dans la garde #5582).
--
-- Additif (L151) : trois index, aucune donnée ni colonne modifiée. Construction
-- simple, pas CONCURRENTLY (`migrate()` tourne dans une transaction, comme
-- 0495, 0523 et 0533) ; le journal des lots est écrit à chaque transition,
-- d'où une attente de verrou bornée à 5 s : la migration échoue et reste en
-- attente plutôt que de faire patienter chaque écrivain derrière la
-- construction. Volume mesuré au central : 119 813 événements, 5 453 lots.
set local lock_timeout = '5s';

create index if not exists work_item_events_item_note_first_idx
    on work_item_events (work_item_id, split_part(note, ' ', 1));

create index if not exists work_item_events_item_state_created_idx
    on work_item_events (work_item_id, state, created_at);

create index if not exists work_items_issue_ref_idx
    on work_items (issue_ref);

comment on index work_item_events_item_note_first_idx is
    '#5582: review catch-up reads the terminal mark and the attempt counter of one lot by (work_item_id, first note word), never by scanning its journal.';
comment on index work_item_events_item_state_created_idx is
    '#5582: agent_mesh_merged_annulled finds the build corrections after a merged milestone for one lot, instead of scanning its journal.';
comment on index work_items_issue_ref_idx is
    '#5582: the review catch-up anti-joins the review lot by issue_ref; the index avoids hashing the wide work_items rows on every sweep.';
