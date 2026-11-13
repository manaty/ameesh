-- SPDX-License-Identifier: AGPL-3.0-only
-- 0569_account_pauses — #6961 : pause PARTAGÉE par compte (harnais, compte),
-- lue par tous les agents du mesh avant de placer un tour.
--
-- La retenue de L226b (`account_holds`) est par (hôte, harnais, compte) : un
-- agent d'un autre hôte, ou une lecture locale plus ancienne, ne la voit pas
-- et peut démarrer un tour sur un compte déjà au plafond dur. Le 11/11,
-- impl-11 a démarré à 0.79, franchi 0.80 en vol (relevé agents-3 à 14:16)
-- et fini à 81 % — aucun démarrage n'a été refusé après le franchissement.
--
-- Cette table porte l'observation qui vaut pour le compte logique (le même
-- sur tous les hôtes, comme `quota_gauge_readings` et `provider_credits`) :
--   until_ts    : échéance de la pause (epoch), la remise à zéro de la
--                 fenêtre qui a franchi son plafond ;
--   observed_at : instant du relevé qui l'a fondée (epoch) ; un relevé plus
--                 récent et sous le plafond la lève, un relevé plus ancien
--                 ne la contredit pas ;
--   reason      : la raison courte du refus (compte, fenêtre, mesure) ;
--   source      : agent@hôte qui l'a publiée (trace, jamais un secret).
--
-- Additif : une table neuve, aucun droit ni trigger changé ; le code d'avant
-- ne la nomme jamais. Les pannes de cette table ne cassent pas la garde de
-- l'appelant, qui garde sa décision locale.
set local lock_timeout = '5s';

create table if not exists account_pauses (
    harness     text not null check (btrim(harness) <> ''),
    account     text not null check (btrim(account) <> ''),
    until_ts    double precision not null,
    observed_at double precision not null,
    reason      text not null default '',
    source      text not null default '',
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now(),
    primary key (harness, account)
);

comment on table account_pauses is
    '#6961: account-level pause shared by every host and agent of the mesh; written when an agent observes a dated hard cap (overage or fixed cap), read before placing a turn, lifted by a newer reading under the cap.';
comment on column account_pauses.until_ts is
    '#6961: epoch when the blocking window resets; the pause is not valid after it.';
comment on column account_pauses.observed_at is
    '#6961: epoch of the reading that founded the pause; a NEWER reading under the cap lifts it, an older one never contradicts it.';
