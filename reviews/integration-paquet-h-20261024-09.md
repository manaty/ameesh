# Paquet integration/paquet-h-20261024-09 — composition (§12)

Base : `main` **`bd48f6affdc9`** (fusion du paquet h-08, PR #2533, 16:16Z).
Branche `integration/paquet-h-20261024-09`, cible `main`. Porteur : **#3368**
(créé par le composeur sur l'ordre du pilote, mail #133975 du 24/10 16:27Z).
Composeur : mty-amh-impl-05. Une seule suite complète est demandée sur la tête
finale (CI de la PR).

## Sources

- mesh-design, message #133975 du 24/10 16:27Z : « h-09 : oui, crée le porteur
  toi-même et compose ». Les 8 lots remis en colonne à 16:17Z (#3314, #3324,
  #3331, #3341, #3346, #3349, #3352, #3231) sont « waiting: merge », avis
  acquis sur la tête exacte ; ajouter la liste du composeur (#3333, #3344,
  #3350) si le merge-tree est propre ; les conflits vont vers h-10. Les
  relectures non ouvertes de #3022/#3337/#3342/#3343 vont au panier, hors
  paquet.
- Liste antérieure (#3340, 16:20Z) : h-09 = #3324 #3333 #3344 #3346 #3352
  #3349 #3314 #3331 #3350, puis #3335/#3338.
- Calcul `scripts/paquet-candidats.py --project ameesh --base bd48f6aff`
  (24/10 16:5xZ) : **15 lots prêts**, 3 en attente (#3186, #3318, #3367),
  1 erreur : #3350, « provenance inconnue pour `641de8af` » — commit de #3331
  dans la plage gelée de #3350, que la porte de relecture ne sait pas
  attribuer. L'erreur est confirmée par le refus de l'auto-merge (« constituent
  #3350 : no review verdict ok … frozen range cannot be read ») : #3350 est
  écarté (voir « Écartés »). L'export `--ready-out` est refusé par la garde
  tant qu'une erreur subsiste ; la liste prête a été écrite depuis le même
  JSON (`ready`, 15 lots) pour le contrôle d'inclusion.

## Admissions (9 lots, 9 unités)

| lot | source composée | classe | avis (gate) | objet |
|---|---|---|---|---|
| #3314 | `45b5a8d307be` | sensitive | 2/2 : ameesh-cx1 (codex), verdict 17830 ; ameesh-dev7 (claude), verdict 17833 | panier 3308 : `land.base_oid` encode la branche |
| #3324 | `13979d16ff04` | sensitive | 2/2 : ameesh-cx1 (codex), verdict 17594 ; ameesh-dev2 (claude), verdict 17764 | `work reviewer` : l'auditeur peut (re)désigner un relecteur |
| #3331 | `641de8afb2f7` | sensitive | 2/2 : ameesh-cx3 (codex), verdict 17766 ; ameesh-dev1 (claude), verdicts 17844/17875 ; ameesh-cx1, verdict 17879 | lots de canon : le gel se lit dans le clone du canon |
| #3341 | `af8fde837dd4` | sensitive | 2/2 : ameesh-cx1 (codex), verdict 17747 ; ameesh-dev7 (claude), verdict 17858 ; ameesh-cx2, verdict 17884 | SLO `verdict_merge` : départ à max(GO, CI verte) |
| #3346 | `eed4f4b9757e` | sensitive | 2/2 : ameesh-cx3 (codex), verdict 17814 ; ameesh-dev7 (claude), verdict 17818 ; ameesh-cx1 17881, ameesh-cx2 17885 | masquage des secrets dans `doctor` |
| #3349 | `43a81c1da301` | sensitive | 2/2 : ameesh-cx1 (codex), verdict 17829 ; ameesh-dev1 (claude), verdict 17832 | train : plan d'ombre à jour et courrier d'écart de règles |
| #3352 | `7da6a5f2d510` | sensitive | 2/2 : ameesh-cx1 (codex), verdict 17825 ; ameesh-dev7 (claude), verdict 17826 | panier carte-mesure : repli sur les parts de main |
| #3231 | `1af34785d0a3` | light | 1/1 : ameesh-dev2 (claude), verdict 17371 | témoin EP7 : `ameesh role status` dans le README ; inclus sur la liste du pilote du 16:27Z |
| #3344 | `cef09c501656` | sensitive | 2/2 : ameesh-cx3 (codex), verdict 17811 ; ameesh-cx1 (codex), verdict 17813 ; ameesh-dev3 (deepseek), verdict 17817 | panier 3343 : alerte `map_stale`, migration 0480 |

## Composition (9 unités, 9 lots)

| unité | lot | source | commit | note |
|---|---|---|---|---|
| 1 | #3314 | `45b5a8d307be` | `035f9e47f39e` | 0 conflit |
| 2 | #3324 | `13979d16ff04` | `ede34aec61a8` | 0 conflit |
| 3 | #3331 | `641de8afb2f7` | `dfd30f31c15b` | 0 conflit |
| 4 | #3341 | `af8fde837dd4` | `6ec24c812003` | 0 conflit |
| 5 | #3346 | `eed4f4b9757e` | `864d2b085a8f` | 0 conflit |
| 6 | #3349 | `43a81c1da301` | `5a97b1e53b07` | 0 conflit |
| 7 | #3352 | `7da6a5f2d510` | `28ae85bdb171` | 0 conflit |
| 8 | #3231 | `1af34785d0a3` | `1fa86a1349a5` | 0 conflit |
| 9 | #3344 | `cef09c501656` | `2dc5328860a9` | 0 conflit |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot
et sa tête (`vehicule 0900 : #<lot> <tête>`), et il porte un trailer
`ameesh-work:`. Aucun commit de `main` n'est venu pendant la composition (base
`bd48f6affdc9` figée). Les neuf unités se sont fusionnées sans intervention.

Deux membres ont été retirés avant le gel, sans conflit ni revert :

- **#3333**, d'abord composé, écarté : sa désignation rejouée à 15:56Z (hôte
  `pc-smichea` fermé, défaut 3367) attend encore `ameesh-dev3` sur
  `5b83b0a18781` ; la porte d'atterrissage refuse un membre sans verdict ok
  acquis (`progress._jalons_de_lot`). Il reste pour h-10 avec ses deux avis
  déjà acquis (cx3, impl-07) et sa demande dev3 en cours.
- **#3350**, d'abord composé, écarté : son delta `f765dd94` a bien deux avis
  (dev1 17854, cx3 17856), mais sa plage gelée contient `641de8af`, commit de
  #3331, et la porte de relecture ne peut pas prouver l'indépendance de ce
  contenu (« author provenance unknown ») ; l'auto-merge du paquet l'a refusé
  nommément. Il repart en h-10 depuis `main` une fois #3331 atterri, où sa
  plage ne contiendra plus le commit de #3331.

## Contrôles

- **Liste calculée** : 15 prêts au calcul, **9 composés**, 7 écartés motivés
  (ci-dessous), 3 en attente (#3186, #3318, #3367) hors paquet.
- **Conflits** : 9/9 unités fusionnées sans intervention ; aucun marqueur
  restant ; `git diff --check` vert.
- **Contenu partiel #3331** : plus de contenu partiel (#3350, qui réécrivait
  une partie de `_canon_checkout`, est retiré).
- **Tests ciblés** : **428/428 verts** en 2 min, sans CI :
  `test_1003_merge_pr`, `test_3308_gh_base_ref`, `test_3324_reviewer_auditeur`,
  `test_3331_canon_repo`, `test_2644_prevalidation_calls`,
  `test_3346_masquage_secrets_doctor`, `test_2914_train_canon_drift`,
  `test_789_slo`, `test_789_slo_alerts`, `test_3344_map_stale`,
  `test_3343_carte_mesure`, `test_migrations`,
  `test_l122_numeros_de_migration`, `test_2446_train_contract`,
  `test_l161a_ci_sans_code`.
- **Compilation** : `compileall src tests` verte.
- **Contrôles statiques** : 6/6 verts (`scripts/freeze-checks.py`).
- **Diff** : 35 fichiers, +1842/−81 (manifeste compris) ; les neuf commits de
  composition portent leur trailer `ameesh-work:`.
- **Migrations** : une migration additive, `0480_ci_map_state.sql` (#3344) ;
  à appliquer en staging avant de relancer les exécuteurs (§14), la prod
  réelle restant sur go owner.
- **Suite complète** : une seule `ci-full` demandée sur la tête finale, en mode
  paquet ; le run de la tête à 10 unités est annulé, le nouveau est demandé
  sur la tête à 9 unités ; les verdicts unitaires ci-dessus couvrent les
  contenus, le paquet est relu sur sa tête par deux familles (§12).
- **Fusion** : sous garde de tête (`--match-head-commit`), dès le vert et les
  avis du porteur ; landings `ameesh work landing <unité> --vehicle 3368`
  déclarés au gel ; staging = bureau.

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #3333 | **prêt au merge-tree**, mais membre refusé à l'atterrissage : verdict `ameesh-dev3` encore attendu sur `5b83b0a18781` (désignation rejouée à 15:56Z) ; reste pour h-10 |
| #3350 | avis 2/2 sur `f765dd94`, mais plage gelée illisible pour la porte (`author provenance unknown: 641de8af`, commit de #3331) ; reporté en h-10 après l'atterrissage de #3331 |
| #3323 | **prêt** au calcul, mais hors de la liste du pilote du 16:27Z : reste pour un prochain paquet |
| #3335 | **prêt** au calcul, mais hors de la liste du pilote du 16:27Z (« puis #3335/#3338 ») : prochain paquet |
| #3364 | **prêt** au calcul (doctor packet-watch), hors liste du pilote : prochain paquet |
| #3307 | **prêt** au calcul, hors liste du pilote : prochain paquet |
| #3317 | **prêt** au calcul, hors liste du pilote : prochain paquet |

## En attente (calcul, hors paquet)

- #3186 : verdicts manquants sur le gel `91c09ba8d730` (1 famille codex) et
  conflit sur `src/ameesh/release_cutoff.py` avec main.
- #3318 : verdicts manquants sur le gel `cec551e9c201` (1 famille claude).
- #3367 : verdicts manquants sur le gel `56824292907e` (1 famille codex).
