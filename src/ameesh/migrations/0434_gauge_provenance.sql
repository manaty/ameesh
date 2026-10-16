-- 0434_gauge_provenance — provenance des relevés de jauge (lot #1132).
--
-- Un relevé de jauge Claude est produit par le harnais pendant un tour, mais
-- le `rate_limit_event` ne porte aucun instant : jusqu'ici `observed_at`
-- (l'heure d'insertion, `now()`) était la seule date. Un relevé PÉRIMÉ
-- re-sélectionné vingt-six heures plus tard par un exécuteur qui ne voyait
-- que des flux figés s'inscrivait donc comme un relevé frais et remplaçait la
-- lecture fraîche d'un autre état ou d'un autre hôte (incident du 13/10 :
-- seven_day 67 % a écrasé 87 % et l'admission est passée sous le plafond
-- overage de 80 %).
--
-- Deux colonnes nullables, purement additives :
--   * `produced_at` : l'instant de PRODUCTION du relevé tel que connu par le
--     lecteur — le ts du marqueur de compte du tour qui l'a publié. NULL
--     quand le flux est sans marqueur (legacy) ou pour Codex : l'heure
--     d'insertion ne doit jamais servir de date de production ;
--   * `source` : le journal d'origine (nom de l'agent), pour retrouver le
--     flux d'un relevé douteux.
--
-- Les lecteurs ordonnent désormais par `produced_at` d'abord (NULL en
-- dernier), `observed_at` ensuite : un relevé périmé réinséré ne peut plus
-- devenir « le dernier relevé ». L'historique existant n'est ni purgé ni
-- réécrit (produced_at NULL).

alter table quota_gauge_readings add column if not exists produced_at timestamptz;
alter table quota_gauge_readings add column if not exists source text;

create index if not exists quota_gauge_readings_produced_idx
    on quota_gauge_readings (harness, account, gauge_key,
                             produced_at desc nulls last, observed_at desc, id desc);
