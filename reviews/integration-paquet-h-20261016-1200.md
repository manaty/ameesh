# Paquet integration/paquet-h-20261016-1200 — composition scriptée (bureau, 16/10/2026 12:07)

Base : origin/main `20aa67782`. Tête : `1f659af84`. Inclus : 14. Écartés : 61.

## Inclus

- #804 Le tirage du lot se fait par l'exécuteur de l'agent, pas par notify — mty-amh-impl-06, tête gelée `7042bfd2e`, verdict ok de ameesh-dev1
- #876 #845 follow-up: les agregats always() de tests.yml laissent un job tests queued  — mty-amh-qa-01, tête gelée `007a39437`, verdict ok de auditeur ; conflit reporté depuis 76526d4d6
- #1917 test_turn_grace SousMoissonneurTest : l'attente 'done' (40 s) echoue sous charge — ameesh-cx1, tête gelée `f83573be0`, verdict ok de mty-amh-qa-01
- #806 Suite #724 : couvrir la reprise de session DeepSeek de bout en bout (le test ne  — mty-amh-impl-08, tête gelée `2ac984882`, verdict ok de ameesh-cx1
- #1393 CI flux : isoler les runs et borner le sweep — mty-amh-impl-06, tête gelée `bfa0cee4a`, verdict ok de ameesh-cx3
- #1483 CI-NEXLINK-WAKE : suites P3 de la R2 Claude #1110 (rattrapage sur les branches d — ameesh-dev5, tête gelée `2c67b2565`, verdict ok de ameesh-dev4 ; conflit reporté depuis ba29c5b1e
- #1533 #1462 (L87b) : trois N de la R2 DeepSeek — L232a sous pause, trim/brut 0441, pré — mty-amh-impl-08, tête gelée `06b64f76f`, verdict ok de ameesh-dev1
- #1615 #1396 : N de la relecture Claude — mty-amh-impl-06, tête gelée `81cae700c`, verdict ok de ameesh-dev1
- #1646 courrier --lot : une branche citée (celle du parent d'un lot empilé) est posée c — mty-amh-impl-08, tête gelée `5e437cdf1`, verdict ok de codex2
- #1729 #1645 étude : --json encore listée comme option d'identifiant conservée + commen — mty-amh-impl-06, tête gelée `61cdbbef9`, verdict ok de mesh-design
- #1787 #1026 : N de la relecture Claude — mty-amh-impl-07, tête gelée `64f7c52e0`, verdict ok de ameesh-dev4
- #1860 #1474 N suite : alias par affectation (e = sys.exit ; e()) et from os import \*  — mty-amh-impl-02, tête gelée `ce762ed74`, verdict ok de ameesh-dev2
- #1992 Banc : les tests LISTEN/NOTIFY ne sont pas isolés entre exécutions concurrentes  — mty-amh-impl-06, tête gelée `032d32982`, verdict ok de mesh-design
- #1995 #1937 : superseded_by « --3 » ou « ² » lève ValueError via _as_int au lieu d'un  — mty-amh-impl-06, tête gelée `67241244f`, verdict ok de mesh-design

## Écartés (coupure suivante)

- #753 Stopping an agent leaves its lots with it: reassign or send them back to the tea — deepseek5 : conflit : docs/EXECUTEUR-MEDIEE.md src/ameesh/mediated_executor/contract.json src/ameesh/storage/postgres/operations.py
- #1466 Comptes : la configuration n'est pas relue par les exécuteurs (agents en pause s — mty-amh-impl-05 : conflit : docs/EXPLOITATION.md src/ameesh/alert_briefs.py src/ameesh/exploitation.py src/ameesh/notify.py
- #1580 Comptes : consommer d'abord le budget qui sera perdu au reset, et proposer les b — mty-amh-impl-03 : tête retirée d'un paquet précédent (paquets/retires.tsv)
- #1677 Prise automatique aveugle : lots bloqués par des lots en qa jamais pris, inactiv — mty-amh-impl-07 : conflit : docs/EXECUTEUR-MEDIEE.md src/ameesh/mediated_executor/contract.json src/ameesh/sous_utilisation.py src/ameesh/storage/postgres/work.py
- #1768 Porte : un paquet Nexlink (integration/**) n'est vert qu'avec un statut lot/* en — ameesh-dev7 : conflit : src/ameesh/land.py src/ameesh/merge_pr.py src/ameesh/merge_queue.py src/ameesh/radar.py
- #1994 #1937 post-fusion : le raccord B/C perd l'acteur des jalons — tout verdict réel  — mty-amh-impl-08 : conflit : src/ameesh/process_registry.py
- #423 L243b RENAME-AGENTS: rename existing agents to <org>-<repo>-<role>-<seq>, old na — mty-amh-impl-05 : conflit : docs/EXECUTEUR-MEDIEE.md src/ameesh/assignments.py src/ameesh/mediated_executor/contract.json src/ameesh/runner.py
- #498 L76c: per-turn delegated cgroup v2 (harness, tools, reparented descendants): sta — mty-amh-impl-02 : tête gelée sans verdict, contenu différent de la tête relue
- #508 Runner: per-persona liveness without a lease (last_seen with host), so deploy ch — mty-amh-impl-07 : conflit : docs/EXECUTEUR-MEDIEE.md src/ameesh/mediated_executor/contract.json src/ameesh/storage/postgres/registry.py
- #711 L122: scoped migration reservations (slot, target repo/branch, targetSha, status — mty-amh-impl-05 : conflit : src/ameesh/storage/postgres/work.py tests/test_l160g_migration_reservations.py
- #793 canon sync: concurrent syncs on one host each push the changed Process rules to  — auditeur : conflit : docs/EXECUTEUR-MEDIEE.md src/ameesh/mediated_executor/contract.json
- #800 Aucune autre famille de modele imposee pour le relecteur : avertir quand le rele — mty-amh-impl-01 : conflit : docs/EXPLOITATION.md docs/PLAN-DE-TRAVAIL.md src/ameesh/alert_briefs.py src/ameesh/exploitation.py
- #811 L243-4: work directory per lot project for multi-project agents, session cwd fro — mty-amh-impl-04 : tête bâtie sur un autre paquet : 50 commits de composition absents de la base (rebaser sur main)
- #917 stale_lot d'un lot sans assigne : le brief part a tous les orchestrateurs, toute — mty-amh-impl-05 : conflit : src/ameesh/alert_briefs.py src/ameesh/stagnation.py src/ameesh/storage/interface.py src/ameesh/storage/postgres/operations.py
- #1104 L539 : une fusion manuelle hors file n'ouvre pas le lot de relecture après fusio — mty-amh-impl-07 : conflit : docs/EXECUTEUR-MEDIEE.md src/ameesh/mediated_executor/contract.json src/ameesh/plan_git.py src/ameesh/review_lot.py
- #1132 Jauges Claude : un relevé périmé à reset passé remplace la lecture fraîche et fa — mty-amh-impl-05 : conflit : src/ameesh/accounts.py src/ameesh/cost.py src/ameesh/gauge_cache.py src/ameesh/mediated_executor/contract.json
- #1152 Redémarrage hors tour : course entre status idle et lancement réel du harnais — mty-amh-impl-04 : conflit : deploy/mise-a-jour/poste.sh deploy/mise-a-jour/vm.sh docs/DEPLOIEMENT-HOTE-AGENTS.md docs/ORCHESTRATEUR.md
- #1161 #1110 suite CI-PENDING : une execution en attente d'un groupe de concurrence (pe — deepseek5 : conflit : src/ameesh/ci.py
- #1283 CI #1236 : tests d'adoption voient une session fictive dans un processus DeepSee — mty-amh-impl-08 : tête bâtie sur un autre paquet : 12 commits de composition absents de la base (rebaser sur main)
- #1343 CI ameesh déclenchée au gel, pas à chaque poussée (relecture avant CI) — mty-amh-impl-04 : conflit : .github/workflows/tests.yml tests/test_l161f_plan.py
- #1359 Limite d'en-cours : au plus 2 lots en build par agent, surplus rendu à la file — mty-amh-impl-06 : conflit : docs/EXECUTEUR-MEDIEE.md docs/PLAN-DE-TRAVAIL.md src/ameesh/backlog.py src/ameesh/handoff.py
- #1374 Fusion automatique : feu au gel, atterrissage automatique à la CI verte (ameesh) — mty-amh-impl-08 : tête retirée d'un paquet précédent (paquets/retires.tsv)
- #1463 L87c : accusé de réception persistant d'une alerte (API et CLI) — ameesh-cx2 : conflit : docs/EXECUTEUR-MEDIEE.md src/ameesh/mediated_executor/contract.json src/ameesh/serve_actions.py tests/test_cluster.py
- #1465 Relecteur qui corrige et fusionne dès le premier tour : consignes et outillage — mty-amh-impl-07 : conflit : src/ameesh/storage/interface.py src/ameesh/storage/postgres/work.py src/ameesh/work.py
- #1476 CI ameesh sous 15 min, même en suite complète — mty-amh-impl-04 : conflit : .github/workflows/carte.yml docs/EXECUTEUR-MEDIEE.md src/ameesh/mediated_executor/contract.json
- #1635 CI : un workflow_dispatch de tests.yml sur une branche de lot publie un check te — ameesh-dev1 : tête gelée sans verdict, contenu différent de la tête relue
- #1739 Prise auto : l'historique d'attribution (30 j, #740) écarte presque tous les age — ameesh-dev5 : conflit : src/ameesh/backlog.py
- #831 #802 follow-up: work_packages writes outside the sync lock (rebase_default), per — mty-amh-impl-01 : conflit : docs/EXECUTEUR-MEDIEE.md src/ameesh/mediated_executor/contract.json
- #897 Suites #423 (N1/N2) : contrat leases.state et trigger key_holders (nom primaire  — mty-amh-impl-05 : conflit : src/ameesh/assignments.py src/ameesh/canon_sync.py src/ameesh/hostcheck.py src/ameesh/mediated_executor/contract.json
- #920 Suite #835 : temoins lock_timeout robustes sous charge (attente_vue 5 s -> 30 s) — mty-amh-impl-05 : conflit : tests/test_migrations.py
- #1022 #711 N (bureau) : a la fusion, le creneau passe en released au lieu de landed — mty-amh-impl-05 : tête gelée sans verdict, contenu différent de la tête relue
- #1286 work merged : le SHA declare n'est rattache a aucun lot — un numero de PR homony — ameesh-cx3 : tête retirée d'un paquet précédent (paquets/retires.tsv)
- #1328 #845 reserve N (R2 cx3) : retirer storage.guard_runs() devenu inutilise et le co — mty-amh-impl-05 : conflit : docs/EXECUTEUR-MEDIEE.md src/ameesh/mediated_executor/contract.json
- #1349 ameesh agent peek <nom> : résumé lisible de ce que fait un agent en ce moment — ameesh-cx3 : conflit : docs/EXECUTEUR-MEDIEE.md docs/api/openapi-v1.json src/ameesh/mediated_executor/contract.json src/ameesh/runner.py
- #1416 #1256 : regrouper les six durcissements non bloquants (N1-N6) du prêt de runners — ameesh-dev5 : tête retirée d'un paquet précédent (paquets/retires.tsv)
- #1425 Suite R2 #753 (N1-N5) : temoins stop/balayage, ordre des gardes, TOCTOU, nom abs — mty-amh-impl-03 : conflit : docs/EXECUTEUR-MEDIEE.md src/ameesh/mediated_executor/contract.json src/ameesh/runner.py src/ameesh/storage/postgres/operations.py
- #1508 #1374 N1 (avis dev4) : docstring de fire._target_lock fausse sur le retour quand — ameesh-dev4 : tête empilée sur #1509 non fusionné (rebaser sur main, ou attendre son paquet)
- #1566 #1561 : les 2 N (P3) de la relecture Claude — fraîcheur avant L226b, fenêtre dat — ameesh-dev3 : conflit : docs/design/etudes/index.md src/ameesh/mesh_cli.py
- #1573 ci.py (#901 suites) : relance courte finie entre deux passages non rouverte par  — ameesh-dev5 : conflit : docs/EXECUTEUR-MEDIEE.md src/ameesh/ci.py src/ameesh/mediated_executor/contract.json tests/test_l161c_failures_realtime.py
- #1625 Prise automatique : un lot sans assigne qu'une attente retient bloque encore la  — ameesh-dev7 : conflit : docs/design/decisions/0037-jamais-a-l-arret.md
- #1628 #1604 : les 2 N de la relecture (statusline_key sans nom d'agent, drapeau d'exce — ameesh-dev5 : tête bâtie sur un autre paquet : 49 commits de composition absents de la base (rebaser sur main)
- #1631 installateur-bootstrap layout(): mkdir sans chmod, une installation neuve sous u — mty-amh-impl-05 : conflit : deploy/mise-a-jour/poste.sh
- #1648 #1580 N (avis dev4) : jauges du widget préférées à un journal plus frais, toléra — mty-amh-impl-08 : tête gelée sans verdict, contenu différent de la tête relue
- #1661 #1649 N1b : claude_gauges() sans compte rend encore une liste nue (cost.py:766) — mty-amh-impl-05 : conflit : src/ameesh/cost.py src/ameesh/gauge_cache.py
- #1664 Alertes stale : une porte humaine/pilote sans lot ouvert ne peut pas éteindre la — mty-amh-impl-03 : conflit : docs/PLANNED-WAITS.md docs/WORK-DEPENDENCIES.md src/ameesh/planned_waits.py src/ameesh/progress_page.html
- #1686 attach --wait : le budget de passation court pendant l'attente ; un tour differe — mty-amh-impl-06 : conflit : src/ameesh/runner.py tests/test_l82_attach.py
- #1693 #759 : N de la relecture Claude — mty-amh-impl-05 : tête retirée d'un paquet précédent (paquets/retires.tsv)
- #1720 Jauges Claude : read_at d'un flux sans marqueur = mtime du journal, surestime la — mty-amh-impl-03 : conflit : src/ameesh/accounts.py src/ameesh/cost.py src/ameesh/mesh_cli.py src/ameesh/reprise.py
- #1726 #1677 : un relecteur désigné (colonne reviewer, 0421) sans lot à lui compte comm — ameesh-dev5 : conflit : docs/EXECUTEUR-MEDIEE.md src/ameesh/backlog.py src/ameesh/mediated_executor/contract.json src/ameesh/sous_utilisation.py
- #1738 #1677 : les 5 N (P3) de la relecture Claude — repos remis à zéro, interrupteur,  — ameesh-dev5 : conflit : src/ameesh/backlog.py src/ameesh/notify.py src/ameesh/storage/interface.py src/ameesh/storage/postgres/work.py
- #1772 #1749 N1 : plan_git.probe ne rapatrie plus un commit absent d'un clone partiel ( — mty-amh-impl-03 : conflit : src/ameesh/plan_git.py
- #1776 Après revert : couvrir le raccourci land et les gels sans diff propre — mty-amh-impl-05 : conflit : src/ameesh/canon.py src/ameesh/plan_git.py
- #1777 #1053 : N de la relecture Claude — mty-amh-impl-07 : conflit : docs/EXECUTEUR-MEDIEE.md src/ameesh/mediated_executor/contract.json tests/test_1037_bascule_harnais.py
- #1789 #1580 : N de la relecture Claude — ameesh-dev5 : conflit : docs/COMPTES.md src/ameesh/accounts.py src/ameesh/mesh_cli.py src/ameesh/reprise.py
- #1813 plan_github N3 : compteurs du cache écrits seulement à la requête réelle suivant — mty-amh-impl-05 : tête gelée 961cd4a6 introuvable
- #1822 Attente fantome « verdict » sur les lots legers dont le pilote a route l'avis AP — mty-amh-docs-01 : conflit : src/ameesh/exploitation.py src/ameesh/mesh_cli.py src/ameesh/stagnation.py src/ameesh/storage/postgres/operations.py
- #1878 test-plan : un module sys/os renommé par affectation (s = sys ; s.exit()) ou get — mty-amh-impl-02 : tête bâtie sur un autre paquet : 50 commits de composition absents de la base (rebaser sur main)
- #1899 Triage stale : branch_head du registre (cache du balayage) pris pour la tête rée — mty-amh-impl-08 : tête gelée sans verdict, contenu différent de la tête relue
- #1904 L242c (#414) : deux N du bureau (commentaire orphelin, docstring fail-closed) — mty-amh-docs-01 : conflit : tests/test_l242c_critical_roles.py
- #1965 L39 : reprise.adopt refuse toute adoption quand un gpg-agent de l'utilisateur re — mty-amh-docs-01 : conflit : tests/test_l39_adoption_reprise.py
- #1979 ameesh work reviewer : pouvoir retirer une désignation (ou la marquer « verdict  — mty-amh-impl-02 : conflit : docs/EXPLOITATION.md src/ameesh/storage/postgres/work.py
