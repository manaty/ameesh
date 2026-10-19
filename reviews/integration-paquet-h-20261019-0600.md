# Paquet integration/paquet-h-20261019-0600 — composition (§12)

Base : `main` **`7f2de9d9da`** (paquet h-0500, PR #1993, fusionné en
`7f2de9d9d` le 19/10 04:10 UTC). Branche `integration/paquet-h-20261019-0600`,
cible `main`. Une seule suite complète est demandée sur la tête finale.

Demandes : composeur, paquet horaire §11 bis — `main` a bougé à `7f2de9d9da`
(h-0500 fusionné, paquet h-0600 dû) ; liste calculée `paquet-candidats.py`.
Entrées : message `ameesh-dev3` #112275 (#2466 × #2481, résolution
`907550b2`), message `ameesh` (CI verte de h-0500, run `39195046608`).

## Admissions (5 unités, 5 lots portés)

- #2602 — événements automatiques : dédoublonnage (lot, étape, destinataire)
- #2634 — `_await_release` relit `lease_held_until` sur l'horloge du worker
- #2638 — désignation au gel : la correction de premier tour d'un relecteur
  n'exclut pas sa famille
- #2498 — `vm.sh drain` : commentaire faux, message NON levé prématuré, rc du
  refus `not_after`
- #2571 — `account_sharing` et `account_registry` relus à chaud (#313 N1)

## Composition (5 unités)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #2602 | `e3bd777a3149` (tête gelée) | `00e18985c950` | sensible — dédoublonnage des événements par état actionnable ; 2 avis (dev7 Claude, dev5 DeepSeek) |
| 2 | #2638 | `8ebc9d516389` (tête gelée) | `56d6aa521938` | léger — familles de relecteurs-correcteurs au gel ; 2 avis (cx3 Codex, dev4 Claude) |
| 3 | #2634 | `d3281f954f8d` (tête gelée) | `2ce5661911c6` | sensible — horloge du worker pour le bail ; 1 avis (cx1 Codex), politique compilée `reviews: 1` |
| 4 | #2498 | `87d72bbf5246` (tête gelée) | `9b83e2125754` | sensible — verdict de drain et `not_after` ; 2 avis (cx2 Codex, impl-08 DeepSeek) |
| 5 | #2571 | `a30bf2bf5b07` (tête gelée) | `f91d7b953e04` | sensible — champs rechargeables des comptes ; 1 avis (cx1 Codex), politique compilée `reviews: 1` |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot
et son SHA, et il porte un trailer `ameesh-work: <lot>`. Aucun conflit de
composition rencontré, aucune résolution, aucune migration SQL.

## Adaptation de test (mécanique, sans avis nouveau)

`tests/test_2638_reviewer_fix_family.py` : le témoin #2602 utilisait
`src/ameesh/db.py` comme chemin par défaut. Depuis le paquet h-0500 (plancher
critique de la porte de fusion, #2627/#2394), `db.py` exige deux avis même
quand le canon à un avis du témoin en déclare un : le troisième gel restait
`waiting` au lieu de désigner. Le témoin est inchangé sur le fond et passe sur
`src/ameesh/work.py`, comme `tests/test_2447_review_designation_activation.py`
le fait déjà. Commit `402b5ba67` ; aucun code produit touché, aucun avis
nouveau (§2/§11 bis).

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #2466 | conflit avec `main` sur le gel `abc2fbf796a5` (`docs/EXECUTEUR-MEDIEE.md`, `src/ameesh/runner.py`, `src/ameesh/storage/postgres/work.py`). La résolution `907550b2` (merge de #2481 dans le gel) a été calculée sur la base h-0500 : `git merge-tree origin/main 907550b2` rend **rc 1** (mêmes trois chemins). Une résolution fraîche contre `main` `7f2de9d9da` est requise, paquet suivant. |
| #2613 | `freeze-after-verdict` : gel effectif refusé (verdicts sur une tête antérieure), à regeler/revoir. |
| #2644 | conflit avec `main` sur `src/ameesh/mesh_cli.py`. |
| #2603 | `verdicts-missing` : aucun avis sur `bab9152355e9` (2 requis). |
| #2601 | `no-verdict` : aucun relecteur désigné sur `b7bf32708dba`. |
| #2451 | `verdicts-missing` : aucun avis sur `284a04d226c5`. |

Les 18 autres lots en attente le restent (verdicts-missing, no-verdict,
freeze-after-verdict, conflit avec `main`) : aucun n'est prêt à cette coupure.

## Contrôles

- Liste calculée `scripts/paquet-candidats.py` à 04:2x UTC, base `7f2de9d9da` :
  **5 lots prêts, 24 en attente, 0 lecture en erreur** (`--ready-out`). Les 5
  prêts sont composés ; les écarts sont motivés ci-dessus. La première lecture
  a rendu rc 1 (3 blobs absents d'un clone partiel : #2443, #2634, #2638) ;
  `git fetch --refetch --no-filter` des trois branches puis seconde lecture
  rc 0 — les trois lots sont désormais lus.
- Composition des 5 unités : **0 conflit**, une unité par commit de fusion.
- `paquet-inclusion` (tête de ce manifeste, `--ready-file` + admissions) :
  **rc 0** — 5 lots attendus, 5 compositions, 0 défaut ; #2638 listé
  « partiel » (125/126 lignes) par l'adaptation de test documentée ci-dessus,
  ligne `db.py` → `work.py`, à lire avant le feu.
- `git diff --check` vert ; aucune migration SQL ; 17 fichiers +722/−47 hors
  manifeste et adaptation de test.
- Prévalidation L161f : **indisponible** sur cette tête, deux refus nommés
  (plan du paquet `full/unsure` et `--host ameesh-agents-1` → `no-vm`), comme
  constaté sur h-0500 ; écart noté au manifeste, run full lancé par le
  fallback du pilote (consigne mesh-design du 19/10 03:37).
- Tests ciblés sur l'arbre composé : 150 tests sur les modules des unités
  (#2602, #2638, #2152, #1466, #160c, #1462, #503) — 1 seul échec, l'adaptation
  #2638 ci-dessus, corrigée ; module #2638 **6 OK** après correction, témoin
  #2634 (`tests.test_runner.AttenteDuRetourDeBailTest`) **1 OK**.
- Suite complète unique demandée sur la tête finale (`ci-full-<sha>`), avec la
  revue unitaire du paquet pour seul avis de la demande de CI (§12,
  `--full --avis 0`).
