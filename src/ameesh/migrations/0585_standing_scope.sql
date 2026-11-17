-- 0585_standing_scope — #7629 : une approbation permanente nomme son
-- bénéficiaire, peut borner sa cible et cite sa charte.
--
-- Constat (ameesh 1.57) : `standing_approvals` ne portait ni bénéficiaire ni
-- cible. `receipts.standing_cover` cherchait par connecteur, opération,
-- classe, montant, devise et canon : l'approbation permanente d'une personne
-- couvrait donc l'action de l'avatar d'une autre personne du même canon.
--
-- Modèle ajouté (spec §8.3, série avatars #7627) :
--
-- * `beneficiary_kind` : `none` (ligne historique, ne couvre RIEN),
--   `any` (le canon a explicitement autorisé une approbation sans
--   bénéficiaire — cas de compatibilité seulement), `agent` (un agent nommé)
--   ou `avatar` (l'avatar de l'approbateur, `avatar` = son nom d'agent, ET
--   ses clones, c'est-à-dire les éphémères dont la lignée `created_by`
--   atteint cet agent) ;
-- * `beneficiary` : `agent:<nom>` pour `agent` et `avatar`, vide sinon ;
-- * `beneficiary_clones` : vrai pour `avatar` seulement (un clone est couvert
--   par la lignée, jamais par une liste figée : les clones naissent après le
--   grant) ;
-- * `targets` : liste de motifs de cible (dépôt, branche, canal). Vide =
--   aucune borne de cible. Un motif ne connaît que `*` (n'importe quelle
--   suite) et `?` (un caractère) ; tout le reste est littéral. Une action
--   n'est couverte que si sa cible correspond à AU MOINS un motif ;
-- * `charter_line` / `charter_version` : la ligne et la version de la charte
--   de la personne que le grant applique. La version est recopiée dans
--   `actions.policy_version` quand une action est couverte (voir la garde
--   ci-dessous).
--
-- La couverture est décidée en SQL, à l'enregistrement (le plafond du canon,
-- côté Python) ET à chaque réservation (`ameesh_standing_covers`, sous le
-- verrou du grant et de l'authentificateur). La ligne d'une approbation est
-- immuable : seules la révocation et la consommation de son plafond
-- l'écrivent.
--
-- MIGRATION ADDITIVE : les colonnes ont un défaut qui ne couvre rien
-- (`none`) pour toute ligne existante ; aucun grant n'est en service (spec
-- §3.5). `ameesh_standing_register` (ancienne signature) reste appelable par
-- un écrivain N-1 : il enregistre un grant `none`, qui ne couvre rien —
-- jamais un élargissement silencieux. Le nouveau chemin est
-- `ameesh_standing_register_scoped`.

-- --------------------------------------------------------------------------
-- 1. portée du grant
-- --------------------------------------------------------------------------

alter table standing_approvals
    add column if not exists beneficiary_kind text not null default 'none',
    add column if not exists beneficiary text not null default '',
    add column if not exists beneficiary_clones boolean not null default false,
    add column if not exists targets jsonb not null default '[]'::jsonb,
    add column if not exists charter_line text not null default '',
    add column if not exists charter_version text not null default '';

comment on column standing_approvals.beneficiary_kind is
    '#7629 : none (ne couvre rien) | any (autorisé explicitement par le canon) | agent | avatar.';
comment on column standing_approvals.beneficiary is
    '#7629 : agent:<nom> pour agent/avatar ; vide pour none/any.';
comment on column standing_approvals.beneficiary_clones is
    '#7629 : vrai si les clones de l''avatar (lignée created_by) sont couverts.';
comment on column standing_approvals.targets is
    '#7629 : motifs de cible (* et ? seulement) ; tableau vide = aucune borne.';
comment on column standing_approvals.charter_line is
    '#7629 : ligne de la charte de la personne que le grant applique.';
comment on column standing_approvals.charter_version is
    '#7629 : version de la charte, recopiée dans actions.policy_version à la couverture.';

alter table standing_approvals
    add constraint standing_approvals_beneficiary_check check (
        (beneficiary_kind = 'none' and beneficiary = '' and not beneficiary_clones)
        or (beneficiary_kind = 'any' and beneficiary = '' and not beneficiary_clones)
        or (beneficiary_kind = 'agent'
            and beneficiary ~ '^agent:[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'
            and not beneficiary_clones)
        or (beneficiary_kind = 'avatar'
            and beneficiary ~ '^agent:[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'
            and beneficiary_clones)
    ),
    add constraint standing_approvals_targets_check check (
        jsonb_typeof(targets) = 'array' and jsonb_array_length(targets) <= 64
    ),
    add constraint standing_approvals_charter_check check (
        length(charter_line) <= 512 and length(charter_version) <= 128
    );

create index if not exists standing_approvals_beneficiary_idx
    on standing_approvals (beneficiary_kind, beneficiary) where revoked_at is null;

-- --------------------------------------------------------------------------
-- 2. motifs de cible et lignée de l'avatar
-- --------------------------------------------------------------------------

-- Un motif de cible : `*` = n'importe quelle suite, `?` = un caractère, tout
-- le reste est littéral (`%`, `_` et `\` sont échappés pour LIKE).
create or replace function ameesh_standing_glob(p_pattern text, p_value text)
returns boolean
language plpgsql immutable as $$
declare
    v_i integer;
    v_char text;
    v_like text := '';
begin
    if p_pattern is null or p_value is null then
        return false;
    end if;
    for v_i in 1..length(p_pattern) loop
        v_char := substr(p_pattern, v_i, 1);
        if v_char = '*' then
            v_like := v_like || '%';
        elsif v_char = '?' then
            v_like := v_like || '_';
        elsif v_char in ('%', '_', '\') then
            v_like := v_like || '\' || v_char;
        else
            v_like := v_like || v_char;
        end if;
    end loop;
    return p_value like v_like escape '\';
end
$$;

-- Une cible est couverte si la liste est vide (aucune borne) ou si AU MOINS
-- un motif correspond.
create or replace function ameesh_standing_target_ok(p_targets jsonb, p_target text)
returns boolean
language plpgsql immutable as $$
declare
    v_pattern text;
begin
    if p_targets is null or jsonb_typeof(p_targets) <> 'array'
       or jsonb_array_length(p_targets) = 0 then
        return true;
    end if;
    for v_pattern in select jsonb_array_elements_text(p_targets) loop
        if ameesh_standing_glob(v_pattern, p_target) then
            return true;
        end if;
    end loop;
    return false;
end
$$;

-- Le proposant est-il le bénéficiaire ? `any` couvre tout (uniquement quand
-- le canon l'a autorisé à l'enregistrement) ; `none` ne couvre rien.
-- `avatar` couvre l'avatar lui-même et ses clones : les éphémères dont la
-- lignée `created_by` atteint l'avatar. Seule une arête VERS UN ÉPHÉMÈRE est
-- suivie : un agent permanent créé par l'avatar rompt la lignée, et ses
-- propres descendants ne sont pas des clones (spec §8.3). `UNION` (pas `ALL`)
-- borne un éventuel cycle de la lignée ; une lignée rompue ne couvre pas.
create or replace function ameesh_standing_beneficiary_ok(
    p_kind text, p_beneficiary text, p_proposed_by text)
returns boolean
language plpgsql stable as $$
declare
    v_ok boolean;
begin
    if p_kind = 'any' then
        return true;
    end if;
    if p_kind = 'none' or p_proposed_by is null then
        return false;
    end if;
    if p_kind = 'agent' then
        return p_proposed_by = p_beneficiary;
    end if;
    if p_kind <> 'avatar' then
        return false;
    end if;
    if p_proposed_by = p_beneficiary then
        return true;
    end if;
    if p_beneficiary not like 'agent:%' or p_proposed_by not like 'agent:%' then
        return false;
    end if;
    with recursive tree(name) as (
        select substr(p_beneficiary, 7)
        union
        select r.name from agent_registry r join tree t on r.created_by = t.name
         where r.ephemeral
    )
    select exists (select 1 from tree t where 'agent:' || t.name = p_proposed_by)
      into v_ok;
    return v_ok;
end
$$;

-- La couverture, en une lecture : grant vivant (non révoqué, non échu),
-- authentificateur encore actif, canon de l'action, connecteur, opération,
-- classe, devise du montant, bénéficiaire et cible. Le plafond cumulé n'est
-- pas ici : il est contrôlé sous le verrou du grant par
-- `ameesh_standing_reserve`. Fonction volatile : elle lit l'heure réelle
-- (`clock_timestamp()`), comme les contrôles d'échéance de 0011.
--
-- Une action ABSENTE de `actions` (outil ou test qui réserve une tentative
-- sans passer par la porte) n'a ni canon, ni bénéficiaire, ni cible à
-- comparer : seuls les grants SANS portée (`any`, aucune cible) restent
-- couverts, et les contrôles de description (connecteur, opération, classe,
-- devise) sont faits par `ameesh_standing_reserve` avec ses paramètres. Un
-- grant `agent`/`avatar` ou borné par une cible n'est JAMAIS couvert sans sa
-- ligne d'action : la portée ne se contourne pas par un identifiant libre.
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

-- --------------------------------------------------------------------------
-- 3. réservation : la couverture complète est recontrôlée sous le verrou
-- --------------------------------------------------------------------------

-- Corps repris de 0011, à trois différences près : `ameesh_standing_covers`
-- remplace le contrôle partiel (connecteur, opération, classe, devise) et
-- ajoute bénéficiaire, cible, canon et authentificateur ; le reste (verrou
-- du grant, verrou partagé de l'authentificateur, échéance à l'heure réelle,
-- cumul, idempotence par (grant, action), contrôles APRÈS l'écriture) est
-- inchangé.
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

-- --------------------------------------------------------------------------
-- 4. enregistrement : nonce + grant en une transaction
-- --------------------------------------------------------------------------

-- Nouveau chemin (#7629) : les mêmes contrôles que 0011 (authentificateur
-- sous verrou partagé, échéances signées, nonce, insertion), plus la portée
-- du grant. Aucun contrôle de politique ici : le plafond du canon est lu par
-- l'appelant (receipts.register_standing) avant l'écriture, et le grant est
-- immuable.
create or replace function ameesh_standing_register_scoped(
    p_approver text, p_nonce text, p_challenge text, p_authenticator_id bigint,
    p_receipt text, p_connector text, p_operations jsonb, p_class text,
    p_max_amount bigint, p_currency text, p_exp bigint, p_iat bigint, p_until bigint,
    p_skew integer, p_registered_by text, p_beneficiary_kind text, p_beneficiary text,
    p_beneficiary_clones boolean, p_targets jsonb, p_charter_line text,
    p_charter_version text)
returns bigint
language plpgsql volatile as $$
declare
    v_id bigint;
begin
    if p_exp is null or p_iat is null or p_until is null then
        raise exception 'ameesh_standing_register_scoped : échéances signées (exp, iat, until) requises';
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
         action_class, max_amount, currency, valid_until, registered_by,
         beneficiary_kind, beneficiary, beneficiary_clones, targets, charter_line,
         charter_version)
    values
        (p_approver, p_nonce, p_authenticator_id, p_challenge, p_receipt, p_connector,
         p_operations, p_class, p_max_amount, p_currency, to_timestamp(p_until),
         p_registered_by, p_beneficiary_kind, p_beneficiary, p_beneficiary_clones,
         p_targets, p_charter_line, p_charter_version)
    returning id into v_id;
    -- ⏱ les INSERT ont pu attendre : un dépassement annule tout
    perform ameesh_receipt_deadlines('enregistrement du grant', p_exp, p_iat, p_skew,
                                     to_timestamp(p_until));
    return v_id;
end
$$;

-- Ancienne signature, conservée pour un écrivain N-1 : elle enregistre un
-- grant `none`, qui ne couvre RIEN. Aucune ligne existante n'est élargie et
-- un déploiement par vagues reste sûr (fail closed).
create or replace function ameesh_standing_register(
    p_approver text, p_nonce text, p_challenge text, p_authenticator_id bigint,
    p_receipt text, p_connector text, p_operations jsonb, p_class text,
    p_max_amount bigint, p_currency text, p_exp bigint, p_iat bigint, p_until bigint,
    p_skew integer, p_registered_by text)
returns bigint
language plpgsql volatile as $$
begin
    return ameesh_standing_register_scoped(
        p_approver, p_nonce, p_challenge, p_authenticator_id, p_receipt, p_connector,
        p_operations, p_class, p_max_amount, p_currency, p_exp, p_iat, p_until, p_skew,
        p_registered_by, 'none', '', false, '[]'::jsonb, '', '');
end
$$;

-- --------------------------------------------------------------------------
-- 5. garde d'action : la version de politique peut suivre une couverture
-- --------------------------------------------------------------------------

-- `policy_version` (et l'empreinte qui la contient) reste IMMUABLE après la
-- proposition, SAUF sur la liaison d'un grant d'approbation permanente :
-- l'action couverte enregistre alors la version de charte du grant (spec
-- #7629 §4). La garde n'ouvre ce changement que si la nouvelle ligne est
-- `approved`, que l'autorisation est `standing`, et que le grant nommé est
-- actif et porte EXACTEMENT cette version de charte. L'empreinte doit changer
-- en même temps que la version : une écriture qui n'en change qu'une est
-- refusée. Le lancement recalcule l'empreinte depuis la ligne et la compare à
-- celle qu'il reçoit : une incohérence ne peut pas autoriser un reçu.
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
