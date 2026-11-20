-- SPDX-License-Identifier: AGPL-3.0-only
-- 0599_pod_supervisor_lease — #7695 (P2 de #7633, design
-- 7633-hote-avatars-pod.md §3.3).
--
-- Un hôte pod n'a qu'UN superviseur : c'est lui qui publie `host_resources`
-- (donc la porte d'hôte), tient `host_lifecycle` et synchronise le canon. Un
-- rollout qui chevauche, un pod dupliqué ou une copie du StatefulSet
-- créeraient deux écrivains : le second pourrait écraser le relevé du premier
-- et rouvrir la porte à tort. Le chart (`replicas: 1`, `OrderedReady`) est une
-- politesse ; la base est l'arbitre.
--
-- `pod_supervisor_leases` porte le bail du superviseur d'un hôte pod :
-- `runner_id` du détenteur, `epoch` incrémentée à chaque prise (le jeton de
-- garde), dates d'acquisition, de renouvellement, d'expiration et de remise.
-- L'API est en SQL (`ameesh_pod_supervisor_*`) pour que la prise soit
-- atomique même avec deux connexions concurrentes.
--
-- Le trigger `host_resources_pod_supervisor_guard` refuse tout relevé d'un
-- hôte qui a un bail vivant si l'auteur du relevé n'en est pas le détenteur
-- (`host_resources.runner_id`). Un hôte sans bail (tous les hôtes non pod,
-- et un pod avant la première acquisition) n'est pas affecté : la garde est
-- additive et ne change pas le sens d'un relevé existant.
--
-- État de supervision seulement : nom d'hôte, identifiant d'exécuteur,
-- numéro d'époque et dates. Aucun contenu de compte, aucun secret.

create table if not exists pod_supervisor_leases (
    host        text not null,
    runner_id   text not null,
    epoch       bigint not null,
    acquired_at timestamptz not null default clock_timestamp(),
    renewed_at  timestamptz not null default clock_timestamp(),
    expires_at  timestamptz not null,
    released_at timestamptz,
    primary key (host),
    constraint pod_supervisor_leases_host_valid check (host <> ''),
    constraint pod_supervisor_leases_runner_valid check (runner_id <> ''),
    constraint pod_supervisor_leases_epoch_valid check (epoch > 0),
    constraint pod_supervisor_leases_expires_valid check (expires_at > acquired_at)
);

comment on table pod_supervisor_leases is
    '#7695: one live supervisor lease per pod host (runner id, epoch, expiry); the single writer of host_resources for that host. State only, no secret.';

comment on column pod_supervisor_leases.epoch is
    '#7695: guard token of the supervisor lease, incremented on every acquisition; a former holder cannot renew or release a newer epoch.';

comment on column pod_supervisor_leases.released_at is
    '#7695: a released lease can be taken immediately; an unreleased one expires after its TTL (the supervisor renews on every resource pass).';

-- Prend le bail d'un hôte pod. Rend l'époque (>= 1) au détenteur, NULL si
-- un autre superviseur le tient encore. Une connexion concurrente qui tente
-- la même prise perd la course : son INSERT ... ON CONFLICT ne rend aucune
-- ligne, ou elle prend l'époque suivante après expiration/remise.
create or replace function ameesh_pod_supervisor_acquire(
    wanted_host text, wanted_runner text, ttl_s double precision
) returns bigint language plpgsql volatile as $$
declare
    obtained bigint;
begin
    if wanted_host is null or wanted_host = ''
       or wanted_runner is null or wanted_runner = ''
       or ttl_s is null or not (ttl_s > 0) then
        return null;
    end if;
    insert into pod_supervisor_leases (host, runner_id, epoch, expires_at)
    values (wanted_host, wanted_runner, 1,
            clock_timestamp() + make_interval(secs => ttl_s))
    on conflict (host) do update
       set runner_id = excluded.runner_id,
           epoch = pod_supervisor_leases.epoch + 1,
           acquired_at = clock_timestamp(),
           renewed_at = clock_timestamp(),
           expires_at = excluded.expires_at,
           released_at = null
     where pod_supervisor_leases.released_at is not null
        or pod_supervisor_leases.expires_at <= clock_timestamp()
    returning epoch into obtained;
    return obtained;
end $$;

comment on function ameesh_pod_supervisor_acquire(text, text, double precision) is
    '#7695: atomically take the supervisor lease of a pod host; NULL while another live lease holds it.';

-- Renouvelle le bail du détenteur courant ; faux si l'époque a changé, si le
-- bail a expiré ou s'il a été remis (l'ancien détenteur ne peut pas écrire).
create or replace function ameesh_pod_supervisor_renew(
    wanted_host text, wanted_runner text, wanted_epoch bigint,
    ttl_s double precision
) returns boolean language plpgsql volatile as $$
declare
    renewed boolean;
begin
    if ttl_s is null or not (ttl_s > 0) then
        return false;
    end if;
    update pod_supervisor_leases
       set renewed_at = clock_timestamp(),
           expires_at = clock_timestamp() + make_interval(secs => ttl_s)
     where host = wanted_host
       and runner_id = wanted_runner
       and epoch = wanted_epoch
       and released_at is null
       and expires_at > clock_timestamp()
    returning true into renewed;
    return coalesce(renewed, false);
end $$;

comment on function ameesh_pod_supervisor_renew(text, text, bigint, double precision) is
    '#7695: renew the supervisor lease of (host, runner, epoch); false for a former holder or an expired/released lease.';

-- Remet le bail à l'arrêt propre (SIGTERM) : le successeur n'attend pas le
-- TTL. Faux si un autre détenteur a déjà pris l'époque suivante.
create or replace function ameesh_pod_supervisor_release(
    wanted_host text, wanted_runner text, wanted_epoch bigint
) returns boolean language plpgsql volatile as $$
declare
    released boolean;
begin
    update pod_supervisor_leases
       set released_at = clock_timestamp()
     where host = wanted_host
       and runner_id = wanted_runner
       and epoch = wanted_epoch
       and released_at is null
    returning true into released;
    return coalesce(released, false);
end $$;

comment on function ameesh_pod_supervisor_release(text, text, bigint) is
    '#7695: release the supervisor lease on a clean stop; a former epoch cannot release the current holder.';

-- Le détenteur vivant d'un hôte pod, pour le diagnostic et les tests
-- (NULL si le bail est libre, expiré ou remis).
create or replace function ameesh_pod_supervisor_holder(wanted_host text)
returns text language sql stable as $$
    select runner_id from pod_supervisor_leases
     where host = wanted_host
       and released_at is null
       and expires_at > clock_timestamp()
$$;

comment on function ameesh_pod_supervisor_holder(text) is
    '#7695: live supervisor runner id of a pod host, NULL when the lease is free.';

-- La garde du relevé : dès qu'un hôte a une ligne de bail (même expirée), un
-- relevé doit venir du détenteur vivant. Sans ligne, aucun changement pour
-- les hôtes non pod.
create or replace function ameesh_pod_reading_supervisor_guard()
returns trigger language plpgsql volatile as $$
begin
    if exists (select 1 from pod_supervisor_leases where host = new.host)
       and not exists (
            select 1 from pod_supervisor_leases
             where host = new.host
               and runner_id = new.runner_id
               and released_at is null
               and expires_at > clock_timestamp()) then
        raise exception 'pod-supervisor-lease: reading for % refused, runner % is not the live lease holder',
            new.host, coalesce(new.runner_id, '<none>')
            using errcode = 'P0001',
                  hint = 'one supervisor per pod host (#7695, design 7633 §3.3)';
    end if;
    return new;
end $$;

comment on function ameesh_pod_reading_supervisor_guard() is
    '#7695: refuse a host_resources reading for a pod host whose author is not the live supervisor lease holder.';

drop trigger if exists host_resources_pod_supervisor_guard on host_resources;
create trigger host_resources_pod_supervisor_guard
before insert on host_resources
for each row execute function ameesh_pod_reading_supervisor_guard();
