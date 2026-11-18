-- SPDX-License-Identifier: AGPL-3.0-only
-- 0594_reponse_correlee — #7672 (contrat public 1.3, E2b) : une réponse est
-- corrélée à la demande qu'elle répond, et une demande n'a qu'une réponse.
--
-- `in_reply_to` nomme la ligne `agent_mailbox` de la demande (kind =
-- 'request'). La colonne est nulle pour tout le courrier ordinaire : elle ne
-- change ni le dépôt, ni la remise, ni le fil existants. L'index unique
-- partiel est l'autorité de la déduplication : deux réponses concurrentes à
-- la même demande ne peuvent pas être écrites, même si les deux ont lu
-- « aucune réponse » avant d'insérer. La surface publique `ameesh.public.mail`
-- vérifie en plus que la demande appartient au destinataire et au tour tenu
-- par le bail ; l'index reste la garde de dernier recours, hors de tout
-- précontrôle.
--
-- Pas de clé étrangère : la validation d'une FK sur `agent_mailbox` scanne
-- toute la table sous verrou, alors que la migration doit rester additive et
-- brève sur une boîte de plusieurs centaines de milliers de lignes. La
-- corrélation est écrite par le seul chemin qui la contrôle (dépôt public) et
-- l'index unique suffit à garantir l'unicité. Une demande supprimée laisse sa
-- réponse lisible, avec une corrélation devenue orpheline — elle n'est jamais
-- réécrite.
--
-- Construction simple, pas `CONCURRENTLY` : `migrate()` exécute toute la
-- passe dans une transaction et Postgres refuse un build concurrent dans un
-- bloc (même choix que 0413, 0497, 0558 sur cette table). L'attente de verrou
-- est bornée à 5 s ; l'index partiel ne porte aucune ligne (colonne neuve
-- entièrement nulle), le build est donc un parcours unique sans écriture
-- d'entrée.
set local lock_timeout = '5s';

alter table agent_mailbox
    add column if not exists in_reply_to bigint;

create unique index if not exists agent_mailbox_in_reply_to_idx
    on agent_mailbox (in_reply_to)
    where in_reply_to is not null;

comment on column agent_mailbox.in_reply_to is
    '#7672: the request (agent_mailbox.id, kind=request) this reply answers; null for ordinary mail. One reply per request, enforced by agent_mailbox_in_reply_to_idx.';
