-- SPDX-License-Identifier: AGPL-3.0-only
-- 0514_alerts_delegations — #4468 : à qui la passe `alerts` a été confiée.
--
-- La règle de vivacité doit s'appliquer à TOUS les candidats routés, pas au
-- seul détenteur du bail : un délégué qui a tourné puis cesse de battre ne
-- doit pas garder la passe indéfiniment. Sans mémoire de la remise, le choix
-- retombe sur « le plus ancien » ou « le plus récent » battement, donc sur un
-- exécutant déjà mort, ou alterne au hasard.
--
-- Cette table retient, par rôle, l'hôte à qui la passe a été confiée en
-- dernier et l'instant (horloge de la base) de cette remise. Elle est écrite
-- par le garde (`role_run`) au moment où il est le délégué et tient le bail
-- d'exécution `alerts-pass:<rôle>` (`merge_locks`), jamais par un lecteur ;
-- l'écriture est fencée par ce bail, donc un garde qui l'a perdu ne peut pas
-- poser une remise périmée. `election.alerts_delegate` s'en sert pour donner
-- une grâce bornée au délégué qui n'a pas encore battu, puis pour l'écarter
-- du choix au profit d'un autre candidat routé.
--
-- Additive : une table neuve, aucune donnée existante lue ni modifiée. Une
-- ligne orpheline est effacée avec sa fiche (`on delete cascade`).
set local lock_timeout = '5s';

create table if not exists alerts_delegations (
    role         text primary key references agent_registry(name) on delete cascade,
    host         text not null,
    entrusted_at timestamptz not null default clock_timestamp()
);

comment on table alerts_delegations is
    '#4468: the host the alerts pass was last entrusted to, and when (database clock), so a delegate that stops beating yields the pass to another routed candidate.';
