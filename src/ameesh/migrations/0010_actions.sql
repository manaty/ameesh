-- 0010_actions — actions et porte (spec §7, R5, lot L5).
--
-- * `actions` : une ligne par action, identité STABLE (`action_id`, clé
--   d'idempotence transmise au connecteur) à travers toutes ses tentatives.
--   L'identité (projet, connecteur, opération, cible, args, montant, devise,
--   version de politique, empreinte) est IMMUABLE après la proposition : le
--   reçu signe l'empreinte, rien ne doit pouvoir changer dessous.
-- * `action_attempts` : une ligne par tentative réellement lancée (le reçu ou
--   la réservation de grant qui l'a autorisée, l'issue).
-- * `action_events` : le journal, en ajout seul. Chaque transition d'état
--   y écrit une ligne par déclencheur, dans la même transaction.
--
-- Les transitions sont décidées sous le verrou de la ligne de l'action
-- (UPDATE conditionnel `WHERE state = <attendu>`, ou `SELECT … FOR UPDATE`
-- puis relecture) et vérifiées une seconde fois par un déclencheur : une
-- transition interdite échoue en base, quel que soit l'appelant.
--
--     proposed → approved → launched → confirmed | failed | unknown
--     unknown  → confirmed | failed            (réconciliation)
--     unknown  → approved                      (seulement si dedupe = guaranteed)
--     failed   → approved                      (nouvelle tentative, nouveau reçu)
--     proposed | approved | failed → cancelled
--
-- Les opérations qui écrivent dans plusieurs tables sont des fonctions
-- PL/pgSQL (un appel, une transaction, avec les deux pilotes). Elles appellent
-- les fonctions de 0011 — `ameesh_receipt_consume`, `ameesh_receipt_deadlines`,
-- `ameesh_standing_reserve`, `ameesh_standing_release` — au lieu d'en dupliquer
-- la logique : les corps PL/pgSQL ne sont résolus qu'à l'exécution, l'ordre
-- 0010 < 0011 est sans effet.
--
-- ÉCHÉANCES : la règle de 0011 s'applique (voir son en-tête). Toute fonction
-- d'ici qui accorde une autorité (lancement, remplacement) suit le motif
-- verrou → relecture et échéances → écriture, en instructions distinctes ;
-- aucune échéance dans le WHERE d'une instruction qui prend un verrou (une
-- condition d'heure n'y serait pas réévaluée après l'attente d'un verrou tenu
-- sans modification de la ligne). Ordre des verrous : la ligne de l'action
-- d'abord, puis celui de 0011 (grant → authentificateur → nonce → lignes
-- écrites).

create table if not exists actions (
    action_id        text primary key
                     check (action_id ~ '^act_[0-9A-Za-z]{26}$'),
    project          text not null
                     check (project ~ '^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$'),
    work_item        bigint references work_items (id),
    proposed_by      text not null
                     check (proposed_by ~ '^(human|agent):[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'),
    connector        text not null
                     check (connector ~ '^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$'),
    operation        text not null
                     check (operation ~ '^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$'),
    -- compte ou objet visé : jamais vide (ameesh-approve, L7, l'exige aussi)
    target           text not null check (length(target) between 1 and 1024),
    args             jsonb not null check (jsonb_typeof(args) = 'object'),
    class            text not null
                     check (class in ('read', 'reversible', 'irreversible', 'costly')),
    amount           bigint check (amount >= 0),
    currency         text check (currency ~ '^[A-Z]{3}$'),
    policy_version   text not null default '1' check (length(policy_version) between 1 and 128),
    digest           text not null check (digest ~ '^sha256:[0-9a-f]{64}$'),
    dedupe           text not null check (dedupe in ('guaranteed', 'none')),
    requires_receipt boolean not null,
    approvers        jsonb not null default '[]'::jsonb
                     check (jsonb_typeof(approvers) = 'array'),
    state            text not null default 'proposed'
                     check (state in ('proposed', 'approved', 'launched', 'confirmed',
                                      'failed', 'unknown', 'cancelled')),
    replaces         text references actions (action_id),
    replaced_by      text references actions (action_id),
    -- décision humaine signée qui assume le doublon (action de remplacement)
    replace_receipt  text,
    replace_approver text,
    replace_nonce    text,
    attempts         integer not null default 0 check (attempts >= 0),
    -- autorisation liée à la PROCHAINE tentative (approve / retry) ; le nonce
    -- du reçu n'est consommé qu'au lancement, avec la transition.
    auth_kind        text check (auth_kind in ('receipt', 'standing', 'none')),
    auth_receipt     text,                  -- le reçu vérifié, en JCS
    auth_approver    text,
    auth_nonce       text,
    auth_challenge   text,
    auth_authenticator_id bigint,
    auth_expires_at  timestamptz,
    auth_grant_id    bigint,
    auth_by          text,
    launch_deadline  timestamptz,
    external_ref     text,
    last_error       text,
    last_actor       text not null default '',
    last_note        text not null default '',
    created_at       timestamptz not null default now(),
    updated_at       timestamptz not null default now(),
    approved_at      timestamptz,
    launched_at      timestamptz,
    finished_at      timestamptz,

    -- une action irréversible ou coûteuse exige TOUJOURS un reçu (ou un grant)
    check (requires_receipt or class in ('read', 'reversible')),
    check (amount is null or currency is not null),
    check (replaces is null or replaces <> action_id),
    check (state <> 'approved' or auth_kind is not null),
    check (auth_kind is distinct from 'none' or not requires_receipt),
    check (auth_kind is distinct from 'receipt'
           or (auth_receipt is not null and auth_approver is not null
               and auth_nonce is not null and auth_challenge is not null
               and auth_authenticator_id is not null and auth_expires_at is not null)),
    check (auth_kind is distinct from 'standing' or auth_grant_id is not null),
    check (state <> 'proposed' or attempts = 0),
    check (replaces is null or (replace_receipt is not null and replace_approver is not null
                                and replace_nonce is not null))
);

create index if not exists actions_state_idx on actions (state, updated_at desc);
create index if not exists actions_project_idx on actions (project, created_at desc);
-- une action inconnue n'est remplacée qu'une fois
create unique index if not exists actions_replaces_idx
    on actions (replaces) where replaces is not null;

create table if not exists action_attempts (
    action_id      text not null references actions (action_id),
    attempt_no     integer not null check (attempt_no >= 1),
    auth_kind      text not null check (auth_kind in ('receipt', 'standing', 'none')),
    receipt_id     text,                    -- challenge du reçu (hex) : identifie le reçu
    receipt        text,                    -- le reçu, en JCS (archive)
    approver       text,
    nonce          text,
    grant_id       bigint,
    reservation_id bigint,
    state          text not null default 'launched'
                   check (state in ('launched', 'confirmed', 'failed', 'unknown')),
    external_ref   text,
    error          text,
    launched_by    text,
    launched_at    timestamptz not null default now(),
    deadline_at    timestamptz,
    finished_at    timestamptz,
    settled_by     text,
    primary key (action_id, attempt_no),
    check (auth_kind <> 'receipt'
           or (receipt_id is not null and approver is not null and nonce is not null)),
    check (auth_kind <> 'standing' or (grant_id is not null and reservation_id is not null))
);

-- un reçu n'autorise qu'UNE tentative (en plus de mesh_consumed_nonces)
create unique index if not exists action_attempts_nonce_idx
    on action_attempts (approver, nonce) where nonce is not null;

create table if not exists action_events (
    id          bigserial primary key,
    action_id   text not null references actions (action_id),
    attempt_no  integer,
    event       text not null,
    from_state  text,
    to_state    text,
    actor       text not null default '',
    note        text not null default '',
    created_at  timestamptz not null default now()
);

create index if not exists action_events_action_idx on action_events (action_id, id);

-- --------------------------------------------------------------------------
-- garde : identité immuable, transitions autorisées
-- --------------------------------------------------------------------------

create or replace function ameesh_actions_guard() returns trigger
language plpgsql as $$
begin
    if (new.action_id, new.project, new.work_item, new.proposed_by, new.connector,
        new.operation, new.target, new.args, new.class, new.amount, new.currency,
        new.policy_version, new.digest, new.dedupe, new.requires_receipt, new.approvers,
        new.replaces, new.replace_receipt, new.replace_approver, new.replace_nonce)
       is distinct from
       (old.action_id, old.project, old.work_item, old.proposed_by, old.connector,
        old.operation, old.target, old.args, old.class, old.amount, old.currency,
        old.policy_version, old.digest, old.dedupe, old.requires_receipt, old.approvers,
        old.replaces, old.replace_receipt, old.replace_approver, old.replace_nonce) then
        raise exception 'ameesh_actions : identité d''action immuable (%)', old.action_id;
    end if;
    if old.replaced_by is not null and new.replaced_by is distinct from old.replaced_by then
        raise exception 'ameesh_actions : % est déjà remplacée par %', old.action_id, old.replaced_by;
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

drop trigger if exists actions_guard on actions;
create trigger actions_guard
    before update on actions
    for each row execute function ameesh_actions_guard();

-- journal : chaque transition (et chaque nouvelle liaison d'autorisation)
-- écrit un événement dans la même transaction que la transition elle-même.
create or replace function ameesh_actions_journal() returns trigger
language plpgsql as $$
begin
    if tg_op = 'INSERT' then
        insert into action_events (action_id, attempt_no, event, from_state, to_state, actor, note)
        values (new.action_id, new.attempts, 'proposed', null, new.state,
                new.last_actor, new.last_note);
        return new;
    end if;
    if new.state is distinct from old.state then
        insert into action_events (action_id, attempt_no, event, from_state, to_state, actor, note)
        values (new.action_id, new.attempts, new.state, old.state, new.state,
                new.last_actor, new.last_note);
    elsif new.replaced_by is distinct from old.replaced_by then
        insert into action_events (action_id, attempt_no, event, from_state, to_state, actor, note)
        values (new.action_id, new.attempts, 'replaced', old.state, new.state,
                new.last_actor, new.last_note);
    elsif (new.auth_kind, new.auth_nonce, new.auth_grant_id)
          is distinct from (old.auth_kind, old.auth_nonce, old.auth_grant_id) then
        insert into action_events (action_id, attempt_no, event, from_state, to_state, actor, note)
        values (new.action_id, new.attempts, 'rebound', old.state, new.state,
                new.last_actor, new.last_note);
    end if;
    return new;
end
$$;

drop trigger if exists actions_journal on actions;
create trigger actions_journal
    after insert or update on actions
    for each row execute function ameesh_actions_journal();

-- le journal est en ajout seul
create or replace function ameesh_action_events_append_only() returns trigger
language plpgsql as $$
begin
    raise exception 'action_events : journal en ajout seul (% refusé)', tg_op;
end
$$;

drop trigger if exists action_events_append_only on action_events;
create trigger action_events_append_only
    before update or delete on action_events
    for each row execute function ameesh_action_events_append_only();

-- --------------------------------------------------------------------------
-- lancement : approved → launched + consommation du nonce (ou réservation sur
-- le grant) + tentative, en UNE transaction ; l'appel externe vient APRÈS.
-- --------------------------------------------------------------------------
-- `p_digest` est l'empreinte recalculée par la porte depuis l'action ; elle
-- doit être celle que le reçu a signée (vérifié en Python juste avant) et
-- celle de la ligne. Résultat : 'ok' ou un code de refus (rien n'a changé).
--
-- Motif de 0011 (en-tête « ÉCHÉANCES ») : verrou → relecture et échéances →
-- écriture, en instructions DISTINCTES ; jamais d'échéance dans le WHERE
-- d'une instruction qui prend un verrou.
--   a. verrou de la ligne de l'action (`SELECT … FOR UPDATE`), puis
--      relecture sous ce verrou : état, empreinte, autorisation liée ;
--   b. reçu : `ameesh_receipt_consume` (authentificateur FOR SHARE → ⏱ exp
--      et iat SIGNÉS, relus dans le reçu lié à la ligne → INSERT du nonce →
--      ⏱) ; grant : `ameesh_standing_reserve` (grant FOR UPDATE →
--      authentificateur FOR SHARE → ⏱ until → écriture → ⏱) ;
--   c. écritures : transition `launched` (verrou a, déjà tenu), tentative ;
--   ⏱ refait après la dernière écriture.
-- ⏱ = `ameesh_receipt_deadlines`, à l'heure réelle (`clock_timestamp()`) ;
-- `p_skew` : tolérance d'horloge sur iat (politique,
-- receipts.DEFAULT_CLOCK_SKEW par défaut). Une échéance dépassée rend son code
-- (`expired`, `iat_future`) et rien n'est écrit : nonce intact, aucune
-- réservation, aucune tentative.
-- Ordre des verrous : action, puis celui de 0011 (grant → authentificateur
-- → nonce → lignes écrites) ; aucune fonction ne prend une action après l'un
-- d'eux : pas de cycle d'attente.
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

-- --------------------------------------------------------------------------
-- issue : launched → confirmed | failed | unknown (connecteur, ou reprise
-- après redémarrage), unknown → confirmed | failed (réconciliation).
-- Une réservation de grant n'est libérée que sur un échec CERTAIN (failed).
-- `p_stale_after` (reprise) : n'agit que si l'échéance du lancement est
-- dépassée d'au moins ce délai, à l'heure réelle ; NULL = sans condition
-- d'échéance.
-- Transition conditionnelle sur l'action ET sur la tentative : action en
-- `p_from` à la tentative `p_attempt`, tentative `p_attempt` en `p_from`.
-- Un état déjà tranché (réconciliation, nouvelle tentative, humain) n'est
-- jamais écrasé : 'state', rien n'a changé.
-- Motif de 0011 : verrou de l'action (`SELECT … FOR UPDATE`), puis relecture
-- et condition d'échéance sous ce verrou, puis écritures — aucune condition
-- d'heure dans le WHERE qui verrouille. Verrous : action → tentative → grant
-- (libération) : même ordre que le lancement.
-- --------------------------------------------------------------------------
create or replace function ameesh_action_settle(
    p_action_id text, p_attempt integer, p_from text, p_to text, p_external_ref text,
    p_error text, p_by text, p_note text, p_settled_by text, p_stale_after double precision)
returns table (result text, released bigint, detail text)
language plpgsql as $$
declare
    a actions%rowtype;
    v_reservation bigint;
    v_released bigint;
begin
    if p_to not in ('confirmed', 'failed', 'unknown') or p_from not in ('launched', 'unknown')
       or (p_from = 'unknown' and p_to = 'unknown') then
        return query select 'invalid'::text, null::bigint, 'issue invalide'::text;
        return;
    end if;
    -- a. verrou de l'action, puis relecture sous ce verrou
    select * into a from actions where actions.action_id = p_action_id for update;
    if not found or a.state <> p_from or a.attempts <> p_attempt then
        return query select 'state'::text, null::bigint,
            'action absente, déjà réglée, ou tentative différente'::text;
        return;
    end if;
    -- échéance du lancement (reprise), à l'heure réelle, après le verrou
    if p_stale_after is not null
       and not coalesce(a.launch_deadline + make_interval(secs => p_stale_after)
                        < clock_timestamp(), false) then
        return query select 'state'::text, null::bigint,
            'échéance du lancement non atteinte'::text;
        return;
    end if;
    -- b. écritures
    update actions
       set state = p_to,
           finished_at = case when p_to in ('confirmed', 'failed') then now() else null end,
           external_ref = coalesce(p_external_ref, actions.external_ref),
           last_error = p_error,
           last_actor = coalesce(p_by, ''),
           last_note = coalesce(p_note, '')
     where actions.action_id = p_action_id;
    update action_attempts t
       set state = p_to,
           external_ref = coalesce(p_external_ref, t.external_ref),
           error = p_error,
           finished_at = now(),
           settled_by = p_settled_by
     where t.action_id = p_action_id and t.attempt_no = p_attempt and t.state = p_from
    returning t.reservation_id into v_reservation;
    if not found then
        -- l'action et sa tentative divergent : incohérence, tout est annulé
        raise exception 'ameesh_action_settle : tentative %#% absente ou pas en %',
            p_action_id, p_attempt, p_from;
    end if;
    if p_to = 'failed' and v_reservation is not null then
        select r.released_amount into v_released
          from ameesh_standing_release(v_reservation, 'failed : ' || p_action_id
                                                      || '#' || p_attempt) r;
    end if;
    return query select 'ok'::text, v_released, null::text;
end
$$;

-- --------------------------------------------------------------------------
-- remplacement : une décision humaine signée qui ASSUME le doublon crée une
-- nouvelle action (nouvel action_id, replaces = l'ancienne) ; consommation du
-- nonce, liaison et création en une transaction.
-- Motif de 0011 : a. verrou de l'action remplacée (`SELECT … FOR UPDATE`),
-- relecture sous ce verrou (unknown, pas encore remplacée, même empreinte) ;
-- b. consommation par `ameesh_receipt_consume` (authentificateur FOR SHARE →
-- ⏱ exp et iat SIGNÉS de la décision `p_receipt`, tolérance `p_skew` →
-- INSERT du nonce → ⏱) ; c. création de la nouvelle action, liaison de
-- l'ancienne ; ⏱ refait après ces écritures. Décision échue → code
-- 'expired' (ou 'iat_future'), rien n'est créé, le nonce reste intact.
-- --------------------------------------------------------------------------
create or replace function ameesh_action_replace(
    p_old text, p_old_digest text, p_new text, p_new_digest text, p_receipt text,
    p_approver text, p_nonce text, p_challenge text, p_authenticator_id bigint, p_by text,
    p_skew integer default 120)
returns table (result text, detail text)
language plpgsql as $$
declare
    o actions%rowtype;
    v_code text := 'state';
    v_exp bigint;
    v_iat bigint;
begin
    begin
        -- a. verrou de l'action remplacée, puis relecture sous ce verrou
        select * into o from actions where actions.action_id = p_old for update;
        if not found or o.state <> 'unknown' or o.replaced_by is not null
           or o.digest <> p_old_digest then
            raise exception 'action % absente, plus en unknown, ou déjà remplacée', p_old;
        end if;

        -- b. la décision : authentificateur, échéances signées, nonce (0011)
        v_code := 'revoked_authenticator';
        if p_authenticator_id is null then
            raise exception 'décision sans authentificateur : aucun remplacement';
        end if;
        v_exp := (p_receipt::jsonb #>> '{request,exp}')::bigint;
        v_iat := (p_receipt::jsonb #>> '{request,iat}')::bigint;
        v_code := 'expired';
        if v_exp is null or v_iat is null then
            raise exception 'décision sans échéances signées (exp, iat) : aucun remplacement';
        end if;
        v_code := 'replay';
        if not ameesh_receipt_consume(
                p_approver, p_nonce,
                coalesce(p_by, 'porte') || ' (remplacement de ' || p_old || ')',
                p_challenge, p_authenticator_id, v_exp, v_iat, p_skew) then
            if not exists (select 1 from mesh_consumed_nonces n
                            where n.approver = p_approver and n.nonce = p_nonce) then
                v_code := 'revoked_authenticator';
                raise exception 'authentificateur révoqué avant le remplacement';
            end if;
            raise exception 'nonce déjà consommé';
        end if;

        -- c. écritures : la nouvelle action, puis la liaison de l'ancienne
        v_code := 'state';
        insert into actions
            (action_id, project, work_item, proposed_by, connector, operation, target, args,
             class, amount, currency, policy_version, digest, dedupe, requires_receipt,
             approvers, replaces, replace_receipt, replace_approver, replace_nonce,
             last_actor, last_note)
        values
            (p_new, o.project, o.work_item, p_approver, o.connector, o.operation, o.target,
             o.args, o.class, o.amount, o.currency, o.policy_version, p_new_digest, o.dedupe,
             o.requires_receipt, o.approvers, o.action_id, p_receipt, p_approver, p_nonce,
             coalesce(p_by, ''), 'remplace ' || o.action_id || ' (doublon assumé par '
                                 || p_approver || ')');
        update actions
           set replaced_by = p_new,
               last_actor = coalesce(p_by, ''),
               last_note = 'remplacée par ' || p_new || ' (doublon assumé par ' || p_approver || ')'
         where actions.action_id = p_old;

        -- ⏱ les écritures ont pu attendre : un dépassement annule tout
        perform ameesh_receipt_deadlines(format('remplacement de %s', p_old),
                                         v_exp, v_iat, p_skew, null);
    exception
        when raise_exception or unique_violation then
            -- un refus d'échéance (0011) porte son propre code : expired, iat_future
            v_code := coalesce(substring(sqlerrm from '^ameesh_echeance \[([a-z_]+)\]'), v_code);
            return query select v_code, sqlerrm::text;
            return;
    end;
    return query select 'ok'::text, null::text;
end
$$;
