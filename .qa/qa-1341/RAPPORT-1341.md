# #1341 — Carte main incomplète et `psql 2/6` annulé au plafond (tête `0532faed4`)

Qualification demandée par le bureau (mail 19:12Z, lot créé 19:12), menée par
`mty-amh-qa-01` le 14/10 de 19:15 à 19:4xZ. **Aucun geste CI : aucun rerun,
dispatch, cancel, push ou fusion.** Lecture seule API GitHub + artefacts.

## 0. Pièces vérifiées (première main)

| pièce | identité | SHA-256 |
|---|---|---|
| log mesure psy1 (bureau) | `carte-38610188898-mesure-psy1.log` | `e7bed608…5615677` |
| log psql 2/6 (bureau) | `tests-38610188942-psql2.log` | `97a2610b…f5361c` |
| mes copies API des 2 logs | téléchargées par `gh api .../jobs/<id>/logs` | identiques aux 2 SHA ci-dessus |
| artefacts | `plan`, 10 × `flux-*`, `mesure-psycopg-1..4`, `mesure-psql-1` | téléchargés, extraits |

Les deux logs du bureau et ceux servis par l'API sont **bit à bit identiques** :
la qualification porte bien sur les pièces du bureau.

## 1. Les deux annulations, en faits

| | mesure psy1 (carte) | psql 2/6 (tests) |
|---|---|---|
| run / tête | 38610188898 / `0532faed4` | 38610188942 / `0532faed4` |
| job | 116028614504 | 116024818443 |
| runner | `ameesh-ci-2-6` | `ameesh-ci-2-4` |
| plafond | `timeout-minutes: 40` (carte.yml) | `timeout-minutes: 45` (tests.yml) |
| début → annulation | 18:27:11 → 19:07:31 (40 min 20 s) | 18:19:26 → 19:04:48 (45 min 22 s) |
| phase qui consomme le plafond | **phase de test : 39 min 58 s** (setup 22 s) | **phase de test : 45 min 22 s** (setup 13 s) |
| modules prévus / commencés / terminés | 61 / 61 / **60** | 44 / 41 / **41** |
| tests prévus / exécutés | 1 159 / 1 167 (dont 1 module partiel) | 928 / 807 |
| statuts | 1 166 succès, 1 sauté, **0 échec** | 804 succès, 3 sautés, **0 échec** |
| troncature | `test_turn_grace` **4/25** (dernier module) ; `durees.json` **absent** de l'artefact | `test_progress_serve` **7/21**, `test_l148_stage2b_scripts` 13/14 ; **3 modules jamais commencés** : `test_receipts`, `test_signing`, `test_tube_ferme` |
| upload artefact | succès (18 Mo) — mais mesure **tronquée** | flux publié (26 Ko) |

Précisions:
- Le « prévu » est le compte `def test` (approximatif : les sous-tests ne sont pas
  comptés), d'où un total exécuté qui peut dépasser le prévu sur un module
  paramétré ; la troncature se lit **module par module** (dernière colonne).
- Le runner a marqué l'annulation à 19:07:31, mais le processus Python orphelin a
  continué ~15 s (2 tests `test_turn_grace` tracés à 19:07:34 et 19:07:46) avant
  d'être tué à 19:07:54 (log : « Terminate orphan process »). La troncature est
  donc réelle, pas un artefact d'horodatage.
- Aucune panne runner : 0 test en échec, dernières assertions `... ok`,
  jobs servis par un runner nommé, pas de fantôme.

## 2. Distribution des parts (même flotte, même tête)

### Run tests 38610188942 (sans mesure, 10 parts)

| part | mur | plan | temps de test | modules | tests |
|---|---|---|---|---|---|
| psql 1/6 | 30,3 min | 11,8 | 22,0 | 41/41 | 777 |
| **psql 2/6** | **45,5 (annulée)** | 11,8 | **33,5** | 41/44 | 807 |
| psql 3/6 | 27,0 | 11,8 | 20,5 | 44/44 | 872 |
| psql 4/6 | 25,3 | 11,8 | 17,5 | 40/40 | 803 |
| psql 5/6 | 22,2 | 11,8 | 15,0 | 41/41 | 856 |
| psql 6/6 | 32,0 | 11,8 | 23,1 | 40/40 | 902 |
| psycopg 1/4 | 9,1 | 22,5 | 8,0 | 64/64 | 1 158 |
| psycopg 2/4 | 17,8 | 22,5 | 15,4 | 62/62 | 1 436 |
| psycopg 3/4 | 9,6 | 22,5 | 8,1 | 62/62 | 1 266 |
| psycopg 4/4 | 13,0 | 22,5 | 11,4 | 62/62 | 1 277 |

- Les parts **psql** s'étalent de 15,0 à 33,5 min de tests (mur 22,2 à 45,5) pour
  une estimation identique de 11,8 min : déséquilibre ×2,2 et sous-estimation ×2 à ×3.
- Les parts **psycopg** finissent en avance sur leur estimation (poids de la
  carte sous couverture, non couverts ici) : aucun risque de plafond (30 min).
- Historique de la part la plus lourde, **psql 2/6** : 44,2 min au run
  38570320043 (13:15Z, **avant** la vague), 34,6 min au run 38571519935,
  45,5 min ici. Elle est intrinsèquement au plafond.

### Run carte 38610188898 (sous mesure, 5 parts)

| part | mur | temps de test | modules | tests | `durees.json` |
|---|---|---|---|---|---|
| **mesure psycopg 1/4** | **40,8 (annulée)** | **37,1** | 61/61 | 1 167 (module final 4/25) | **absent** |
| mesure psycopg 2/4 | 20,8 | 18,9 | 62/62 | 1 349 | présent |
| mesure psycopg 3/4 | 38,2 | 35,9 | 66/66 | 1 290 | présent |
| mesure psycopg 4/4 | 16,1 | 14,8 | 61/61 | 1 310 | présent |
| mesure psql 1/1 | 38,8 | 30,8 | 45/45 | 956 | présent |

- Déséquilibre ×2,5 entre parts sous mesure (14,8 à 37,1 min).
- **Surcoût de la couverture** (mesure / sans mesure, 185 modules communs
  psycopg) : médiane **1,68×**, maximum **9,3×** (`test_503_agent_host`
  625,6 s sous mesure contre 67,3 s sans), puis `test_502_restart_lease` 7,6×,
  `test_l62_projects` 6,1×, `test_l1170_test_script_selection` 6,1×. Le facteur
  varie d'un module à l'autre : c'est ce qui casse l'équilibre (voir §3).
- `mesure psql 1/1` (liste pilote, 45 modules) : 38,8 min de mur pour un
  plafond de 40 — **97 % du plafond**, et **annulée à 40 min 33 s** au run
  carte précédent (38601979838, 17:11→17:52).

## 3. Cause racine — une chaîne, trois maillons

1. **La part mesure psql ne tient pas dans 40 min.** Une seule part séquentielle
   de 45 modules sous couverture = 30,8 min de tests + ~8 min de surcoût
   (setUpClass par module, combinaison de couverture) ≈ 39–41 min. Annulée deux
   fois de suite (40 min 33 s au run précédent ; 38 min 46 s ici).
2. **Sans elle, la carte publiée n'a aucune durée psql.** Vérifié sur
   l'artefact `carte` publié pour `96a795fb2` (run 38601979838) : **248 modules
   avec durée psycopg, 0 avec durée psql**. Or `scripts/test-plan.py` pondère
   les parts avec les durées de la carte ; sans durées psql, il retombe sur
   `tests/parts/durees.json` — table statique du **10/10 sur `ubuntu-latest`**,
   qui ne couvre que **76 modules** ; les ~174 autres reçoivent un poids
   « nombre de tests × durée moyenne ».
3. **Le découpage psql est donc faux**, et la part la plus lourde (`psql 2/6`,
   ~33,5 min de tests, ~45,5 min de mur) atteint le plafond de 45 min.

Côté mesure psycopg, le maillon est différent : `carte.yml` lance
`test-parts.py run --parts 4 …` **sans `--map`** — le découpage sous mesure
utilise la table statique **non couverte**, alors que le surcoût de couverture
varie de 1,2× à 9,3× selon les modules. Une part qui hérite de
`test_503_agent_host` (9,3×) explose : psy1 = 37,1 min, psy3 = 35,9 min contre
14,8–18,9 min pour psy2/psy4.

**Ce n'est donc ni une panne runner, ni un plafond « trop bas » en soi, ni
principalement la vague de relances** : c'est une répartition pondérée par une
table qui ne correspond ni aux durées réelles (psql) ni aux durées sous
couverture (mesure).

## 4. Impact de la vague de 83 relances (même flotte)

Relevé API à 19:2xZ (100 derniers runs) :
- **70 runs en tentative ≥ 2** (67 encore en file, 2 `pending`, 1 terminé) ;
- **74 runs vivants** au total, **71 en file** ;
- plus vieux run en file créé **13:55:15Z**, soit **~5 h 24 d'âge** ;
- flotte : 24 runners `ameesh-ci`, 8 jobs par hôte.

Comparaison des mêmes parts psql entre le run 38570320043 (13:15Z, avant la
vague : 16 jobs) et le run courant 38610188942 (18:19Z, pendant la vague) :
1/6 19,4→30,3 min ; 4/6 34,6→25,3 ; 5/6 39,5→22,2 ; 6/6 27,4→32,0 ;
2/6 44,2→45,5.

L'effet de la vague n'est **pas uniforme** : elle ajoute de la file et de la
contention (jusqu'à ~1,6× sur certaines parts) mais n'explique pas les deux
annulations — `psql 2/6` était déjà à 44,2 min avant la vague, et `mesure
psycopg 1/4` était à 33,6 min au run carte précédent (33,6 → 40,8 min cette
fois). La cause dominante reste la pondération ; la vague est un facteur
aggravant de capacité (file à 5 h 24).

## 5. Correctifs proposés (répartition justifiée, pas plafond à l'aveugle)

Simulations faites avec l'algorithme exact de `scripts/test-parts.py:split`
(plus lourd d'abord, part la moins chargée), sur les durées mesurées de ce run.

| # | correctif | preuve / effet simulé | classe |
|---|---|---|---|
| A | **Rendre le découpage sous mesure conscient de la couverture** : passer la carte au `test-parts.py run` de carte.yml (`--map`, cache `carte-` restauré) ou une table de durées sous mesure dédiée | max des 4 parts mesure psycopg : **37,1 → 26,7 min** de tests (~30 min de mur) ; sous le plafond 40 actuel, marge ~10 min | sensible CI |
| B | **Découper la part mesure psql en 2 parts** (le job `carte` fusionne déjà `mesure-*`) plutôt que monter le plafond | max : **30,8 → 19,2 min** de tests (~24–27 min de mur) ; supprime le risque à 97 % du plafond | sensible CI |
| C | **Donner des durées psql réelles pour tous les modules** : la carte n'en porte aucune (mesure psql annulée), et la table statique n'en couvre que 76 sur 250 — ~174 modules n'ont qu'un repli « nombre de tests × durée moyenne ». Même après la publication courante, la mesure psql ne couvre que la liste pilote (45 modules). Source la moins chère : agréger les durées par module des flux des runs tests (chaque part écrit `duree_s` par test) et rafraîchir `tests/parts/durees.json` (voie prévue : `test-parts.py durations`) | 6 parts psql rééquilibrées : max **33,5 → 22,0 min** de tests (~29–31 min de mur) sous plafond 45 | sensible CI (données) |
| D | **Garde de complétude** : une part mesure sans `durees.json` (annulée) est fusionnée **silencieusement** — c'est arrivé pour `96a795fb2` (carte publiée sans durées psql). Faire échouer (ou marquer explicitement) le job `carte` si une part attendue n'a pas son `durees.json` | empêche le faux PASS « upload success » et la publication partielle silencieuse | sensible CI |
| E | **Capacité** : étaler les vagues de relances (ou plafonner les runs simultanés) ; file à 5 h 24, 74 runs vivants pour 24 runners | réduit la contention et les durées de file | pilote/bureau |

Les correctifs A–D sont des changements `.github/**`/`tests/parts/**` : classe
sensible CI (§8), relecture indépendante, **aucun push de ma part** — la
décision et le créneau appartiennent au pilote (fenêtre 38076 toujours fermée
à 19:3xZ).

## 6. Publication : état et options

- Le job `carte` du run 38610188898 est **en file depuis 19:07:58** et
  `if: always()` : il publiera une carte contenant **les durées psql de la
  liste pilote** (mesure psql 1/1 a réussi cette fois) mais **sans les durées
  psy1** (artefact sans `durees.json`) et avec une couverture psy1 partielle
  (`test_turn_grace` 4/25). La mesure publiée n'est donc **pas complète**.
- Le run tests 38610188942 conclura **rouge** par l'agrégat requis : `psql 2/6`
  annulée ⇒ garde #940. `0532faed4` n'a donc pas de suite complète verte, et
  3 modules (`test_receipts`, `test_signing`, `test_tube_ferme`) n'ont pas été
  validés en psql sur cette tête.
- Options (décision pilote/bureau) :
  1. laisser la carte se publier (la couverture, utile à la sélection, est
     presque complète ; les durées psy1 retombent sur la carte précédente) et
     re-mesurer psy1 au prochain push main, avec A/B appliqués ;
  2. re-mesurer psy1 avant/après la publication, puis re-publier — geste CI
     (close/reopen ou dispatch), coordination pilote requise.
  3. dans tous les cas, une suite complète verte sur `0532faed4` exige un
     nouveau run (push, close/reopen ou dispatch), après la fenêtre.

## 7. Reproductibilité

- `analysis-1341.py` (ce dossier) recalcule tous les chiffres depuis
  `artifacts/` ; sortie brute `analysis-1341.json`.
- `artifacts/` : plan.json, 10 flux du run tests, 4 parts mesure psycopg +
  mesure psql 1/1 (flux, `durees.json`), jobs JSON des 2 runs, table statique
  `durees-0532faed4.json`, les 2 logs, et la copie bureau sous `bureau/outils/`.
- SHA-256 des pièces dans `artifacts/SHA256SUMS` (à générer au dépôt du lot).
