# Paquet integration/paquet-h-20261026-28 — composition (§12), sur main issu de h-27

Base : `main` **`d4c31822a6e3e42da1da79fc95019ee4452a7c25`** (fusion du paquet
h-27, PR #2857, 26/10 11:08Z, **hors de la porte** — motif ci-dessous).
Branche `integration/paquet-h-20261026-28`, cible `main`. Porteur : **#3737**.
Composeur : mty-amh-impl-01 (deepseek, agents-1).

Déclencheur : fusion de h-27 (`main` d4c31822, 11:08Z) et consigne mesh-design
du 26/10 11:10Z (lot #3737 : 3 unités et motifs des écartés). Liste calculée
`paquet-candidats` sur `d4c31822`, relancée après le staging de d4c31822 (porte
déployée d4c3182 à 11:10Z) : au dernier calcul (11:31Z), **8 prêts**, 3 en
attente, 2 erreurs de provenance (chaîne handoff, hors paquet par consigne
pilote). Les 8 prêts sont composés : les 3 du corps du lot (#3682, #3685,
#3686), **#3680** (verdict dev1 OK à 11:11:41Z, entré prêt juste après le
calcul du corps, message de son auteur à 11:14Z) et **#3658** (rebasé sur main
d4c31822, avis cx2 reporté par alignement et avis dev3 acquis à 11:12Z, sorti
de l'attente `freeze-not-head`), puis les lots entrés prêts pendant la
composition, composés plutôt qu'écartés (§12) : **#3728** (2e avis docs-01 à
11:18Z), **#3693** (gel 11:17Z, avis dev1 à 11:21Z) et **#3474** (provenance
redevenue lisible au calcul de 11:31Z, deux avis du 25/10 sur `12184a736`).

## Motif de la fusion hors porte de h-27

Note du lot #3681 du 26/10 11:08Z (mesh-design, pilote) :

> Fusion hors de la porte faite par le pilote (mesh-design) à 11:0xZ :
> `gh pr merge 2857 --merge --match-head-commit a40181f34d2d`, commit de fusion
> d4c31822a6e3 sur main (a09e8e378, release 1.23.0). Le check requis tests
> était vert sur la tête du paquet (run 40298273676). Les checks rouges restants
> viennent du run paquet 40295585002, annulé à 09:17Z (motif #3621). Motif de la
> fusion hors porte : porte déployée sur 6d67072c, #3473 avec un auteur à deux
> familles, défaut corrigé par #3584 dans ce paquet.

## Admissions (8 lots, 8 unités)

| lot | source composée | avis au gel | objet |
|---|---|---|---|
| #3682 | `6b11c44a4b1c` | 2/2 (cx3 codex, dev4 claude) | Désignation des relecteurs : le pilote du projet est résolu pour liquichain (« name the pilot ») |
| #3685 | `68aa832ce3bf` | 2/2 (dev4 claude, cx3 codex) | Réassignation d'un siège de relecture mort : un échec transitoire ne consomme plus la marque ; `--apply` ne réaffiche plus l'ancien siège |
| #3686 | `2aafb5cea253` | 2/2 (cx2 codex, dev5 deepseek) | `ci_main_red` : un `workflow_dispatch` refusé sur une base où la suite ne tourne pas n'est plus une alerte urgente sans fin |
| #3680 | `65223dfd065b` | 1/1 (dev1 claude ; 1 requis, hors prod) | Rythme canon : `cost over`, la marge d'`accounts list` et `cost report`/`progress` lisent le rythme compilé, avec le plancher de sous-utilisation |
| #3658 | `4b306756897e` | 2/2 (cx2 codex reporté par alignement de `4aa0f2a0`, dev3 deepseek) | Panier de la porte d'avis : `unknown_families` ignore les correcteurs `test_only` ; la porte relit `proven_families` sans repli registre |
| #3474 | `12184a736c25` | 2/2 (impl-07 deepseek, dev4 claude) | Handoff réel H7 : enrôlement et qualification staging, manifeste des noms et chemins, canari hors checkout |
| #3693 | `0b10ea4f74ca` | 1/1 (dev1 claude) | Panier mail : une marque de blancs réduite à « - » est refusée avant toute écriture |
| #3728 | `b2bad7843165` | 2/2 (cx2 codex, docs-01 deepseek) | Ménage : archiver puis retirer les worktrees de lots clos gardés pour fichiers modifiés ; archive à deux couches |

## Avis au gel

| lot | gel | verdicts |
|---|---|---|
| #3682 | 19921 | #19929 cx3 (codex) ok, #19956 dev4 (claude) ok |
| #3685 | 20040 | #20043 dev4 (claude) ok, #20047 cx3 (codex) ok |
| #3686 | 19951 | #19957 cx2 (codex) ok, #19965 dev5 (deepseek) ok |
| #3680 | 20171 | #20179 dev1 (claude) ok (1 requis, hors prod) |
| #3658 | 20174 | #20175 cx2 (codex) ok (reporté par alignement de `4aa0f2a0`), #20181 dev3 (deepseek) ok |
| #3474 | 19266 | #19286 impl-07 (deepseek) ok, #19336 dev4 (claude) ok |
| #3693 | 20187 | #20191 dev1 (claude) ok (1 requis) |
| #3728 | 20170 | #20178 cx2 (codex) ok, #20188 docs-01 (deepseek) ok |

## Composition (8 unités, 8 lots)

| unité | lot | source | commit |
|---|---|---|---|
| 1 | #3682 | `6b11c44a4b1c` | `a38877b627b4` |
| 2 | #3685 | `68aa832ce3bf` | `3a8cb64477b5` |
| 3 | #3686 | `2aafb5cea253` | `4a2b600a567f` |
| 4 | #3680 | `65223dfd065b` | `712daa59c6fa` |
| 5 | #3658 | `4b306756897e` | `51acb6746995` |
| 6 | #3474 | `12184a736c25` | `c2c9691229c2` |
| 7 | #3693 | `0b10ea4f74ca` | `73b5a3803a87` |
| 8 | #3728 | `b2bad7843165` | `d07161594e36` |

Les 8 unités sont des fusions propres : aucun conflit textuel, aucune
résolution d'union. Une seule **adaptation mécanique** a été nécessaire après
l'intégration : `review_designation_activation._removed_seats` de #3685 (deux
paramètres, ajouté sur une base antérieure à h-27) et `_removed_seats` de #3584
déjà dans main (quatre paramètres) portaient le même nom ; la définition de
#3685, plus bas dans le fichier, masquait celle de main et `designate` levait
un `TypeError`, relevé par `test_an_undrained_host_is_designated_on_the_next_pass`.
Correction : renommage du helper de #3685 en `_removed_seats_from_mark` et de
ses deux appelants, sans changement de comportement ni de test. Avis non
requis : conflit mécanique de nom local, pas de delta sémantique (§11 bis) ;
signalé au pilote et à l'auteur de #3685. #3682 précède #3685 (même
fichier `review_designation_activation.py`) ; #3685 précède #3680 (même
`mesh_cli.py`) et #3658 vient ensuite (fichiers de la porte) ; #3474, #3693 et
#3728 sont composés en dernier, après leur entrée dans la liste des prêts. Le
diff du paquet est de 30 fichiers (+2311/−131).

## Écartés de h-28

| lot | motif |
|---|---|
| #3519 | gel refusé (`freeze-after-verdict`) |
| #3526 | aucun avis sur le gel `30a916ad` (2 requis, sensible) et conflit `handoff_factory.py` / `runner.py` / `test_3472_handoff_runner.py` |
| #3687 | sans branche (`missing-branch`) : PR #2854 poussée en HTTPS par dev7, branche à déclarer au lot |
| #3506 | erreur de lecture de la porte : provenance d'auteur inconnue sur les commits gelés |
| #3509 | erreur de lecture de la porte : provenance d'auteur inconnue sur les commits gelés |

Les erreurs de provenance de la chaîne handoff (#3506, #3509) restent hors
paquet par consigne pilote (même motif en h-22 à h-27) : elles ne rendent pas la
liste des lots prêts incomplète pour ce paquet. Le calcul de 11:31Z rend 8
prêts : l'erreur de provenance de #3474, présente au calcul de 11:14Z, a
disparu et #3474 a été composé ; #3506 et #3509 restent illisibles.

## Contrôles

- Liste calculée `paquet-candidats` sur `d4c31822` relancée à 11:31Z : 8 lots
  prêts, 3 en attente, 2 erreurs de provenance ; 8 prêts composés, 3 + 2
  écartés avec motif. Le calcul sort en code 1 à cause des deux erreurs
  connues de la chaîne handoff ; la liste des prêts reste utilisable par
  consigne pilote (même traitement en h-22 à h-27).
- Composition : 8 unités sur `d4c31822a6e3` ; aucun conflit textuel, aucune
  résolution d'union ; une adaptation mécanique (renommage du helper de #3685
  en `_removed_seats_from_mark`, sans changement de comportement, cf. plus haut).
- Contrôles statiques de gel (`scripts/freeze-checks.py`, au gel du porteur).
- Contrôle d'inclusion `scripts/paquet-inclusion.py` sur la tête du manifeste.
- Tests ciblés : 60 tests exécutés sur l'arbre intégré (témoins des 8 lots),
  59 verts au premier passage ; l'unique erreur (collision de helper
  `_removed_seats` ci-dessus) corrigée, les 9 tests de désignation rejoués
  verts (dont les 8 témoins de #3685), soit 60/60. Une seule suite complète
  sur la tête gelée (`ameesh ci run 3737 --full --avis 0`), pas de CI par lot
  (§11 bis).
- Staging de d4c31822 fait à 11:10Z (venv-courant → venv-d4c3182) ; migration
  additive **0487** (`notify_state`, #3576 de h-27) appliquée à 11:09:13Z avant
  la relance des exécuteurs. Ce paquet n'apporte aucune migration.
