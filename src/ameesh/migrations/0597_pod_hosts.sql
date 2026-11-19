-- SPDX-License-Identifier: AGPL-3.0-only
-- 0597_pod_hosts — #7694 (P1 de #7633, design 7633-hote-avatars-pod.md §8.3).
--
-- `pod_hosts` est l'identité d'un hôte enfant porté par un pod éphémère : le
-- nom logique stable (`host`), le pod Kubernetes qui le porte (`pod_uid`,
-- `namespace`), son template, son parent, son tenant et son UID. Le
-- pourvoyeur d'enfants (P4) l'écrit dans la même transaction que
-- `host_tenancy` et `host_tenants` ; un `pod_hosts` absent, expiré ou dont le
-- `pod_uid` ne correspond pas doit fermer la réclamation
-- (`pod-host-unknown`, fail closed).
--
-- Cette tranche ne livre que la table et son accès minimal : l'hôte pod
-- durable (P1) n'en a pas besoin, et aucun enfant n'est créé ici. Aucune
-- donnée de compte, aucun secret : un UID, un nom de pod et des dates.
--
-- Additive seulement : table neuve, aucun lecteur existant n'est modifié.

create table if not exists pod_hosts (
    host        text not null,
    namespace   text not null,
    template    text not null,
    parent      text not null,
    tenant      text not null,
    uid         integer not null,
    runner_id   text not null,
    pod_uid     text not null,
    state       text not null default 'requested',
    requested_at timestamptz not null default now(),
    started_at  timestamptz,
    expires_at  timestamptz not null,
    deleted_at  timestamptz,
    primary key (host),
    -- Un UID de pod Kubernetes n'identifie qu'un pod : deux hôtes enfants ne
    -- peuvent pas se réclamer du même pod.
    unique (namespace, pod_uid),
    constraint pod_hosts_state_valid
        check (state in ('requested', 'running', 'deleted', 'failed')),
    constraint pod_hosts_tenant_valid
        check (tenant ~ '^human:[A-Za-z0-9][A-Za-z0-9._@-]*$'),
    constraint pod_hosts_uid_valid check (uid > 0),
    constraint pod_hosts_host_valid check (host <> ''),
    constraint pod_hosts_pod_uid_valid check (pod_uid <> ''),
    constraint pod_hosts_expires_valid check (expires_at > requested_at)
);

comment on table pod_hosts is
    '#7694: ephemeral child pod host (logical host name, Kubernetes pod UID, template, parent, tenant, UID, TTL); written by the pod provisioner (P4), never a secret.';

comment on column pod_hosts.pod_uid is
    '#7694: Kubernetes `metadata.uid` of the pod; a missing or mismatched UID closes the claim (pod-host-unknown).';

comment on column pod_hosts.state is
    '#7694: requested | running | deleted | failed; expiry and cleanup belong to the provisioner (P4).';
