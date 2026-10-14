# Relevé initial groupé — alertes `slo_breach` (lecture seule)

Total alertes : 88 — P1 (owner, urgent) : 15 — autres : 73
Par niveau : {'3': 74, '1': 13, '2': 1}
Lots distincts : 81

## P1 historiques (notifiés à l'owner, urgent)
| lot | titre | état | assigné | étape | niv | retard | cible | cibles | depuis |
|---|---|---|---|---|---|---|---|---|---|
| #364 | L232b: SSH release controller and delegated host hel | qa | mty-amh-impl-04 | verdict_merge | 3 | 18359s (cible 900s) | bureau, auditeur, owner | 2026-10-13 17:08Z |
| #414 | L242c CRITICAL-ROLES (#340 2c): critical agents deri | build | mty-amh-impl-07 | build_quiet | 3 | 15176s (cible 3600s) | bureau, auditeur, owner | 2026-10-13 17:16Z |
| #414 | L242c CRITICAL-ROLES (#340 2c): critical agents deri | build | mty-amh-impl-07 | verdict_merge | 3 | 30601s (cible 900s) | bureau, auditeur, owner | 2026-10-13 13:44Z |
| #645 | PARITY-ACTIVATE : activation de GRAPH_READ puis de l | blocked | orchestrateur | intake_started | 3 | 56175s (cible 900s) | bureau, auditeur, owner | 2026-10-13 06:38Z |
| #705 | Designate the reviewer when a lot enters qa: today a | qa | mty-amh-impl-03 | verdict_merge | 3 | 48834s (cible 900s) | bureau, auditeur, owner | 2026-10-13 08:40Z |
| #724 | Bug: DeepSeek sessions never resumed — find_session  | build | mty-amh-impl-01 | build_quiet | 1 | 2734s (cible 3600s) | assignee, owner | 2026-10-13 20:44Z |
| #747 | CI root cause 1: ameesh CI starved on hosted runners | qa | mesh-design | verdict_merge | 3 | 18070s (cible 900s) | bureau, auditeur, owner | 2026-10-13 17:13Z |
| #780 | An outdated runner marks the host's canon invalid af | build | mty-amh-impl-04 | build_quiet | 3 | 9633s (cible 3600s) | bureau, auditeur, owner | 2026-10-13 18:49Z |
| #810 | L243-3: consumers of projects: — pick-up, orchestrat | qa | mty-amh-impl-07 | verdict_merge | 3 | 21909s (cible 900s) | bureau, auditeur, owner | 2026-10-13 16:09Z |
| #843 | TEST-476-READY : publier atomiquement le PID de fixt | build | mty-amh-impl-04 | build_quiet | 3 | 9210s (cible 3600s) | bureau, auditeur, owner | 2026-10-13 18:56Z |
| #880 | CI-PSQL-TIMEOUT : plafond des jobs psql a 45 min (ou | qa | mty-amh-impl-05 | verdict_merge | 3 | 32497s (cible 900s) | bureau, auditeur, owner | 2026-10-13 13:13Z |
| #925 | FLAKY-653-CONCURRENCE : le témoin de concurrence Sco | qa | mty-amh-impl-03 | verdict_merge | 3 | 19320s (cible 900s) | bureau, auditeur, owner | 2026-10-13 16:52Z |
| #932 | CI-CACHE-VENV : le cache actions/cache du venv .venv | qa | mty-amh-impl-01 | verdict_merge | 3 | 24941s (cible 900s) | bureau, auditeur, owner | 2026-10-13 15:18Z |
| #947 | CI : le plafond de 30 min du job psql rend le check  | build | mty-amh-impl-05 | build_quiet | 1 | 1526s (cible 3600s) | assignee, owner | 2026-10-13 21:04Z |
| #947 | CI : le plafond de 30 min du job psql rend le check  | build | mty-amh-impl-05 | verdict_merge | 3 | 20585s (cible 900s) | bureau, auditeur, owner | 2026-10-13 16:31Z |

## Autres alertes (1×/2×/3×, non P1)
| lot | titre | état | assigné | étape | niv | retard | cible | cibles | depuis |
|---|---|---|---|---|---|---|---|---|---|
| #90 | PRIORITÉ propriétaire : desktop 0.2.5 Win/Mac + Comp | intake | deepseek2 | intake_started | 3 | 293062s (cible 3600s) | bureau, auditeur | 2026-10-10 12:05Z |
| #99 | Premier job de CI exécuté sur un appareil de la fami | blocked | deepseek7 | intake_started | 3 | 282958s (cible 3600s) | bureau, auditeur | 2026-10-10 14:53Z |
| #183 | L160f : radar et file de fusion pour Nexlink, à fair | intake | — | intake_started | 3 | 241116s (cible 3600s) | bureau, auditeur | 2026-10-11 02:31Z |
| #183 | L160f : radar et file de fusion pour Nexlink, à fair | intake | — | signal_assigned | 3 | 241116s (cible 3600s) | bureau, auditeur | 2026-10-11 02:31Z |
| #388 | L63b: adopt/resume refuse without bypass when the se | qa | mty-amh-impl-02 | verdict_merge | 3 | 17595s (cible 1800s) | bureau, auditeur | 2026-10-13 17:06Z |
| #420 | L244 QA-ROLE: a qa role, its agents and its sign-off | qa | mty-amh-impl-03 | verdict_merge | 3 | 27746s (cible 1800s) | bureau, auditeur | 2026-10-13 14:17Z |
| #423 | L243b RENAME-AGENTS: rename existing agents to <org> | qa | mty-amh-impl-05 | verdict_merge | 3 | 5864s (cible 1800s) | bureau, auditeur | 2026-10-13 20:21Z |
| #470 | MP-13-2b : câblage des initiateurs desktop/web/app v | blocked | mty-nxl-impl-02 | verdict_merge | 3 | 28855s (cible 1800s) | bureau, auditeur | 2026-10-13 13:58Z |
| #508 | Runner: per-persona liveness without a lease (last_s | qa | mty-amh-impl-07 | verdict_merge | 3 | 21007s (cible 1800s) | bureau, auditeur | 2026-10-13 16:09Z |
| #513 | DEEPSEEK-PRICES : barème de deepseek-flash périmé, l | qa | mty-amh-impl-02 | verdict_merge | 3 | 23972s (cible 1800s) | bureau, auditeur | 2026-10-13 15:20Z |
| #516 | RT-7d-N1 : purge par objet d'une copie seule (sans l | blocked | orchestrateur | intake_started | 3 | 62536s (cible 3600s) | bureau, auditeur | 2026-10-13 04:07Z |
| #532 | PGBOUNCER-LOCAL: local pgbouncer (session mode) on p | qa | mty-amh-impl-06 | verdict_merge | 3 | 18004s (cible 1800s) | bureau, auditeur | 2026-10-13 16:59Z |
| #537 | config.mask_dsn shows quoted keyword passwords, 'pas | qa | mty-amh-impl-05 | verdict_merge | 3 | 31599s (cible 1800s) | bureau, auditeur | 2026-10-13 13:12Z |
| #540 | TEAM-QUEUE : au plus un lot non démarré en attente p | qa | mty-amh-impl-07 | verdict_merge | 3 | 19587s (cible 1800s) | bureau, auditeur | 2026-10-13 16:33Z |
| #564 | MP-17-GATES : contrat constructible | qa | deepseek3 | verdict_merge | 3 | 12487s (cible 1800s) | bureau, auditeur | 2026-10-13 18:31Z |
| #580 | CIV-1 : primitives civiles et signatures | qa | deepseek2 | verdict_merge | 3 | 22915s (cible 1800s) | bureau, auditeur | 2026-10-13 15:37Z |
| #619 | SB-4-STORE : budget et eviction | qa | deepseek2 | verdict_merge | 3 | 19626s (cible 1800s) | bureau, auditeur | 2026-10-13 16:32Z |
| #621 | DH-9 : node Android — STORE | qa | deepseek7 | verdict_merge | 3 | 15061s (cible 1800s) | bureau, auditeur | 2026-10-13 17:48Z |
| #625 | A-2C-3 : crédit des grants | qa | deepseek4 | verdict_merge | 1 | 232s (cible 1800s) | assignee | 2026-10-13 21:55Z |
| #688 | ECO-RW-1 : catalogue de récompenses — modèle (reward | intake | deepseek3 | intake_started | 3 | 53300s (cible 3600s) | bureau, auditeur | 2026-10-13 06:41Z |
| #688 | ECO-RW-1 : catalogue de récompenses — modèle (reward | intake | deepseek3 | verdict_merge | 3 | 21440s (cible 1800s) | bureau, auditeur | 2026-10-13 16:02Z |
| #739 | NX-BILLING-2-PURCHASE-SUITE : changement de formule  | qa | deepseek4 | verdict_merge | 1 | 128s (cible 1800s) | assignee | 2026-10-13 21:57Z |
| #768 | LCN-EXPIRABLE-WINDOW : /ledger/expirable relaie mont | qa | deepseek6 | verdict_merge | 3 | 4397s (cible 1800s) | bureau, auditeur | 2026-10-13 20:46Z |
| #782 | session restore: le home du propriétaire retombe sur | qa | mty-amh-impl-06 | verdict_merge | 3 | 23703s (cible 1800s) | bureau, auditeur | 2026-10-13 15:24Z |
| #789 | SLO internes et escalades automatiques (canon §9, ho | build | mty-amh-impl-03 | build_quiet | 1 | 2795s (cible 7200s) | assignee | 2026-10-13 19:43Z |
| #795 | Un événement préempte le tour en cours (étude harnai | qa | mty-amh-impl-02 | verdict_merge | 3 | 41442s (cible 1800s) | bureau, auditeur | 2026-10-13 10:28Z |
| #796 | Mesure fiable : turn_costs.work_item_id, started_at, | qa | mty-amh-qa-01 | verdict_merge | 3 | 6949s (cible 1800s) | bureau, auditeur | 2026-10-13 20:03Z |
| #802 | canon sync: concurrent syncs of one host time out on | qa | mty-amh-impl-01 | verdict_merge | 3 | 42595s (cible 1800s) | bureau, auditeur | 2026-10-13 10:09Z |
| #803 | Prise automatique : capacités de classe sensible et  | qa | mty-amh-impl-08 | verdict_merge | 3 | 13380s (cible 1800s) | bureau, auditeur | 2026-10-13 18:16Z |
| #804 | Le tirage du lot se fait par l'exécuteur de l'agent, | qa | mty-amh-impl-06 | verdict_merge | 3 | 22227s (cible 1800s) | bureau, auditeur | 2026-10-13 15:49Z |
| #813 | #705 follow-up: has_reviewer caches a negative answe | qa | mty-amh-impl-07 | verdict_merge | 3 | 9756s (cible 1800s) | bureau, auditeur | 2026-10-13 19:17Z |
| #814 | Canon clone left behind on ameesh-agents-2: the regi | qa | mty-amh-impl-03 | verdict_merge | 3 | 40751s (cible 1800s) | bureau, auditeur | 2026-10-13 10:40Z |
| #824 | CI-CAPACITY: size the ameesh-ci runner pool to the p | build | mty-amh-impl-05 | verdict_merge | 3 | 31422s (cible 1800s) | bureau, auditeur | 2026-10-13 13:15Z |
| #825 | MOBILE-PROD-ROUTE : la route OTA de production diver | qa | deepseek6 | verdict_merge | 3 | 22821s (cible 1800s) | bureau, auditeur | 2026-10-13 15:39Z |
| #834 | GIT-IDENTITY : isoler auteur et committer par sessio | qa | mty-amh-impl-01 | verdict_merge | 3 | 42071s (cible 1800s) | bureau, auditeur | 2026-10-13 10:18Z |
| #835 | MIGRATION-LOCK : borner attente ACCESS EXCLUSIVE de  | qa | mty-amh-impl-03 | verdict_merge | 3 | 12768s (cible 1800s) | bureau, auditeur | 2026-10-13 18:26Z |
| #856 | CI-FLOW-ORPHANS : conclure les checks tests (flux) a | qa | mty-amh-impl-02 | verdict_merge | 3 | 26854s (cible 1800s) | bureau, auditeur | 2026-10-13 14:32Z |
| #869 | #439-suites n — push_body page_inquiry, triggers 404 | build | mty-nxl-impl-04 | build_quiet | 1 | 3861s (cible 7200s) | assignee | 2026-10-13 19:25Z |
| #869 | #439-suites n — push_body page_inquiry, triggers 404 | build | mty-nxl-impl-04 | verdict_merge | 3 | 13102s (cible 1800s) | bureau, auditeur | 2026-10-13 18:21Z |
| #877 | L361c : l'indexation d'un fil médié passe par le cen | qa | mty-amh-impl-08 | verdict_merge | 3 | 18814s (cible 1800s) | bureau, auditeur | 2026-10-13 16:46Z |
| #896 | GIT-IDENTITY : le runner fixe l'identité git de chaq | qa | mty-amh-impl-03 | verdict_merge | 3 | 26377s (cible 1800s) | bureau, auditeur | 2026-10-13 14:40Z |
| #898 | CODEX-PAUSE-PACE : les agents Codex en pause jusqu'à | qa | mty-amh-impl-06 | verdict_merge | 3 | 24444s (cible 1800s) | bureau, auditeur | 2026-10-13 15:12Z |
| #899 | CI-PSQL-GROWTH : la part psql d'une PR grandit avec  | qa | mty-amh-impl-05 | verdict_merge | 3 | 6104s (cible 1800s) | bureau, auditeur | 2026-10-13 20:17Z |
| #901 | CI-RERUN-FANTOME : un run relancé (gh run rerun) n'e | qa | mty-amh-impl-05 | verdict_merge | 3 | 23928s (cible 1800s) | bureau, auditeur | 2026-10-13 15:20Z |
| #904 | REL11-SMOKE-D7-WITNESS : exiger bool(stored) sur la  | qa | mty-nxl-impl-03 | verdict_merge | 3 | 6766s (cible 1800s) | bureau, auditeur | 2026-10-13 20:06Z |
| #910 | L232b suite : entree operateur d'installation du con | qa | mty-amh-impl-06 | verdict_merge | 3 | 12770s (cible 1800s) | bureau, auditeur | 2026-10-13 18:26Z |
| #917 | stale_lot d'un lot sans assigne : le brief part a to | qa | mty-amh-impl-05 | verdict_merge | 3 | 24046s (cible 1800s) | bureau, auditeur | 2026-10-13 15:18Z |
| #923 | HANDOFF-TEXT-TAKER : handoff.py:284 dit encore « the | qa | mty-amh-impl-07 | verdict_merge | 3 | 19589s (cible 1800s) | bureau, auditeur | 2026-10-13 16:33Z |
| #924 | REL-PUBLISH-SANS-ARCHIVE : la publication desktop do | qa | deepseek6 | verdict_merge | 2 | 3046s (cible 1800s) | pilot | 2026-10-13 21:08Z |
| #940 | CI-AGREGAT-BASH : le check requis « tests » est vert | qa | mty-amh-qa-01 | verdict_merge | 3 | 18953s (cible 1800s) | bureau, auditeur | 2026-10-13 16:43Z |
| #956 | MIGRATION-RESERVATION-STALE : la réservation d'un lo | qa | mty-amh-impl-08 | verdict_merge | 3 | 19974s (cible 1800s) | bureau, auditeur | 2026-10-13 16:26Z |
| #964 | Courrier --lot : ne pas reassigner le lot quand l'ex | intake | deepseek2 | intake_started | 3 | 15134s (cible 3600s) | bureau, auditeur | 2026-10-13 17:17Z |
| #964 | Courrier --lot : ne pas reassigner le lot quand l'ex | intake | deepseek2 | verdict_merge | 3 | 12650s (cible 1800s) | bureau, auditeur | 2026-10-13 18:28Z |
| #967 | Suite #956 (N R2 bureau) : ORDER BY id sur le SELECT | qa | mty-amh-impl-08 | verdict_merge | 3 | 7873s (cible 1800s) | bureau, auditeur | 2026-10-13 19:48Z |
| #968 | #829 N1 — le rôle billing admis en SQL n'a pas de re | qa | deepseek4 | verdict_merge | 3 | 6689s (cible 1800s) | bureau, auditeur | 2026-10-13 20:08Z |
| #971 | TEST-FLAKE : test_panne_de_base_definitive_tue_avant | build | mty-amh-impl-08 | build_quiet | 1 | 3789s (cible 7200s) | assignee | 2026-10-13 19:26Z |
| #972 | MIGRATE-YES-CONTENT-PROD : confirm_prod n'exige --ye | qa | deepseek3 | verdict_merge | 3 | 17444s (cible 1800s) | bureau, auditeur | 2026-10-13 17:08Z |
| #976 | agent-mail : les messages en attente d'un agent arre | qa | mty-amh-impl-01 | verdict_merge | 3 | 4733s (cible 1800s) | bureau, auditeur | 2026-10-13 20:40Z |
| #983 | #966-N1 : readout prod lu sur la prod vivante après  | qa | mty-nxl-impl-04 | verdict_merge | 3 | 4857s (cible 1800s) | bureau, auditeur | 2026-10-13 20:38Z |
| #985 | #966-N3 : la base jetable locale n'est pas supprimée | qa | deepseek6 | verdict_merge | 1 | 20s (cible 1800s) | assignee | 2026-10-13 21:59Z |
| #987 | CI-MACOS-HOSTED : le job hosted « plateforme macOS » | qa | mty-amh-impl-05 | verdict_merge | 3 | 14050s (cible 1800s) | bureau, auditeur | 2026-10-13 18:05Z |
| #988 | #531 FAUX MERGE : work close --delivered absent de m | qa | mty-amh-impl-02 | verdict_merge | 3 | 13089s (cible 1800s) | bureau, auditeur | 2026-10-13 18:21Z |
| #989 | #391 N1 (bureau) : notify-send reçoit le titre et le | qa | mty-amh-impl-07 | verdict_merge | 3 | 14358s (cible 1800s) | bureau, auditeur | 2026-10-13 18:00Z |
| #1003 | REL-11 : le jeton gh de l'hote n'a pas le scope work | qa | mty-amh-impl-03 | verdict_merge | 3 | 5436s (cible 1800s) | bureau, auditeur | 2026-10-13 20:29Z |
| #1005 | work sync-merges : cibler la branche d'integration d | blocked | mty-amh-impl-08 | intake_started | 3 | 11824s (cible 3600s) | bureau, auditeur | 2026-10-13 18:12Z |
| #1025 | DID-WEB-PROD-CABLAGE : cabler le did:web de la plate | intake | — | intake_started | 3 | 7549s (cible 3600s) | bureau, auditeur | 2026-10-13 19:23Z |
| #1025 | DID-WEB-PROD-CABLAGE : cabler le did:web de la plate | intake | — | signal_assigned | 3 | 7549s (cible 3600s) | bureau, auditeur | 2026-10-13 19:23Z |
| #1026 | STALE-LOT-RACE : le brief stale_lot part a tous les  | qa | mty-amh-impl-02 | verdict_merge | 3 | 6173s (cible 1800s) | bureau, auditeur | 2026-10-13 20:16Z |
| #1056 | #976 suite R2 : observer le PID du verrou dans les t | intake | mty-amh-impl-01 | intake_started | 1 | 2479s (cible 3600s) | assignee | 2026-10-13 20:48Z |
| #1064 | Mac #261 : build Swift apple-cf/apple-metal échoue a | intake | mty-nxl-impl-01 | intake_started | 1 | 1524s (cible 3600s) | assignee | 2026-10-13 21:04Z |
| #1073 | REVUE #1035 t2 : R1 delta escalades SLO (prod) | build | auditeur | verdict_merge | 1 | 836s (cible 1800s) | assignee | 2026-10-13 21:45Z |
| #1075 | Codex primaire : multiplicateur de rythme pour vouch | qa | mty-amh-impl-07 | verdict_merge | 1 | 275s (cible 1800s) | assignee | 2026-10-13 21:55Z |
| #1077 | P12-HINTS-N4 (mobile) : le fil ouvert ouvre une 2e p | build | claude2 | verdict_merge | 1 | 369s (cible 1800s) | assignee | 2026-10-13 21:53Z |
