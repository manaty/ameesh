# Paquet integration/paquet-h-20261016-1700 — composition r2 (mty-amh-impl-01, 16/10/2026 19:0xZ)

Recomposition après la CI rouge du run `38941239652` sur l'ancienne tête
`16ec0d9356b35fbcb22fb75fba488a6772aca105` : retrait des lots fautifs sur attribution de première main du composeur
(courrier bureau 18:05Z, arbitrage pilote mesh-design 18:08Z), diagnostic
reporté au paquet suivant. Base : `origin/main` `dffe30ea`. Branche : `integration/paquet-h-20261016-1700`.
Tête de composition : `06e55d232`. Exécuteur désigné : **mty-amh-impl-01** (composeur,
CI et fusion). Unités incluses : 18. Gels et avis des lots conservés
(contenus constants, §11bis/12) ; aucune nouvelle relecture.

La colonne « commit de composition » porte la preuve d'inclusion : chaque lot a
son commit dans la branche (`git log dffe30ea..06e55d232`). Les résolutions d'union sont
signées dans le message du commit correspondant.

## Admissions retenues à la coupure

#2061, #1631, #1898, #1683, #1777, #935, #1328, #1661, #1686, #1965, #1776, #1416, #916, #1904, #1915, #1635, #793, #1899

## Composition

| unité | lot | source | commit de composition | note |
|---|---|---|---|---|
| 1 | #2061 | `df41347e9e90fb85b449cbe617fd78186dbb06b4` | `2a68088b5` | #1770 compagnon : carte CI en 6 parts psycopg et 4 psql pour tenir le plafond de 40 min — mty-amh-impl-08, verdict ok de mesh-design |
| 2 | #1631 | `bc41ac54dd482485b615882ff4f7141d4f1f8906` | `15e85173c` | installateur-bootstrap layout(): mkdir sans chmod, une installation neuve sous umask 077… — mty-amh-impl-05, verdict ok de ameesh-dev4 |
| 3 | #1898 | `36debc111122bbc9722281629c9fdd793d015a61` | `b8f14de58` | #794 : N de la relecture Claude — mty-amh-impl-04, verdict ok de ameesh-dev4 |
| 4 | #1683 | `c78ea67d3dda57c9c2390561a7199704e0746b41` | `a02d6cc38` | #1483 N de la R1 dev4 : base_failures par concluded_at, ci.yml générique écarté, union al… — mty-amh-impl-07, verdict ok de mty-amh-impl-07 et mesh-design |
| 5 | #1777 | `a60eb2ee` | `4948dda5e` | #1053 : N de la relecture Claude — mty-amh-impl-07, verdict ok de ameesh-dev4 |
| 6 | #935 | `cd9b9b3058cfa3639b73880acb5bc1ae8a6e248f` | `b0e028fda` | Témoin balance_low de la prise par l'exécuteur : tester le refus d'un candidat libre (P3… — mty-amh-docs-01, verdict ok de ameesh-dev4 |
| 7 | #1328 | `da5195c63` | `4d8972a77` | #845 reserve N (R2 cx3) : retirer storage.guard_runs() devenu inutilise et le commentaire… — mty-amh-impl-05, verdict ok de mesh-design |
| 8 | #1661 | `243334375` | `e5158f1a5` | #1649 N1b : claude_gauges() sans compte rend encore une liste nue (cost.py:766) — mty-amh-impl-05, verdict ok de mesh-design |
| 9 | #1686 | `46e06086447575207aa32aae15c98f214f3af646` | `09c9e5e98` | attach --wait : le budget de passation court pendant l'attente ; un tour differe compte c… — mty-amh-impl-06, verdict ok de ameesh-dev4 |
| 10 | #1965 | `d9268583e` | `fd29325ac` | L39 : reprise.adopt refuse toute adoption quand un gpg-agent de l'utilisateur rend les fi… — mty-amh-docs-01, verdict ok de mesh-design |
| 11 | #1776 | `4d3e509d77b7db2985e64eef0e91449b554353af` | `ffc6f2fbc` | Après revert : couvrir le raccourci land et les gels sans diff propre — mty-amh-impl-05, verdict ok de ameesh-dev4 |
| 12 | #1416 | `5d315ff30` | `3fc6ade84` | #1256 : regrouper les six durcissements non bloquants (N1-N6) du prêt de runners CI — ameesh-dev5, verdict ok de ameesh-cx2 |
| 13 | #916 | `c9e6dc6cc61f7e14a027067b92eb650c0205d822` | `70d090745` | ACTIVATION-FULL-SUITE : declarer full_suite dans cfg.radar (pc-smichea, agents-1/-2) avan… — mty-amh-qa-01, verdict ok de auditeur |
| 14 | #1904 | `305074f08` | `aa3885d02` | L242c (#414) : deux N du bureau (commentaire orphelin, docstring fail-closed) — mty-amh-docs-01, verdict ok de ameesh-dev4 |
| 15 | #1915 | `ddff5504020dd21204bcfd651dcd99881130714d` | `f9f32f698` | Workflows Process liés à une révision, avec reçus d'étape natifs (opt-in par projet) — de… — mty-amh-impl-02, verdict ok de bureau |
| 16 | #1635 | `1fe796bbeb` | `da77b5861` | CI : un workflow_dispatch de tests.yml sur une branche de lot publie un check tests vert… — ameesh-dev1, verdict ok de mesh-design |
| 17 | #793 | `9e10d58fcb` | `fb17215f3` | canon sync: concurrent syncs on one host each push the changed Process rules to every age… — auditeur, verdict ok de mty-amh-impl-01 et ameesh-dev3 |
| 18 | #1899 | `430064d2650b06be8533ec712002a56d7e853f28` | `6bbc10e8f` | Triage stale : branch_head du registre (cache du balayage) pris pour la tête réelle du lot — mty-amh-impl-08, verdict ok de mesh-design et mty-amh-qa-01 |

## Retirés de ce véhicule (diagnostic reporté au suivant)

- #1467 `0234aafab8` — train : test_1467_train_candidates (8 ERROR),
  test_1467_train_admissions (7), test_1680_packet_reservation (9 : `TRUNCATE
  merge_locks` bloqué par la FK `integration_trains`), test_cluster (28 colonnes
  `integration_train_fires` ni accordées ni exclues). 25 occurrences.
- #1343 `6626477618` et #1476 `f90bc4c6af` — plan/CI : test_l161f_plan (5 FAIL,
  plan composé 8 parts psycopg au lieu de 4). Union #1343×#1476×#2061/#1683
  incohérente ; correctif de conception renvoyé au paquet suivant.
- #1739 `07ed9e4570` — repli orchestrateur (`backlog.py:773`, `fallback: True`)
  contre L165/L804 (test_l804_prise_executeur.test_orchestrateur_local_ne_se_sert_pas) ;
  sa source précède l'arrivée de test_l804 dans main. Arbitrage de règle au
  pilote, hors composition.
- #2022 `0d1b9edc6` — test_l159a_capacity_guard : `max_zombies` non validé
  (`host-policy-invalid` absent).
- #811 `ad3e9990b` — test_1270_rate_limit_retry (3 ERROR, `AgentWorker.cfg`
  absent de la doublure ; `_pick` lit `self.cfg`) et test_l514_wake_on_event
  (2 FAIL ; `lot_workdir.py`).
- #423 `738a717d1` — test_705_reviewer_designated (ERROR `Mock` non
  subscriptable, `work.py:796 row["name"]`) et
  test_l242c.test_homonyme_etranger_jamais_orchestrateur (ERROR `'object' has no
  attribute 'query'`, `registry.resolve_name` médié).
- #1484 `768237114d` — test_expected_output : ligne 402 de
  `tests/test_l56_role_executor.py` sans préfixe `attendu(`.
- #1669 `e56c03675` — dépend de #1476 (source chaînée) ; retiré avec lui.
- #1359 `0d3056eff` — conservation impossible sans réparer une chaîne dépendante
  des retirés (conflits `work.py`/`progress.py` : alias #423, `_ci_suite_lines`
  #1476, `interface.py`) ; retrait mécanique (arbitrage pilote 18:08Z).
- #1465 `c9885d1b5` — table `provenance_receipts` : 8 colonnes absentes du
  contrat des rôles superviseur (`test_cluster.test_chaque_colonne_est_accordee_ou_exclue`,
  rejoué sur la tête après retrait de #1467 : les 20 colonnes du train sont
  parties, restent les 8 de `provenance_receipts`). Retiré sur le bilan final
  (arbitrage pilote 18:13Z : retirer les autres fautifs révélés et les nommer).
- #935 `cd9b9b305` — **conservé** : l'attribution initiale (test_l804) était
  erronée, corrigée par la lecture du code (`fallback` de #1739).

## Réparations de composition portées par la branche r2

- `#1631 × #1484` (`15e85173c`) : #1631 est conservé ; son delta est extrait
  (`cherry-pick -m 1`) et le test ajouté par #1484 (retiré) est écarté, seule
  la preuve propre à #1631 (`test_real_layout_publishes_reviewed_modes_under_a_restrictive_umask`)
  est gardée.
- `#1635 × #1476` (`da77b5861`) : l'union `tests.yml` de #1635 est rejouée sur la base
  de main (9 parts psql #1692), sans le découpage 12 parts de #1476 ; le nom
  dynamique `tests (dispatch)` et le portier restent.
- Retraits mécaniques : `d9a80c1a7` (réparations #1467/#423), `d77716c19`
  (`interface.py`, apporté par #1359), `33c798527` (#1359) non rejoués.
- `contract.json` : l'entrée refusée `operations.state_lots_activity`, classée
  par le compteur de #1359, est retirée avec lui (l'opération n'existe plus dans
  l'interface) ; compteurs dérivés **64/364/428**, `test_l107` 22 OK, rejoué.

## Contrôles de composition

- Rejeu first-parent `git cherry-pick -m 1` (résolutions d'union préservées),
  rerere actif ; aucun marqueur de conflit restant ; `git diff --check` propre.
- Compteurs du contrat médié redérivés sur l'arbre composé, `test_l107` rejoué.
- Contrôle d'inclusion `scripts/paquet-inclusion.py` sur ce manifeste.
- **Une seule CI complète** sur cette tête gelée (§12) ; aucune CI de lot ; le
  feu du pilote sur l'ancienne tête ne couvre pas cette nouvelle tête.

## Écartés du véhicule (inchangé)

- #1516 `c44f57c4`, #2008 `bd6931cb4`, #1600, #1648, #1693, #1592 (remplaçante
  en attente de GO) — motifs du manifeste 1700 d'origine, inchangés.
