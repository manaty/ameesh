-- SPDX-License-Identifier: AGPL-3.0-only
-- 0490_account_force_expiry — #3755 : un forçage manuel a une échéance, et
-- l'overage d'un compte est lisible depuis n'importe quel hôte.
--
-- `ameesh accounts use` retire le choix automatique. Sans échéance, le
-- forçage reste en place jusqu'à ce que quelqu'un pense à le lever : sur
-- pc-smichea le 26/10/2026, le Codex secondaire a été forcé à ~11:45Z et
-- laissé à 100 %, « in overage (billed beyond the plan) », jusqu'au retour
-- manuel à l'automatique à 13:41Z. `account_active.forced_until` porte
-- l'instant où le forçage rend la main à l'automate. NULL = ligne antérieure
-- à la migration : le forçage en cours n'est pas levé rétroactivement, il
-- reste sans échéance jusqu'à la prochaine pose.
--
-- `account_overage` n'était calculée que par l'hôte qui voit le compte, dans
-- SA configuration et SES journaux. La passe d'alertes tourne chez le
-- détenteur du bail (agents-1 ou agents-2) et n'a donc pas vu le dépassement
-- de pc-smichea. Les deux drapeaux d'overage du relevé sont désormais
-- publiés avec les jauges dans l'historique partagé : la passe de n'importe
-- quel hôte peut lever l'alerte pour tous les comptes qui publient.
--
-- Additif seulement : deux colonnes nullables, aucune donnée réécrite.
alter table account_active add column if not exists forced_until timestamptz;

comment on column account_active.forced_until is
    '#3755: instant where a manual force returns to the automatic choice (NULL: no expiry, legacy force)';

alter table quota_gauge_readings add column if not exists overage_capable boolean;
alter table quota_gauge_readings add column if not exists overage_in_use boolean;

comment on column quota_gauge_readings.overage_capable is
    '#3755: the harness allows paid usage beyond the plan (Claude overageStatus, Codex credits); NULL when unknown';
comment on column quota_gauge_readings.overage_in_use is
    '#3755: the reading says the account is billed beyond its plan now; NULL when unknown';
