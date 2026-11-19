-- SPDX-License-Identifier: AGPL-3.0-only
-- #7865 : retour arrière de 0596 (dépenses pré-déléguées).
--
-- À appliquer AVANT de redéployer le code N-1 : le ledger, ses fonctions, ses
-- colonnes et son déclencheur disparaissent ; les fonctions remplacées par
-- 0596 reprennent leur corps de 0585 (garde, couverture, réservation) ou de
-- 0010 (lancement). Script manuel, non rejoué par l'applicateur.
set local lock_timeout = '5s';

drop trigger if exists spend_ledger_settle on action_attempts;
drop function if exists ameesh_spend_settle_ledger();
drop function if exists ameesh_spend_confirm(bigint, text, bigint, timestamptz);
drop function if exists ameesh_spend_request_stop(bigint, text, text, date, text);
drop function if exists ameesh_spend_stop(bigint, text, text);
drop function if exists ameesh_spend_ledger_start(text, bigint, bigint, text, text,
                                                 text, text, bigint, text, text);
drop function if exists ameesh_spend_overdue(bigint, date);
drop function if exists ameesh_spend_projected_extra(bigint, date);
drop function if exists ameesh_spend_review_deadline(timestamptz, timestamptz, integer);
drop function if exists ameesh_spend_check(bigint, text, bigint, text);
drop function if exists ameesh_spend_state(bigint);
drop function if exists ameesh_spend_ongoing_daily(bigint);
drop function if exists ameesh_spend_total_accrued(bigint, date);
drop function if exists ameesh_spend_month_spent(bigint, date);
drop function if exists ameesh_spend_day_spent(bigint, date);
drop function if exists ameesh_spend_row_days(text, date, date, date, date);
drop function if exists ameesh_standing_register_spend(
    text, text, text, bigint, text, text, jsonb, text, bigint, text, bigint, bigint,
    bigint, integer, text, text, text, boolean, jsonb, text, text, bigint, bigint,
    jsonb, jsonb, integer);
drop table if exists spend_review_confirmations;
drop table if exists spend_ledger;
alter table actions
    drop column if exists recurrence,
    drop column if exists spend_provider,
    drop column if exists spend_purpose;
alter table standing_approvals
    drop column if exists max_per_month,
    drop column if exists max_per_day,
    drop column if exists spend_providers,
    drop column if exists spend_purposes,
    drop column if exists review_horizon_hours;

create or replace function ameesh_actions_guard() returns trigger
language plpgsql as $$
begin
    if (new.action_id, new.project, new.work_item, new.proposed_by, new.connector,
        new.operation, new.target, new.args, new.class, new.amount, new.currency,
        new.dedupe, new.requires_receipt, new.approvers,
        new.replaces, new.replace_receipt, new.replace_approver, new.replace_nonce)
       is distinct from
       (old.action_id, old.project, old.work_item, old.proposed_by, old.connector,
        old.operation, old.target, old.args, old.class, old.amount, old.currency,
        old.dedupe, old.requires_receipt, old.approvers,
        old.replaces, old.replace_receipt, old.replace_approver, old.replace_nonce) then
        raise exception 'ameesh_actions : identité d''action immuable (%)', old.action_id;
    end if;
    if (new.policy_version, new.digest) is distinct from (old.policy_version, old.digest) then
        if new.state <> 'approved'
           or new.auth_kind is distinct from 'standing'
           or new.auth_grant_id is null
           or new.policy_version = old.policy_version
           or new.digest = old.digest
           or not exists (select 1 from standing_approvals s
                           where s.id = new.auth_grant_id
                             and s.revoked_at is null
                             and s.charter_version = new.policy_version) then
            raise exception 'ameesh_actions : version de politique et empreinte immuables hors couverture permanente (%)',
                old.action_id;
        end if;
    end if;
    if old.replaced_by is not null and new.replaced_by is distinct from old.replaced_by then
        raise exception 'ameesh_actions : % est déjà remplacée par %', old.action_id, new.replaced_by;
    end if;
    if new.attempts < old.attempts then
        raise exception 'ameesh_actions : compteur de tentatives décroissant (%)', old.action_id;
    end if;
    if new.state is distinct from old.state then
        if not ((old.state = 'proposed' and new.state in ('approved', 'cancelled'))
             or (old.state = 'approved' and new.state in ('launched', 'cancelled'))
             or (old.state = 'launched' and new.state in ('confirmed', 'failed', 'unknown'))
             or (old.state = 'unknown'  and new.state in ('confirmed', 'failed', 'approved'))
             or (old.state = 'failed'   and new.state in ('approved', 'cancelled'))) then
            raise exception 'ameesh_actions : transition interdite % → % (%)',
                old.state, new.state, old.action_id;
        end if;
        if old.state = 'unknown' and new.state = 'approved' and old.dedupe <> 'guaranteed' then
            raise exception 'ameesh_actions : issue inconnue sans déduplication garantie, '
                            'nouvelle tentative interdite (%)', old.action_id;
        end if;
        if new.state = 'launched' and new.attempts <> old.attempts + 1 then
            raise exception 'ameesh_actions : un lancement est une nouvelle tentative (%)',
                old.action_id;
        end if;
    elsif new.attempts <> old.attempts then
        raise exception 'ameesh_actions : tentative comptée sans lancement (%)', old.action_id;
    end if;
    if new.state = 'approved' and new.replaced_by is not null then
        raise exception 'ameesh_actions : % est remplacée, plus de tentative', old.action_id;
    end if;
    new.updated_at := now();
    return new;
end
$$;

create or replace function ameesh_standing_covers(p_grant_id bigint, p_action_id text)
returns boolean
language plpgsql volatile as $$
declare
    g standing_approvals%rowtype;
    a actions%rowtype;
    v_canon text;
begin
    select * into g from standing_approvals where id = p_grant_id;
    if not found then
        return false;
    end if;
    if g.revoked_at is not null then
        return false;
    end if;
    if g.valid_until <= clock_timestamp() then
        return false;
    end if;
    select ac.canon into v_canon
      from authenticators ac
     where ac.id = g.authenticator_id and ac.revoked_at is null;
    if not found then
        return false;
    end if;
    select * into a from actions where action_id = p_action_id;
    if not found then
        return g.beneficiary_kind = 'any' and jsonb_array_length(g.targets) = 0;
    end if;
    if v_canon is distinct from a.canon then
        return false;
    end if;
    if g.connector is distinct from a.connector
       or g.action_class is distinct from a.class
       or not coalesce(g.operations @> jsonb_build_array(a.operation), false) then
        return false;
    end if;
    if coalesce(a.amount, 0) > 0
       and (a.currency is null or g.currency is distinct from a.currency) then
        return false;
    end if;
    if not ameesh_standing_beneficiary_ok(g.beneficiary_kind, g.beneficiary,
                                          a.proposed_by) then
        return false;
    end if;
    if not ameesh_standing_target_ok(g.targets, a.target) then
        return false;
    end if;
    return true;
end
$$;

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
    -- c. la description fournie par l'appelant (elle fait foi pour un outil
    --    sans ligne d'action ; une action réelle est recontrôlée dans
    --    `ameesh_standing_covers` sur sa ligne en base)
    if v_grant.connector is distinct from p_connector
       or v_grant.action_class is distinct from p_class
       or not coalesce(v_grant.operations @> jsonb_build_array(p_operation), false) then
        return;
    end if;
    if p_amount > 0
       and (p_currency is null or v_grant.currency is distinct from p_currency) then
        return;
    end if;
    -- ⏱ après le dernier verrou (b), avant toute décision de couverture :
    --   l'heure réelle, pas now() (figé avant l'attente des verrous a et b).
    --   Un grant échu refuse en `expired`, même si sa portée ne couvrirait pas.
    perform ameesh_receipt_deadlines(format('réservation sur le grant %s', p_grant_id),
                                     null, null, null, v_grant.valid_until);
    -- d. la couverture : canon, bénéficiaire, cible (relus sous les verrous
    --    a et b) et, pour une action réelle, la description de sa ligne
    if not ameesh_standing_covers(p_grant_id, p_action_id) then
        return;
    end if;
    if v_live.id is not null then
        return query select v_live.id, v_grant.id, v_live.amount, v_grant.consumed_amount,
                            v_grant.max_amount, true;
        return;
    end if;
    if v_grant.consumed_amount + p_amount > v_grant.max_amount then
        return;
    end if;
    -- d. l'écriture (le verrou a est tenu ; l'index unique (grant, action)
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

create or replace function ameesh_action_launch(
    p_action_id text, p_digest text, p_auth_kind text, p_nonce text, p_grant_id bigint,
    p_by text, p_timeout double precision, p_skew integer default 120)
returns table (result text, attempt integer, reservation bigint, detail text)
language plpgsql as $$
declare
    a actions%rowtype;
    v_code text := 'state';
    v_attempt integer;
    v_reservation bigint;
    v_exp bigint;
    v_iat bigint;
    v_until timestamptz;
begin
    begin
        -- a. verrou de l'action, puis relecture sous ce verrou
        select * into a from actions where actions.action_id = p_action_id for update;
        if not found or a.state <> 'approved' or a.digest <> p_digest
           or a.auth_kind is distinct from p_auth_kind
           or a.auth_nonce is distinct from p_nonce
           or a.auth_grant_id is distinct from p_grant_id then
            return query select 'state'::text, null::integer, null::bigint,
                'action absente, plus en approved, ou autorisation changée'::text;
            return;
        end if;
        v_attempt := a.attempts + 1;

        if a.requires_receipt and a.auth_kind not in ('receipt', 'standing') then
            v_code := 'receipt_required';
            raise exception 'reçu ou grant requis';
        end if;

        -- b. l'autorisation (0011) : verrous, échéances à l'heure réelle, écriture
        if a.auth_kind = 'receipt' then
            v_code := 'revoked_authenticator';
            if a.auth_authenticator_id is null then
                raise exception 'reçu lié sans authentificateur : rien n''est lancé';
            end if;
            v_exp := (a.auth_receipt::jsonb #>> '{request,exp}')::bigint;
            v_iat := (a.auth_receipt::jsonb #>> '{request,iat}')::bigint;
            v_code := 'expired';
            if v_exp is null or v_iat is null then
                raise exception 'reçu lié sans échéances signées (exp, iat) : rien n''est lancé';
            end if;
            v_code := 'replay';
            if not ameesh_receipt_consume(
                    a.auth_approver, a.auth_nonce,
                    coalesce(p_by, 'porte') || ' (' || a.action_id || '#' || v_attempt || ')',
                    a.auth_challenge, a.auth_authenticator_id, v_exp, v_iat, p_skew) then
                if not exists (select 1 from mesh_consumed_nonces n
                                where n.approver = a.auth_approver and n.nonce = a.auth_nonce) then
                    v_code := 'revoked_authenticator';
                    raise exception 'authentificateur révoqué avant le lancement';
                end if;
                raise exception 'nonce déjà consommé';
            end if;
        elsif a.auth_kind = 'standing' then
            v_code := 'no_cover';
            select r.reservation_id into v_reservation
              from ameesh_standing_reserve(a.auth_grant_id, a.action_id, coalesce(a.amount, 0),
                                           coalesce(p_by, 'porte'), a.connector, a.operation,
                                           a.class, a.currency) r;
            if v_reservation is null then
                raise exception 'le grant % ne couvre plus l''action (plafond, échéance, révocation)',
                    a.auth_grant_id;
            end if;
            -- le grant reste verrouillé (FOR UPDATE de la réservation) : son
            -- échéance, relue ici, est recontrôlée après les écritures
            select s.valid_until into v_until from standing_approvals s
             where s.id = a.auth_grant_id;
        end if;

        -- c. écritures (la ligne de l'action est verrouillée depuis a)
        v_code := 'state';
        update actions
           set state = 'launched',
               attempts = v_attempt,
               launched_at = clock_timestamp(),
               launch_deadline = clock_timestamp() + make_interval(secs => greatest(p_timeout, 0)),
               external_ref = null,
               last_error = null,
               last_actor = coalesce(p_by, ''),
               last_note = 'lancée (tentative ' || v_attempt || ')'
         where actions.action_id = p_action_id
        returning * into a;

        insert into action_attempts
            (action_id, attempt_no, auth_kind, receipt_id, receipt, approver, nonce, grant_id,
             reservation_id, state, launched_by, launched_at, deadline_at)
        values
            (a.action_id, v_attempt, a.auth_kind, a.auth_challenge, a.auth_receipt,
             a.auth_approver, a.auth_nonce, a.auth_grant_id, v_reservation, 'launched',
             p_by, a.launched_at, a.launch_deadline);

        -- ⏱ les écritures ont pu attendre : un dépassement annule tout
        perform ameesh_receipt_deadlines(format('lancement de %s#%s', a.action_id, v_attempt),
                                         v_exp, v_iat, p_skew, v_until);
    exception
        when raise_exception or unique_violation then
            -- un refus d'échéance (0011) porte son propre code : expired, iat_future
            v_code := coalesce(substring(sqlerrm from '^ameesh_echeance \[([a-z_]+)\]'), v_code);
            return query select v_code, null::integer, null::bigint, sqlerrm::text;
            return;
    end;
    return query select 'ok'::text, v_attempt, v_reservation, null::text;
end
$$;
