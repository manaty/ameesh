# Paquet integration/paquet-h-20261018-1200 — composition

Base : `main` **`8493fa3c466b`** (fusion de la PR #1807, paquet h-1100,
18/10 11:18Z ; CI 39121002324 verte sur la tête `f8526622`). Aucun paquet
ouvert ; `main` n'a pas bougé pendant la composition.

Demandes : **mesh-design** (pilote) 18/10 11:06Z et 11:10Z (onze unités :
#2394, #2260, #711, #2160, #2344, #2288, #2404, #2432, #2435, #1820, #2397) ;
audit SLO de l'auditeur 10:51Z et lot #2404 (liste calculée des lots prêts).
Vérification indépendante des têtes gelées à la forge, des verdicts sur ces
têtes et du merge-tree : trois lots supplémentaires prêts et sans conflit sont
ajoutés (#2399, #2408, #2370), quatre sont écartés avec motif (#1819, #2359,
#377, #1349). **Quatorze lots composés.**

## Admissions (14 lots, 14 unités)

- #2394, #2260, #711, #2160, #2344, #2288, #2404, #2432, #2435, #1820,
  #2397, #2399, #2408, #2370.

## Composition (14 unités)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #2394 | `8d70ae2b856f` (tête gelée) | `a284bab2d` | porte de fusion : `required_verdicts` aligné sur le canon (doute/files vide → 1 avis, critique 2) ; verdicts dev2 (Claude) + cx3 (Codex) ok sur `8d70ae2b`, porte 2/2 ; sensible (porte de fusion) |
| 2 | #2260 | `b135669f8cb8` (tête gelée) | `536ad92e4` | balayage des fusions : preuve de livraison (PR fusionnée ou tête ancêtre) exigée avant clôture ; avis impl-05 (DeepSeek) ok sur `b135669f`, correction codex3 incluse |
| 3 | #711 | `96c5b478057b` (remise) | `3d04b5a0e` | réservations de migration à portée (slot, cible, `targetSha`, statut) ; gel `50fe8357`, remise `refs/ameesh/resolutions/711/main-210ec24a` = `96c5b478` (merge mécanique), avis dev7 (Claude) ok sur `50fe8357`, avis auditeur (Codex) sur la tête antérieure `140a3687` (contenu inchangé) ; SQL/stockage sensible |
| 4 | #2160 | `5a78c2a18d92` (tête gelée) | `8a7b77781` | grâce « session lock held » : les ajournements d'attach comptent ; avis dev1 (Claude) ok sur `5a78c2a1` ; sensible concurrence |
| 5 | #2344 | `b7b66d9c7625` (tête gelée) | `b870e46ba` | garde des checks requis : repli sur la déclaration limité à un 403 prouvé ; avis cx1 (Codex) ok sur `b7b66d9c`, 0 B/0 N |
| 6 | #2288 | `0ddecb6d418d` (tête gelée) | `fa235f7b3` | garde reset-failed : témoin négatif (unité active) et diagnostic ActiveState ; avis cx2 (Codex) ok sur `0ddecb6d` |
| 7 | #2404 | `3fb8413d27a9` (tête gelée) | `79d53cec5` | composition des paquets horaires : liste calculée des prêts et écartés motivés ; avis cx2 (Codex) ok sur `3fb8413d` après correction B1-B3 |
| 8 | #2432 | `c74bf066805d` (tête gelée) | `55367a4ff` | test dédié : `land_head` sous clone radar occupé (`CloneBusy`) ; avis dev4 (Claude) ok sur `c74bf066` ; delta effectif test seul depuis #2428 fusionné |
| 9 | #2435 | `bf58671744ef` (tête gelée) | `34f4c11ce` | #2432 : le témoin prouve l'atomicité de `land_head` (mutant du verrou englobant tué) ; empilé sur #2432 (`c74bf066` + 44 l.), avis claude2 (Claude) ok sur `bf586717` ; delta effectif test seul |
| 10 | #1820 | `ff5d5f2a5e01` (tête gelée) | `e0f580e47` | #1770 : nommer le conflit D/F du plan au lieu de head-moved ; avis cx2 (Codex) ok sur `ff5d5f2a`, correction directe du relecteur ; la branche porte #1770 déjà fusionné, delta effectif 4 fichiers |
| 11 | #2397 | `aee1b55dff24` (tête gelée) | `e9be8b943` | découpage CI : `weights()` distingue durées de la carte et de la table ; avis cx3 (Codex) ok sur `aee1b55d` |
| 12 | #2399 | `9bff2e6852ad` (tête gelée) | `fccba8ef6` | sync-merges : honorer un gel d'origine verdict (SHA = tête) et lier sa correction ; avis dev7 (Claude) ok sur `9bff2e68` (11:21Z) ; ajouté après vérification merge-tree |
| 13 | #2408 | `7de00a8f774b` (tête gelée) | `363af0cda` | #2371 : test d'import sous-processus indépendant de `PYTHONWARNINGS` ; avis dev7 (Claude) ok sur `7de00a8f` ; delta effectif test seul depuis #2371 fusionné |
| 14 | #2370 | `7ae5343cd9fd` (tête gelée) | `9d30b765e` | feu : réconciliation de fusion avec gel abrégé ; avis codex3 (Codex) + dev7 (Claude) ok sur `7ae5343c` ; sensible (`fire.py`) |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot
et il porte un trailer `ameesh-work: <lot>` (14 trailers de composition).

## Conflits et résolutions

- Aucun conflit entre les quatorze unités : `git merge-tree --write-tree` rc 0
  pour chaque tête sur `8493fa3c` et pour la composition séquentielle dans
  l'ordre du tableau ; `git diff --check` rc 0.
- #711 n'est pas composé depuis sa tête gelée `50fe8357` mais depuis la remise
  `96c5b478` (merge mécanique du gel sur `main` `210ec24a`), qui résout les
  trois fichiers de stockage ; merge-tree rc 0 sur `8493fa3c`.
- #2435 est empilé sur #2432 : sa branche contient `c74bf066` + le delta de
  44 lignes du témoin ; composé immédiatement après #2432, sans résolution.
  Le contrôle d'inclusion lit donc #2432 en **partiel** à la tête (une ligne
  d'import remplacée par le témoin de #2435) : contenu attendu, pas une
  omission.
- #1820 porte le contenu de #1770 (déjà dans `main`) : sa branche est une
  remise sur la résolution `827ce3d9`, le delta effectif contre `8493fa3c`
  reste les 4 fichiers du lot.
- Trois lots ajoutés après la liste du pilote (#2399, #2408, #2370) : têtes
  égales à la forge, verdicts ok sur ces têtes, merge-tree rc 0 sur la base et
  sur la tête composée.

## Preuves

- `git diff --stat 8493fa3c..HEAD` : **47 fichiers hors ce manifeste,
  +3 915/−223** (48 fichiers avec le manifeste), dont `src/ameesh/prevalidation.py`,
  `src/ameesh/fire.py`, `src/ameesh/merge_queue.py`, `src/ameesh/migration_reservations.py`,
  `src/ameesh/packet_candidates.py`, `src/ameesh/plan_git.py`,
  `src/ameesh/storage/**`, la migration `0419_migration_reservation_slots.sql`,
  `deploy/mise-a-jour/poste.sh`, `deploy/sql/role-superviseur.sql`, les docs,
  20 fichiers de tests (`tests/test_*.py`) et `tests/parts/pilote-psql.txt`.
- `git log --first-parent 8493fa3c..HEAD` : 14 unités, 14 trailers
  `ameesh-work` ; têtes sources : `8d70ae2b856f`, `b135669f8cb8`,
  `96c5b478057b`, `5a78c2a18d92`, `b7b66d9c7625`, `0ddecb6d418d`,
  `3fb8413d27a9`, `c74bf066805d`, `bf58671744ef`, `ff5d5f2a5e01`,
  `aee1b55dff24`, `9bff2e6852ad`, `7de00a8f774b`, `7ae5343cd9fd`.
- `git diff --check 8493fa3c..HEAD` : rc 0, aucun marqueur de conflit.
- Tests ciblés locaux sur l'arbre composé (`PYTHONPATH=src python -m unittest`,
  via `ameesh heavy`) : **662 tests OK, 6 ignorés** en 654 s — les 20 modules
  de tests touchés par le diff plus `test_cluster` (classement de la migration
  `0419` et contrat des rôles superviseur).
- `scripts/paquet-inclusion.py --manifest reviews/integration-paquet-h-20261018-1200.md --head HEAD --expected 2394,2260,711,2160,2344,2288,2404,2432,2435,1820,2397,2399,2408,2370 --ready-file <18 prêts> --excused 1819,2359,377,1349` :
  rc 0, 14/14 attendus (13 présents, #2432 partiel expliqué par #2435),
  18 lots prêts (14 admis, 4 écartés consignés), aucun défaut.
- Une seule suite complète sur la tête gelée du paquet (§11 bis) ; aucune CI
  de lot entre deux coupures.

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #1819 | Prêt (gel `f7705b52`, avis impl-06 ok), mais `git merge-tree` rc 1 sur `src/ameesh/prevalidation.py` avec #1820 (les deux modifient les lectures au SHA et le plan) ; remise à préparer et delta à relire avant composition. |
| #2359 | Aucun gel déclaré (aucun jalon `frozen` ; avis cx3 sur `6ab07fac`) ; `deploy/**` critique (deux avis dus, un seul livré) ; `git merge-tree` rc 1 sur `deploy/mise-a-jour/poste.sh` avec #2288. |
| #377 | `git merge-tree` rc 1 sur `src/ameesh/main.py` contre `8493fa3c` (gel `7f279512`, avis auditeur + dev2) ; aucune branche enregistrée (head nul) et transport `ba7dcae45` = chaîne ancienne. À réadmettre par le pilote avec une remise fraîche. |
| #1349 | `git merge-tree` rc 1, conflits 3-way sur `docs/EXECUTEUR-MEDIEE.md`, `docs/api/openapi-v1.json`, `src/ameesh/main.py`, `src/ameesh/mediated_executor/contract.json`, `src/ameesh/runner.py`, etc. ; remise cx3 en cours après h-1100. |

Les autres lots en `qa` ne sont pas prêts (verdict, correction ou dépendance
bloquante) et ne sont pas admis : #2298, #2326, #2384, #2421, #2426, #2395,
#2389, #2317, #2318, #2349, #2398, #1693, #1022, #879, #897, #423, #1398
(bloqué par #1349).
