# Paquet integration/paquet-h-20261016-2315 — composition

Base : `main` après atterrissage de PR1534 — merge
`fbb8eb581aa3b7b2e77cab684c0c48717b2f8e2f` (parents `cb77f899` +
`1d319ee5`, arbre `4bc4bc9b` = tête gelée du paquet h-2100) ; branche
`integration/paquet-h-20261016-2315`. Exécuteur désigné :
**mty-amh-impl-01** (composition, CI et fusion), route pilote mesh-design
17/10 00:11 (mail « prochain véhicule après 1534 »). Gels et avis des lots
conservés (contenus constants, §11bis/§12) ; aucune nouvelle relecture ; aucune
CI de lot ; PR1534 close non rouverte.

La colonne « commit de composition » porte la preuve d'inclusion : chaque lot a
son commit dans la branche (`git log --first-parent`).

## Admissions retenues à la coupure

#2120 (tête `0f1277cbd15d`, GO natif Codex, prérequis #2022 atterri, admission
pilote 23:11) ; #2151 (remise `ac7a862ff88b`, GO Codex, incident #1770) ;
#2142 (gel `50be3e812175`, classe légère) ; #1628 (remise `3d11bf184558` sur
main, admission pilote « 1628 déjà admis », chaîne `e8733132d` non reprise) ;
#2094 (gel `439194d2892e`, GO Codex natif, admission pilote 23:16).

## Composition

| unité | lot | source | commit de composition | note |
|---|---|---|---|---|
| 1 | #2120 | `0f1277cbd15d` | `c96b9b9e1` | `parse_count` borné (limite de chiffres Python) — auteur ameesh-dev5, GO natif Codex conservé sur la tête gelée, prérequis #2022 (paquet h-2100) atterri ; merge-tree main × tête propre (relevé dev5, reconfirmé à la composition) |
| 2 | #2151 | `ac7a862ff88b` | `c7fcc8f01` | Fixtures de carte bornées, test seul (+35/−2, `tests/test_l161e_map.py`) — auteur mty-amh-impl-08, avis Codex GO 0B/0N sur le delta seul, preuve VM auteur 34 tests 18.802 s contre 1437 s |
| 3 | #2142 | `50be3e812175` | `4c52bfdb2` | `work resolve --done` idempotent + nettoyage piloté depuis le worktree principal — auteur deepseek4, classe légère, aucune porte de fusion |
| 4 | #1628 | remise `3d11bf184558` | `8307b0f8b` | 2 N de #1604 (`statusline_key` avec nom d'agent, drapeau d'exception local dans `cmd_hook`) — auteur ameesh-dev5, GO dev7 conservé ; remise sur main `ac7bfdb23`, ancienne chaîne `e8733132d` non reprise |
| 5 | #2094 | `439194d2892e` | `c9b1f593a` | Témoin « acteur inconnu » mockant `close_merged`, réellement appelé après la garde (#1286) — auteur mty-amh-impl-04, GO Codex natif 0B/0N, produit inchangé, preuve auteur rouge/vert +57 tests retenue sans rejeu |

## Retiré de ce véhicule

- **#1914** (sensible concurrence/verrous) : tête gelée `de95fa7d`, puis
  correction du relecteur mesh-design `671da314` poussée à 22:47Z (le relecteur
  devenu auteur de sa correction). Pour un lot sensible, le §2 exige la
  relecture d'une seconde famille sur la correction : non acquise à la coupure
  (`reviewer` = mesh-design seul). Retiré jusqu'à résolution (§12), candidat au
  prochain véhicule avec cet avis. Signalé au pilote.

## Contrôles de composition

- Union sur `fbb8eb581` : quatre unions, un seul conflit — `src/ameesh/cli.py`
  (#1628 × main), résolu par la résolution rerere enregistrée : garde de main
  `_Remise(cfg, bk, name, binding)` + drapeau local `remise_ok` de #1628
  (`BaseException`). Delta vs `main` = strictement les 4 fichiers de #1628 ;
  delta vs tête #1628 = strictement les apports de main (#811 `cfg`/`projects`,
  etc.).
- `git diff --check` propre, aucun marqueur de conflit, **aucune migration**
  dans le delta.
- Delta total vs `fbb8eb581` : 10 fichiers, +356/−27 —
  `src/ameesh/{cli,entry,fastmail,resolutions,resources}.py`,
  `tests/{test_1003_merge_pr,test_1604_hook_rapide,test_2022_host_zombies,test_l160d_resolutions,test_l161e_map}.py`.
- Contrôle d'inclusion `scripts/paquet-inclusion.py --manifest … --head c9b1f593a
  --expected 2120,2151,2142,1628,2094` : **5 lots attendus, 5 présents, 0 à
  lire, aucun défaut détecté**.

## Témoins ciblés sur l'arbre d'union (psql + psycopg)

`tests.test_2022_host_zombies`, `tests.test_2022_fleet`, `tests.test_l161e_map`,
`tests.test_l160d_resolutions`, `tests.test_l160e_merge_queue`,
`tests.test_l160e2_land`, `tests.test_l160g_migration_reservations`,
`tests.test_1651_ci_parts`, `tests.test_l750_release`,
`tests.test_1604_hook_rapide`, `tests.test_1003_merge_pr` —

- psql : **362 OK, 13 sauts** (246,2 s), rc=0 ;
- psycopg : **362 OK, 13 sauts** (298,2 s), rc=0.

Sauts environnementaux (« coverage requis » dans `test_l161e_map`), sans
rapport avec le delta. Aucune CI de lot ; une seule CI complète sur la tête
gelée du paquet (§12).

## Gel / CI / fusion

- Tête gelée et PR : `integration/paquet-h-20261016-2315` → `main`, tête
  `c9b1f593a` (composition) + manifeste ; base `main` `fbb8eb581`.
- Feu pilote demandé sur cette tête exacte ; fusion auto
  (`gh pr merge --auto --match-head-commit`) à la CI verte, aucun fichier
  `.github/workflows/**` au delta (repli `merge-pr` sans objet).
- Une seule CI : run PR sur la tête exacte ; aucune CI de lot.
