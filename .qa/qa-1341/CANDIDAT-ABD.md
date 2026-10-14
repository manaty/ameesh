# #1341 — candidat A/B/D (préparation locale, aucun push, aucune CI)

Demande du pilote du 14/10 19:27Z (mail 40135) : préparer A+B+D localement sur la
base qualifiée, avec preuves de répartition et témoins (part manquante / part
annulée / artefact uploadé sans `durees.json` / carte complète), puis candidat
borné pour revue ; qualifier C dans le même diagnostic. **Rien n'a été poussé** :
aucun push, run, rerun, cancel, fusion, ni tête déplacée. Le correctif n'est pas
retardé par la paperasse #388 (réponse envoyée séparément).

## 1. Candidat

| | |
|---|---|
| base | `0532faed4` (main, merge #829) |
| branche locale | `qa/1341-abd` (worktree `.qa/qa-1341/wt-abd`) |
| commit | `9f5bf357d10613296a4396c2bdadce8c38919797` |
| arbre | `309aa1eea0a53b6a17d000c63c3edf89627adb20` |
| patch-id | `07ae9cced4ecb3e438e42d4f02e19d1efc54c035` |
| pièce transférable | `candidat-ABD.patch`, sha256 `ba39d0945f7065200a76a94d1ddfd02e5dbb4589575715c0d974868c1f9568e7` (395 lignes) |

Blobs :

| fichier | blob |
|---|---|
| `.github/workflows/carte.yml` | `13b610754c9ec3c7aead94ef71bef4dd6cd5a487` |
| `scripts/map.py` | `4c4a80be2950ac08139abe9484f3a36c6ba5bdfb` |
| `tests/test_carte_parts_completes.py` | `bd4365c2534bfc60017e3e81fa87e37bd0e7c5e8` |

## 2. Les trois correctifs

- **A — découpage sous mesure conscient du surcoût** (`carte.yml`) : le job
  `mesure` restaure la carte précédente du cache (`actions/cache/restore`, clé
  `carte-`) et passe `--map` à `test-parts.py run` quand elle est présente ; la
  table statique reste le repli. Le surcoût de couverture varie de 1,2× à 9,3×
  selon les modules et la table statique ne couvre que 76 modules sur 250.
- **B — mesure psql en 2 parts** (`carte.yml`) : la part unique de 45 modules
  pesait 38 min de mur pour un plafond de 40 et a été annulée deux fois
  (dernier run 38601979838, puis 38610188898) ; deux parts de ~19 min.
- **D — publication conditionnée à une mesure complète** (`map.py` +
  `carte.yml`) : `merge --attendu` refuse de publier si une part attendue
  manque, n'a pas écrit ses durées, ou si la matrice `mesure` n'est pas verte ;
  `PARTS_MESURE` est tenu en miroir de la matrice par
  `tests/test_carte_parts_completes.py`, et l'artefact/cache ne s'écrivent
  qu'après une fusion réussie. Sans cela, la carte `96a795fb2` a été publiée
  avec 248 durées psycopg et 0 psql, et les runs suivants sont retombés sur la
  table statique.

## 3. Simulations A/B/C (reproductibles)

Algorithme exact `weight`/`split` de `scripts/test-parts.py`, sur les durées
mesurées de l'incident (`0532faed4`), 250 modules. Chiffres = max sur les parts
(enveloppe « span » / Σ des `duree_s`), en minutes.

| scénario | max enveloppe | max Σ tests | repli | lecture |
|---|---|---|---|---|
| psycopg 4 parts : A0 statique (comportement du run) | 39,8 | 37,1 | 174 | reproduit les 4 parts réelles |
| psycopg 4 parts : A1 avec la carte en cache (`96a795fb2`, 248 psy, 0 psql) | 34,0 | 32,6 | 2 | **A, premier run après l'incident** |
| psycopg 4 parts : A1 avec la seule carte du run courant (psy2-4, 189 modules) | 39,0 | 36,4 | 61 | A ne suffit pas si la carte est partielle |
| psycopg 4 parts : A1 carte complète sous mesure (régime établi) | 28,0 | 27,2 | 0 | cible de A une fois B+D en place |
| psql 6 parts : S0 statique (comportement du run) | 44,2 | 33,5 | 174 | reproduit le plan réel (`psql 2/6` annulée) |
| psql 6 parts : C durées réelles des flux (245/250 modules) | 30,5 | 22,4 | 2 | **C qualifié**, non implémenté |
| psql 6 parts : C enveloppe des flux | 28,4 | 23,5 | 2 | variante |
| mesure psql pilote (45 modules) : 1 part actuelle | 35,8 | 30,8 | — | annulée deux fois |
| mesure psql pilote : B0 en 2 parts, poids statiques | 19,2 | 16,4 | 39 | |
| mesure psql pilote : B1 en 2 parts, poids carte | 18,1 | 15,4 | 0 | **B** |

Invariants vérifiés (`analyse/simulate-1341b.json`, `analyse/preuve-AB.json`) :

- A0 reproduit **exactement** les 4 parts psycopg réellement mesurées
  (`A0_reproduit_les_parts: true`) ; S0 reproduit **exactement** le plan psql du
  run (`S0_reproduit_le_plan: true`) ;
- le découpage rendu par la CLI (`test-parts.py split --map`, le chemin que
  prend `carte.yml`) est **égal** à la simulation pour A0, A1, B0, B1 ; A0 ≠ A1
  et B0 ≠ B1, donc le correctif change bien la répartition ;
- la carte lue porte 248 durées psycopg et 0 psql, comme constaté.

**Précision vs `RAPPORT-1341.md` §5** : le « 37,1 → 26,7 » du rapport est le
scénario A1 carte complète (27,2 Σ ici) ; avec la carte réellement en cache au
moment de l'incident, A donne 32,6 Σ / 34,0 enveloppe (marge ~5 min sous le
plafond 40), et 36,4 si la carte ne porte que les durées du run courant. B
(30,8 → 19,2) et C (33,5 → 22,4) sont confirmés. A seul ne suffit donc pas :
c'est la combinaison B (la mesure psql aboutit) + D (la carte n'est plus
publiée tronquée) qui installe le régime où A atteint ~27 min.

## 4. Témoins D (bloc `run:` réel, `bash -e`)

`analyse/resultats.json` + `analyse/temoins-D.log` :

| scénario | code | carte publiée | attendu |
|---|---|---|---|
| complet (6 parts avec durées) | 0 | oui, 6 modules | carte publiée |
| part manquante (`mesure-psql-2` absente) | 2 | non | refus |
| part annulée (artefact psy1 sans `durees.json`, cas réel) | 2 | non | refus |
| durées vides | 2 | non | refus |
| matrice de mesure non verte (`cancelled`) | 1 | non | refus avant fusion |
| avant correctif (même pièce, sans `--attendu`) | 0 | **oui, 5 modules** | défaut d'origine reproduit |

## 5. Tests ciblés exécutés (auteur)

```
cd .qa/qa-1341/wt-abd
PYTHONPATH=src python3 -m unittest tests.test_carte_parts_completes -v      # 12 OK
PYTHONPATH=src python3 -m unittest tests.test_l161e_map tests.test_l116_parts tests.test_l161f_plan
                                                                            # 115 OK, 1 skip
```

Journal : `analyse/tests-cibles.log`, sha256
`0f994e5b24e724cfd2eaab0ecc7ff92ec58d00a2904a0f26504986ab9fb80acf`. Aucune
suite complète, aucune CI (canon §3 : relecture avant CI).

## 6. Limites et points pour la revue

- A ne s'applique que si le cache porte une carte avec des durées **sous
  mesure** ; la carte en cache aujourd'hui n'a **aucune durée psql** (d'où B) et
  il faudra un premier run complet pour installer le régime ~27 min.
- D rend le job `carte` **rouge** quand une part de mesure manque : c'est le
  but (pas de faux PASS, pas de carte tronquée qui écrase la précédente), mais
  un incident de mesure ne publiera plus aucune carte — la sélection retombe
  alors sur la carte précédente, intacte, au lieu d'une carte amputée.
- C est qualifié (source : Σ des `duree_s` des flux des runs tests, 245/250
  modules ; voie prévue `test-parts.py durations`), **pas implémenté** ici.
- Aucune hausse de plafond, aucune relance partielle, aucun geste CI.

## 7. Reproductibilité

```
cd .qa/qa-1341/analyse
python3 simulate-1341b.py > simulate-1341b.log   # A/B/C, écrit simulate-1341b.json
python3 preuve-AB.py > preuve-AB.log             # CLI == simulation, écrit preuve-AB.json
python3 temoins-D.py > temoins-D.log             # témoins D, écrit resultats.json
```

Pièces (sha256) : `simulate-1341b.json` `108a6511…`, `simulate-1341b.py`
`1e7c1a25…`, `preuve-AB.json` `30bf2aea…`, `preuve-AB.py` `6dee3d4b…`,
`resultats.json` `85811185…`, `temoins-D.py` `45be283d…`, `c-psql-flux.json`
`13396df4…`, `flux-durations.py` `69a37a68…`, `candidat-ABD.patch`
`ba39d094…`.

## 8. Revue demandée

A et B sont des découpages CI sensibles ; D touche la porte de publication
(artefact + cache) : deux avis indépendants Codex+Claude hors auteur DeepSeek
(mesh-design 19:27). Les relecteurs peuvent appliquer `candidat-ABD.patch` sur
`0532faed4` et rejouer les trois scripts et les tests ciblés ; aucune suite
complète, aucun rerun de CI. Le push éventuel et la CI de la tête gelée
viendront après coordination pilote, pas avant.
