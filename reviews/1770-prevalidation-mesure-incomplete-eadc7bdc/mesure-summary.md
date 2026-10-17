# Mesure carte 39015105210 sur main eadc7bdc — incomplète, aucune carte

Run : https://github.com/manaty/ameesh/actions/runs/39015105210
Dispatch : `workflow_dispatch`, headSha `eadc7bdcfb5b105a4bef23a513f57b2843154615`, 2026-10-17T07:50:01Z.
Conclusion du run : `cancelled`. Aucun artefact `carte`, aucun cache `carte-eadc7bdc`.

## Parts de mesure

| Part | Résultat | Durée |
|---|---|---|
| plan de mesure | success | 14 s |
| mesure psql 1/4 | success | 26 m 48 s |
| mesure psql 2/4 | success | 33 m 21 s |
| mesure psql 3/4 | success | 14 m 05 s |
| mesure psql 4/4 | success | 24 m 07 s |
| mesure psycopg 1/6 | success | 23 m 52 s |
| mesure psycopg 2/6 | success | 32 m 41 s |
| **mesure psycopg 3/6** | **cancelled au plafond** | **40 m 46 s** (07:50:21 → 08:31:07) |
| mesure psycopg 4/6 | success | 28 m 21 s |
| mesure psycopg 5/6 | success | 34 m 34 s |
| mesure psycopg 6/6 | success | 20 m 49 s |
| job `carte` | failure | `RESULTAT_MESURE: cancelled` → `::error::mesure cancelled : carte non publiée (part en échec ou annulée)` |

9 parts de mesure sur 10 vertes ; **`mesure psycopg 3/6` annulée par `timeout-minutes: 40`** alors qu'elle
progressait encore (dernier événement de test à 08:30:57, annulation 08:31:07).

## Diagnostic de la part psycopg 3/6 (annulée)

- Plan de mesure : 87 modules assignés, estimation **1 588,7 s** (26,5 min).
- Exécution réelle : **74 modules démarrés, 13 jamais démarrés** (test_l498_doctor, test_l53_restore_guard,
  test_l54_memory_runner, test_l66_desktop, test_l70_budget, test_l72_panne_de_base, test_l750_release,
  test_l756_full_suite, test_l795_preemption_evenement, test_l848_github_quota, test_l932_ci_venv_cache,
  test_migrations, test_plan_git) ; 1 333 tests exécutés, temps de test cumulé **2 316,3 s (38,6 min)**
  sur un créneau de 39 min 58 s. La part n'était pas bloquée, elle était plus lente que son estimation.
- Modules dominants (durées réelles, flux `ameesh-test-event/1`) :
  `test_503_agent_host` **1 183,7 s** (42 tests), `test_l26_exploitation` 165,4 s,
  `test_l40_delegation` 157,6 s, `test_518_dsn_argv` 98,1 s, `test_l105_plafonds_du_tour` 82,9 s.
- La table de durées du plan vient de la carte **d900521b** (cache `carte-d900521b`, elle-même partielle :
  3 couvertures ignorées) ; elle donne `test_503_agent_host` à **486,2 s**, soit **×2,4** sous la durée
  réelle sous charge. L'estimation totale 1 588,7 s sous-estime donc la part de ~50 %.
- Conclusion : le plafond de 40 min n'est pas atteint par un test bloqué mais par une **partition sous-estimée**
  pour cette part (poids de carte mesurés sous une autre charge). Il manquait ~13 modules à la part.

## Couverture : 1 fichier de 0 octet, malgré #2177

Contrôle des 10 parts téléchargées (9 740 fichiers `.coverage*`, en-tête SQLite) :

- **1 fichier vide** : `mesure-psycopg-6/cov/.coverage.ameesh-ci-1.pid148376.X2fizArx` (0 octet).
- Localisation par PID et par contexte des fichiers voisins (lus en SQLite) :
  PID 148315 et 148439 → `tests.test_l16_acp.AcpTest.test_protocole_invalide_finit_en_erreur`.
  Le PID 148376 tombe entre les deux, dans la même séquence ACP.
- #2177 (faux agent ACP qui sauve sa couverture avant de sortir sur SIGTERM) est bien dans ce main
  (`4ff17851` ancêtre de `eadc7bdc`) et a réduit les pertes (3 fichiers au run précédent, 1 ici), mais
  **ne couvre pas ce chemin de mort** : le processus tué n'est pas (ou pas seulement) le faux agent qui
  a le gestionnaire SIGTERM — pont `python -m ameesh.acp` ou enfant tué avant l'installation du
  gestionnaire. Sans traçage des PID enfants, la cause exacte n'est pas qualifiable depuis les flux.
- Conséquence : même si `psycopg 3/6` avait fini, la carte assemblée aurait été **partielle** (1 couverture
  ignorée) et refusée par le plan (#1257/#1690/#1707). La mesure est donc rouge pour deux causes
  indépendantes : part annulée au plafond, et couverture perdue.

## Refus nommé

Sans carte, le plan L161f est en `mode full` / `sure=false` (« no map ») et la prévalidation refuse
`card-missing` avant tout envoi VM (voir `refus.json`, `plan.json`).
Aucun retry, aucun second dispatch, aucune garde assouplie, aucun fichier de mesure supprimé.
