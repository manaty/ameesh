# Paquet integration/paquet-h-20261019-1000 — composition (§12)

Base : `main` **`1a8d751b7f54`** (release 1.13.0 landing, commit du 19/10
09:00:46 UTC). Première composition sur `8bfa0974c` (paquet h-0900), puis mise
à jour du paquet en `be13f6543c9d` (fusion de la nouvelle cible, sans
conflit). Branche `integration/paquet-h-20261019-1000`, cible `main`. Une seule
suite complète est demandée sur la tête finale.

Demandes : composeur, paquet horaire §11 bis — `main` a bougé à `8bfa0974c`
(h-0900 fusionné) ; liste calculée `paquet-candidats.py` à 08:38 UTC sur cette
base (23 candidats, 4 prêts, 0 erreur), 4 unités composées à 08:40. La cible a
ensuite reçu la release 1.13.0 (`1a8d751b7`) ; le paquet a été mis à jour sans
conflit (`be13f6543`) et la liste recalculée à 09:05 UTC sur la nouvelle base
(23 candidats, 5 prêts, 18 en attente, 0 erreur). Entrées : la liste calculée
elle-même (#2404) ; #2682 devient prêt après la composition (voir
« Écartés ») ; #2466 signalé par le pilote pour le paquet suivant, mais refusé
par la porte du gel (voir « Écartés »).

## Admissions (4 unités, 4 lots portés)

- #2520 — CI ameesh : rétention de l'artefact carte de 30 à 3 jours
- #2668 — désignation au gel : départager les familles éligibles selon le rythme
- #2673 — clone radar : sans `ameesh.target`, la cible par défaut suit
  `origin/HEAD` du checkout source (release/1.11.0 depuis le 16/10) — 13 lots
  classés « release »
- #2488 — N 1955 (dev2) : `exploitation.stop` audite la résolution d'alias
  avant l'arrêt

## Composition (4 unités)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #2520 | `4f2519bf3ae4` (tête gelée) | `4b5026370b8e` | sensible — 2 avis (cx3 Codex, dev7 Claude) |
| 2 | #2668 | `a93edd65f778` (tête gelée) | `17d27ccd0eb0` | léger — 3 avis au journal (dev7, mesh-design, cx3) |
| 3 | #2673 | `ccd7b017803f` (tête gelée) | `51d482c1642e` | sensible — 2 avis (dev1 Claude, cx1 Codex) |
| 4 | #2488 | `3c7c54912a65` (tête gelée) | `8f38f2e5dd12` | sensible — 2 avis (dev4 Claude, mty-amh-qa-01 DeepSeek) |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot
et son SHA, et il porte un trailer `ameesh-work: <lot>` (relu par
`scripts/paquet-compose.py --units` : 4 unités, 4 lots portés). Aucun conflit
de composition, aucune résolution ; aucune migration SQL.

Mise à la cible : `be13f6543c9d` fusionne `main` `1a8d751b7` (release 1.13.0)
dans le paquet ; aucun conflit (seuls `pyproject.toml` et
`docs/versions/1.13.0.md` bougent). `paquet-compose.py --units --base
1a8d751b7` relu après la mise à jour : 4 unités, 4 lots portés.

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #2682 | prêt mécaniquement sur le gel final `986bbb550c4f` (classe light, avis cx3 seul) ; la désignation manuelle du pilote (dev4 Claude + cx3 Codex, deux familles) n'a pas de verdict dev4 sur cette tête (son `blocked` porte sur `f3440851e`) — exclu de ce véhicule, relecture dev4 attendue |
| #2629, #2653 | `missing-branch` : lots de canon (home), sans branche ameesh à composer |
| #2466 | `freeze-after-verdict` : le gel `25ccd438` suit les avis sur `abc2fbf` ; la dispense de re-verdict du pilote est enregistrée mais le lecteur des candidats (`plan_git.freeze_candidate`) l'ignore (#2681) ; admission explicite du pilote prévue pour h-1100, pas ce véhicule |
| #2562 | `freeze-after-verdict` |
| #286, #114 | `blocked-verdict` |
| #2451 | `conflict` avec `main` : `scripts/garde-mecanique.py`, `tests/support.py` |
| #2443 | `freeze-after-verdict` : nouvelle tête `47bf93cc` (après main `8bfa0974`) sans gel ni avis dev4 ; la résolution fraîche `fb3eb8a9` (#2466 × #2443, arbre identique à `bb88db0a`) est prête pour le paquet suivant avec #2466 |
| #2585 | `verdicts-missing` (cx1 seul) + conflit `src/ameesh/review_lot.py` |
| #2648 | `verdicts-missing` sur `7e0db389ceb2` (aucun avis) |
| #2511 | `verdicts-missing` sur `0a5231d8ae98` (cx1 seul) |
| #2613 | `verdicts-missing` sur `0183415640a7` (cx1 seul) |
| #2623 | `verdicts-missing` sur `7c7a32cff8ca` (cx2 seul) |
| #2539 | `verdicts-missing` sur `701cba88e60b` (cx3 seul) |
| #2582 | `verdicts-missing` sur `a200d5e6b2d0` (aucun) |
| #2538 | `verdicts-missing` sur `e7834ed960b7` (cx3 seul) |
| #2455, #2459 | `target-mismatch` + `branch-not-published` : branches canon hors de la cible |
| #1022 | `freeze-unresolved` + conflit sur `src/ameesh/migration_reservations.py` et al. |

## Contrôles

- Liste calculée `scripts/paquet-candidats.py` à 08:38 UTC, base
  `8bfa0974cd4a` : **23 candidats, 4 prêts, 19 en attente, 0 erreur**
  (`--ready-out`), 4 unités composées. Recalcul à 09:05 UTC sur la base
  `1a8d751b7f54` : **23 candidats, 5 prêts, 18 en attente, 0 erreur** — les 4
  composés restent prêts, #2682 devient prêt après la composition et est
  écarté avec motif (voir « Écartés »).
- Composition : **4 unités, 0 conflit** ; 20 fichiers, +1019/−39 hors
  manifeste (identique après la mise à la cible `be13f6543`) ; `git diff
  --check` vert, aucun marqueur de conflit, aucune migration SQL.
- `paquet-inclusion` (tête composée `be13f65436a9`, ce manifeste ajouté
  au-dessus, `--ready-file` recalculée) : **rc 0** — 4 admissions,
  4 compositions, 0 omission, 0 défaut ; contenus `présent` pour les 4
  unités ; #2682 seul écarté de la liste prête, motif lu. La section
  « Admissions » du manifeste fournit la liste attendue (pas de
  `--no-expected`) ; contrôle relancé à la tête finale.
- `scripts/garde-mecanique.py --json` : **0 constat**, 0 exemption inutilisée, 9 exemptions utilisées (toutes au registre).
- `scripts/doc-links.py` : 171 fichiers, **0 lien cassé**.
- Tests ciblés sur l'arbre composé : **74 tests OK par pilote** (psql 57 s,
  psycopg 42 s), 0 échec, 0 erreur — modules
  `test_2668_designation_pace`,
  `test_2447_review_designation_activation::ActivationTest::test_the_freeze_prefers_the_raw_plan_pace_over_declared_multipliers`,
  `test_2673_radar_clone_target`,
  `test_423_agent_aliases::SessionCommandAliasTest::test_stop_by_alias_bad_reason_has_no_side_effects`,
  `test_2404_paquet_candidats` (les deux pilotes DB, base jetable).
- Suite complète unique demandée sur la tête finale (`ci-full-<sha>`), avec la
  revue unitaire du paquet pour seul avis de la demande de CI (§12,
  `--full --avis 0`).
