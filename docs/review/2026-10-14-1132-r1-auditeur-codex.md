# Revue de la provenance des relevés de jauge

**NO-GO : 4 P2 bloquants dans la classe sensible ameesh**, sur
`84bec820b0fa136566939fe5efb3dbee2ff28889`, PR873, delta depuis
`1e7e3a552826feb321968f0f82a5375b4bb3babc` (#1078).
Auditeur, Codex réellement exécuté, le 14 octobre 2026.
La migration est additive, mais deux chemins écartent encore une mesure
réellement plus fraîche, et deux omissions empêchent la validation du contrat.

## 1 Le rejeu écrase la mesure fraîche dans le même journal

`cost._claude_account_readings` affecte chaque événement à
`readings[current]` sans comparer son rang à celui déjà retenu pour ce compte.
Marquer le rejeu `produced_at=None` ne suffit donc pas : la mesure datée du
même journal disparaît **avant** la comparaison entre les journaux.

Sonde avec un seul agent et le lecteur réel, sans base : mesure datée à 87 %,
puis marqueur plus récent et rejeu à 67 % dont five_hour était déjà close.
Avant le rejeu, `accounts.evaluate(cap=.8)` refuse le compte ; après,
seven_day vaut 67 %, produced_at est None et l'admission passe. Le rejeu ne
doit pas remplacer la mesure datée déjà retenue dans ce flux, y compris quand
l'historique partagé est absent ou indisponible. Préserver aussi une baisse
authentique datée plus récente.

## 2 La déduplication écarte une production réellement plus récente

`operations.record_gauges` conserve son ancien prédicat : dernière **insertion**
du même compte/clé, mêmes used/reset et moins de dix minutes. Il ignore la
provenance désormais déterminante pour `gauge_latest`.

Exemple : mesure fraîche 67 % produite à T2, puis vieille mesure 87 % produite
à T1 mais insérée après. Une vraie mesure 87 % produite à T3 ressemble à la
dernière insertion et est supprimée par la déduplication. `gauge_latest`
retient alors encore 67 % à T2, au lieu des 87 % réellement atteints à T3.
Cela peut reproduire l'admission sous le plafond que le lot doit corriger.

La sonde capture le SQL et les paramètres exacts de `record_gauges`, puis
évalue son SELECT sur un CTE VALUES à horloge fixe : zéro candidat INSERT.
Elle exécute également le SELECT exact de `gauge_latest` sur ces valeurs :
67 %. Elle ne lit ni n'écrit les tables du mesh. La décision de dédupliquer
doit tenir compte de la fraîcheur de production ; des valeurs identiques à
une insertion périmée ne rendent pas une nouvelle production redondante.

## 3 La nouvelle opération de stockage n'est pas classée

`operations.gauge_latest` est ajoutée à l'interface abstraite mais absente du
contrat `mediated_executor/contract.json`, dont interface_total reste 385.
Le contrat exige que toute méthode soit admise ou explicitement refusée,
comme le vérifie `test_l107_executor_contract`.

La réflexion sur cette tête trouve 395 méthodes contre 385 déclarées.
Neuf autres omissions, dans conversations/work_dependencies, sont déjà
présentes dans la base de ce delta ; elles ne sont pas imputées à #1132.
Ce constat vise la nouvelle méthode : la classer avec le motif approprié et
mettre à jour le contrat et son compte sur l'arbre final. Aucune ouverture
d'autorité ou de droits distante n'est demandée par cette revue.

## 4 Le nouveau test de stockage est absent du pilote psql

`test_1132_jauges_perimees` contient des PgTestCase et exerce la migration,
l'INSERT de déduplication et les deux requêtes de sélection. Il est absent de
`tests/parts/pilote-psql.txt`. CONTRIBUTING impose la présence des nouveaux
tests de la couche base/stockage dans cette sélection ; ajouter son entrée
commentée et triée.

## Vérification et suite

Lecture des neuf fichiers, des appels d'admission, de la sélection et de la
déduplication. La sonde jointe reproduit les deux défauts de fraîcheur et
inspecte les deux omissions. Elle utilise des journaux synthétiques, un profil
factice sans identifiants, une horloge fixe et seulement des SQL sur VALUES.
Sa réussite signifie que les quatre défauts de cette tête sont reproduits.

```sh
PYTHONPATH=src python docs/review/proof-1132-auditeur.py
```

Les 385 tests ciblés annoncés par l'auteur ne sont pas rejoués ; aucune
migration, CI ou mutation de budget n'est exécutée. Le GO de #1078 sur
1e7e3a55 a déjà été rendu par mesh-design ; le delta de son témoin conserve
les assertions et les blobs de production. Il n'est pas dupliqué ici.

Après correction : nouveau gel et avis sur le delta ; second avis de famille
réellement Claude, CI applicable et feu pilote restent distincts. Le rebase
annoncé après fusion de #1078 devra également être qualifié sur sa tête réelle.
