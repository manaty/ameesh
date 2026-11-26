-- SPDX-License-Identifier: AGPL-3.0-only
-- 0609_turn_costs_service_tier — #8296 (issue manaty/ameesh#6534).
--
-- Le grand livre prixait un tour par modèle seulement. Or Codex est lancé en
-- mode rapide par `service_tier=fast`, qu'OpenAI facture deux fois le tarif
-- Standard : la prime n'atteignait ni `turn_costs.usd`, ni le plafond
-- journalier, ni les vues par agent. La colonne porte le palier effectif du
-- tour (`fast`, `default`, …), NULL quand aucun palier n'est connu (autre
-- harnais, tour d'avant ce lot).
--
-- Additive et compatible N-1 : une colonne nullable que l'ancien code ne lit
-- ni n'écrit ; aucun index, les vues budget agrègent déjà par agent et modèle.

alter table turn_costs add column if not exists service_tier text;

comment on column turn_costs.service_tier is
    '#8296 : palier de service effectif du tour (fast, default, …), NULL si inconnu';
