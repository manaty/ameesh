-- SPDX-License-Identifier: AGPL-3.0-only
-- 0048_mediated_executor — le serveur de l'API d'exécuteur médiée
-- (`/api/exec/v1`, lot L108 ; contrat L107, `docs/EXECUTEUR-MEDIEE.md`).
--
-- Trois tables, écrites par le serveur seul (jamais par un exécuteur en
-- direct : l'appareil n'a aucun accès à la base) :
--
-- `exec_idempotency` : une ligne par (exécuteur, Idempotency-Key). Elle est
--   écrite dans la transaction de l'opération : même clé et même empreinte
--   (`request_sha256`, SHA-256 du JSON canonique du corps) → la réponse est
--   rejouée ; même clé, autre empreinte → 409 `idempotency_mismatch`. Les
--   lignes de plus de 24 h ne comptent plus et sont élaguées par le serveur.
--
-- `exec_host_availability` : le dernier état de la porte d'hôte rapporté
--   par l'exécuteur (`PUT /host/availability`, `ameesh-exec-availability/1`).
--   `seq` ne recule jamais. Un hôte sans ligne, `available` faux, ou dont la
--   fenêtre (`until_at`) est passée, est indisponible : `leases.claim` y est
--   refusé (403 `host_unavailable`) et `agents.claimable` y rend une liste
--   vide.
--
-- `exec_audit` : journal append-only des écritures et des refus de l'API
--   (opération, agent, statut, code d'erreur, bail perdu, rejeu). Jamais
--   d'argument ni de corps : rien d'autrui, aucun secret.
--
-- Numérotation de la voie B : 0048 (L108), 0049 (L110), 0050 (L111), après
-- 0043 à 0046 (L95, L96, L73, L105) et 0047 (L106), lots de la 1.6.0.

create table if not exists exec_idempotency (
    executor_id    text not null,
    key            text not null,
    op             text not null,
    request_sha256 text not null,
    status         integer not null,
    response       jsonb not null,
    at             timestamptz not null default now(),
    primary key (executor_id, key)
);

create index if not exists exec_idempotency_at on exec_idempotency (at);

create table if not exists exec_host_availability (
    host        text primary key,
    executor_id text not null,
    available   boolean not null,
    state       text not null check (state in ('available', 'draining', 'stopped')),
    seq         bigint not null,
    until_at    timestamptz,
    caps        jsonb not null default '{}'::jsonb,
    reason      text not null default '',
    updated_at  timestamptz not null default now()
);

create table if not exists exec_audit (
    id              bigserial primary key,
    at              timestamptz not null default now(),
    executor_id     text not null,
    host            text not null,
    principal       text not null check (principal in ('executor', 'session', 'none')),
    agent           text,
    op              text not null,
    route           text not null,
    status          integer not null,
    error           text,
    fenced          boolean not null default false,
    replayed        boolean not null default false,
    idempotency_key text
);

create index if not exists exec_audit_at on exec_audit (at);
create index if not exists exec_audit_executor on exec_audit (executor_id, at desc);
