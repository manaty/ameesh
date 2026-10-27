-- SPDX-License-Identifier: AGPL-3.0-only
-- 0497_courrier_cle_idempotence — #3889 (canon §16) : un message de la
-- conversation déposé sous une Idempotency-Key l'est UNE fois.
--
-- `serve.act` réconcilie une réservation restée sans réponse par le courrier
-- déposé sous sa clé (`payload.idempotency_key`) ; une absence de courrier à
-- un instant ne prouve pas qu'un autre exécuteur (la première requête encore
-- vivante, une seconde reprise) n'est pas sur le point de l'écrire. L'effet
-- est donc exclusif ici : le second dépôt sous la même clé échoue (23505) et
-- `chat.post_human_message` rend le message déjà déposé (cx2, gel #21556).
--
-- Additif : ni vue, ni table, ni ligne modifiée ; seules les lignes qui
-- portent une clé sont indexées (aucune avant ce lot). Construction simple,
-- pas `CONCURRENTLY` (`migrate()` tourne dans une transaction, comme 0489),
-- attente de verrou bornée à 5 s : la messagerie est très écrite.
set local lock_timeout = '5s';

create unique index if not exists agent_mailbox_idempotency_key_idx
  on agent_mailbox (sender, recipient, (payload->>'idempotency_key'))
  where (payload->>'idempotency_key') is not null;
