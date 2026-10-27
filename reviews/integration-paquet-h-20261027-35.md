# Paquet integration/paquet-h-20261027-35 — composition (§12), sur main issu de h-34

Base : `main` **`fb029a526557f041fa26ae6b5e627215f8dec862`** (fusion de h-34,
PR #2950 ; arbre identique à la tête de paquet `a41acf1ec33b`). Branche
`integration/paquet-h-20261027-35`, cible `main`. Porteur : **#3879**.
Composeur : mty-amh-impl-01 (deepseek, agents-1).

Déclencheur : lot #3879 (mesh-design, 27/10 04:18Z), précisé par le pilote
(mesh-design, 27/10 04:40Z, 04:50Z, 05:10Z) : composer sur la tête de `main`
après h-34 avec #3876, #3843, #3853 et #3877, chacun s'il est prêt au gel.
#3506 (panier handoff-reel, SLO niveau 3) est ajouté sur la condition du
pilote et la demande de l'auditeur : la porte devait lire sa provenance, et le
bureau a confirmé h-34 `fb029a52` en staging VM (#148858, 05:31Z) ; la liste
recalculée depuis un worktree **exactement à la base** ne rend plus aucune
erreur et sort #3506 prêt. #3886 (panier pg-up, léger) est devenu prêt pendant
la composition (gel #21357 à 05:00Z, avis dev3 à 05:07Z) : il entre aussi.

Méthode : liste calculée `scripts/paquet-candidats.py` sur la base exacte
(`fb029a526557`, worktree détaché à cette référence : le calcul depuis un
arbre en avance est refusé), composition par `scripts/paquet-compose.py` (un
commit de fusion par unité, trailer `ameesh-work:`), manifeste, contrôle
d'inclusion, tests ciblés **avant** le gel, `freeze-checks`, puis une seule
`ci run --full --avis 0` sur la tête gelée. Aucune migration dans le paquet.

## Admissions (6 lots, 6 unités)

| lot | source composée | avis au gel | objet |
|---|---|---|---|
| #3876 | `70635efe95d8` | gel #21284 : #21292 ameesh-cx2 ok, #21296 ameesh-dev3 ok | désignation au gel : une pause courte de la famille codex ne la retire plus des éligibles |
| #3843 | `6e5877597d24` | gel #21235 : #21251 ameesh-cx2 ok, #21275 mesh-design ok | panier #3836 : commentaire 180 s exact, limite du `lock_timeout` documentée, appels imbriqués couverts |
| #3853 | `f318f0421ba7` | gel #21338 : #21348 ameesh-dev3 ok | panier #3848 : sauvegarde du chemin VM, relance staging seulement au retour du poste, `--hotes` cohérent |
| #3877 | `8b23a843605c` | gel #21307 : #21312 ameesh-dev3 ok | incident CI : attentes bornées sur la tête d'atterrissage (2644, `l232a` laissé à #3868, `l159b`, runner) |
| #3506 | `30bccc832746` | gel #20690 : #20691 ameesh-dev1 ok, #20692 mty-amh-impl-05 ok | panier handoff-reel : catégories publiques fermées des refus retry/cancel, témoin H6 non vacuuous |
| #3886 | `9757f801fd4f` | gel #21357 : #21368 ameesh-dev3 ok | panier ameesh : `pg-up.sh` purge le banc avec l'interpréteur du venv, hors d'un clone |

#3877 est l'incident CI de `main` (P1) mais entre comme unité normale du
paquet, sur son gel `8b23a843605c` tel quel : décision du pilote (04:25Z), sans
exception d'incident ni CI hors coupure. Le hunk `l232a` est laissé à #3868,
déjà dans `main`.

#3506 a été exclu de h-33 et h-34 sur une porte illisible (provenance
inconnue de `790265d8e396`). #3837 (fusions d'une tête réécrite retirées de la
lecture) est dans `main` depuis h-34, et le calcul mené depuis un worktree à
la base `fb029a52` rend la liste **complète, zéro erreur** : #3506 est prêt sur
`30bccc832746` avec ses deux avis portés. Il entre.

## Composition (6 unités, 6 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #3876 | `70635efe95d8` | `d600cda148f7` | composition (fusion propre) |
| 2 | #3843 | `6e5877597d24` | `a08a89e3af23` | composition (fusion propre) |
| 3 | #3853 | `f318f0421ba7` | `1c97cd0e9d43` | composition (fusion propre) |
| 4 | #3877 | `8b23a843605c` | `8582e2751378` | composition (fusion propre, tests seuls) |
| 5 | #3506 | `30bccc832746` | `da9cb6de4fc3` | composition (fusion propre) |
| 6 | #3886 | `9757f801fd4f` | `898c40b38bf9` | composition (fusion propre) |

6 fusions propres, **0 résolution de composition**, 0 union mécanique,
0 adaptation de test. Les lots sont disjoints ou sans chevauchement
conflictuel : #3843 (`deploy/serveur/installer-serveur.sh`,
`docs/EXPLOITATION.md`, `src/ameesh/storage/postgres/work.py`, tests), #3853
(`deploy/mise-a-jour/staging.sh`, `src/ameesh/release_cutoff.py`, docs,
tests), #3876 (`src/ameesh/account_window.py`,
`src/ameesh/review_designation.py`, test), #3877 (trois fichiers de test),
#3506 (`src/ameesh/handoffs.py`, `src/ameesh/mesh_cli.py`, docs, test) et
#3886 (`scripts/pg-up.sh`, deux tests) fusionnent chacun proprement sur la
tête courante.

## Écartés de h-35

| lot | motif |
|---|---|
| #3519 | hold pilote actif (mesh-design, depuis 02:00Z) : désignation en attente de la provenance (9 commits inconnus) avant le staging de #3837 ; le gel effectif est en outre refusé après verdict (`freeze-after-verdict`). |
| #3526 | non prêt : aucun verdict sur son gel `5e1bfc4a8067` (sensible) — le calcul le rend `verdicts-missing`. |
| #3885 | non prêt : conflit avec la cible sur `docs/EXPLOITATION.md` et `src/ameesh/role_timers.py` (sensible : empreintes et migration additive 0492) ; remise à préparer, paquet suivant. |
| #3889 | non prêt : verdict bloqué sur son gel (le calcul refuse le gel effectif, `blocked-verdict`) ; correction due, paquet suivant. |

La liste calculée du 27/10 05:33Z sur `fb029a526557` (worktree détaché à
cette référence) rend **6 lots prêts** — #3877, #3506, #3876, #3843, #3853
et #3886 — tous composés ici, et 0 erreur de lecture. Les calculs précédents,
menés depuis un worktree en avance sur la base, étaient refusés par le garde
de code ; celui mené depuis l'ancien runtime (avant #3837) rendait #3506 en
erreur, ce qui explique son absence de h-33 et h-34.

## Migrations et exploitation

Aucune migration.

- **#3853** modifie `deploy/mise-a-jour/staging.sh` et
  `src/ameesh/release_cutoff.py` : le staging depuis une VM garde une
  sauvegarde côté manaty-mesh-1 avant les migrations, ne relance plus toute
  la séquence à chaque coupure tant que le poste est absent, et `--hotes`
  nommant `pc-smichea` absent ne sort plus en 2. À activer au prochain
  déploiement de staging avec la tête.
- **#3843** corrige le commentaire de durée de `installer-serveur.sh` (180 s
  réels) et documente la limite du `lock_timeout` pris avant le `SET LOCAL`
  (`storage/postgres/work.py`, `docs/EXPLOITATION.md`), appels imbriqués
  couverts par deux témoins.
- **#3876** modifie la désignation au gel (`review_designation.py`) et la
  lecture de fenêtre (`account_window.py`) : une pause courte de la famille
  codex ne retire plus toute la famille des éligibles.
- **#3877** ne touche que des tests (attentes bornées de 2644, `l159b`,
  runner) : aucun effet d'exploitation.
- **#3506** ferme les catégories publiques des refus de `handoff retry` /
  `cancel` (`handoffs.py`, `mesh_cli.py`) sans exposer les messages des
  helpers ; durcit le témoin H6 qui passait à vide sur un checkout
  group-writable.
- **#3886** corrige `scripts/pg-up.sh` : la purge du banc utilise
  l'interpréteur du venv ameesh quand il existe (ou `python3` du clone avec
  `PYTHONPATH=src`), donc plus de `ModuleNotFoundError` quand le script est
  lancé depuis une copie déployée.

## Contrôles

- Inclusion : **aucun défaut** (6 attendus, 6 compositions, 0 retrait,
  contenus à la tête : 6 présents, 0 à lire ; liste calculée du 05:33Z sur
  `fb029a526557`, 0 erreur).
- `freeze-checks` : **8/8 verts** (garde mécanique #2449, compilation, contrat
  de train #3606, attribution #1632, lint L63, alert types #1060, doc readers
  L161a, contrat de la porte #3687) sur la tête à 6 unités.
- Tests ciblés : **454 tests, 0 échec** (pilote psql) sur la tête composée —
  #3843 `test_3836_idle_xact`, #3853 `test_1155_staging`, `test_1323_actif`,
  `test_2448_release_cutoff`, #3877 `test_2644_prevalidation_calls`,
  `test_l159b_offload`, `test_runner`, #3506 `test_3473_handoff_cli`, #3886
  `test_3886_pg_up_purge_python`, `test_l159f2_pg_up` (412 + 42). #3876
  `test_3876_designation_short_window` et `test_1617_review_designation` :
  verts en psql sur la tête ; la passe psycopg de la sélection #3876 a été
  interrompue par une rotation de session pendant `test_2447`, sans échec
  observé. La suite complète de la CI fait foi pour les deux pilotes.
