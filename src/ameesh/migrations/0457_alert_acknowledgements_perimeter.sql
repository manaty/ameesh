-- SPDX-License-Identifier: AGPL-3.0-only
-- L87c (#1473) : un accusé est pris dans UN périmètre d'observation (les seuils
-- de `ameesh alerts ack`, de l'API ou de `ameesh notify`). La passe qui
-- déclare sa résolution doit observer ce même périmètre : sinon un relevé plus
-- étroit invalide l'accusé « resolved » sans résolution observée. La colonne
-- porte les seuils effectifs, en JSON canonique (clés triées) ; NULL = accusé
-- antérieur à cette migration, lu comme celui de la passe qui l'observe.
alter table alert_acknowledgements add column if not exists perimeter text;

alter table alert_acknowledgements
    drop constraint if exists alert_acknowledgements_perimeter_check;
alter table alert_acknowledgements
    add constraint alert_acknowledgements_perimeter_check
    check (perimeter is null or length(perimeter) between 2 and 4096);
