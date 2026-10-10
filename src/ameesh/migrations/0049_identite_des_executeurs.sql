-- SPDX-License-Identifier: AGPL-3.0-only
-- 0049_identite_des_executeurs — enrôlement et identité des exécuteurs
-- médiés (lot L110, étude de l'exécuteur médié §3).
--
-- Un appareil prêté (VM Compute) n'a aucun accès à la base : il parle au
-- serveur du mesh par `/api/exec/v1`. Son identité tient en quatre tables.
--
-- `executor_invitations` : le code d'enrôlement émis par un humain habilité
-- (`ameesh host enroll`). Seul le SHA-256 du code est gardé ; le code est à
-- usage unique (`consumed_at`) et de courte durée (`expires_at`). Il est lié
-- au mesh, à l'hôte et, si l'humain l'a fixée, à une liste blanche d'agents.
--
-- `executors` : un exécuteur enrôlé. Sa clé publique P-256 (JWK) et son
-- empreinte RFC 7638 (`thumbprint`, le `kid` de ses assertions) ; la liaison
-- facultative à la clé d'appareil Nexlink (`device_key_sha256`, empreinte de
-- la SPKI). `revoked_at` posé : plus aucun jeton, baux relâchés.
--
-- `executor_tokens` : jetons d'accès (`amx1.`, 10 min) et de session
-- (`ams1.`, liés à (agent, epoch)). Seul leur SHA-256 est gardé. Un jeton de
-- session ne vaut que tant que le bail (agent, epoch) de l'exécuteur vit.
--
-- `executor_assertion_jti` : les `jti` des assertions ES256 déjà vues, gardés
-- jusqu'à leur échéance : une assertion ne sert qu'une fois.
--
-- `executor_events` : journal en ajout seul (invitation, enrôlement,
-- révocation), lisible par `ameesh host show`.
--
-- Aucune de ces tables ne touche aux approbations (décision 0012) : ni
-- `mesh_approvals`, ni nonces, ni autorisations permanentes, ni
-- authentificateurs.
--
-- Numérotation de la voie B : 0048 (L108), 0049 (L110), 0050 (L111), après
-- 0043 à 0047 (lots de la 1.6.0).

create table if not exists executor_invitations (
    code_sha256      text primary key,
    mesh             text not null,
    host             text not null,
    agents_allowlist jsonb null,
    created_by       text not null,
    created_at       timestamptz not null default now(),
    expires_at       timestamptz not null,
    consumed_at      timestamptz null,
    executor_id      text null,
    cancelled_at     timestamptz null,
    constraint executor_invitations_sha_ck check (code_sha256 ~ '^[0-9a-f]{64}$'),
    constraint executor_invitations_ttl_ck check (expires_at > created_at)
);

create index if not exists executor_invitations_host_idx
    on executor_invitations (host) where consumed_at is null and cancelled_at is null;

create table if not exists executors (
    id                 text primary key,
    mesh               text not null,
    host               text not null,
    public_key         jsonb not null,
    thumbprint         text not null unique,
    agents_allowlist   jsonb null,
    label              text not null default '',
    device_key_sha256  text null,
    device_attestation jsonb null,
    enrolled_by        text not null,
    enrolled_at        timestamptz not null default now(),
    last_seen_at       timestamptz null,
    revoked_at         timestamptz null,
    revoked_by         text null,
    revoked_why        text null,
    constraint executors_id_ck check (id ~ '^[0-9a-f]{16}$')
);

create index if not exists executors_host_idx on executors (host);

create table if not exists executor_tokens (
    token_sha256 text primary key,
    kind         text not null check (kind in ('executor', 'session')),
    executor_id  text not null references executors (id) on delete cascade,
    agent        text null,
    epoch        bigint null,
    created_at   timestamptz not null default now(),
    expires_at   timestamptz not null,
    constraint executor_tokens_sha_ck check (token_sha256 ~ '^[0-9a-f]{64}$'),
    constraint executor_tokens_session_ck
        check ((kind = 'session') = (agent is not null and epoch is not null))
);

create index if not exists executor_tokens_executor_idx on executor_tokens (executor_id);
create index if not exists executor_tokens_expires_idx on executor_tokens (expires_at);

create table if not exists executor_assertion_jti (
    executor_id text not null references executors (id) on delete cascade,
    jti         text not null,
    expires_at  timestamptz not null,
    primary key (executor_id, jti)
);

create table if not exists executor_events (
    id          bigserial primary key,
    at          timestamptz not null default now(),
    kind        text not null,
    host        text not null,
    executor_id text null,
    actor       text not null,
    detail      jsonb not null default '{}'::jsonb
);

create index if not exists executor_events_host_idx on executor_events (host, id);
