-- SPDX-License-Identifier: AGPL-3.0-only
-- 0041_plafond_de_contexte — plafond de contexte réglable par agent et grand
-- livre sans doublon (lot L60).
--
-- `context_max_tokens` : au-delà de ce nombre de jetons d'entrée relus
-- (cache compris) au dernier tour, l'exécuteur tourne la session avant le
-- tour suivant (résumé de reprise, mécanisme L11). NULL = défaut de
-- l'exécuteur (`AMEESH_CONTEXT_MAX_TOKENS`, 15 M) ; 0 = plafond désactivé.
-- Posé par `ameesh set <agent> context_max_tokens=…`.

alter table agent_registry add column if not exists context_max_tokens bigint
    check (context_max_tokens is null or context_max_tokens >= 0);

-- Grand livre sans doublon : `turn_costs.spend_key` porte la clé du marqueur
-- comptable (`spend_pending` : agent, index de début, instant de pose). La
-- ligne et l'effacement du marqueur ne sont pas atomiques : un exécuteur arrêté
-- entre les deux laissait le marqueur, et l'exécuteur suivant réécrivait la
-- même ligne en réparant. Avec la clé, la réparation ne réécrit rien.
alter table turn_costs add column if not exists spend_key text;
create unique index if not exists turn_costs_spend_key on turn_costs (spend_key);
