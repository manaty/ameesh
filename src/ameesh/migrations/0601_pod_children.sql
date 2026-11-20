-- SPDX-License-Identifier: AGPL-3.0-only
-- 0601_pod_children — #7697 (P4 de #7633, design 7633-hote-avatars-pod.md §8).
--
-- Un avatar `avatar.children: pod` ne parle jamais à l'API Kubernetes. Il
-- écrit une DEMANDE (`pod_requests`) ; le pourvoyeur du mesh
-- (`system:ameesh-pod-provisioner`, #6091) lit la demande, crée un pod à
-- partir d'un TEMPLATE FIGÉ (`pod_templates`), puis écrit dans la même
-- transaction l'identité de l'hôte enfant (`pod_hosts`, 0597), sa politique
-- (`host_tenancy`) et sa liaison (`host_tenants`). Le pod et les lignes sont
-- supprimés à l'échéance (TTL) par le pourvoyeur.
--
-- `pod_templates` porte le template validé et son empreinte, plus le dernier
-- battement du pourvoyeur : `last_seen_at` est la condition qui fait
-- disparaître `spawn-pod-unsupported`. Un template enregistré mais dont le
-- pourvoyeur ne bat plus ne suffit pas à faire naître un enfant.
--
-- `pod_requests` ne porte aucun secret : un nom de Secret (`credential_secret`,
-- créé par l'opérateur), le harnais, un profil d'outils/contexte (noms et
-- digests) et l'empreinte de la demande. La clé d'API d'organisation est lue
-- par l'enfant dans son Secret monté ; le relais de modèle est une tranche
-- ultérieure (D6, pilote 20/11).
--
-- `runner_presence.pod_uid` est le UID Kubernetes observé par le runner d'un
-- enfant. La garde de réclamation `ameesh_tenant_claim_ok` exige, pour un
-- hôte de `pod_hosts`, une ligne vivante ET un battement dont le `pod_uid`
-- correspond (fail closed, `pod-host-unknown`). Les hôtes non pod n'ont
-- aucune ligne `pod_hosts` : leur garde est inchangée.
--
-- Additive : deux tables neuves, une colonne nullable, et le remplacement
-- d'une fonction dont le sens ne change que pour les hôtes de `pod_hosts`.
-- Aucun contenu de compte, aucun secret.

create table if not exists pod_templates (
    name          text not null,
    namespace     text not null,
    image         text not null,
    fingerprint   text not null,
    spec          jsonb not null,
    registered_by text not null,
    registered_at timestamptz not null default now(),
    last_seen_at  timestamptz not null default now(),
    active        boolean not null default true,
    primary key (name),
    constraint pod_templates_name_valid
        check (name ~ '^[a-z0-9]([-a-z0-9]*[a-z0-9])?$' and length(name) <= 63),
    constraint pod_templates_namespace_valid check (namespace <> ''),
    constraint pod_templates_image_valid check (image <> ''),
    constraint pod_templates_fingerprint_valid
        check (fingerprint ~ '^[0-9a-f]{64}$')
);

comment on table pod_templates is
    '#7697: frozen child-pod template registered by the pod provisioner, with its fingerprint and heartbeat; state only, no secret.';
comment on column pod_templates.last_seen_at is
    '#7697: last provisioner heartbeat; `children: pod` refuses while no template was seen recently (spawn-pod-unsupported).';
comment on column pod_templates.spec is
    '#7697: the frozen pod manifest with placeholders, validated by the provisioner; never a secret.';

create table if not exists pod_requests (
    id                uuid primary key default gen_random_uuid(),
    child             text not null,
    parent            text not null,
    tenant            text not null,
    host              text not null,
    template          text not null references pod_templates (name),
    namespace         text not null,
    pod_name          text,
    harness           text not null,
    credential_secret text not null,
    uid               integer not null,
    unix_user         text not null,
    runner_id         text not null,
    profile           jsonb not null default '{}'::jsonb,
    fingerprint       text not null,
    state             text not null default 'requested',
    requested_by      text not null,
    claimed_by        text,
    requested_at      timestamptz not null default now(),
    claimed_at        timestamptz,
    started_at        timestamptz,
    expires_at        timestamptz not null,
    finished_at       timestamptz,
    error             text,
    unique (child),
    unique (host),
    constraint pod_requests_state_valid
        check (state in ('requested', 'claimed', 'running', 'cancelled',
                         'failed', 'expired', 'deleted')),
    constraint pod_requests_child_valid check (child <> ''),
    constraint pod_requests_parent_valid check (parent <> ''),
    constraint pod_requests_tenant_valid
        check (tenant ~ '^human:[A-Za-z0-9][A-Za-z0-9._@-]*$'),
    constraint pod_requests_host_valid
        check (host ~ '^[a-z0-9]([-a-z0-9]*[a-z0-9])?$' and length(host) <= 63),
    constraint pod_requests_namespace_valid check (namespace <> ''),
    constraint pod_requests_harness_valid check (harness <> ''),
    constraint pod_requests_uid_valid check (uid between 30000 and 60000),
    constraint pod_requests_user_valid
        check (unix_user ~ '^[a-z_][a-z0-9_-]{0,31}$'),
    constraint pod_requests_runner_valid check (runner_id <> ''),
    constraint pod_requests_pod_name_valid
        check (pod_name is null
               or (pod_name ~ '^[a-z0-9]([-a-z0-9]*[a-z0-9])?$'
                   and length(pod_name) <= 63)),
    constraint pod_requests_credential_valid
        check (credential_secret ~ '^ameesh-child-[a-z0-9]([-a-z0-9]*[a-z0-9])?$'
               and length(credential_secret) <= 63),
    constraint pod_requests_fingerprint_valid
        check (fingerprint ~ '^[0-9a-f]{64}$'),
    constraint pod_requests_expires_valid check (expires_at > requested_at),
    constraint pod_requests_error_valid
        check (error is null or length(error) <= 1000)
);

comment on table pod_requests is
    '#7697: parent-to-provisioner request for one child pod (child, host, template, harness, credential Secret name, TTL, fingerprint); no secret.';
comment on column pod_requests.state is
    '#7697: requested -> claimed -> running, or cancelled/failed/expired/deleted; the provisioner owns every transition after requested.';
comment on column pod_requests.profile is
    '#7697: tool/MCP names and context digest of the child (never a value); the effective lists live in spawn_profiles/spawn_contexts.';
comment on column pod_requests.credential_secret is
    '#7697: name of the per-child Secret carrying the organisation API key; the provisioner never reads it.';

create index if not exists pod_requests_pending
    on pod_requests (requested_at)
    where state = 'requested';
create index if not exists pod_requests_live
    on pod_requests (expires_at)
    where state in ('requested', 'claimed', 'running');

alter table runner_presence
    add column if not exists pod_uid text;

comment on column runner_presence.pod_uid is
    '#7697: Kubernetes metadata.uid observed by a child-pod runner; the claim guard requires it to equal pod_hosts.pod_uid.';

-- La garde de réclamation d'un hôte pod enfant : en plus de la garde
-- multi-tenant de 0575, il faut une ligne `pod_hosts` vivante et le UID du
-- pod observé par le runner (`runner_presence.pod_uid`). Un pod disparu, une
-- ligne expirée, un pod recréé (UID neuf) ou un runner qui ne bat pas
-- ferment la réclamation. Un hôte sans ligne `pod_hosts` garde exactement le
-- comportement de 0575.
create or replace function ameesh_tenant_claim_ok(h text, claimer text, responsible text)
returns boolean language sql stable as $$
    select (not ameesh_host_shared(h)
            or exists (
                select 1
                  from host_tenants t
                  join host_tenancy y
                    on y.host = t.host and y.shared and t.tenant = any (y.tenants)
                 where t.host = h
                   and t.tenant = responsible
                   and (t.runner_id = claimer
                        or claimer = 'attach:' || t.unix_user || '@' || h)))
       and (
            not exists (select 1 from pod_hosts p where p.host = h)
            or exists (
                select 1
                  from pod_hosts p
                  join runner_presence rp
                    on rp.host = p.host
                   and rp.tenant = p.tenant
                   and rp.runner_id = p.runner_id
                   and rp.pod_uid = p.pod_uid
                 where p.host = h
                   and p.state = 'running'
                   and p.expires_at > now()
                   and p.tenant = responsible
                   and p.runner_id = claimer))
$$;

comment on function ameesh_tenant_claim_ok(text, text, text) is
    '#7106/#7697: shared-host claim guard, plus a live pod_hosts row whose pod_uid matches the runner heartbeat for child pods.';
