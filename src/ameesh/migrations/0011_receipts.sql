-- 0011_receipts — reçus d'approbation ameesh-receipt/1 (spec §8, lot L6).
--
-- * `authenticators` : copie de travail du registre de confiance, qui vit dans
--   le canon (Member.authenticators, modifié seulement par PR revue). Chaque
--   ligne garde sa provenance (`canon_ref` = chemin@SHA du commit). Une ligne
--   n'est jamais modifiée dans sa clé : un changement de clé révoque la ligne
--   et en crée une nouvelle (l'historique reste lisible).
-- * `standing_approvals` : les grants (approbations permanentes bornées, §8.3).
--   Le nonce du reçu est consommé UNE fois, à l'enregistrement ; ensuite chaque
--   tentative couverte RÉSERVE son montant sur le plafond cumulé.
-- * `standing_reservations` : le lien tentative ↔ grant (action_id, montant).
--   Une réservation n'est libérée que sur un échec certain (failed), jamais
--   sur une issue inconnue.
-- * `mesh_consumed_nonces` (0006) reste le registre des nonces consommés ; on
--   y ajoute le challenge du reçu, pour l'audit.
--
-- Les opérations qui écrivent dans deux tables sont des fonctions PL/pgSQL :
-- un seul appel, une seule transaction, avec les deux pilotes (psql n'accepte
-- pas un CTE de modification imbriqué dans la requête d'enrobage JSON).

create table if not exists authenticators (
    id              bigserial primary key,
    approver        text not null
                    check (approver ~ '^human:[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'),
    facade          text not null
                    check (facade in ('webauthn', 'ed25519', 'device-es256')),
    credential_id   text not null
                    check (credential_id ~ '^[A-Za-z0-9_:.=-]+$'
                           and length(credential_id) <= 1024),
    public_key      text not null,          -- base64url : COSE_Key, ou brute (Ed25519, SEC1)
    key_fingerprint text not null,          -- sha256 hex des octets de la clé
    aaguid          text,
    level           text not null default 'standard'
                    check (level in ('standard', 'eleve')),
    canon_ref       text not null
                    check (canon_ref ~ '^\S+@([0-9a-f]{40}|[0-9a-f]{64})$'),
    enrolled_at     timestamptz not null default now(),
    updated_at      timestamptz not null default now(),
    revoked_at      timestamptz,
    revoked_reason  text
);

-- un identifiant de credential n'appartient qu'à un seul approbateur actif
create unique index if not exists authenticators_live_idx
    on authenticators (facade, credential_id) where revoked_at is null;
create index if not exists authenticators_approver_idx
    on authenticators (approver, facade);

create table if not exists standing_approvals (
    id               bigserial primary key,
    approver         text not null,
    nonce            text not null,
    authenticator_id bigint not null references authenticators (id),
    challenge        text not null,
    receipt          text not null,         -- le reçu vérifié, en JCS
    connector        text not null,
    operations       jsonb not null check (jsonb_typeof(operations) = 'array'),
    action_class     text not null
                     check (action_class in ('read', 'reversible', 'irreversible', 'costly')),
    max_amount       bigint not null check (max_amount >= 0),
    currency         text,
    consumed_amount  bigint not null default 0,
    uses             integer not null default 0,
    valid_until      timestamptz not null,
    registered_by    text,
    created_at       timestamptz not null default now(),
    last_used_at     timestamptz,
    revoked_at       timestamptz,
    revoked_by       text,
    unique (approver, nonce),
    check (consumed_amount >= 0 and consumed_amount <= max_amount)
);

create index if not exists standing_approvals_lookup_idx
    on standing_approvals (connector, action_class) where revoked_at is null;

create table if not exists standing_reservations (
    id              bigserial primary key,
    grant_id        bigint not null references standing_approvals (id),
    action_id       text not null,
    amount          bigint not null check (amount >= 0),
    reserved_by     text,
    reserved_at     timestamptz not null default now(),
    released_at     timestamptz,
    released_reason text
);

-- une seule réservation vivante par (grant, action)
create unique index if not exists standing_reservations_live_idx
    on standing_reservations (grant_id, action_id) where released_at is null;
create index if not exists standing_reservations_action_idx
    on standing_reservations (action_id);

alter table mesh_consumed_nonces
    add column if not exists challenge text;

-- Enregistrement d'un grant : consommation du nonce ET insertion du grant,
-- ou rien. NULL si le nonce était déjà consommé ou si l'authentificateur
-- n'est plus actif (révoqué entre la vérification et l'enregistrement).
create or replace function ameesh_standing_register(
    p_approver text, p_nonce text, p_challenge text, p_authenticator_id bigint,
    p_receipt text, p_connector text, p_operations jsonb, p_class text,
    p_max_amount bigint, p_currency text, p_valid_until double precision,
    p_registered_by text)
returns bigint
language plpgsql as $$
declare
    v_id bigint;
begin
    insert into mesh_consumed_nonces (approver, nonce, consumed_by, challenge)
    select p_approver, p_nonce, coalesce(p_registered_by, 'standing'), p_challenge
     where exists (select 1 from authenticators a
                    where a.id = p_authenticator_id and a.revoked_at is null)
    on conflict (approver, nonce) do nothing;
    if not found then
        return null;
    end if;
    insert into standing_approvals
        (approver, nonce, authenticator_id, challenge, receipt, connector, operations,
         action_class, max_amount, currency, valid_until, registered_by)
    values
        (p_approver, p_nonce, p_authenticator_id, p_challenge, p_receipt, p_connector,
         p_operations, p_class, p_max_amount, p_currency, to_timestamp(p_valid_until),
         p_registered_by)
    returning id into v_id;
    return v_id;
end
$$;

-- Réservation d'un montant sur un grant pour une tentative d'action.
-- La décision est UN SEUL UPDATE … WHERE cumul + montant <= max … : sous
-- READ COMMITTED, deux réservations concurrentes se sérialisent sur la ligne
-- du grant et la seconde réévalue la condition sur la version à jour.
-- Idempotente par (grant, action) : une réservation vivante est rendue telle
-- quelle (reused = true), à condition que le grant soit encore valable.
-- Aucune ligne rendue = pas de couverture.
create or replace function ameesh_standing_reserve(
    p_grant_id bigint, p_action_id text, p_amount bigint, p_reserved_by text,
    p_connector text, p_operation text, p_class text, p_currency text)
returns table (reservation_id bigint, standing_id bigint, reserved_amount bigint,
               grant_consumed bigint, grant_max bigint, reused boolean)
language plpgsql as $$
declare
    v_existing record;
    v_grant record;
    v_id bigint;
begin
    if p_amount is null or p_amount < 0 then
        raise exception 'ameesh_standing_reserve : montant invalide';
    end if;
    select r.id, r.amount into v_existing
      from standing_reservations r
     where r.grant_id = p_grant_id and r.action_id = p_action_id
       and r.released_at is null;
    if found then
        if v_existing.amount <> p_amount then
            raise exception 'ameesh_standing_reserve : action déjà réservée pour un autre montant';
        end if;
        return query
            select v_existing.id, s.id, v_existing.amount, s.consumed_amount, s.max_amount, true
              from standing_approvals s
             where s.id = p_grant_id
               and s.revoked_at is null
               and s.valid_until > now()
               and s.connector = p_connector
               and s.operations @> jsonb_build_array(p_operation)
               and s.action_class = p_class
               and exists (select 1 from authenticators a
                            where a.id = s.authenticator_id and a.revoked_at is null);
        return;
    end if;
    begin
        update standing_approvals s
           set consumed_amount = s.consumed_amount + p_amount,
               uses = s.uses + 1,
               last_used_at = now()
         where s.id = p_grant_id
           and s.revoked_at is null
           and s.valid_until > now()
           and s.consumed_amount + p_amount <= s.max_amount
           and s.connector = p_connector
           and s.operations @> jsonb_build_array(p_operation)
           and s.action_class = p_class
           and (p_amount = 0 or s.currency = p_currency)
           and exists (select 1 from authenticators a
                        where a.id = s.authenticator_id and a.revoked_at is null)
        returning s.consumed_amount, s.max_amount into v_grant;
        if not found then
            return;
        end if;
        insert into standing_reservations (grant_id, action_id, amount, reserved_by)
        values (p_grant_id, p_action_id, p_amount, p_reserved_by)
        returning id into v_id;
    exception when unique_violation then
        -- Course avec une réservation concurrente de la MÊME action : ce bloc
        -- est annulé (l'UPDATE du plafond aussi) et on rend la réservation
        -- gagnante.
        return query
            select r.id, r.grant_id, r.amount, s.consumed_amount, s.max_amount, true
              from standing_reservations r
              join standing_approvals s on s.id = r.grant_id
             where r.grant_id = p_grant_id and r.action_id = p_action_id
               and r.released_at is null and r.amount = p_amount;
        return;
    end;
    return query
        select v_id, p_grant_id, p_amount, v_grant.consumed_amount, v_grant.max_amount, false;
end
$$;

-- Libération d'une réservation (échec CERTAIN de la tentative) : le montant
-- revient au plafond, une seule fois.
create or replace function ameesh_standing_release(p_reservation_id bigint, p_reason text)
returns table (standing_id bigint, released_amount bigint, grant_consumed bigint)
language plpgsql as $$
declare
    v record;
begin
    update standing_reservations r
       set released_at = now(), released_reason = p_reason
     where r.id = p_reservation_id and r.released_at is null
    returning r.grant_id as gid, r.amount as amt into v;
    if not found then
        return;
    end if;
    return query
        update standing_approvals s
           set consumed_amount = s.consumed_amount - v.amt
         where s.id = v.gid
        returning s.id, v.amt, s.consumed_amount;
end
$$;
