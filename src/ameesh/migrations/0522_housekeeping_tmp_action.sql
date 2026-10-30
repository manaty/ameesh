-- SPDX-License-Identifier: AGPL-3.0-only
-- 0522_housekeeping_tmp_action — #4888 : la passe d'alertes ne trie plus tout
-- le journal du ménage pour lire la dernière ligne `tmp` de chaque chemin.
--
-- Constat (30/10, agents-1) : `ameesh alerts` a dépassé 60 s puis 100 s pour
-- un bail de 60 s. Mesuré sur la base commune : deux lectures
-- `latest_by_path(None, "tmp")` de `_housekeeping_alerts` pesaient 12,5 s et
-- 9,2 s dans la passe. Le plan seq-scannait `housekeeping_log` (584 k lignes,
-- 270 Mo, dont 508 k `tmp`) puis merge-joint sur la table entière : la fenêtre
-- 24 h ne réduisait rien, et la variante `unowned` (7 lignes) scannait aussi
-- tout.
--
-- L'index porte les chemins candidats par action (`unowned`, `over_quota`,
-- `unremovable`, `truncated`, `mounts_unreadable`) et par hôte ; la dernière
-- ligne de chaque chemin candidat se lit ensuite par l'index existant
-- (kind, host, path, at desc, id desc). Additif : aucun journal modifié.
create index if not exists housekeeping_log_kind_action_path_time_idx
    on housekeeping_log (kind, action, host, path, at desc);

comment on index housekeeping_log_kind_action_path_time_idx is
    '#4888: read the candidate paths of the housekeeping alerts by action, then the latest line per path; stops the alerts pass from seq-scanning the whole journal.';
