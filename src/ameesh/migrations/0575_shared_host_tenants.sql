-- SPDX-License-Identifier: AGPL-3.0-only
-- 0575_shared_host_tenants — #7106 (avatar B3, docs/design/5322-avatar-humain.md
-- §3.4) : hôte partagé multi-tenant.
--
-- Un hôte dont la fiche Host déclare `policy.shared` héberge plusieurs
-- tenants (un par humain) ; chacun a son utilisateur système, son runner et
-- ses dossiers de comptes. Trois tables :
--
-- * `host_tenancy` : ce que dit le canon (écrit par `canon sync` de l'hôte,
--   une ligne par canon qui déclare l'hôte partagé) ;
-- * `host_tenants` : la liaison tenant → utilisateur système, UID et runner,
--   écrite par le superviseur d'hôte (`ameesh host tenant add`), jamais par
--   le runner d'un tenant ;
-- * `runner_presence` : le battement du runner d'un tenant (la présence de
--   §3.2, point 5).
--
-- `ameesh_tenant_claim_ok` est la garde de la réclamation : sur un hôte
-- partagé, seul le runner lié au tenant `responsible` de l'agent (ou la
-- session `attach` de son utilisateur système) prend le bail ; un hôte non
-- partagé garde ses règles. Limite (§3.4) : l'identité de l'exécuteur reste
-- celle de sa configuration ; l'isolation SQL par rôle est un travail
-- distinct (0033/L155), non revendiqué ici.
--
-- Additive seulement : tables et fonctions neuves ; sans ligne
-- `host_tenancy`, aucune réclamation ne change de sens.

create table if not exists host_tenancy (
    host       text not null,
    canon      text not null default '',
    shared     boolean not null,
    -- `human:<id>` résolus par le canon, dans l'ordre de la fiche
    tenants    text[] not null default '{}',
    canon_ref  text,
    synced_at  timestamptz not null default now(),
    primary key (host, canon)
);

comment on table host_tenancy is
    '#7106: shared-host policy of a Host card (policy.shared/tenants), written by canon sync of that host.';

create table if not exists host_tenants (
    host          text not null,
    tenant        text not null,
    unix_user     text not null,
    uid           integer not null,
    runner_id     text not null,
    registered_by text not null,
    registered_at timestamptz not null default now(),
    primary key (host, tenant),
    constraint host_tenants_tenant_valid check (tenant ~ '^human:[A-Za-z0-9][A-Za-z0-9._@-]*$'),
    constraint host_tenants_uid_valid check (uid > 0),
    constraint host_tenants_user_valid check (unix_user ~ '^[a-z_][a-z0-9_-]{0,31}$'),
    unique (host, uid),
    unique (host, unix_user),
    unique (runner_id)
);

comment on table host_tenants is
    '#7106: tenant of a shared host bound to its system user, UID and runner id; written by the host supervisor only.';

create table if not exists runner_presence (
    host      text not null,
    tenant    text not null,
    runner_id text not null,
    uid       integer,
    version   text,
    seen_at   timestamptz not null default now(),
    primary key (host, tenant),
    foreign key (host, tenant) references host_tenants (host, tenant) on delete cascade
);

comment on table runner_presence is
    '#7106: heartbeat of a tenant runner on a shared host (presence, design §3.2 point 5).';

-- Vrai si un canon déclare `host` partagé.
create or replace function ameesh_host_shared(h text)
returns boolean language sql stable as $$
    select exists (select 1 from host_tenancy y where y.host = h and y.shared)
$$;

-- La garde de réclamation d'un hôte partagé : `claimer` est le runner lié au
-- tenant `responsible` (ou `attach:<utilisateur>@<hôte>` de son utilisateur
-- système), et ce tenant est déclaré par le canon. Hôte non partagé : vrai.
create or replace function ameesh_tenant_claim_ok(h text, claimer text, responsible text)
returns boolean language sql stable as $$
    select not ameesh_host_shared(h)
        or exists (
            select 1
              from host_tenants t
              join host_tenancy y
                on y.host = t.host and y.shared and t.tenant = any (y.tenants)
             where t.host = h
               and t.tenant = responsible
               and (t.runner_id = claimer
                    or claimer = 'attach:' || t.unix_user || '@' || h))
$$;
