# Paquet integration/paquet-h-20261016-1700 — composition scriptée (mty-amh-impl-01, 16/10/2026)

Base : `origin/main` `dffe30ea` (paquet horaire 1445 `f87803963` fusionné en `dffe30ea`).
Branche : `integration/paquet-h-20261016-1700`. Tête de composition : `04b5b0114`. Exécuteur désigné :
**mty-amh-impl-01** (composeur, CI et fusion ; désignation mesh-design 14:05,
confirmée par l'auditeur). Unités incluses : 29. PR : ouverte juste après le
commit de ce manifeste (tête poussée à suivre).

La colonne « commit de composition » porte la preuve d'inclusion : chaque lot a
son commit dans la branche (`git log dffe30ea..04b5b0114`). #1971+#1972 `19f1d44dc8` sont
déjà ancêtres de la base (aucun commit propre). Les résolutions d'union sont
signées dans le message du commit correspondant.

## Admissions retenues à la coupure

#2061, #1484, #1631, #1898, #1683, #1777, #811, #935, #1328, #423, #1661, #1686, #1669, #1965, #1776, #1465, #1467, #2022, #1416, #1635, #916, #1476, #1904, #1915, #1343, #793, #1359, #1739, #1899

## Composition

| unité | lot | source | commit de composition | note |
|---|---|---|---|---|
| 1 | #2061 | `df41347e9e90fb85b449cbe617fd78186dbb06b4` | `ea3cf671a` | #1770 compagnon : carte CI en 6 parts psycopg et 4 psql pour tenir le plafond de 40 min — mty-amh-impl-08, verdict ok de mesh-design |
| 2 | #1484 | `768237114de7bd812fc35ca1e7c1df00de2f08cc` | `a8ceca883` | L232b : sept réserves mineures du complément Claude #364 — mty-amh-impl-05, verdict ok de agent:ameesh-dev2 et ameesh-cx1 |
| 3 | #1631 | `bc41ac54dd482485b615882ff4f7141d4f1f8906` | `def2806b7` | installateur-bootstrap layout(): mkdir sans chmod, une installation neuve sous umask 077… — mty-amh-impl-05, verdict ok de ameesh-dev4 |
| 4 | #1898 | `36debc111122bbc9722281629c9fdd793d015a61` | `20bd14558` | #794 : N de la relecture Claude — mty-amh-impl-04, verdict ok de ameesh-dev4 |
| 5 | #1683 | `c78ea67d3dda57c9c2390561a7199704e0746b41` | `ce3bd0686` | #1483 N de la R1 dev4 : base_failures par concluded_at, ci.yml générique écarté, union al… — mty-amh-impl-07, verdict ok de mty-amh-impl-07 et mesh-design |
| 6 | #1777 | `a60eb2ee` | `c112a89a2` | #1053 : N de la relecture Claude — mty-amh-impl-07, verdict ok de ameesh-dev4 |
| 7 | #811 | `ad3e9990b0100417523106073307557e9acb0785` | `66cc89ca0` | L243-4: work directory per lot project for multi-project agents, session cwd from agent_l… — mty-amh-impl-04, verdict ok de mesh-design |
| 8 | #935 | `cd9b9b3058cfa3639b73880acb5bc1ae8a6e248f` | `6661e58b4` | Témoin balance_low de la prise par l'exécuteur : tester le refus d'un candidat libre (P3… — mty-amh-docs-01, verdict ok de ameesh-dev4 |
| 9 | #1328 | `da5195c63` | `e3d2452d8` | #845 reserve N (R2 cx3) : retirer storage.guard_runs() devenu inutilise et le commentaire… — mty-amh-impl-05, verdict ok de mesh-design |
| 10 | #423 | `738a717d1` | `2258cd0d1` | L243b RENAME-AGENTS: rename existing agents to <org>-<repo>-<role>-<seq>, old names kept… — mty-amh-impl-05, verdict ok de ameesh-cx2 |
| 11 | #1661 | `243334375` | `a95e501de` | #1649 N1b : claude_gauges() sans compte rend encore une liste nue (cost.py:766) — mty-amh-impl-05, verdict ok de mesh-design |
| 12 | #1686 | `46e06086447575207aa32aae15c98f214f3af646` | `bb5c08907` | attach --wait : le budget de passation court pendant l'attente ; un tour differe compte c… — mty-amh-impl-06, verdict ok de ameesh-dev4 |
| 13 | #1669 | `e56c036753565ca058dfd8ea1f92ae457bca1a3d` | `27285ca1c` | #1476 N (dev1) : historique de branche du lot — marqueur de conflit au commit intermediai… — mty-amh-impl-01, verdict ok de ameesh-cx1 |
| 14 | #1965 | `d9268583e` | `c890e90fa` | L39 : reprise.adopt refuse toute adoption quand un gpg-agent de l'utilisateur rend les fi… — mty-amh-docs-01, verdict ok de mesh-design |
| 15 | #1776 | `4d3e509d77b7db2985e64eef0e91449b554353af` | `4c707bf10` | Après revert : couvrir le raccourci land et les gels sans diff propre — mty-amh-impl-05, verdict ok de ameesh-dev4 |
| 16 | #1465 | `c9885d1b5` | `6617b1ef5` | Relecteur qui corrige et fusionne dès le premier tour : consignes et outillage — mty-amh-impl-07, verdict ok de agent:ameesh-dev2 et ameesh-cx2 |
| 17 | #1467 | `0234aafab8` | `625d59d34` | Train de paquets automatique : chaque heure, l'outil compose, valide et fusionne les lots… — ameesh-cx3, verdict ok de ameesh-dev4 et deepseek3 |
| 18 | #2022 | `0d1b9edc6` | `258613ac2` | Surveillance de la flotte : alerte précoce disque, zombies, VM de CI et serveur hors de «… — ameesh-dev2, verdict ok de mty-amh-docs-01 et ameesh-dev2 et ameesh-cx2 |
| 19 | #1416 | `5d315ff30` | `adcb17624` | #1256 : regrouper les six durcissements non bloquants (N1-N6) du prêt de runners CI — ameesh-dev5, verdict ok de ameesh-cx2 |
| 20 | #916 | `c9e6dc6cc61f7e14a027067b92eb650c0205d822` | `f8c1ddb33` | ACTIVATION-FULL-SUITE : declarer full_suite dans cfg.radar (pc-smichea, agents-1/-2) avan… — mty-amh-qa-01, verdict ok de auditeur |
| 21 | #1476 | `f90bc4c6af` | `5e9c22602` | CI ameesh sous 15 min, même en suite complète — mty-amh-impl-04, verdict ok de ameesh-dev1 |
| 22 | #1904 | `305074f08` | `d83af882d` | L242c (#414) : deux N du bureau (commentaire orphelin, docstring fail-closed) — mty-amh-docs-01, verdict ok de ameesh-dev4 |
| 23 | #1915 | `ddff5504020dd21204bcfd651dcd99881130714d` | `8485e516c` | Workflows Process liés à une révision, avec reçus d'étape natifs (opt-in par projet) — de… — mty-amh-impl-02, verdict ok de bureau |
| 24 | #1635 | `1fe796bbeb` | `46820bf18` | CI : un workflow_dispatch de tests.yml sur une branche de lot publie un check tests vert… — ameesh-dev1, verdict ok de mesh-design |
| 25 | #1343 | `6626477618` | `8526b25d5` | CI ameesh déclenchée au gel, pas à chaque poussée (relecture avant CI) — mty-amh-impl-04, verdict ok de ameesh-cx2 et ameesh-dev4 |
| 26 | #793 | `9e10d58fcb` | `9274e3ce7` | canon sync: concurrent syncs on one host each push the changed Process rules to every age… — auditeur, verdict ok de mty-amh-impl-01 et ameesh-dev3 |
| 27 | #1739 | `07ed9e4570` | `7a62c854d` | Prise auto : l'historique d'attribution (30 j, #740) écarte presque tous les agents au re… — ameesh-dev5, verdict ok de ameesh-dev1 |
| 28 | #1899 | `430064d2650b06be8533ec712002a56d7e853f28` | `de82663ce` | Triage stale : branch_head du registre (cache du balayage) pris pour la tête réelle du lot — mty-amh-impl-08, verdict ok de mesh-design et mty-amh-qa-01 |
| 29 | #1359 | `0d3056eff` (delta propre vs `4518c5292`) | `33c798527` | Limite d'en-cours : au plus 2 lots en build par agent, surplus rendu à la file — mty-amh-impl-06, transport par extraction (cherry-pick -m 1), verdict ok de ameesh-dev2 |

## Écartés de ce véhicule

- #1516 `c44f57c4` — attend l'attestation cx1 ; #2043 `b843de06ca` en dépend.
- #2008 `bd6931cb4` — non admis (relecture dev1 non rendue).
- #1600 `e5047ddab` / remise `d438e0287c74` — union ×base rc=1, remise due.
- #1648, #1693 — écartés (motif du registre ; #1693 exclu staging) ; #2009 reste
  inclus tel quel dans le paquet 1445.
- #1592 — ancienne tête `1140ff94` **VOID** (retrait pilote 15:47, témoin faux
  depuis l'atterrissage #1270) ; remplaçante `fca660b5` en attente du GO dev7,
  au prochain véhicule.

## Réparations d'union portées par la branche

- `#1467 × #1761` (`d9a80c1a7`) : la garde « pas de profil radar » de #1467 ne
  vaut que pour un hôte qui **déclare** `radar.projects` (`declared_projects`) ;
  sans projet déclaré, `queue add` garde le clone jetable testé de #1761. Test
  #1467 adapté (config avec un projet non correspondant) — écart à signer pilote.
- `#109 mediated × #423` (`d9a80c1a7`) : `agents.resolve_name` précède toute
  opération mediée (deux assertions de `test_l109_mediated_mode`).
- `test_1467_train_runner` (`d9a80c1a7`) : le gabarit du calendrier compilé est
  fourni au `SimpleNamespace` (le contrôle « merge policy » est bien atteint).
- `test_423_agent_aliases` (`d9a80c1a7`) : `reprise.holders` neutralisé (le banc
  ne lit pas `/proc` d'un autre processus du même uid) — garde éprouvée par
  `test_l39`/`test_1037`.
- `interface × #1359` (`d77716c19`) : `HANDOFF_PREFIX`/`HANDOFF_NOTE`/
  `HANDOFF_RETURN_NOTE` définis (textes du main « pause budget (reprise) : … »),
  `operations.state_lots_activity` classée refusée (`3ae7f388d`).

## Contrôles de composition

- Aucun marqueur de conflit restant ; `git diff --check` propre ; compilation
  Python de l'arbre composé ; balayage d'import des 287 modules sans échec.
- Compteurs du contrat médié dérivés sur l'arbre composé (#1517 : aucune clé
  `interface_total` dans `refused`) : 65 opérations / 378 refusées / 443 au
  total, `tests/test_l107_executor_contract.py` 22 OK.
- Contrôle d'inclusion `scripts/paquet-inclusion.py` (verdict consigné au tour
  de gel) ; tests ciblés des points d'union et modules neufs, deux pilotes
  (`tests-1700b-psql.log`, `tests-1700b-psycopg.log`).
- Une seule CI complète, sur cette tête gelée (§12) ; aucune CI de lot.
