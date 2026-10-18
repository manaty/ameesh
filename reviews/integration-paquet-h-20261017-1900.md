# Paquet integration/paquet-h-20261017-1900 — composition

Base : `main` **`6199ad736`** (fusion de #1626/PR #1673 et #2271). La branche est composée
directement sur cette base ; le diff de la PR ne porte que les 6 unités ci-dessous et ce manifeste.
Tête de composition (6 unités) : **`8f0938c31`** ; la tête de branche au gel, qui porte ce
manifeste, est le SHA exact communiqué au pilote et couvert par l'unique suite complète du paquet.

Demandes : **pilote mesh-design**, 17/10 18:57Z (mail #101055) : abandonner h-1800 (PR #1675,
`CONFLICTING` sur main ; feu retiré, auto-fusion désarmée) et construire
`integration/paquet-h-20261017-1900` sur `6199ad736` avec, dans l'ordre, #2233 (`528e7329`),
#2230 (`df995cb6`), #2281 (`5042113a`), #1896 (`74493db1`), #1400 (`54203a78`), puis
#2235+#2268 (`52af71e93c67`) ; #1768 est **exclu** (conflit avec main dans `fire.py` et
`radar.py` ; rebase demandé à dev7, paquet suivant) ; #2122 reste hors paquet tant que la source
§11 bis n'est pas jugée. Simulation merge-tree du pilote : les cinq premiers passent sans conflit ;
#2235 conflicte avec #2230 sur `tests/test_1969_bascule.py` seulement → résolution mécanique des
témoins (union des tests, sans rebase du lot), conformément à son mandat du 17/10 18:51 dans le fil
#2235. Le pilote donne le feu sur la tête du paquet.

Exécuteur : **mty-amh-impl-01** (composition, ouverture de la PR, unique CI de paquet, remise au
pilote). Aucune CI de lot, aucun push de branche de lot, aucune fusion parallèle.

## Admissions (7 lots attendus du véhicule)

- #2233
- #2230
- #2281
- #1896
- #1400
- #2235
- #2268

## Composition (6 unités, 7 lots)

| unité | lot | source | commit | note |
|---|---|---|---|---|
| 1 | #2233 | `528e73296d61` (`lot/2233-underuse-long-window`, PR #1637) | `97f7e111e` | verdict `ok` mty-amh-qa-01 (DeepSeek) sur `528e7329`, auteur Codex ameesh-cx1 ; classe `light` ; 3 f. +141/−24 ; composition propre sur `6199ad736` |
| 2 | #2230 | `df995cb6a237` (`lot/2230-activation-lanceurs-robuste`, sans PR) | `3d3e8435e` | **deux avis** `ok` sur `df995cb6` : Claude ameesh-dev7 + Codex ameesh-cx2, auteur DeepSeek deepseek5 ; classe `sensitive` (`deploy/mise-a-jour/bascule.py`) ; 3 f. +112/−28 ; composition propre |
| 3 | #2281 | `5042113a818f` (`lot/2281-no-verdict-gate`, PR #1665) | `4b36595ce` | **deux avis** `ok` sur `5042113a` : ameesh-cx1 (Codex R2) + ameesh-dev1 (Claude), auteur DeepSeek mty-amh-impl-05 ; classe `sensitive` (porte de file, `merge_queue.py`) ; 3 f. +221/−15 ; composition propre |
| 4 | #1896 | `74493db1d922` (`lot/1896-docstrings-accounts-fraicheur`) | `3b1ebd8e2` | verdict `ok` codex1 (Codex) sur `74493db1`, auteur DeepSeek ameesh-dev3 ; classe `sensitive` argent (`accounts.py`, hors SQL/sécurité/prod : un avis suffit) ; 2 f. +29/−6 ; composition propre |
| 5 | #1400 | `54203a789723` (`lot/1400-declaration-fusion`) | `5ab4430db` | verdict `ok` ameesh-dev7 (Claude) sur `54203a78`, auteur DeepSeek ameesh-dev3 ; classe `sensitive` concurrence/état de lot, hors SQL/sécurité/prod : un avis suffit ; 8 f. +374/−13 ; composition propre |
| 6 | #2235+#2268 | `52af71e93c67` (`lot/2235-bascule-controles`, PR #1652) | `8f0938c31` | **deux avis** `ok` sur la même tête `52af71e9` : ameesh-dev2 (Claude) + mty-amh-impl-08 (DeepSeek), auteur Codex ameesh-cx3 ; classe `sensitive` prod (`deploy/mise-a-jour/bascule.py`) ; 3 f. +166/−25 ; **seul conflit de composition** avec #2230 sur `tests/test_1969_bascule.py`, résolu par union mécanique des témoins (sans rebase du lot) |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot et il porte le
trailer `ameesh-work: #<lot>` (deux trailers pour l'unité #2235+#2268). Aucun contenu nouveau hors
la résolution d'union ci-dessous. Total : **18 fichiers, +1043/−111**, aucun `.github/**`, aucune
migration.

## Conflit de composition et résolution mécanique

Le seul conflit est `tests/test_1969_bascule.py` entre #2230 (unité 2, `test_retour_bascule_meme_si_les_lanceurs_ne_peuvent_pas_etre_ecrits`)
et #2235+#2268 (unité 6, `test_ecriture_echouee_retire_le_temporaire_et_preserve_la_cible`,
`test_echec_du_nettoyage_ne_masque_pas_lechec_decriture`). Résolution : **union des témoins** des
deux côtés dans le même fichier, aucun rebase ni modification d'un lot ; `bascule.py` et
`docs/BASCULE.md` se composent sans conflit. Le fichier résolu garde 26 tests (union des 19 de
#2230 et des 25 de #2235+#2268, sans doublon) et `tests.test_1969_bascule` est vert sur la tête
composée. Le mandat du pilote (17/10 18:51, fil #2235) autorise explicitement cette union par le
compositeur.

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #1768 | exclu par le pilote (18:57Z) : conflit avec `main` dans `fire.py` et `radar.py` ; rebase demandé à dev7, paquet suivant |
| #2122 | hors paquet tant que le pilote n'a pas jugé la source §11 bis (18:57Z) |
| #1280 | lot SQL : second avis Codex dû sur la tête gelée `c597f86e` ; retenue explicite d'impl-07 (17:52Z) |
| autres lots `qa` | hors des admissions du pilote du 18:57Z ; candidats aux paquets suivants |

## Remplacement de h-1800

Ce véhicule remplace **h-1800** (PR #1675, base `98ef31e6`, tête `92625326`) : devenue
`CONFLICTING` avec `main` `6199ad736` (conflits `fire.py`/`radar.py` entre #1768 et #2271/#2238),
elle est abandonnée par le pilote ; son feu est retiré et son auto-fusion désarmée. Ses unités
#2233, #2230 et #2281 sont recomposées ici sans modification ; #1768 repart au paquet suivant après
rebase. Le run `39056370651` de h-1800 est annulé (dépassement de la part `psycopg 4/4`, sans test
en échec) ; aucune relance n'est faite.

## Preuves

- `git diff --check` propre, aucun marqueur de conflit.
- Chaque unité est un merge `--no-ff` de la tête exacte listée ; seconds parents conformes (6/6) et
  trailers `ameesh-work` présents (6/6, deux pour l'unité 6).
- Les têtes distantes des lots sont inchangées (`ls-remote` = sources listées).
- Tests ciblés sur la tête composée `8f0938c31` : `tests.test_1400_declaration_fusion`,
  `tests.test_1566_fraicheur_l226b`, `tests.test_2233_underuse_limits`,
  `tests.test_1969_bascule`, `tests.test_l160e_merge_queue`, `tests.test_1374_fire`,
  `tests.test_1003_merge_pr`, `tests.test_merged_work_item_tracking` = **330 OK** (psycopg,
  207 s).
- Aucun `.github/**`, aucune migration ; les changements de prod sont ceux de #2230 et
  #2235+#2268, chacun relu par deux familles.
- Contrôle d'inclusion `scripts/paquet-inclusion.py --manifest <ce fichier> --head <tête> --expected 2233,2230,2281,1896,1400,2235,2268` : à rejouer sur la tête finale avant le feu.

## CI et fusion

- **Une seule** suite complète sur la tête exacte du paquet : poussée de
  `integration/paquet-h-20261017-1900` **sans PR ouverte** ; le portier `paquet.yml` constate
  qu'aucune PR ne couvre la tête et appelle `tests.yml` en `workflow_call` ; la PR n'est ouverte
  qu'après la décision du portier.
- Le check requis `tests` doit être `success` avec des tests réellement exécutés (pas
  `skipped`/absent, #2271). Aucune CI de lot, aucune étiquette `ci-full` à la main.
- Feu du pilote sur ce SHA après `tests` vert (`gh pr merge --auto --match-head-commit <sha>` ou
  repli `ameesh work merge-pr`). Ne pas rebaser ; toute nouvelle tête annule le feu.
- Après fusion : rattachement des unités au merge réel et aux runs, clôture des lots par le
  runner, fermeture de #1675, confirmation au pilote et au bureau. Aucune opération hôte ici.
