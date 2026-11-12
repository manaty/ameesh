-- SPDX-License-Identifier: AGPL-3.0-only
-- 0574_attach_grants — journal des grants d'attach et de leurs refus (#7119).
--
-- Avatar D1 (docs/design/5322-avatar-humain.md §5.2 ; décision Q3 #213938,
-- option a) : un humain ne s'attache à un agent épinglé ailleurs que sur un
-- grant court (5 min), à usage unique, signé par ameesh-approve/WebAuthn et
-- lié à la clé d'appareil de son poste. L'hôte de l'agent vérifie le grant
-- (attach_grant.redeem) ; CHAQUE issue est écrite ici : le grant accepté
-- (`granted`) comme chaque refus (`refused`, avec son code stable).
--
-- L'usage unique est porté par la consommation du nonce du reçu
-- (`mesh_consumed_nonces`, 0011) ; l'index partiel ci-dessous en est la
-- seconde garde : deux lignes `granted` pour le même `attach_id` sont
-- impossibles.
--
-- Table en ajout seul : aucune opération courante ne met à jour ni ne
-- supprime une ligne. Aucun secret, aucune preuve, aucun contenu de session :
-- l'identité, l'agent, l'hôte, l'appareil, le code et la raison.

create table if not exists attach_grant_events (
    id          bigserial primary key,
    at          timestamptz not null default now(),
    outcome     text not null check (outcome in ('granted', 'refused')),
    code        text not null check (length(code) between 1 and 64),
    reason      text not null default '' check (length(reason) <= 2000),
    human       text not null default '' check (length(human) <= 200),
    agent       text not null default '' check (length(agent) <= 200),
    host        text not null default '' check (length(host) <= 253),
    tenant      text not null default '' check (length(tenant) <= 200),
    device_key  text not null default '' check (length(device_key) <= 1024),
    attach_id   text not null default '' check (length(attach_id) <= 64),
    nonce       text not null default '' check (length(nonce) <= 128),
    exp         bigint,
    checked_by  text not null check (length(checked_by) between 1 and 200),
    constraint attach_grant_events_granted_ck check (
        outcome <> 'granted'
        or (code = 'ok' and human like 'human:%' and agent <> '' and host <> ''
            and device_key <> '' and attach_id ~ '^att_[0-9a-z]{26}$'
            and nonce <> '' and exp is not null))
);

create unique index if not exists attach_grant_events_granted_uq
    on attach_grant_events (attach_id) where outcome = 'granted';

create index if not exists attach_grant_events_agent_idx
    on attach_grant_events (agent, at desc);

comment on table attach_grant_events is
  '#7119 (avatar D1) : journal en ajout seul des grants d''attach acceptés et de chaque refus (code stable, raison), sans secret ni contenu de session.';
