-- SPDX-License-Identifier: AGPL-3.0-only
-- 0610_tenant_seat_login — #7911 (P3 bis de #7633, design 7633 §7.2, §7.3).
--
-- Q2 bis (décision owner #233251, option a, signée le 27/11 00:18Z) : un
-- abonnement personnel reste interdit sur un hôte partagé VM ; il est admis
-- sur un hôte pod à volume par tenant, sous les conditions §7.2 et après que
-- le bureau a vérifié les conditions du fournisseur avec la personne (D2),
-- preuve citée sur le lot. Le canon porte les conditions mécaniques
-- (`subscription_allowed`) ; cette migration porte la connexion du siège.
--
-- La connexion est humaine et interactive. Le bureau marque le tenant `login`
-- (`tenant_logins`, geste humain/avatar, jamais un agent ni le runner du
-- tenant), le runner du tenant rend ses baux et cesse de réclamer au
-- battement suivant (`runner_presence.state = 'login'`), puis un pod de
-- connexion dédié (un conteneur, le PVC du seul tenant, TTL <= 30 min)
-- reçoit `kubectl exec`. Aucun `pods/exec` n'est délivré sur le pod partagé.
--
-- `tenant_logins` porte la fenêtre ouverte : `terms_ref` est la référence
-- citable de la vérification D2 (mail, décision, note de lot — jamais un
-- secret), `opened_by` l'auteur du geste, l'échéance et la fermeture.
-- `pod_name` est le pod de connexion désigné à l'ouverture (nom unique, rendu
-- par `host tenant login`) : la fenêtre ne rend pas la main à l'expiration
-- tant que la fin de CE pod n'a pas été observée (`pod_ended_at`, B/P2 du
-- 27/11 : le pod pouvait survivre à la fenêtre SQL et écrire le PVC pendant
-- que le runner reprenait). Le runner observe la fin sur le volume partagé
-- (battement du pod) ; sans observation, la fenêtre reste fermée (fail
-- closed) et se ferme par `logout`, après suppression du pod constatée.
-- L'index partiel `tenant_logins_one_open` garantit une seule fenêtre
-- ouverte par tenant.
--
-- Additive seulement : table neuve, colonne à défaut, fonction de garde
-- remplacée à l'identique pour les hôtes non pod (aucune ligne `tenant_logins`
-- n'existe avant ce lot, donc le code d'avant ne voit aucune différence).

create table if not exists tenant_logins (
    id         bigserial primary key,
    host       text not null,
    tenant     text not null,
    state      text not null default 'open',
    terms_ref  text not null,
    opened_by  text not null,
    opened_at  timestamptz not null default now(),
    expires_at timestamptz not null,
    -- pod de connexion désigné par `host tenant login` : son nom est rendu au
    -- bureau AVANT la création, pour que le runner sache quel battement
    -- observer ; NULL = fenêtre d'avant ce delta, bornée par l'échéance.
    pod_name   text,
    -- fin observée du pod (battement silencieux du volume) ou constat de sa
    -- suppression par le bureau ; NULL = rien n'a encore prouvé que le PVC
    -- n'est plus monté par le pod de connexion.
    pod_ended_at timestamptz,
    pod_ended_by text,
    closed_at  timestamptz,
    closed_by  text,
    constraint tenant_logins_state_valid
        check (state in ('open', 'closed')),
    constraint tenant_logins_terms_valid
        check (btrim(terms_ref) <> '' and length(terms_ref) <= 300),
    constraint tenant_logins_opened_by_valid
        check (btrim(opened_by) <> '' and length(opened_by) <= 120),
    constraint tenant_logins_window_valid
        check (expires_at > opened_at
               and expires_at <= opened_at + interval '30 minutes'),
    constraint tenant_logins_pod_name_valid
        check (pod_name is null
               or (length(pod_name) <= 63
                   and pod_name ~ '^ameesh-login-[a-z0-9-]+$')),
    constraint tenant_logins_pod_end_valid
        check ((pod_ended_at is null and pod_ended_by is null)
               or (pod_ended_at is not null and pod_ended_at >= opened_at
                   and btrim(pod_ended_by) <> ''
                   and length(pod_ended_by) <= 120)),
    constraint tenant_logins_closed_valid
        check ((state = 'open' and closed_at is null and closed_by is null)
               or (state = 'closed' and closed_at is not null
                   and btrim(closed_by) <> '')),
    foreign key (host, tenant) references host_tenants (host, tenant)
        on delete cascade
);

comment on table tenant_logins is
    '#7911: seat-connection window of a pod tenant (open/closed, D2 terms proof, author, TTL <= 30 min); state only, no credential.';

comment on column tenant_logins.terms_ref is
    '#7911: citable reference of the supplier-terms verification made with the person before the seat connection (D2, decision #233251); a reference, never a secret.';

comment on column tenant_logins.opened_by is
    '#7911: human or avatar who opened the window (never an agent nor the tenant runner); the CLI operator() gate enforces it.';

comment on column tenant_logins.pod_name is
    '#7911: dedicated connection pod designated at open (returned to the bureau before creation); the window stays closed to claims until this pod''s end is observed.';

comment on column tenant_logins.pod_ended_at is
    '#7911: observed end of the connection pod (silent liveness beat on the tenant volume, or the bureau''s observed deletion); only then does the window stop blocking claims, expiry alone does not.';

create unique index if not exists tenant_logins_one_open
    on tenant_logins (host, tenant)
    where state = 'open';

create unique index if not exists tenant_logins_pod_name
    on tenant_logins (host, pod_name)
    where pod_name is not null;

create index if not exists tenant_logins_expiry
    on tenant_logins (expires_at)
    where state = 'open';

-- Vrai pendant la fenêtre ouverte, tant que la fin du pod désigné n'a pas été
-- observée. Une fenêtre SANS pod (`pod_name` null, ligne d'avant ce delta)
-- reste bornée par son échéance ; une fenêtre AVEC pod ne rend pas la main à
-- `expires_at` : le pod peut survivre à la fenêtre SQL (B/P2 #46731) et le
-- runner reprendrait pendant qu'il écrit le PVC. La fin observée est la seule
-- sortie automatique ; `logout` (après suppression constatée) est l'autre.
create or replace function ameesh_tenant_login_active(h text, t text)
returns boolean language sql stable as $$
    select exists (
        select 1 from tenant_logins l
         where l.host = h
           and l.tenant = t
           and l.state = 'open'
           and (case when l.pod_name is null then l.expires_at > now()
                     else l.pod_ended_at is null end)
    )
$$;

comment on function ameesh_tenant_login_active(text, text) is
    '#7911: a tenant seat-connection window is open and its designated connection pod has not been observed ended; the claim guard and the runner heartbeat use it.';

-- `runner_presence.state` publie ce que le runner a vu : `running` (il sert
-- les tours) ou `login` (il a rendu ses baux et ne réclame plus). L'état fait
-- partie du contrat de lecture du superviseur (aucun secret).
alter table runner_presence
    add column if not exists state text not null default 'running'
    check (state in ('running', 'login'));

comment on column runner_presence.state is
    '#7911: running | login; the tenant runner publishes `login` once it has released its leases and stopped claiming for a seat connection.';

-- La garde de réclamation de 0601, plus la fenêtre `login` : pendant la
-- connexion du siège, ni le runner ni une session attach ne prennent un bail
-- du tenant. Le reste est identique (hôte non partagé, hôte enfant).
create or replace function ameesh_tenant_claim_ok(h text, claimer text, responsible text)
returns boolean language sql stable as $$
    select (not ameesh_host_shared(h)
            or (exists (
                select 1
                  from host_tenants t
                  join host_tenancy y
                    on y.host = t.host and y.shared and t.tenant = any (y.tenants)
                 where t.host = h
                   and t.tenant = responsible
                   and (t.runner_id = claimer
                        or claimer = 'attach:' || t.unix_user || '@' || h))
                and not ameesh_tenant_login_active(h, responsible)))
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
                   and p.runner_id = claimer
                   and rp.seen_at > now() - interval '120 seconds'))
$$;

comment on function ameesh_tenant_claim_ok(text, text, text) is
    '#7106/#7697/#7911: shared-host claim guard; a live seat-login window closes it, plus a live pod_hosts row whose pod_uid matches a fresh (120 s) runner heartbeat for child pods.';
