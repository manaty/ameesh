# #1341 — gel v2 : plan de mesure unique par run (correctif R1)

Correctif demandé par le pilote (mesh-design, 14/10 20:51Z) après la R1 Codex
indépendante (ameesh-cx1) : **B confirmé** sur le gel v1 `9f5bf357d` — chaque
job `mesure` restaurait la carte du cache et recalculait son découpage ; deux
jobs pouvaient lire deux cartes différentes (cache écrit entre-temps) et
produire des parts disjointes (module omis, autre doublé) avec des
`durees.json` non vides et une matrice verte. Rien n'a été poussé sur une
branche : aucun push de `qa/1341-abd`, aucun run/rerun/cancel, aucune fusion,
aucune hausse de plafond.

## 1. Gel v2

| | |
|---|---|
| base | `0532faed4` (inchangée) |
| v1 (gel relu par R1) | `9f5bf357d10613296a4396c2bdadce8c38919797` |
| v2 (delta R1) | `a8213ee03293d08b708cfebc312188d3ac76651e` (parent v1) |
| arbre v2 | `523c57f3b2a5387a25e7d9c6290d0e85ed5560bf` |
| patch-id v2 (vs base) | `3c87ed0c77c29926bdbc336f55932f4785eaa363` |
| patch-id delta (v1 → v2) | `623fd6b6d8091b1f0d660be247599c9e5343c950` |
| patch v2 | `candidat-ABD-v2.patch`, sha256 `84eb8f060d1640701660378a336867e6e2d118e0426f36aad7a8298c14eadd1f` (850 lignes) |
| patch delta | `delta-R1-v2.patch`, sha256 `bcadbff73ed725cebcb23052a7a74c43f1381a2fc5372c19d0ff9a79b19dbd08` (704 lignes) |

Réfs publiées (aucune branche, aucun déclencheur CI) :

```
git fetch origin refs/atelier/1341/gel-candidat-v2-a8213ee0 \
                 refs/atelier/1341/union-v2-main-2458ec05 \
                 refs/atelier/1341/preuves-v2
```

| ref | objet |
|---|---|
| `refs/atelier/1341/gel-candidat-v2-a8213ee0` | `a8213ee0…` (le candidat v2) |
| `refs/atelier/1341/union-v2-main-2458ec05` | `7ca354a521ab6088e8aee5ddc552512d93ec61e6` (parents v2 + main `2458ec053123`) |
| `refs/atelier/1341/preuves-v2` | ce bundle (manifeste, patchs, témoins) |

Union avec la main courante `2458ec053123d3f33644fef1e092fc84cc2773d9` :
arbre `b45d4c598fffe72adbca9e8cfb64ceb626a224de` (`merge-tree --write-tree`
rc=0, 0 conflit) ; `union vs main` = exactement les 4 fichiers du lot,
patch-id `3c87ed0c` constant ; `union vs v2` = exactement les 3 fichiers du
delta de main (`docs/EXPLOITATION.md`, `scripts/test-plan.py`,
`tests/test_l161f_plan.py`) ; blobs identiques candidat/union ; `candidat-ABD-v2.patch`
rejoué sur `2458ec05` (index temporaire) redonne l'arbre `b45d4c59`.

## 2. Le correctif

- **`plan-mesure`** (nouveau job, `needs` de `mesure`) : restaure la carte
  **une seule fois par run**, exécute
  `scripts/test-parts.py plan --parts "$PARTS_MESURE" --list-psql … --map …`,
  publie l'artefact `plan-mesure` (`plan.json` + `carte-source.json.gz`).
  `PARTS_MESURE` remonte au niveau `env:` du workflow : le plan et la fusion
  lisent la même liste (une seule source, toujours tenue en miroir de la
  matrice par `tests/test_carte_parts_completes.py`).
- **`mesure`** : `needs: plan-mesure`, télécharge l'artefact et exécute
  `test-parts.py run --plan` ; plus de `actions/cache/restore`, plus de
  `--map`, plus de `LISTE` dans les parts. Le découpage ne dépend plus de la
  carte présente sur chaque runner au démarrage du job.
- **`carte`** : télécharge `plan-mesure`, réutilise `carte-source.json.gz`
  comme `--previous` (même carte que celle du plan) et fusionne avec
  `--plan` en plus de `--attendu` (D).
- **`scripts/map.py`** : `couverture_incomplete()` + `merge --plan` — refuse
  de publier (rc 2, aucune carte) si un module du plan est omis, mesuré deux
  fois, mesuré hors de sa part, ou si une part mesurée n'est pas dans le plan.
- **`scripts/test-parts.py`** : sous-commande `plan` (lecture unique de la
  carte, découpage figé) ; `parts_du_plan()` refuse un nom de part inconnu ou
  une numérotation non contiguë.
- **`tests/test_carte_parts_completes.py`** : 12 → 27 tests, dont le témoin
  K1/K2, la couverture effective et la structure « un seul plan par run ».

## 3. Témoins (rejouables)

`analyse/temoins-v2.py` (sha256 `4107f9ffef16a1aad7ebf42c2e18b302a8cc6a9cec4fcbb248eebb954fcc333b`),
journal `analyse/temoins-v2.log` (sha256 `b6019db2ac41f1c72d16ed7ac385a70e6329a7aac6a645d2ec04de13303b41d4`),
résultats `analyse/resultats-v2.json` (sha256 `109f5471730fe07b03f43629c2e1fb59b44e0e4f19745ddf9160a455e2d1d377`) :

1. **témoin source K1/K2** : `part1(K1) = [a,d]`, `part2(K2) = [a,c]` → `b`
   omis, `a` doublé ; `parts_incompletes` (garde D de la v1) = `[]` (muette) ;
   `couverture_incomplete` refuse (doublon `a`, `b` non mesuré, `a` hors
   part) ; le plan unique calculé sur K1 couvre exactement `{a,b,c,d}`.
2. **plan du job == `split`** (l'ancienne route de chaque part) sur la carte de
   l'incident `96a795fb2` : les 6 parts identiques (psycopg pondéré par la
   carte, psql par la table statique).
3. **bloc réel du job `plan-mesure`** (extrait du YAML, `bash -e`) : avec la
   carte → `plan.json` + `carte-source.json.gz` (poids psycopg = carte) ;
   sans carte → plan statique, pas de carte source.
4. **bloc réel de la fusion** : complet `0`/publiée (6 modules), part
   manquante `2`, part annulée sans `durees.json` `2`, durées vides `2`,
   matrice non verte `1`, avant correctif `0`/publiée tronquée (défaut
   d'origine reproduit), **dérive de cache K1/K2 `2`/refusée** (couverture),
   plan unique K1 `0`/publiée (4 modules, une fois).

## 4. Tests ciblés (auteur, aucune suite complète, aucune CI)

`analyse/tests-cibles-v2.log` sha256
`a276b0be4cd1e5ed72eca61af6a87a72a0e87030045d03ec2e81cb0eec646141` :
`tests.test_carte_parts_completes` 27 OK ; `tests.test_l161e_map`,
`tests.test_l116_parts`, `tests.test_l161f_plan` 115 OK, 1 skip.

Les preuves A/B/C de la v1 restent **bit à bit identiques** sur le code v2 :
`analyse/simulate-1341b.json` sha256 `108a6511…` et `analyse/preuve-AB.json`
sha256 `30bf2aea…` inchangés (le correctif ne touche ni la pondération ni le
découpage, seulement l'endroit où ils sont calculés).

## 5. Limites et points pour la revue

- Le plan fige le découpage du run ; sans carte en cache il retombe sur la
  table statique (même repli qu'avant), et `A` garde sa limite connue : il
  faut un premier run complet pour installer le régime pondéré.
- La couverture effective exige que chaque module du plan apparaisse dans le
  `durees.json` de sa part : un module de test sans aucun test ferait échouer
  la publication (aucun aujourd'hui, 251/251 en ont) — choix fail-closed
  assumé, plutôt qu'une carte silencieusement incomplète.
- `D` continue de refuser part manquante / annulée / vide ; `--plan` ajoute la
  couverture ; le tout reste sans CI et sans push de branche.
- `analyse/temoins-D.py` et `analyse/resultats.json` sont les témoins du
  **gel v1** (leur bloc de fusion est celui de la v1) : le rejeu sur le gel v2
  est `analyse/temoins-v2.py`.
