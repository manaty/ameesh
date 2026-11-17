-- SPDX-License-Identifier: AGPL-3.0-only
-- 0586_accord_cle_fichier — reçu par clé fichier sous accord journalisé
-- (manaty/ameesh#5821, lot #7631).
--
-- La façade `ed25519` ne fait pas autorité humaine (spec §8.1). Un canon peut
-- désormais nommer, sur l'authentificateur `ed25519` d'un humain, les
-- opérations pour lesquelles il accepte un reçu de cette clé fichier, à
-- condition que le reçu cite l'entrée du journal de conversation (agent, seq)
-- où l'humain a donné son accord. Liste vide (défaut) : refus, comme avant.
--
-- Additive : une colonne avec défaut ; l'ancien code ne la lit ni ne l'écrit.
set local lock_timeout = '5s';
alter table authenticators
    add column if not exists accord_operations jsonb not null default '[]'::jsonb;
