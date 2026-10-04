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
-- * `standing_reservations` : le lien tentative ↔ grant (action_id, montant,
--   et la description de l'action : connecteur, opération, classe, devise).
--   Une réservation n'est libérée que sur un échec certain (failed), jamais
--   sur une issue inconnue.
-- * `mesh_consumed_nonces` (0006) reste le registre des nonces consommés ; on
--   y ajoute le challenge du reçu, pour l'audit.
--
-- Les opérations qui écrivent dans deux tables sont des fonctions PL/pgSQL :
-- un seul appel, une seule transaction, avec les deux pilotes (psql n'accepte
-- pas un CTE de modification imbriqué dans la requête d'enrobage JSON).
--
-- ÉCHÉANCES — règle commune à toute écriture qui accorde une autorité.
-- Toute échéance (exp du reçu, until du grant, iat futur au-delà de la
-- tolérance) est recontrôlée EN SQL, dans la transaction de l'écriture, APRÈS
-- l'acquisition du DERNIER verrou et immédiatement avant l'écriture, contre
-- `clock_timestamp()` (l'heure réelle) — jamais contre `now()`, figé au début
-- de la transaction, donc AVANT toute attente de verrou. Les échéances signées
-- sont passées en paramètres (l'hôte qui a vérifié la signature peut avoir
-- une autre heure : celle de la base fait foi pour écrire). Le contrôle est
-- `ameesh_receipt_deadlines()`, qui LÈVE une exception
-- `ameesh_echeance [expired|iat_future]` : la transaction entière est
-- annulée — aucune réservation, aucun nonce consommé, aucun grant enregistré.
-- Une écriture peut elle-même attendre un verrou (index unique : l'INSERT …
-- ON CONFLICT d'un nonce attend l'issue d'une insertion concurrente du même
-- nonce) : le contrôle est donc REFAIT après l'écriture, avant de rendre la
-- main ; un dépassement annule là aussi toute la transaction.
--
-- Motif imposé, en trois instructions DISTINCTES de la même fonction :
--   1. PRENDRE les verrous (`SELECT … FOR UPDATE` / `FOR SHARE`, dans des
--      variables ou par `perform`) ;
--   2. relire l'état verrouillé et comparer les échéances à
--      `clock_timestamp()` (`perform ameesh_receipt_deadlines(…)`, qui lève) ;
--   3. écrire.
-- JAMAIS d'échéance dans la clause WHERE de l'instruction qui prend le
-- verrou (ni d'un UPDATE) : PostgreSQL ne réévalue ce WHERE après l'attente
-- (EvalPlanQual) que si la ligne a été MODIFIÉE par la transaction qui tenait
-- le verrou ; si elle n'a fait qu'un `SELECT … FOR UPDATE / FOR SHARE`, la
-- condition reste celle évaluée AVANT l'attente. Les tests de course le
-- vérifient avec des verrous tenus sans modification de la ligne.
--
-- INVENTAIRE des fonctions qui décident d'une autorisation (verrous dans
-- l'ordre où ils sont pris ; « ⏱ » = contrôle des échéances) :
--
-- 1. ameesh_receipt_consume — consommation du nonce d'un reçu d'action
--    (receipts.verify_receipt(consume_by=…) → receipts.consume_nonce).
--    Échéances : exp, iat (+ tolérance).
--      a. authenticators[id] FOR SHARE (sérialisé avec une révocation) ;
--      ⏱ immédiatement avant l'INSERT ;
--      b. INSERT mesh_consumed_nonces ON CONFLICT DO NOTHING (verrou de la
--         clé (approver, nonce) : attend une insertion concurrente) ;
--      ⏱ après l'INSERT.
-- 2. ameesh_standing_register — enregistrement d'un grant
--    (receipts.register_standing). Échéances : exp, iat (+ tolérance), until.
--      a. authenticators[id] FOR SHARE ;
--      ⏱ immédiatement avant les INSERT ;
--      b. INSERT mesh_consumed_nonces ON CONFLICT DO NOTHING (clé du nonce) ;
--      c. INSERT standing_approvals (clé unique (approver, nonce) ; la clé
--         étrangère vers authenticators est couverte par a) ;
--      ⏱ après les INSERT.
-- 3. ameesh_standing_reserve — réservation sur un grant
--    (receipts.standing_reserve, receipts.standing_cover). Échéance :
--    standing_approvals.valid_until (le until signé, relu sous le verrou a).
--      a. standing_approvals[grant] FOR UPDATE ;
--      b. authenticators[grant.authenticator_id] FOR SHARE ;
--      ⏱ immédiatement avant la décision (réutilisation d'une réservation
--         vivante, ou UPDATE du cumul + INSERT de la réservation) ;
--      c. UPDATE standing_approvals (verrou a, déjà tenu), INSERT
--         standing_reservations (index unique (grant, action) vivant) ;
--      ⏱ après l'INSERT.
-- 4. ameesh_standing_release — libération après un échec certain
--    (receipts.standing_release). Verrous : a. standing_approvals[grant] FOR
--    UPDATE (même ordre que la réservation), b. UPDATE de la réservation.
--    Aucune échéance : la libération n'accorde aucune autorité, elle rend du
--    plafond ; toute réservation ultérieure recontrôle until sous verrou (3).
--
-- Hors inventaire (n'accordent rien, aucune échéance) : verify_receipt sans
-- consommation (lecture seule : le verdict n'autorise rien à lui seul) ;
-- revoke_standing et la révocation d'authentificateurs par
-- sync_authenticators (elles RETIRENT de l'autorité ; un UPDATE de la ligne,
-- sérialisé avec les FOR SHARE ci-dessus).
--
-- Ordre global des verrous : grant → authentificateur → nonce → lignes
-- écrites. Aucune fonction ne prend un grant après un authentificateur :
-- pas de cycle d'attente.

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
    -- description de l'action réservée : une nouvelle tentative de la même
    -- action ne réutilise la réservation que si elle est IDENTIQUE
    connector       text not null,
    operation       text not null,
    action_class    text not null,
    currency        text,
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

-- Contrôle des échéances à l'heure RÉELLE de la base (`clock_timestamp()`).
-- Appelé APRÈS le dernier verrou, immédiatement avant l'écriture, puis après
-- elle (voir l'en-tête). Lève `ameesh_echeance [code] : …` (codes de
-- receipts.py : expired, iat_future) : la transaction entière est annulée.
-- Ordre des contrôles : exp, iat, until (celui de la vérification Python).
create or replace function ameesh_receipt_deadlines(
    p_what text, p_exp bigint, p_iat bigint, p_skew integer, p_until timestamptz)
returns void
language plpgsql volatile as $$
declare
    v_now timestamptz := clock_timestamp();
begin
    if p_exp is not null and to_timestamp(p_exp) <= v_now then
        raise exception 'ameesh_echeance [expired] : % : reçu expiré (exp %, heure de la base %), rien n''est écrit',
            p_what, to_timestamp(p_exp), v_now;
    end if;
    if p_iat is not null
       and to_timestamp(p_iat) > v_now + make_interval(secs => coalesce(p_skew, 0)) then
        raise exception 'ameesh_echeance [iat_future] : % : iat dans le futur au-delà de % s de tolérance (iat %, heure de la base %), rien n''est écrit',
            p_what, coalesce(p_skew, 0), to_timestamp(p_iat), v_now;
    end if;
    if p_until is not null and p_until <= v_now then
        raise exception 'ameesh_echeance [expired] : % : grant échu (until %, heure de la base %), rien n''est écrit',
            p_what, p_until, v_now;
    end if;
end
$$;

-- Consommation du nonce d'un reçu d'action (inventaire, 1). Vrai si consommé
-- ici ; faux si le nonce l'était déjà, ou si l'authentificateur n'est plus
-- actif (révoqué entre la vérification et la consommation). Une échéance
-- dépassée lève une exception : rien n'est consommé.
create or replace function ameesh_receipt_consume(
    p_approver text, p_nonce text, p_by text, p_challenge text, p_authenticator_id bigint,
    p_exp bigint, p_iat bigint, p_skew integer)
returns boolean
language plpgsql volatile as $$
begin
    if p_exp is null or p_iat is null then
        raise exception 'ameesh_receipt_consume : échéances signées (exp, iat) requises';
    end if;
    -- a. l'authentificateur signataire, sous verrou PARTAGÉ : une révocation
    --    en cours est attendue puis vue ; une révocation postérieure attend.
    if p_authenticator_id is not null then
        perform 1 from authenticators a
         where a.id = p_authenticator_id and a.revoked_at is null
           for share;
        if not found then
            return false;
        end if;
    end if;
    -- ⏱ après le dernier verrou, immédiatement avant l'écriture
    perform ameesh_receipt_deadlines('consommation du nonce', p_exp, p_iat, p_skew, null);
    -- b. la décision atomique (clé primaire (approver, nonce)) ; peut attendre
    --    l'issue d'une insertion concurrente du même nonce
    insert into mesh_consumed_nonces (approver, nonce, consumed_by, challenge)
    values (p_approver, p_nonce, p_by, p_challenge)
    on conflict (approver, nonce) do nothing;
    if not found then
        return false;
    end if;
    -- ⏱ l'INSERT a pu attendre : un dépassement annule la consommation
    perform ameesh_receipt_deadlines('consommation du nonce', p_exp, p_iat, p_skew, null);
    return true;
end
$$;

-- Enregistrement d'un grant (inventaire, 2) : consommation du nonce ET
-- insertion du grant, ou rien. NULL si le nonce était déjà consommé ou si
-- l'authentificateur n'est plus actif (révoqué entre la vérification et
-- l'enregistrement). L'authentificateur est relu sous verrou partagé (`FOR
-- SHARE`) : une révocation en cours est attendue puis vue, une révocation
-- postérieure attend la fin de l'enregistrement. Une échéance dépassée (exp,
-- iat, until) lève une exception : ni nonce consommé, ni grant.
create or replace function ameesh_standing_register(
    p_approver text, p_nonce text, p_challenge text, p_authenticator_id bigint,
    p_receipt text, p_connector text, p_operations jsonb, p_class text,
    p_max_amount bigint, p_currency text, p_exp bigint, p_iat bigint, p_until bigint,
    p_skew integer, p_registered_by text)
returns bigint
language plpgsql volatile as $$
declare
    v_id bigint;
begin
    if p_exp is null or p_iat is null or p_until is null then
        raise exception 'ameesh_standing_register : échéances signées (exp, iat, until) requises';
    end if;
    -- a. verrou partagé de l'authentificateur signataire
    perform 1 from authenticators a
     where a.id = p_authenticator_id and a.revoked_at is null
       for share;
    if not found then
        return null;
    end if;
    -- ⏱ après le dernier verrou, immédiatement avant l'écriture
    perform ameesh_receipt_deadlines('enregistrement du grant', p_exp, p_iat, p_skew,
                                     to_timestamp(p_until));
    -- b. le nonce (peut attendre une insertion concurrente du même nonce)
    insert into mesh_consumed_nonces (approver, nonce, consumed_by, challenge)
    values (p_approver, p_nonce, coalesce(p_registered_by, 'standing'), p_challenge)
    on conflict (approver, nonce) do nothing;
    if not found then
        return null;
    end if;
    -- c. le grant
    insert into standing_approvals
        (approver, nonce, authenticator_id, challenge, receipt, connector, operations,
         action_class, max_amount, currency, valid_until, registered_by)
    values
        (p_approver, p_nonce, p_authenticator_id, p_challenge, p_receipt, p_connector,
         p_operations, p_class, p_max_amount, p_currency, to_timestamp(p_until),
         p_registered_by)
    returning id into v_id;
    -- ⏱ les INSERT ont pu attendre : un dépassement annule tout
    perform ameesh_receipt_deadlines('enregistrement du grant', p_exp, p_iat, p_skew,
                                     to_timestamp(p_until));
    return v_id;
end
$$;

-- Une réservation vivante ne sert une nouvelle tentative que pour la MÊME
-- action : montant, devise, connecteur, opération et classe identiques. Toute
-- différence lève une erreur (refus explicite, jamais un succès silencieux) ;
-- le message dit ce qui diffère.
create or replace function ameesh_standing_same_action(
    p_live standing_reservations, p_amount bigint, p_connector text, p_operation text,
    p_class text, p_currency text)
returns void
language plpgsql as $$
declare
    v_diff text;
begin
    v_diff := concat_ws(', ',
        case when p_live.amount is distinct from p_amount
             then format('montant %s au lieu de %s', p_amount, p_live.amount) end,
        case when p_live.currency is distinct from p_currency
             then format('devise %s au lieu de %s', coalesce(p_currency, 'null'),
                         coalesce(p_live.currency, 'null')) end,
        case when p_live.connector is distinct from p_connector
             then format('connecteur %s au lieu de %s', coalesce(p_connector, 'null'),
                         p_live.connector) end,
        case when p_live.operation is distinct from p_operation
             then format('opération %s au lieu de %s', coalesce(p_operation, 'null'),
                         p_live.operation) end,
        case when p_live.action_class is distinct from p_class
             then format('classe %s au lieu de %s', coalesce(p_class, 'null'),
                         p_live.action_class) end);
    if v_diff <> '' then
        raise exception 'ameesh_standing_reserve : action % déjà réservée sur le grant % avec une autre description (%)',
            p_live.action_id, p_live.grant_id, v_diff;
    end if;
end
$$;

-- Réservation d'un montant sur un grant pour une tentative d'action
-- (inventaire, 3).
--
-- 1. Verrou du grant (`SELECT … FOR UPDATE`) : les réservations d'un même
--    grant se sérialisent ici, y compris celles de la MÊME action.
-- 2. Tout est RELU et réévalué APRÈS le verrou : la fonction est VOLATILE et
--    tourne sous READ COMMITTED, chaque instruction prend donc un nouvel
--    instantané, et la ligne verrouillée est la dernière version validée.
--    L'authentificateur signataire est relu sous verrou partagé (`FOR
--    SHARE`), qui sérialise avec sa révocation (un UPDATE de la ligne).
-- 3. L'échéance (`valid_until`) est contrôlée APRÈS ce DERNIER verrou,
--    immédiatement avant la décision et l'écriture, contre
--    `clock_timestamp()` (jamais `now()`, figé avant les attentes) : un
--    grant échu pendant l'attente du verrou du grant OU de celui de
--    l'authentificateur ne couvre plus rien. Le contrôle est refait après
--    l'INSERT ; un dépassement lève `ameesh_echeance` et annule tout.
-- 4. Idempotente par (grant, action) : une réservation vivante est rendue
--    telle quelle (reused = true), à condition que la description de l'action
--    soit IDENTIQUE (montant, devise, connecteur, opération, classe) — toute
--    différence est une erreur, jamais un succès — et que le grant soit encore
--    valable et couvre l'action.
-- Aucune ligne rendue = pas de couverture ; `ameesh_echeance` = grant échu
-- (receipts.standing_reserve rend alors None).
create or replace function ameesh_standing_reserve(
    p_grant_id bigint, p_action_id text, p_amount bigint, p_reserved_by text,
    p_connector text, p_operation text, p_class text, p_currency text)
returns table (reservation_id bigint, standing_id bigint, reserved_amount bigint,
               grant_consumed bigint, grant_max bigint, reused boolean)
language plpgsql volatile as $$
declare
    v_grant standing_approvals%rowtype;
    v_live standing_reservations%rowtype;
    v_consumed bigint;
    v_id bigint;
begin
    if p_amount is null or p_amount < 0 then
        raise exception 'ameesh_standing_reserve : montant invalide';
    end if;
    -- a. verrou du grant
    select s.* into v_grant from standing_approvals s where s.id = p_grant_id for update;
    if not found then
        return;
    end if;
    -- (après le verrou : instantané neuf)
    select r.* into v_live
      from standing_reservations r
     where r.grant_id = p_grant_id and r.action_id = p_action_id
       and r.released_at is null;
    if found then
        perform ameesh_standing_same_action(v_live, p_amount, p_connector, p_operation,
                                            p_class, p_currency);
    end if;
    if v_grant.revoked_at is not null then
        return;
    end if;
    -- b. l'authentificateur qui a signé le grant, sous verrou PARTAGÉ : une
    --    révocation (UPDATE de la ligne) en cours est attendue puis vue ; une
    --    révocation postérieure attend la fin de cette réservation.
    perform 1 from authenticators a
     where a.id = v_grant.authenticator_id and a.revoked_at is null
       for share;
    if not found then
        return;
    end if;
    if v_grant.connector is distinct from p_connector
       or not coalesce(v_grant.operations @> jsonb_build_array(p_operation), false)
       or v_grant.action_class is distinct from p_class
       or (p_amount > 0 and (p_currency is null or v_grant.currency is distinct from p_currency))
    then
        return;
    end if;
    -- ⏱ après le dernier verrou (b), immédiatement avant la décision : l'heure
    --   réelle, pas now() (figé avant l'attente des verrous a et b)
    perform ameesh_receipt_deadlines(format('réservation sur le grant %s', p_grant_id),
                                     null, null, null, v_grant.valid_until);
    if v_live.id is not null then
        return query select v_live.id, v_grant.id, v_live.amount, v_grant.consumed_amount,
                            v_grant.max_amount, true;
        return;
    end if;
    if v_grant.consumed_amount + p_amount > v_grant.max_amount then
        return;
    end if;
    -- c. l'écriture (le verrou a est tenu ; l'index unique (grant, action)
    --    vivant peut attendre une transaction concurrente)
    update standing_approvals s
       set consumed_amount = s.consumed_amount + p_amount,
           uses = s.uses + 1,
           last_used_at = clock_timestamp()
     where s.id = p_grant_id
    returning s.consumed_amount into v_consumed;
    insert into standing_reservations
        (grant_id, action_id, amount, connector, operation, action_class, currency, reserved_by)
    values
        (p_grant_id, p_action_id, p_amount, p_connector, p_operation, p_class, p_currency,
         p_reserved_by)
    returning id into v_id;
    -- ⏱ l'INSERT a pu attendre : un dépassement annule la réservation
    perform ameesh_receipt_deadlines(format('réservation sur le grant %s', p_grant_id),
                                     null, null, null, v_grant.valid_until);
    return query select v_id, p_grant_id, p_amount, v_consumed, v_grant.max_amount, false;
end
$$;

-- Libération d'une réservation (échec CERTAIN de la tentative) : le montant
-- revient au plafond, une seule fois (inventaire, 4). Verrou du grant
-- d'abord, comme la réservation (même ordre : pas de cycle d'attente), puis
-- la réservation. Aucune échéance : libérer n'accorde aucune autorité (une
-- réservation ultérieure recontrôle until sous verrou).
create or replace function ameesh_standing_release(p_reservation_id bigint, p_reason text)
returns table (standing_id bigint, released_amount bigint, grant_consumed bigint)
language plpgsql volatile as $$
declare
    v_grant_id bigint;
    v_amount bigint;
begin
    select r.grant_id into v_grant_id from standing_reservations r
     where r.id = p_reservation_id;
    if not found then
        return;
    end if;
    -- a. verrou du grant
    perform 1 from standing_approvals s where s.id = v_grant_id for update;
    -- b. la réservation, relue sous ce verrou : libérée une seule fois
    update standing_reservations r
       set released_at = clock_timestamp(), released_reason = p_reason
     where r.id = p_reservation_id and r.released_at is null
    returning r.amount into v_amount;
    if not found then
        return;
    end if;
    return query
        update standing_approvals s
           set consumed_amount = s.consumed_amount - v_amount
         where s.id = v_grant_id
        returning s.id, v_amount, s.consumed_amount;
end
$$;
