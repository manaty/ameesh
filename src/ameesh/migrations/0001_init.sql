-- 0001_init — le cœur du mesh : registre des agents, boîte aux lettres, notifications.
--
-- Correspondance avec la spec §5.1 et le design d'un orchestrateur de tickets existant :
--   agent_registry (nom, chantier, harnais, hôte, bail, statut, modèle, budget)
--   agent_mailbox  (expéditeur, destinataire, corps, créé, remis, signature)
-- `sender`/`recipient` portent les `from`/`to` de la spec (mots réservés SQL) ;
-- `kind`/`payload`/`work_item_id`/`slack_ts` reprennent les colonnes du design
-- de cet orchestrateur pour que la même table serve aux transports suivants (R6).

create table if not exists agent_registry (
    name             text primary key
                     check (name ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'),
    chantier         text not null default '',
    harness          text not null default 'other',
    host             text not null default '',
    cwd              text,
    session_id       text,
    status           text not null default 'idle'
                     check (status in ('idle','queued','running','blocked','stopped','dead')),
    status_text      text not null default '',
    model            text,
    budget_usd       numeric(12,4),
    spent_usd        numeric(12,4) not null default 0,
    turns            bigint not null default 0,
    pending_prompt   text,
    last_error       text,
    lease_owner      text,
    lease_expires_at timestamptz,
    lease_epoch      bigint not null default 0,
    last_turn_at     timestamptz,
    last_seen        timestamptz not null default now(),
    created_at       timestamptz not null default now(),
    updated_at       timestamptz not null default now()
);

-- Un agent n'est réclamable que sur son hôte (R3/v1 : épinglé, sessions locales).
create index if not exists agent_registry_host_idx
    on agent_registry (host, status);
create index if not exists agent_registry_lease_idx
    on agent_registry (lease_expires_at) where lease_owner is not null;

create table if not exists agent_mailbox (
    id            bigserial primary key,
    sender        text not null,
    recipient     text not null,
    body          text not null,
    kind          text not null default 'notify'
                  check (kind in ('request','reply','notify')),
    payload       jsonb not null default '{}'::jsonb,
    work_item_id  text,
    status        text not null default 'pending'
                  check (status in ('pending','delivered','answered')),
    created_at    timestamptz not null default now(),
    delivered_at  timestamptz,
    signature     text,
    signature_key text,
    slack_ts      text,
    host          text,
    meta          jsonb not null default '{}'::jsonb
);

-- La requête chaude : « les non-lus de X », et l'historique récent.
create index if not exists agent_mailbox_unread_idx
    on agent_mailbox (recipient, id) where delivered_at is null;
create index if not exists agent_mailbox_recent_idx
    on agent_mailbox (recipient, created_at desc);

-- LISTEN/NOTIFY : chaque message déposé réveille l'exécuteur du destinataire.
create or replace function agent_mesh_notify_mail() returns trigger
language plpgsql as $$
begin
    perform pg_notify('agent_mail', json_build_object(
        'to', new.recipient, 'id', new.id, 'from', new.sender)::text);
    return new;
end $$;

drop trigger if exists agent_mailbox_notify on agent_mailbox;
create trigger agent_mailbox_notify
    after insert on agent_mailbox
    for each row execute function agent_mesh_notify_mail();

-- Les changements de bail/statut réveillent les exécuteurs : une place se
-- libère, un agent expire, un tour se termine.
create or replace function agent_mesh_notify_lease() returns trigger
language plpgsql as $$
begin
    perform pg_notify('agent_lease', json_build_object(
        'agent', new.name, 'owner', new.lease_owner,
        'epoch', new.lease_epoch, 'status', new.status)::text);
    return new;
end $$;

drop trigger if exists agent_registry_notify on agent_registry;
create trigger agent_registry_notify
    after insert or update of lease_owner, lease_epoch, status on agent_registry
    for each row execute function agent_mesh_notify_lease();
