-- SPDX-License-Identifier: AGPL-3.0-only
-- 0451_account_identities — l'identité d'un compte est le dossier résolu
-- qu'il lit, pas son seul nom (#1883, arbitrage pilote du 16/10).
--
-- Le même nom de compte déclaré dans deux configurations de l'hôte (profils
-- de runner différents) peut résoudre vers deux dossiers différents : chaque
-- processus lit alors un compte différent sous le même nom, et la lecture
-- saine de l'un sert de preuve pour l'autre. C'est l'incident du 16/10/2026 :
-- « secondaire » en dépassement facturé au-delà du forfait, tantôt lu à 47 %
-- (sa vraie jauge), tantôt à 34 % (celle du primaire), et une bascule en
-- boucle.
--
-- Chaque processus publie ici l'empreinte (sha256 du chemin résolu, **jamais**
-- le chemin : un dossier de configuration est un secret d'hôte) des comptes
-- `config_dir` qu'il déclare, et relit celles des autres. Deux empreintes
-- vivantes sous le même (hôte, harnais, nom) sont un conflit d'identité : le
-- compte est refusé au placement et signalé à l'owner, au lieu de servir une
-- lecture du mauvais compte comme preuve saine.
--
-- La vivacité est celle de `accounts.IDENTITY_WINDOW_S` : une empreinte non
-- republiée depuis la fenêtre ne compte plus, ce qui laisse le conflit se
-- résoudre après le nettoyage des configurations.
--
-- Additive : nouvelle table, aucun objet existant modifié.

create table if not exists account_identities (
    host       text        not null,
    harness    text        not null,
    account    text        not null,
    identity   text        not null,
    first_seen timestamptz not null default now(),
    last_seen  timestamptz not null default now(),
    primary key (host, harness, account, identity)
);

create index if not exists account_identities_seen_idx
    on account_identities (host, harness, account, last_seen);
