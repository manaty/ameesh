-- SPDX-License-Identifier: AGPL-3.0-only
-- 0059_turn_costs_price_fallback — tour facturé au repli prudent (lot L225).
--
-- Un tour compté au barème dont le modèle n'a pas de tarif, et dont la famille
-- n'en a aucun (harnais sans famille, famille vide), est facturé au maximum de
-- tout le barème, composante par composante : il reste dans la dépense et les
-- plafonds. `price_fallback` (non NULL) le dit, avec sa raison ; l'alerte
-- `turn_unpriced` le lit. Seul un tour sans barème du tout est écarté
-- (`void_reason`, migration 0043).
--
-- Numéro : 0059 ; 0058 est pris par la branche du lot L160e1 (file de fusion).

alter table turn_costs add column if not exists price_fallback text;

-- Écritures de cache Claude du tour (`cache_creation_input_tokens`), facturées
-- au tour (×1,25 / ×2) : sans elles, le grand livre ne peut pas ré-estimer un
-- tour Claude par ses jetons (`cost correct`). NULL : inconnues (harnais sans
-- écriture de cache, ligne d'avant 0059).
alter table turn_costs add column if not exists cache_write_tokens bigint;
