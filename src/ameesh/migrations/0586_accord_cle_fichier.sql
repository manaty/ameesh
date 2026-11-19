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
-- `accord_claims` : un accord (agent, seq) ne cite qu'UN geste (kind, ref) —
-- la clé primaire tranche la course entre deux reçus. Une réutilisation est
-- refusée (`accord_ref`), pas seulement visible : le « go » de l'humain est
-- ponctuel, sauf le grant qu'il nomme lui-même comme mandat.
--
-- Additive : une colonne avec défaut et une table neuve ; l'ancien code ne
-- les lit ni ne les écrit.
set local lock_timeout = '5s';
alter table authenticators
    add column if not exists accord_operations jsonb not null default '[]'::jsonb;

create table if not exists accord_claims (
    agent      text not null,
    seq        bigint not null,
    kind       text not null check (kind in ('action', 'standing', 'decision')),
    ref        text not null,
    claimed_at timestamptz not null default clock_timestamp(),
    primary key (agent, seq)
);

-- Même contrat d'écriture que les colonnes réécrites de 0516 : un accord écrit
-- sous une ancienne orthographe est ramené au primaire, et `agent_rename`
-- réécrit la colonne avec l'entrée de journal (conversation_log.agent, en
-- cascade) — sinon le même go redeviendrait citable sous la nouvelle
-- orthographe.
drop trigger if exists ameesh_agent_spelling on accord_claims;
create trigger ameesh_agent_spelling before insert or update of agent on accord_claims
    for each row execute function ameesh_agent_spelling('agent');
