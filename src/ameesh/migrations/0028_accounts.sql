-- 0028_accounts — comptes multiples par fournisseur (lot L30, décision 0027).
--
-- Les PROFILS de compte (dossier de configuration du harnais, ou clé d'API)
-- sont des secrets d'hôte : ils vivent dans la configuration de l'hôte, jamais
-- en base ni dans le canon. La base ne porte que des NOMS de comptes et l'état
-- d'exécution qui doit survivre au redémarrage de l'exécuteur et être partagé
-- par ses workers :
--
--   * `account_active`   : le compte actif par (hôte, harnais), et s'il est
--                          forcé à la main (`ameesh accounts use`) ;
--   * `account_holds`    : un compte quitté au seuil n'est repris qu'après la
--                          remise à zéro de sa fenêtre (`until`, epoch) ;
--   * `account_switches` : le journal des bascules (automatiques, retours au
--                          primaire, forçages manuels).
--
-- Les relevés existants de L26 (migration 0027) sont étendus d'une colonne de
-- compte, plutôt que dupliqués dans des tables parallèles :
--
--   * `quota_gauge_readings.account` : la jauge d'un forfait est celle d'un
--     COMPTE (lue dans les journaux de ce compte) ;
--   * `provider_balances.account`    : le solde d'un compte payé au token
--     (une clé d'API = un compte) ;
--   * `turn_costs.account`           : la dépense d'un tour, attribuée au
--     compte qui l'a portée (plafond horaire par compte).
--
-- Colonnes facultatives : NULL = hôte sans comptes déclarés (comportement
-- d'avant L30), toujours valide.

create table if not exists account_active (
    host        text not null,
    harness     text not null,
    account     text not null,
    forced      boolean not null default false,
    updated_at  timestamptz not null default now(),
    primary key (host, harness)
);

create table if not exists account_holds (
    host        text not null,
    harness     text not null,
    account     text not null,
    -- epoch de la remise à zéro attendue ; null = inconnue (le compte est repris
    -- dès qu'il repasse sous le seuil)
    until_ts    double precision,
    reason      text,
    created_at  timestamptz not null default now(),
    primary key (host, harness, account)
);

create table if not exists account_switches (
    id            bigserial primary key,
    host          text not null,
    harness       text not null,
    from_account  text,
    to_account    text not null,
    -- bascule (seuil atteint), retour (fenêtre remise à zéro), manuel
    -- (`accounts use`), auto (`accounts auto`), config (compte retiré)
    kind          text not null check (kind in ('bascule', 'retour', 'manuel', 'auto', 'config')),
    reason        text,
    agent         text,
    at            timestamptz not null default now()
);

create index if not exists account_switches_host_idx
    on account_switches (host, harness, at desc, id desc);

alter table quota_gauge_readings add column if not exists account text;
create index if not exists quota_gauge_readings_account_idx
    on quota_gauge_readings (harness, account, gauge_key, observed_at desc, id desc)
    where account is not null;

alter table provider_balances add column if not exists account text;
create index if not exists provider_balances_account_idx
    on provider_balances (provider, account, currency, observed_at desc, id desc)
    where account is not null;

alter table turn_costs add column if not exists account text;

create index if not exists turn_costs_account_time_idx
    on turn_costs (harness, account, recorded_at desc)
    where account is not null;
