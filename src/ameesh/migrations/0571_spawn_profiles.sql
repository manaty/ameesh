-- SPDX-License-Identifier: AGPL-3.0-only
-- 0571_spawn_profiles — #7034 (conception #7031 §4) : l'état d'un enfant
-- restreint, hors de la carte du parent.
--
--   spawn_profiles  la liste effective de l'enfant (outils, serveurs MCP) et
--                   l'empreinte du fichier de réglages que le runner lui
--                   remet ; une ligne par enfant éphémère. Elle est écrite
--                   dans la même transaction que la création de l'enfant :
--                   un enfant ne peut pas exister sans sa liste.
--   spawn_contexts  l'extrait de contexte borné (16 Kio) confié au démarrage,
--                   avec son empreinte SHA-256 ; supprimé à l'expiration,
--                   après le rapport final.
--   spawn_reports   le compteur des rapports progress/question et l'état des
--                   rapports started/final (idempotents par événement dans le
--                   journal du parent). Aucun secret ici : le corps du rapport
--                   est dans le journal et le courrier, pas dans cette table.
--
-- Aucune de ces tables ne porte d'autorité : elle disent ce que le runner a
-- le droit de donner à un enfant, jamais un droit du parent.
-- `on update cascade` (#4299) : un renommage de l'agent déplace la ligne de
-- son enfant avec lui (`child` est classée CASCADED) ; la suppression de
-- l'agent emporte la ligne.
create table if not exists spawn_profiles (
    child           text primary key references agent_registry(name)
                    on delete cascade on update cascade,
    parent          text not null,
    harness         text not null,
    tools           text[] not null default '{}',
    mcp             text[] not null default '{}',
    -- #7034 × #7107 : les serveurs MCP retenus, filtrés depuis le `.mcp.json`
    -- du parent au moment du spawn. Un enfant placé sur un autre hôte ne peut
    -- pas relire ce fichier : le runner écrit son config privé depuis ici.
    -- Refusé au spawn si un serveur porte un secret littéral.
    mcp_servers     jsonb not null default '{}'::jsonb,
    -- empreinte de la politique (noms d'outils + serveurs MCP)
    config_digest   text,
    created_at      timestamptz not null default now()
);

create index if not exists spawn_profiles_parent on spawn_profiles(parent);

create table if not exists spawn_contexts (
    child       text primary key references agent_registry(name)
                on delete cascade on update cascade,
    parent      text not null,
    digest      text not null check (digest ~ '^[0-9a-f]{64}$'),
    bytes       integer not null check (bytes between 0 and 16384),
    text        text not null check (length(text) <= 16384),
    created_at  timestamptz not null default now()
);

create table if not exists spawn_reports (
    child       text primary key references agent_registry(name)
                on delete cascade on update cascade,
    seq         bigint not null default 0 check (seq >= 0),
    started_at  timestamptz,
    final_at    timestamptz,
    final_status text check (final_status in ('completed', 'failed', 'expired')),
    updated_at  timestamptz not null default now()
);

-- Les enfants échus d'un parent, sans rapport final : le balayage du tour du
-- parent les trouve sans parcourir le registre.
create index if not exists agent_registry_children_expiry
    on agent_registry (created_by, ephemeral_expires_at)
    where ephemeral;
