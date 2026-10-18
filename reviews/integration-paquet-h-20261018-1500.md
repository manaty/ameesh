# Paquet integration/paquet-h-20261018-1500 — composition

Base : `main` **`4c73962e656f`** (fusion de la PR #1854, paquet h-1400,
18/10 15:01Z, CI complète `39135420081` verte sur `567e6c13`). Branche
`integration/paquet-h-20261018-1500`, cible `main`, PR à ouvrir. Une seule
suite complète est demandée sur la tête de ce manifeste.

Demandes : liste calculée des lots prêts (#2404) sur la base `4c73962e6`
(**4 prêts, 30 en attente, 0 erreur** ; #2483 est l'incident CI, hors
véhicule) ; admissions du pilote 18/10 15:09Z (#2442, #2349, #377, #2359) ;
#1955 signalé prêt par sa relecture dev2 15:36Z.

## Admissions

- Liste calculée 15:40Z : **#2446, #1057, #1819** ; #2483 (incident) est
  écarté du véhicule, voir plus bas.
- Pilote 15:09Z : **#2442** (remise `a716b92cdd79`), **#2359** (remise
  `a4b17607d205`, avis dev5 désigné) ; #2349 et #377 à porter aussi.
- #1955 : verdict dev2 15:36Z sur `5733709f6`, mais le calcul frais demande
  2 avis (classe sensible) : écarté, voir plus bas.

## Composition (5 unités)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #1057 | `6c5af76616ee` (tête gelée) | `932664547aa4` | Doc : `leases.state` annonce encore 4 clés ; `docs/EXECUTEUR-MEDIEE.md` + `storage/interface.py` ; 2 f. +4/−3 ; avis cx3 (Codex) + dev1 (Claude) ; sensible (documentation du contrat) |
| 2 | #1819 | `4ff54bb85045` (tête gelée) | `115d3d720883` | #1770 : ignorer `refs/replace` dans les lectures au SHA (`offload.py`, `prevalidation.py`) ; 3 f. +103/−4 ; léger, 0 avis requis — relecture après fusion ; témoin dédié `test_1819_no_replace_objects` |
| 3 | #2446 | `37a6755ded58` (tête gelée) | `48580cca6a3c` | Train #1467 : assemblage depuis la configuration (`train_assembly`/`train_contracts`/`train_initial`), bail de service exclusif, mode ombre, contrat CI `paquet.yml`/`tests.yml`, frontière initiale ; 19 f. +2305/−63 ; avis dev4 (Claude) + cx2 (Codex) ; sensible (porte de fusion, concurrence, configuration) |
| 4 | #2442 | `a716b92cdd79` (remise sur main) | `077166240ba6` | Porte #1979 : un réalignement mécanique sur la cible ne redéclenche plus la revue (`verdict_carry.py`, `merge_queue.py`, `mesh_cli.py`, `runner.py`) ; 6 f. +1104/−1 ; avis cx1 (Codex) + qa-01 (DeepSeek) sur le gel `548b707159` ; remise = main `4c73962e6` × gel, conflit unique `mesh_cli.py` (bloc de restitution) ; sensible (porte de fusion) |
| 5 | #2359 | `a4b17607d205` (remise) | `ee4b1b7a43c8` | #2339 P3 : la sonde d'auto-prise ne dépend plus des attributs internes de `take_pass` (`auto-take-probe.py`, `poste.sh`) ; 4 f. +87/−24 ; avis cx3 (Codex) + dev5 (DeepSeek) sur le gel `6ab07fac` ; remise = gel × `2ee1c545`, conflit `poste.sh` résolu ; sensible (scripts d'hôte) |

Chaque commit de composition a le lot en second parent, son sujet nomme le
lot et il porte un trailer `ameesh-work: <lot>` (5 trailers de composition).

## Conflits et résolutions

- #2446 : `src/ameesh/config.py` = union `release` (main) + `train` (lot),
  résolution `af6ab1c34` (#2479 × #2446) déjà intégrée à sa tête `37a6755d` ;
  le delta net contre `4c73962e6` est bien les 19 fichiers du lot.
- #2442 : remise `a716b92cd` (parents `548b7071` × `4c73962e6`), conflit
  unique `mesh_cli.py` résolu en main + bloc de restitution ; merge-tree rc 0
  contre la base, delta composé 6 fichiers +1104/−1.
- #2359 : remise `a4b17607` (parents `6ab07fac` × `2ee1c545`), conflit
  `deploy/mise-a-jour/poste.sh` résolu ; le diff brut de la remise contre
  `4c73962e6` inclut le retard de main, mais le merge-tree rc 0 et l'arbre
  composé ne portent que 4 fichiers +87/−24.
- #1057 et #1819 : fusion propre.
- #2446 × #2442 : `docs/EXPLOITATION.md` et `src/ameesh/runner.py` touchés
  par les deux ; auto-fusionnés sans conflit, couverts par les témoins
  ci-dessous.
- Aucune autre résolution ; aucun marqueur de conflit (`git diff --check`
  rc 0).

## Preuves

- `git diff --shortstat 4c73962e6..HEAD` : **32 fichiers, +3603/−95**.
- `git diff --check 4c73962e6..HEAD` : rc 0.
- `git diff --name-only 4c73962e6..HEAD` : aucun `*.sql`, aucune migration.
- `scripts/paquet-compose.py --units --base 4c73962e6` : **5 unités de
  fusion, 5 lots portés** — `#1057 6c5af76616ee`, `#1819 4ff54bb85045`,
  `#2446 37a6755ded58`, `#2442 a716b92cdd79`, `#2359 a4b17607d205`.
- Témoins ciblés sur l'arbre composé (`PYTHONPATH=src python -m unittest`,
  `TMPDIR` dédié) : **319 tests OK** —
  - #2446 + #1467 + config release : 100 OK (assemblage, contrat, frontière,
    bail de service, ombre, CI, service #1467, release #2448) ;
  - #2442 + #2447 + plan : 103 OK (verdict carry, désignation, plan) ;
  - #2359 + #1819 + #1770 + contrat d'exécuteur : 95 OK ;
  - doc-links : 21 OK ; `scripts/doc-links.py --root docs` : **170 fichiers,
    0 lien cassé** ;
  - recouvrement `runner.py` / `merge_queue.py` (#2442 × #2446) :
    `tests.test_runner` + `tests.test_l160e_merge_queue` 111 OK ;
  - moteur #1467 (7 modules : runner, runtime, candidates, cutoffs, policy,
    admissions, history) : 140 OK ; liste des parts CI
    (`test_l161a_ci_sans_code`) : 7 OK.
- `scripts/paquet-candidats.py` (arbre pré-#423, base fraîche `4c73962e6`,
  clone complet dédié) : 4 prêts, 30 en attente, 0 erreur ; #2446 y est
  présent avec ses deux verdicts. Le calcul depuis l'arbre main est empêché
  par la colonne `agent_registry.aliases` absente de la base (0418 non
  appliquée) : même écart que h-1300/h-1400, assumé côté calcul.

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #2483 | Incident CI main (run `39133881211`), correctif `bc840b3de9` en CI de lot et PR #1857 propre : un incident se fusionne hors coupure, il n'est pas une unité de paquet. |
| #2349 | Remise `4eeed0ef` prête (merge-tree rc 0, 6 f. +127/−9) mais **avis delta absent** sur la remise au moment de la composition (dev4 sollicité 15:35Z) : à reprendre au paquet suivant. |
| #377 | **1/2 avis** (auditeur, Codex) : classe sensible (identité/droits), deux familles requises par le calcul. Le pilote le dit prêt ; le contrôle mécanique refuse, écart signalé au pilote. |
| #1955 | **1/2 avis** (dev2, Claude) sur `5733709f6` : le calcul demande 2 avis pour sa classe sensible ; le pilote annonce qu'un seul suffit — écart signalé, lot non composé. |
| #2449 | **1/2 avis** (dev7), classe sensible : second avis requis. |
| #2481 | **1/2 avis** (cx3 sur `5e196bb1`, gel `918d782f`) : second avis requis. |

Les autres lots en `qa` ne sont pas prêts au calcul `paquet-candidats`
(verdicts manquants, gel non égal à la tête, conflit ou dépendance bloquante)
et ne sont pas admis.

## CI

Suite complète unique (`ci-full`, mode full, avis 0 — composition mécanique)
demandée sur la tête de ce manifeste ; auto-merge `MERGE` armé sur cette tête.

## Après fusion (staging)

- Aucune migration nouvelle dans ce paquet. Les migrations additives `0418`
  (#423) et `0464` (#2421) de h-1300 restent à appliquer par le bureau avant
  relance des exécuteurs, comme noté aux paquets précédents.
- Déploiement staging dédié au paquet : VM d'agents d'abord, pc-smichea en
  dernier si saines ; smoke réel.
- #2446 n'est « fait » qu'après deux paquets horaires réels en mode ombre
  (comparaison au manifeste, écarts expliqués), puis activation — gestes
  pilotes.
