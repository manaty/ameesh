-- SPDX-License-Identifier: AGPL-3.0-only
-- #793 : le repère des envois Process est indépendant du dernier état
-- enregistré. Un sync ancien peut finir après un sync récent : record_state
-- ne doit jamais réarmer un envoi déjà engagé. Numéro réservé au lot #793.
set local lock_timeout = '5s';
alter table canon_state add column if not exists process_commit text;
-- À l'installation, ne pas renvoyer les règles déjà vues. La valeur NULL
-- d'un nouvel hôte est initialisée par le premier envoi, via son état connu.
update canon_state set process_commit = last_good_commit
    where process_commit is null;
