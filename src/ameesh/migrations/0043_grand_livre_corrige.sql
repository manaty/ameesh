-- SPDX-License-Identifier: AGPL-3.0-only
-- 0043_grand_livre_corrige — corrections tracées du grand livre (lot L95).
--
-- Avant L60 et L71, des lignes de `turn_costs` sont fausses :
--
--   * coût cumulé d'une session reprise (Claude `total_cost_usd`, Codex usage
--     du fil) compté comme coût d'un seul tour ;
--   * tours au modèle « inconnu », facturés au tarif le plus cher de la
--     famille alors que le harnais lançait son modèle par défaut ;
--   * doublons écrits par la réparation du marqueur comptable.
--
-- `ameesh cost correct` les recalcule ou les écarte. Rien n'est supprimé :
--
--   * `turn_costs.void_reason` : non NULL = ligne **écartée** des sommes
--     (dépense, rapports, garde de budget), avec sa raison. La ligne reste,
--     et ses cumuls servent encore de repère (`last_reading`).
--   * `turn_cost_corrections` : journal append-only ; chaque correction y
--     garde la copie complète de la ligne d'avant (`old_row`), les valeurs
--     écrites (`new_values`), la passe (`run_id`) et son auteur.
--
-- Numéro : 0043, premier libre après 0042 (L70) ; 0045 est pris par L73.

alter table turn_costs add column if not exists void_reason text;

create table if not exists turn_cost_corrections (
    id           bigserial primary key,
    run_id       text not null,
    turn_cost_id bigint not null,
    kind         text not null,
    reason       text not null,
    old_row      jsonb not null,
    new_values   jsonb not null,
    actor        text not null,
    at           timestamptz not null default now()
);

create index if not exists turn_cost_corrections_row
    on turn_cost_corrections (turn_cost_id, id);
