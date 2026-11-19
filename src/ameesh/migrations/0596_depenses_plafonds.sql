-- 0596_depenses_plafonds — #7865 : dimension « dépense » des approbations
-- permanentes, plafonds mensuels et journaliers, ledger des dépenses
-- récurrentes et arrêt depuis le relevé de l'owner.
--
-- Constat (manaty/ameesh#6096) : `standing_approvals` ne portait qu'un
-- `max_amount` TOTAL et une devise, consommés par `standing_reservations` ;
-- une action de dépense n'enregistrait ni récurrence, ni fournisseur, ni
-- objet ; rien ne projetait le coût d'une ressource récurrente sur le mois.
--
-- Modèle ajouté (migration ADDITIVE : les lignes existantes gardent des
-- défauts qui ne couvrent aucune dépense) :
--
-- * `standing_approvals.max_per_month` / `.max_per_day` : plafonds de dépense
--   en unités mineures, dans la devise du grant (0 = pas de plafond de cette
--   dimension ; les deux à 0 = grant sans dimension dépense) ;
-- * `.spend_providers` / `.spend_purposes` : périmètre fermé de la dépense
--   (fournisseurs et objets autorisés, listes JSON de textes). Une action de
--   dépense hors de ces listes n'est PAS couverte ;
-- * `.review_horizon_hours` : l'horizon de la prochaine revue humaine, recopié
--   du canon (`extensions.ameesh.standing_approvals.review_horizon_hours`) à
--   l'enregistrement. La projection mensuelle d'une ressource récurrente
--   court jusqu'à cet horizon ; ce n'est pas une constante cachée ;
-- * `actions.recurrence` (`once` | `daily`), `.spend_provider`,
--   `.spend_purpose` : la description de la dépense, copiée dans la même
--   transaction que l'action (elle est aussi dans `args`, donc signée par le
--   reçu ; la garde d'action rend ces colonnes immuables) ;
-- * `spend_ledger` : le grand livre. Une ligne par action de dépense, avec le
--   montant, la récurrence, le fournisseur, l'objet, le décideur et le grant.
--   Le ledger est la SEULE source du déjà-dépensé et de la projection :
--   `day_spent`, `month_spent`, `total_accrued` et `projected` sont calculés
--   à partir de ses lignes (montants et dates), jamais d'un champ libre du
--   connecteur. Une dépense `once` compte son montant une fois, le jour de
--   son lancement ; une dépense `daily` compte son coût QUOTIDIEN pour chaque
--   jour où la ressource tourne, de `started_on` à `stopped_on` (ou
--   aujourd'hui tant qu'elle est en cours). Une ligne `failed` ne compte pas ;
--   une ligne `unknown` reste en cours (on ne suppose jamais qu'elle n'a pas
--   eu lieu).
--
-- RÉSERVATION ATOMIQUE : `ameesh_standing_reserve` garde le verrou du grant
-- (`FOR UPDATE`), puis recontrôle la couverture, les plafonds ET écrit le
-- ledger dans la MÊME transaction. Deux actions concurrentes sur le même
-- grant se sérialisent sur ce verrou ; la seconde voit la première. Un
-- dépassement lève `ameesh_spend [code] : …` — refus nommé, rien n'est
-- réservé ni écrit. L'idempotence par (grant, action) reste celle de 0011 :
-- une réservation vivante est rendue telle quelle, sans recalcul ni double
-- écriture du ledger.
--
-- Arrêt (#7865 B1/B2) : une ressource `daily` s'arrête par une ACTION du
-- connecteur (`operation = 'stop'`) que `ameesh_spend_request_stop` lie à la
-- ligne ; la ligne ne passe `stopped` qu'à la CONFIRMATION de cette action
-- (déclencheur ci-dessous), jamais sur déclaration. Le jour de l'arrêt
-- compte encore. À la prochaine revue humaine, l'owner peut confirmer la
-- ressource (`ameesh_spend_confirm`, acteur `human:`) ; sans confirmation,
-- le balayage `ameesh action spend sweep` émet l'arrêt AVANT le jour qui
-- franchirait le plafond mensuel ou le total. Le relevé (`ameesh action
-- recap`) liste les ressources en cours avec la commande d'arrêt d'une
-- ligne ; quand la projection atteint un plafond ou que la revue est
-- dépassée, il marque la ressource « ARRÊT REQUIS ».
--
-- ÉCHÉANCES : la règle de 0011 s'applique (verrou → relecture → échéances →
-- écriture). L'appel à `ameesh_spend_check` et l'INSERT du ledger se font
-- après le verrou du grant et de l'authentificateur, avant l'écriture.

-- --------------------------------------------------------------------------
-- 1. colonnes du grant et de l'action
-- --------------------------------------------------------------------------

alter table standing_approvals
    add column if not exists max_per_month bigint not null default 0,
    add column if not exists max_per_day bigint not null default 0,
    add column if not exists spend_providers jsonb not null default '[]'::jsonb,
    add column if not exists spend_purposes jsonb not null default '[]'::jsonb,
    add column if not exists review_horizon_hours integer not null default 24;

comment on column standing_approvals.max_per_month is
    '#7865 : plafond de dépense mensuel, unités mineures de currency (0 = aucun).';
comment on column standing_approvals.max_per_day is
    '#7865 : plafond de dépense journalier, unités mineures de currency (0 = aucun).';
comment on column standing_approvals.spend_providers is
    '#7865 : fournisseurs autorisés pour la dépense ([] = aucun périmètre de dépense).';
comment on column standing_approvals.spend_purposes is
    '#7865 : objets autorisés pour la dépense ([] = aucun périmètre de dépense).';
comment on column standing_approvals.review_horizon_hours is
    '#7865 : horizon de la prochaine revue humaine, recopié du canon à l''enregistrement.';

alter table standing_approvals
    add constraint standing_approvals_spend_caps_check check (
        max_per_month >= 0 and max_per_day >= 0
        and (max_per_month > 0 or max_per_day = 0)
        and (max_per_month = 0 or max_per_day <= max_per_month)
    ),
    add constraint standing_approvals_spend_scope_check check (
        jsonb_typeof(spend_providers) = 'array'
        and jsonb_array_length(spend_providers) <= 64
        and jsonb_typeof(spend_purposes) = 'array'
        and jsonb_array_length(spend_purposes) <= 64
    ),
    add constraint standing_approvals_review_horizon_check check (
        review_horizon_hours between 1 and 168
    ),
    add constraint standing_approvals_spend_currency_check check (
        max_per_month = 0 or currency is not null
    );

-- Les lignes antérieures (aucune en service, spec §3.5) gardent 0 / 0 / [] /
-- [] / 24 : elles ne portent aucune dimension dépense et ne couvrent donc
-- jamais une action de dépense. Aucune contrainte ne resserre une ligne
-- existante : le défaut satisfait les CHECK.
-- ameesh: constraint not tightened

alter table actions
    add column if not exists recurrence text not null default 'once',
    add column if not exists spend_provider text not null default '',
    add column if not exists spend_purpose text not null default '';

comment on column actions.recurrence is
    '#7865 : once | daily — copie immuable de args.spend.recurrence.';
comment on column actions.spend_provider is
    '#7865 : fournisseur de la dépense, copie immuable de args.spend.provider.';
comment on column actions.spend_purpose is
    '#7865 : objet de la dépense, copie immuable de args.spend.purpose.';

alter table actions
    add constraint actions_recurrence_check check (recurrence in ('once', 'daily')),
    add constraint actions_spend_fields_check check (
        length(spend_provider) <= 128 and length(spend_purpose) <= 512
        and (recurrence = 'once'
             or (spend_provider <> '' and spend_purpose <> '' and amount is not null))
    );

-- ameesh: constraint not tightened (défauts once / '' / '' sur les lignes
-- existantes : aucune n'a de récurrence daily, la seconde branche est vide).

-- La garde d'action de 0585, avec les trois colonnes de dépense dans
-- l'identité immuable. `policy_version` et `digest` gardent l'exception de
-- couverture permanente de 0585.
create or replace function ameesh_actions_guard() returns trigger
language plpgsql as $$
begin
    if (new.action_id, new.project, new.work_item, new.proposed_by, new.connector,
        new.operation, new.target, new.args, new.class, new.amount, new.currency,
        new.dedupe, new.requires_receipt, new.approvers,
        new.replaces, new.replace_receipt, new.replace_approver, new.replace_nonce,
        new.recurrence, new.spend_provider, new.spend_purpose)
       is distinct from
       (old.action_id, old.project, old.work_item, old.proposed_by, old.connector,
        old.operation, old.target, old.args, old.class, old.amount, old.currency,
        old.dedupe, old.requires_receipt, old.approvers,
        old.replaces, old.replace_receipt, old.replace_approver, old.replace_nonce,
        old.recurrence, old.spend_provider, old.spend_purpose) then
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

-- --------------------------------------------------------------------------
-- 2. ledger des dépenses
-- --------------------------------------------------------------------------

create table if not exists spend_ledger (
    id              bigserial primary key,
    action_id       text not null unique references actions (action_id),
    grant_id        bigint not null references standing_approvals (id),
    reservation_id  bigint references standing_reservations (id),
    approver        text not null,
    decided_by      text not null,
    provider        text not null check (provider <> '' and length(provider) <= 128),
    purpose         text not null check (purpose <> '' and length(purpose) <= 512),
    amount          bigint not null check (amount >= 0),
    currency        text not null check (currency ~ '^[A-Z]{3}$'),
    recurrence      text not null check (recurrence in ('once', 'daily')),
    state           text not null default 'ongoing'
                    check (state in ('ongoing', 'stopped', 'done', 'failed')),
    started_at      timestamptz not null default clock_timestamp(),
    started_on      date not null default ((clock_timestamp() at time zone 'UTC')::date),
    stopped_at      timestamptz,
    stopped_on      date,
    stopped_by      text not null default '',
    stop_reason     text not null default '',
    -- #7865 : arrêt d'une ressource récurrente — une ACTION du connecteur
    -- (`operation = 'stop'`), liée à la ligne ; la ligne ne passe `stopped`
    -- qu'à la confirmation de cette action, jamais sur déclaration.
    stop_action_id  text references actions (action_id),
    stop_requested_at timestamptz,
    stop_requested_on date,
    stop_requested_by text not null default '',
    confirmed_at    timestamptz,
    confirmed_by    text not null default '',
    -- #7865 (B4) : la décision signée de l'owner qui a confirmé la revue ; la
    -- preuve est vérifiée côté Python (`decisions.verify_proof`), l'identifiant
    -- est recopié ici pour l'audit du ledger.
    confirmed_decision_id bigint,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now(),
    -- une dépense ponctuelle est `done` après confirmation, `failed` si
    -- l'échec est certain, `ongoing` tant que l'issue est inconnue ; une
    -- ressource quotidienne ne passe jamais `done` (elle s'arrête).
    check (recurrence = 'daily' or state in ('ongoing', 'done', 'failed')),
    check (state <> 'done' or recurrence = 'once'),
    check (state <> 'stopped' or (stopped_at is not null and stopped_on is not null)),
    check (stopped_on is null or stopped_on >= started_on)
);

comment on table spend_ledger is
    '#7865 : le grand livre des dépenses — source unique du déjà-dépensé et de la projection.';

create index if not exists spend_ledger_state_idx
    on spend_ledger (state, recurrence, started_on);
create index if not exists spend_ledger_grant_idx
    on spend_ledger (grant_id, state, started_on);
create index if not exists spend_ledger_decided_idx
    on spend_ledger (decided_by, created_at desc);
-- #7865 (B3) : une action d'arrêt ne peut être liée qu'à UNE ressource ; une
-- même action ne doit donc jamais arrêter comptablement plusieurs lignes.
create unique index if not exists spend_ledger_stop_action_once
    on spend_ledger (stop_action_id) where stop_action_id is not null;

-- #7865 (B4, rejeu) : l'usage d'une décision signée est DURABLE et unique.
-- `spend_ledger.confirmed_decision_id` ne garde que la dernière confirmation ;
-- cette table d'historique prouve qu'une décision a déjà servi, même après
-- une confirmation plus récente, et empêche un rejeu de rafraîchir la revue.
create table if not exists spend_review_confirmations (
    decision_id  bigint primary key,
    ledger_id    bigint not null references spend_ledger (id) on delete cascade,
    -- l'heure SIGNÉE de la réponse (approval `created_at`), pas l'heure de
    -- l'appel : elle ordonne deux confirmations dans la même horloge.
    signed_at    timestamptz not null,
    confirmed_at timestamptz not null default clock_timestamp(),
    confirmed_by text not null
);
comment on table spend_review_confirmations is
    '#7865 : usage unique et durable des décisions signées qui confirment une revue.';
create index if not exists spend_review_confirmations_ledger_idx
    on spend_review_confirmations (ledger_id, confirmed_at desc);

-- --------------------------------------------------------------------------
-- 3. calculs du ledger (jour, mois, total, projection)
-- --------------------------------------------------------------------------

-- Nombre de jours de [p_from, p_to] couverts par une ligne du ledger.
-- `once` : un seul jour, celui du lancement. `daily` : chaque jour où la
-- ressource tourne, borné par stopped_on (ou p_to tant qu'elle est en cours).
create or replace function ameesh_spend_row_days(
    p_recurrence text, p_started_on date, p_stopped_on date, p_from date, p_to date)
returns bigint
language sql immutable as $$
    select case
        when p_recurrence = 'once' then
            case when p_started_on between p_from and p_to then 1::bigint else 0::bigint end
        when p_recurrence = 'daily' then
            greatest(0, least(coalesce(p_stopped_on, p_to), p_to)
                        - greatest(p_started_on, p_from) + 1)::bigint
        else 0::bigint
    end
$$;

create or replace function ameesh_spend_day_spent(p_grant_id bigint, p_day date)
returns bigint
language sql stable as $$
    select coalesce(sum(l.amount), 0)::bigint
      from spend_ledger l
     where l.grant_id = p_grant_id
       and l.state <> 'failed'
       and ameesh_spend_row_days(l.recurrence, l.started_on, l.stopped_on,
                                 p_day, p_day) > 0
$$;

create or replace function ameesh_spend_month_spent(p_grant_id bigint, p_day date)
returns bigint
language sql stable as $$
    select coalesce(sum(l.amount
                        * ameesh_spend_row_days(l.recurrence, l.started_on, l.stopped_on,
                                                date_trunc('month', p_day)::date, p_day)),
                    0)::bigint
      from spend_ledger l
     where l.grant_id = p_grant_id
       and l.state <> 'failed'
$$;

create or replace function ameesh_spend_total_accrued(p_grant_id bigint, p_day date)
returns bigint
language sql stable as $$
    select coalesce(sum(l.amount
                        * ameesh_spend_row_days(l.recurrence, l.started_on, l.stopped_on,
                                                l.started_on, p_day)),
                    0)::bigint
      from spend_ledger l
     where l.grant_id = p_grant_id
       and l.state <> 'failed'
$$;

create or replace function ameesh_spend_ongoing_daily(p_grant_id bigint)
returns bigint
language sql stable as $$
    select coalesce(sum(l.amount), 0)::bigint
      from spend_ledger l
     where l.grant_id = p_grant_id
       and l.recurrence = 'daily' and l.state = 'ongoing'
$$;

-- #7865 (B2) : la prochaine revue humaine d'une ressource récurrente. La
-- dernière confirmation humaine (ou, à défaut, le lancement) ouvre une
-- nouvelle fenêtre de `review_horizon_hours` ; passée cette échéance sans
-- confirmation, le balayage arrête la ressource AVANT le jour suivant.
create or replace function ameesh_spend_review_deadline(
    p_started_at timestamptz, p_confirmed_at timestamptz, p_horizon integer)
returns timestamptz
language sql immutable as $$
    select coalesce(p_confirmed_at, p_started_at)
           + make_interval(hours => coalesce(p_horizon, 24))
$$;

-- Coût des ressources récurrentes en cours entre aujourd'hui (déjà compté
-- dans `month_spent`) et la prochaine revue de CHAQUE ressource. Une
-- ressource dont la revue est dépassée ne projette plus rien : elle doit
-- être arrêtée (le balayage le fait), et le gate refuse toute nouvelle
-- dépense tant qu'elle n'est ni confirmée ni arrêtée (`spend_review`).
create or replace function ameesh_spend_projected_extra(p_grant_id bigint, p_today date)
returns bigint
language sql stable as $$
    select coalesce(sum(l.amount * greatest(0,
               ((coalesce(l.confirmed_at, l.started_at)
                 + make_interval(hours => g.review_horizon_hours))
                at time zone 'UTC')::date - p_today)), 0)::bigint
      from spend_ledger l
      join standing_approvals g on g.id = l.grant_id
     where l.grant_id = p_grant_id
       and l.recurrence = 'daily' and l.state = 'ongoing'
$$;

-- Nombre de ressources récurrentes dont la revue humaine est dépassée.
create or replace function ameesh_spend_overdue(p_grant_id bigint, p_today date)
returns bigint
language sql stable as $$
    select count(*)::bigint
      from spend_ledger l
      join standing_approvals g on g.id = l.grant_id
     where l.grant_id = p_grant_id
       and l.recurrence = 'daily' and l.state = 'ongoing'
       and ((coalesce(l.confirmed_at, l.started_at)
             + make_interval(hours => g.review_horizon_hours))
            at time zone 'UTC')::date <= p_today
$$;

-- L'état de projection d'un grant, à l'heure réelle : déjà dépensé
-- aujourd'hui, ce mois, projection jusqu'à la prochaine revue humaine
-- (aujourd'hui déjà compté, jours suivants ajoutés au coût quotidien des
-- ressources en cours).
create or replace function ameesh_spend_state(p_grant_id bigint)
returns table (day_spent bigint, month_spent bigint, total_accrued bigint,
               ongoing_daily bigint, projected bigint, horizon date,
               max_per_day bigint, max_per_month bigint, max_amount bigint,
               currency text)
language plpgsql volatile as $$
declare
    g standing_approvals%rowtype;
    v_today date := (clock_timestamp() at time zone 'UTC')::date;
    v_horizon date;
    v_month bigint;
    v_total bigint;
    v_ongoing bigint;
    v_extra bigint;
begin
    select * into g from standing_approvals where id = p_grant_id;
    if not found then
        return;
    end if;
    v_horizon := ((clock_timestamp()
                   + make_interval(hours => g.review_horizon_hours))
                  at time zone 'UTC')::date;
    select ameesh_spend_month_spent(p_grant_id, v_today),
           ameesh_spend_total_accrued(p_grant_id, v_today),
           ameesh_spend_ongoing_daily(p_grant_id),
           ameesh_spend_projected_extra(p_grant_id, v_today)
      into v_month, v_total, v_ongoing, v_extra;
    return query select
        ameesh_spend_day_spent(p_grant_id, v_today),
        v_month,
        v_total,
        v_ongoing,
        v_month + v_extra,
        v_horizon,
        g.max_per_day,
        g.max_per_month,
        g.max_amount,
        g.currency;
end
$$;

-- Le contrôle d'une action de dépense sur un grant. Rend un code stable
-- (`spend_*`) ou NULL si elle passe. Appelé SOUS le verrou du grant par
-- `ameesh_standing_reserve` ; il relit l'action en base (montant, devise,
-- récurrence, fournisseur, objet : la description signée, pas un paramètre
-- libre de l'appelant).
create or replace function ameesh_spend_check(
    p_grant_id bigint, p_action_id text, p_amount bigint, p_currency text)
returns text
language plpgsql volatile as $$
declare
    g standing_approvals%rowtype;
    a actions%rowtype;
    v_today date := (clock_timestamp() at time zone 'UTC')::date;
    v_horizon date;
    v_day bigint;
    v_month bigint;
    v_total bigint;
    v_extra bigint;
    v_candidate bigint;
begin
    select * into g from standing_approvals where id = p_grant_id;
    if not found then
        return 'spend_scope';
    end if;
    if g.max_per_month = 0 and g.max_per_day = 0 then
        return null;                    -- grant sans dimension dépense
    end if;
    select * into a from actions where action_id = p_action_id;
    if not found then
        return 'spend_scope';           -- pas de description signée en base
    end if;
    if a.recurrence not in ('once', 'daily') then
        return 'spend_recurrence';
    end if;
    if a.spend_provider = '' or a.spend_purpose = '' then
        return 'spend_scope';
    end if;
    if p_amount is null or p_amount < 0 or coalesce(a.amount, -1) <> p_amount then
        return 'spend_amount';
    end if;
    if p_currency is null or a.currency is distinct from p_currency
       or g.currency is distinct from p_currency then
        return 'spend_currency';
    end if;
    if not (g.spend_providers @> jsonb_build_array(a.spend_provider)) then
        return 'spend_provider';
    end if;
    if not (g.spend_purposes @> jsonb_build_array(a.spend_purpose)) then
        return 'spend_purpose';
    end if;
    if a.recurrence = 'daily' and p_amount = 0 then
        return 'spend_amount';
    end if;
    v_horizon := ((clock_timestamp()
                   + make_interval(hours => g.review_horizon_hours))
                  at time zone 'UTC')::date;
    v_day := ameesh_spend_day_spent(g.id, v_today);
    v_month := ameesh_spend_month_spent(g.id, v_today);
    v_total := ameesh_spend_total_accrued(g.id, v_today);
    v_extra := ameesh_spend_projected_extra(g.id, v_today);
    if a.recurrence = 'daily' then
        v_candidate := p_amount * (1 + greatest(0, v_horizon - v_today));
    else
        v_candidate := p_amount;
    end if;
    if g.max_per_day > 0 and v_day + p_amount > g.max_per_day then
        return 'spend_daily_cap';
    end if;
    -- Une ressource en cours dont la revue humaine est dépassée n'est ni
    -- confirmée ni arrêtée : aucune nouvelle dépense avant que le balayage
    -- (ou l'owner) ne tranche. C'est le gate qui refuse, pas le balayage.
    if ameesh_spend_overdue(g.id, v_today) > 0 then
        return 'spend_review';
    end if;
    -- La projection mensuelle compte le déjà dépensé, le coût de CHAQUE
    -- ressource en cours jusqu'à sa prochaine revue (confirmation comprise),
    -- puis la candidate jusqu'à la prochaine revue du grant.
    if v_month + v_extra + v_candidate > g.max_per_month then
        return 'spend_monthly_cap';
    end if;
    -- Le plafond TOTAL ne regarde pas le mois : il inclut le coût futur des
    -- ressources daily déjà en cours jusqu'à leur revue, sinon une ressource
    -- démarrée le mois dernier laisserait passer une candidate au-delà du
    -- total (B cx2).
    if v_total + v_extra + v_candidate > g.max_amount then
        return 'spend_total_cap';
    end if;
    return null;
end
$$;

-- Écrit (ou réactive après un échec certain) la ligne du ledger d'une action.
-- Ne touche jamais une ligne vivante : la réservation idempotente de 0011
-- rend la même réservation et le ledger n'est pas réécrit.
create or replace function ameesh_spend_ledger_start(
    p_action_id text, p_grant_id bigint, p_reservation_id bigint, p_approver text,
    p_decided_by text, p_provider text, p_purpose text, p_amount bigint,
    p_currency text, p_recurrence text)
returns bigint
language plpgsql volatile as $$
declare
    v_id bigint;
begin
    insert into spend_ledger
        (action_id, grant_id, reservation_id, approver, decided_by, provider, purpose,
         amount, currency, recurrence, state, started_at, started_on)
    values
        (p_action_id, p_grant_id, p_reservation_id, p_approver, p_decided_by,
         p_provider, p_purpose, p_amount, p_currency, p_recurrence, 'ongoing',
         clock_timestamp(), (clock_timestamp() at time zone 'UTC')::date)
    on conflict (action_id) do update
        set grant_id = excluded.grant_id,
            reservation_id = excluded.reservation_id,
            approver = excluded.approver,
            decided_by = excluded.decided_by,
            provider = excluded.provider,
            purpose = excluded.purpose,
            amount = excluded.amount,
            currency = excluded.currency,
            recurrence = excluded.recurrence,
            state = 'ongoing',
            started_at = excluded.started_at,
            started_on = excluded.started_on,
            stopped_at = null,
            stopped_on = null,
            stopped_by = '',
            stop_reason = '',
            updated_at = now()
        where spend_ledger.state = 'failed'
    returning id into v_id;
    if v_id is null then
        select id into v_id from spend_ledger where action_id = p_action_id;
    end if;
    return v_id;
end
$$;

-- Demande d'arrêt d'une ressource récurrente : elle LIE l'action du
-- connecteur (`operation = 'stop'`, proposée par un acteur authentifié) à la
-- ligne ; la ligne ne passe `stopped` qu'à la confirmation de cette action
-- (déclencheur ci-dessous). Une seule demande vivante ; None si la ligne
-- n'est pas une ressource quotidienne en cours ou si l'action n'est pas un
-- arrêt proposé/approuvé de CETTE ressource.
--
-- #7865 (B3) : l'action doit viser exactement la ressource — même connecteur,
-- même cible, même projet — et ses `args` signés doivent nommer cette ligne
-- et son action de départ. Une action d'arrêt ne peut être liée qu'à une
-- seule ligne (index unique partiel `spend_ledger_stop_action_once`), sinon
-- la confirmation d'un arrêt de B arrêterait comptablement A.
create or replace function ameesh_spend_request_stop(
    p_ledger_id bigint, p_action_id text, p_by text,
    p_on date default null, p_reason text default '')
returns table (id bigint, action_id text, stop_action_id text, state text,
               stop_requested_on date)
language plpgsql volatile as $$
declare
    v_ledger spend_ledger%rowtype;
    v_original actions%rowtype;
    v_stop actions%rowtype;
begin
    select * into v_ledger from spend_ledger where spend_ledger.id = p_ledger_id;
    if not found or v_ledger.recurrence <> 'daily' or v_ledger.state <> 'ongoing'
       or v_ledger.stop_action_id is not null then
        return;
    end if;
    select * into v_stop from actions where actions.action_id = p_action_id;
    if not found or v_stop.operation <> 'stop'
       or v_stop.state not in ('proposed', 'approved') then
        return;
    end if;
    select * into v_original from actions
     where actions.action_id = v_ledger.action_id;
    if not found then
        return;
    end if;
    if v_stop.connector is distinct from v_original.connector
       or v_stop.target is distinct from v_original.target
       or v_stop.project is distinct from v_original.project
       or v_stop.args #>> '{spend_stop,ledger_id}' is distinct from v_ledger.id::text
       or v_stop.args #>> '{spend_stop,action_id}' is distinct from v_ledger.action_id then
        return;
    end if;
    if exists (select 1 from spend_ledger
                where spend_ledger.stop_action_id = p_action_id) then
        return;
    end if;
    return query
        update spend_ledger l
           set stop_action_id = p_action_id,
               stop_requested_at = clock_timestamp(),
               stop_requested_on = coalesce(
                   p_on, (clock_timestamp() at time zone 'UTC')::date),
               stop_requested_by = coalesce(p_by, ''),
               stop_reason = left(coalesce(p_reason, ''), 512),
               updated_at = now()
         where l.id = p_ledger_id and l.recurrence = 'daily' and l.state = 'ongoing'
           and l.stop_action_id is null
        returning l.id, l.action_id, l.stop_action_id, l.state, l.stop_requested_on;
end
$$;

-- Confirmation d'une ressource récurrente : elle ouvre une nouvelle fenêtre
-- `review_horizon_hours` à partir de l'heure RÉELLE de la base. La preuve
-- humaine (décision signée de l'owner, option `confirm-spend-<id>`) est
-- vérifiée côté Python ; le SQL recopie l'acteur `human:` et l'identifiant de
-- la décision, jamais une date d'appel.
--
-- #7865 (B4, rejeu) : `p_signed_at` est l'heure SIGNÉE de la réponse. Une
-- décision ne sert qu'une fois (historique durable `spend_review_confirmations`,
-- clé primaire) ; rejouer la même décision sur la même ligne est idempotent et
-- ne réécrit PAS `confirmed_at`, même après une décision plus récente. Une
-- décision déjà utilisée ailleurs, une signature antérieure ou égale à la
-- dernière confirmation, ou une signature future sont refusées. Aucune
-- écriture si la ligne n'est pas une ressource quotidienne en cours, si un
-- arrêt est demandé, ou si l'acteur n'est pas `human:`.
create or replace function ameesh_spend_confirm(
    p_ledger_id bigint, p_by text, p_decision bigint, p_signed_at timestamptz)
returns table (id bigint, action_id text, state text, confirmed_at timestamptz,
               confirmed_decision_id bigint)
language plpgsql volatile as $$
declare
    v_ledger spend_ledger%rowtype;
    v_used bigint;
    v_now timestamptz;
begin
    if p_by is null or p_by not like 'human:%'
       or p_decision is null or p_decision <= 0 then
        return;
    end if;
    select * into v_ledger from spend_ledger
     where spend_ledger.id = p_ledger_id for update;
    if not found or v_ledger.recurrence <> 'daily' or v_ledger.state <> 'ongoing'
       or v_ledger.stop_action_id is not null then
        return;
    end if;
    select ledger_id into v_used from spend_review_confirmations
     where decision_id = p_decision;
    if found then
        -- rejeu de la même décision sur la même revue : aucun rafraîchissement
        if v_used = p_ledger_id then
            return query select l.id, l.action_id, l.state, l.confirmed_at,
                                l.confirmed_decision_id
                           from spend_ledger l where l.id = p_ledger_id;
        end if;
        return;  -- une décision ne confirme qu'une seule ressource
    end if;
    v_now := clock_timestamp();
    -- la fenêtre de fraîcheur est celle de la signature de décision
    -- (`decisions.SIGN_TTL_S`, 1 h) : une signature plus ancienne ne peut pas
    -- servir pour un premier usage tardif.
    if p_signed_at is null
       or p_signed_at > v_now + interval '2 minutes'
       or p_signed_at < v_now - interval '1 hour'
       or (v_ledger.confirmed_at is not null and p_signed_at <= v_ledger.confirmed_at) then
        return;
    end if;
    begin
        update spend_ledger l
           set confirmed_at = v_now,
               confirmed_by = p_by,
               confirmed_decision_id = p_decision,
               updated_at = now()
         where l.id = p_ledger_id and l.recurrence = 'daily' and l.state = 'ongoing'
           and l.stop_action_id is null;
        if not found then
            return;
        end if;
        insert into spend_review_confirmations
            (decision_id, ledger_id, signed_at, confirmed_at, confirmed_by)
        values (p_decision, p_ledger_id, p_signed_at, v_now, p_by);
    exception when unique_violation then
        -- course entre deux lignes : la décision a été consommée ailleurs
        return;
    end;
    return query select l.id, l.action_id, l.state, l.confirmed_at,
                        l.confirmed_decision_id
                   from spend_ledger l where l.id = p_ledger_id;
end
$$;

-- L'issue d'une tentative met le ledger à jour : un échec certain retire la
-- dépense du ledger et libère une demande d'arrêt non aboutie ; une issue
-- confirmée clôt une dépense ponctuelle, laisse courir une ressource
-- quotidienne, et ARRÊTE la ressource dont cette tentative est l'action
-- d'arrêt ; une issue inconnue ne touche à rien (la dépense a peut-être eu
-- lieu, l'arrêt peut-être pris).
create or replace function ameesh_spend_settle_ledger() returns trigger
language plpgsql as $$
begin
    if new.state is not distinct from old.state then
        return new;
    end if;
    if new.state = 'failed' then
        update spend_ledger
           set state = 'failed', updated_at = now()
         where action_id = new.action_id and state = 'ongoing';
        update spend_ledger
           set stop_action_id = null,
               stop_requested_at = null,
               stop_requested_on = null,
               stop_requested_by = '',
               stop_reason = '',
               updated_at = now()
         where stop_action_id = new.action_id and state = 'ongoing';
    elsif new.state = 'confirmed' then
        update spend_ledger
           set state = case when recurrence = 'daily' then 'ongoing' else 'done' end,
               updated_at = now()
         where action_id = new.action_id and state = 'ongoing';
        update spend_ledger
           set state = 'stopped',
               stopped_at = clock_timestamp(),
               stopped_on = coalesce(stop_requested_on,
                                     (clock_timestamp() at time zone 'UTC')::date),
               stopped_by = case when stop_requested_by <> '' then stop_requested_by
                                 else coalesce(new.settled_by, '') end,
               stop_reason = case when stop_reason <> '' then stop_reason
                                  else 'arrêt confirmé par le connecteur' end,
               updated_at = now()
         where stop_action_id = new.action_id and state = 'ongoing';
    end if;
    return new;
end
$$;

-- ameesh: trigger compatible with previous code (le déclencheur ne fait que
-- recopier dans le ledger l'issue déjà enregistrée par la tentative)
drop trigger if exists spend_ledger_settle on action_attempts;
create trigger spend_ledger_settle
    after insert or update of state on action_attempts
    for each row execute function ameesh_spend_settle_ledger();

-- --------------------------------------------------------------------------
-- 4. couverture : la portée de dépense fait partie du grant
-- --------------------------------------------------------------------------

-- Corps de 0585, plus la portée de dépense : un grant qui porte des plafonds
-- de dépense n'est couvert que par une action qui nomme un fournisseur et un
-- objet AUTORISÉS. Une action absente de `actions` ne porte pas cette
-- description : elle ne couvre rien sur un grant de dépense (jamais un
-- contournement par un identifiant libre).
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
        return g.beneficiary_kind = 'any' and jsonb_array_length(g.targets) = 0
           and g.max_per_month = 0 and g.max_per_day = 0;
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
    if g.max_per_month > 0 or g.max_per_day > 0 then
        if a.recurrence not in ('once', 'daily')
           or a.spend_provider = '' or a.spend_purpose = '' then
            return false;
        end if;
        if not (g.spend_providers @> jsonb_build_array(a.spend_provider))
           or not (g.spend_purposes @> jsonb_build_array(a.spend_purpose)) then
            return false;
        end if;
    elsif a.spend_provider <> '' or a.spend_purpose <> '' or a.recurrence = 'daily' then
        -- #7865 (B cx2) : une action de dépense ne se couvre JAMAIS par un
        -- grant sans dimension dépense (le ledger, la projection et le
        -- balayage n'existeraient pas : contournement des plafonds).
        return false;
    end if;
    return true;
end
$$;

-- --------------------------------------------------------------------------
-- 5. réservation : plafonds et ledger dans la transaction du verrou
-- --------------------------------------------------------------------------

-- Corps de 0585, plus le contrôle `ameesh_spend_check` et l'écriture du
-- ledger APRÈS le verrou du grant et de l'authentificateur, avant les
-- écritures de réservation. Un refus lève `ameesh_spend [code] : …` : le
-- bloc appelant (`ameesh_action_launch`) le rend comme code de refus, rien
-- n'est réservé ni écrit.
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
    v_spend text;
    v_ledger bigint;
    v_action actions%rowtype;
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
    -- b. l'authentificateur qui a signé le grant, sous verrou PARTAGÉ
    perform 1 from authenticators a
     where a.id = v_grant.authenticator_id and a.revoked_at is null
       for share;
    if not found then
        return;
    end if;
    -- c. la description fournie par l'appelant
    if v_grant.connector is distinct from p_connector
       or v_grant.action_class is distinct from p_class
       or not coalesce(v_grant.operations @> jsonb_build_array(p_operation), false) then
        return;
    end if;
    if p_amount > 0
       and (p_currency is null or v_grant.currency is distinct from p_currency) then
        return;
    end if;
    -- ⏱ après le dernier verrou (b), avant toute décision de couverture
    perform ameesh_receipt_deadlines(format('réservation sur le grant %s', p_grant_id),
                                     null, null, null, v_grant.valid_until);
    -- d. la couverture : canon, bénéficiaire, cible, portée de dépense
    if not ameesh_standing_covers(p_grant_id, p_action_id) then
        return;
    end if;
    if v_live.id is not null then
        return query select v_live.id, v_grant.id, v_live.amount, v_grant.consumed_amount,
                            v_grant.max_amount, true;
        return;
    end if;
    -- e. les plafonds de dépense, sur le ledger, sous le verrou du grant.
    --    L'action est relue : montant, devise, récurrence, fournisseur et
    --    objet sont ceux de sa ligne immuable, pas un paramètre libre.
    if v_grant.max_per_month > 0 or v_grant.max_per_day > 0 then
        v_spend := ameesh_spend_check(v_grant.id, p_action_id, p_amount, p_currency);
        if v_spend is not null then
            select * into v_action from actions where action_id = p_action_id;
            raise exception 'ameesh_spend [%] : dépense refusée sur le grant % '
                '(action %, fournisseur %, objet %, montant % %, déjà aujourd''hui '
                '%, ce mois %, projection % ; plafond jour %, mois %, total %)',
                v_spend, v_grant.id, p_action_id,
                coalesce(v_action.spend_provider, '?'),
                coalesce(v_action.spend_purpose, '?'),
                p_amount, coalesce(p_currency, '?'),
                ameesh_spend_day_spent(v_grant.id, (clock_timestamp() at time zone 'UTC')::date),
                ameesh_spend_month_spent(v_grant.id, (clock_timestamp() at time zone 'UTC')::date),
                (select s.projected from ameesh_spend_state(v_grant.id) s),
                v_grant.max_per_day, v_grant.max_per_month, v_grant.max_amount;
        end if;
    end if;
    if v_grant.consumed_amount + p_amount > v_grant.max_amount then
        return;
    end if;
    -- f. l'écriture (le verrou a est tenu)
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
    if v_grant.max_per_month > 0 or v_grant.max_per_day > 0 then
        select * into v_action from actions where action_id = p_action_id;
        v_ledger := ameesh_spend_ledger_start(
            p_action_id, p_grant_id, v_id, v_grant.approver, v_action.proposed_by,
            v_action.spend_provider, v_action.spend_purpose, p_amount, p_currency,
            v_action.recurrence);
    end if;
    -- ⏱ l'INSERT a pu attendre : un dépassement annule la réservation
    perform ameesh_receipt_deadlines(format('réservation sur le grant %s', p_grant_id),
                                     null, null, null, v_grant.valid_until);
    return query select v_id, p_grant_id, p_amount, v_consumed, v_grant.max_amount, false;
end
$$;

-- --------------------------------------------------------------------------
-- 6. enregistrement : le chemin avec la dimension dépense
-- --------------------------------------------------------------------------

-- Même corps que `ameesh_standing_register_scoped` (0585), plus les cinq
-- colonnes de dépense. L'ancienne fonction à 21 arguments reste appelable par
-- un écrivain N-1 : elle laisse les défauts (0 / 0 / [] / [] / 24), donc un
-- grant sans dimension dépense — jamais un élargissement silencieux.
-- Le contrôle du plafond du canon (durée, montant, devise, plafonds mensuel
-- et journalier, fournisseurs, objets, horizon) est fait côté Python, avant
-- cette écriture, par `receipts.register_standing`.
create or replace function ameesh_standing_register_spend(
    p_approver text, p_nonce text, p_challenge text, p_authenticator_id bigint,
    p_receipt text, p_connector text, p_operations jsonb, p_class text,
    p_max_amount bigint, p_currency text, p_exp bigint, p_iat bigint, p_until bigint,
    p_skew integer, p_registered_by text, p_beneficiary_kind text, p_beneficiary text,
    p_beneficiary_clones boolean, p_targets jsonb, p_charter_line text,
    p_charter_version text, p_max_per_month bigint, p_max_per_day bigint,
    p_spend_providers jsonb, p_spend_purposes jsonb, p_review_horizon_hours integer)
returns bigint
language plpgsql volatile as $$
declare
    v_id bigint;
begin
    if p_exp is null or p_iat is null or p_until is null then
        raise exception 'ameesh_standing_register_spend : échéances signées (exp, iat, until) requises';
    end if;
    perform 1 from authenticators a
     where a.id = p_authenticator_id and a.revoked_at is null
       for share;
    if not found then
        return null;
    end if;
    perform ameesh_receipt_deadlines('enregistrement du grant', p_exp, p_iat, p_skew,
                                     to_timestamp(p_until));
    insert into mesh_consumed_nonces (approver, nonce, consumed_by, challenge)
    values (p_approver, p_nonce, coalesce(p_registered_by, 'standing'), p_challenge)
    on conflict (approver, nonce) do nothing;
    if not found then
        return null;
    end if;
    insert into standing_approvals
        (approver, nonce, authenticator_id, challenge, receipt, connector, operations,
         action_class, max_amount, currency, valid_until, registered_by,
         beneficiary_kind, beneficiary, beneficiary_clones, targets, charter_line,
         charter_version, max_per_month, max_per_day, spend_providers, spend_purposes,
         review_horizon_hours)
    values
        (p_approver, p_nonce, p_authenticator_id, p_challenge, p_receipt, p_connector,
         p_operations, p_class, p_max_amount, p_currency, to_timestamp(p_until),
         p_registered_by, p_beneficiary_kind, p_beneficiary, p_beneficiary_clones,
         p_targets, p_charter_line, p_charter_version, coalesce(p_max_per_month, 0),
         coalesce(p_max_per_day, 0), coalesce(p_spend_providers, '[]'::jsonb),
         coalesce(p_spend_purposes, '[]'::jsonb), coalesce(p_review_horizon_hours, 24))
    returning id into v_id;
    perform ameesh_receipt_deadlines('enregistrement du grant', p_exp, p_iat, p_skew,
                                     to_timestamp(p_until));
    return v_id;
end
$$;

-- --------------------------------------------------------------------------
-- 7. lancement : un refus de plafond de dépense porte son code
-- --------------------------------------------------------------------------

-- Corps de 0010, une différence : l'extraction du code de refus reconnaît
-- aussi `ameesh_spend [code]`, levé par la réservation. Le code rendu à
-- `actions.execute` est alors `spend_daily_cap`, `spend_monthly_cap`, … et
-- l'action n'est pas lancée.
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
            select s.valid_until into v_until from standing_approvals s
             where s.id = a.auth_grant_id;
        end if;

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

        perform ameesh_receipt_deadlines(format('lancement de %s#%s', a.action_id, v_attempt),
                                         v_exp, v_iat, p_skew, v_until);
    exception
        when raise_exception or unique_violation then
            -- un refus d'échéance (0011) ou de plafond de dépense (0596)
            -- porte son propre code : expired, iat_future, spend_*.
            v_code := coalesce(substring(sqlerrm from '^ameesh_[a-z]+ \[([a-z_]+)\]'), v_code);
            return query select v_code, null::integer, null::bigint, sqlerrm::text;
            return;
    end;
    return query select 'ok'::text, v_attempt, v_reservation, null::text;
end
$$;
