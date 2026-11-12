-- SPDX-License-Identifier: AGPL-3.0-only
-- 0572_conversation_moves — #7050 (avatar A2, conception 5322 §2.3) : une
-- ligne par tentative de déplacement de conversation d'un agent vers un autre
-- hôte, à la frontière de tour.
--
-- Machine d'état (`conversation_move.py`) :
--   planned → prepared → switched | recovered → rehydrating → verified
--   et `failed` depuis tout état non terminal.
--
-- Clé d'idempotence `move_id` ; `(agent, epoch_source)`, l'epoch du bail de la
-- source au moment du plan, est unique hors des tentatives échouées : un
-- second plan sur le même epoch relit la ligne vivante, et une tentative
-- échouée (ou périmée) laisse planifier de nouveau. Au plus une tentative
-- ouverte par agent (index partiel).
--
-- La ligne ne porte que des identifiants, des hôtes, des dossiers de travail,
-- des bornes de séquence et un digest : jamais le texte du segment, jamais un
-- chemin de credential ni de transcript (`checks` : booléens seulement).
--
-- Additif seulement : table neuve, aucune colonne existante ne change de sens ;
-- le code d'avant ne la nomme jamais.
set local lock_timeout = '5s';

create table if not exists conversation_moves (
    move_id uuid primary key,
    agent text not null check (length(agent) between 1 and 256),
    epoch_source bigint not null,
    state text not null default 'planned' check (state in
        ('planned', 'prepared', 'switched', 'recovered', 'rehydrating',
         'verified', 'failed')),
    reason text not null check (reason in ('preference', 'manual')),
    requested_by text not null check (length(requested_by) between 1 and 256),
    -- la source : sa ligne de registre au plan, rendue telle quelle à l'échec
    source_host text not null check (length(source_host) between 1 and 256),
    source_cwd text check (length(source_cwd) <= 4096),
    source_owner text check (length(source_owner) <= 512),
    source_session text check (length(source_session) <= 256),
    source_cache text not null default 'kept'
        check (source_cache in ('kept', 'stale-cache')),
    -- la cible : son dossier vérifié, puis son bail et sa session provisoire
    target_host text not null check (length(target_host) between 1 and 256),
    target_cwd text check (length(target_cwd) <= 4096),
    target_owner text check (length(target_owner) <= 512),
    target_epoch bigint,
    target_session text check (length(target_session) <= 256),
    target_turn_started boolean not null default false,
    checks jsonb not null default '{}',
    -- le segment : bornes et digest, jamais son texte
    from_seq bigint,
    to_seq bigint,
    digest text check (digest ~ '^[0-9a-f]{64}$'),
    error text check (length(error) <= 2000),
    created_at timestamptz not null default clock_timestamp(),
    updated_at timestamptz not null default clock_timestamp(),
    prepared_at timestamptz,
    switched_at timestamptz,
    verified_at timestamptz,
    failed_at timestamptz,
    check (target_host <> source_host),
    check ((target_owner is null) = (target_epoch is null)),
    check (state not in ('rehydrating', 'verified')
           or (target_owner is not null and digest is not null)),
    check (state <> 'verified' or target_session is not null)
);

create unique index if not exists conversation_moves_epoch_uq
    on conversation_moves (agent, epoch_source) where state <> 'failed';
create unique index if not exists conversation_moves_open_uq
    on conversation_moves (agent)
    where state in ('planned', 'prepared', 'switched', 'recovered', 'rehydrating');
create index if not exists conversation_moves_target
    on conversation_moves (target_host)
    where state in ('planned', 'prepared', 'switched', 'recovered', 'rehydrating');

comment on table conversation_moves is
    '#7050 (design 5322 §2.3): one row per conversation move attempt; ids, hosts, bounds and digest only — never the segment text nor a credential or transcript path.';
