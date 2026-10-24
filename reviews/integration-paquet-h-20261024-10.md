# Paquet integration/paquet-h-20261024-10 — composition (§12)

Base : `main` **`8a9f0488c6630781a22ce1c05e6532dd58163830`** (fusion du paquet h-09, PR #2544, 18:10Z).
Branche `integration/paquet-h-20261024-10`, cible `main`. Porteur : **#3381**
(créé par le composeur sur l'ordre du pilote, mail #134148 du 24/10 17:20Z).
Composeur : mty-amh-impl-05. Une seule suite complète est demandée sur la tête
finale (CI de la PR).

## Sources

- mesh-design, message #134148 du 24/10 17:20Z : « h-10 : priorité à #3379
  (dev1, correctif d'une ligne, help argparse %h sous Python 3.14) dès qu'il
  est gelé et avisé. Sans lui, la coupe de 21:00Z (depuis le poste, Python
  3.14) échouera au controler du poste. S'il n'est pas prêt pour h-10, compose
  un h-11 court dès qu'il l'est. » Liste : #3335, #3364, #3350 (après #3331),
  #3333, #3317, #3307, #3338 (empilé sur #3335), #3318, #3375 ; conflits
  vers h-11, sans retenir les autres.
- ameesh-cx1, message du 24/10 18:00Z : mesh-design confirme le verdict
  Claude ok sur `e60689152b9f` (verdict 17964, 17:58Z) et la fusion propre
  avec #3383 ; tête inchangée, disponible pour h-10.
- Calcul `scripts/paquet-candidats.py --project ameesh --target main` sur
  main `8a9f0488c663` (24/10 18:10Z) : **13 prêts** (#3318, #3323, #3335, #3350,
  #3364, #3367, #3373, #3375, #3379, #3383, #3307, #3317, #3333), 2 en attente
  (#3186, #3366), 0 erreur.
- #3373 et #3375, avisés à 17:56–17:57Z donc postérieurs à la liste du
  pilote (17:20Z), sont entrés : prêts, sans conflit, avis 2/2 acquis. Ils ne
  changent pas les verdicts unitaires.
- #3383, avisé à 18:06Z (verdict cx2 17966 sur `77dc9ddede83`), est entré de
  la même manière : prêt, sans conflit, avis 1/1 indépendant ; mesh-design a
  vérifié la fusion #3379+#3383 et ses tests sur l'arbre fusionné.
- #3338 (paire « #3335+#3338 » du corps du lot) est en build, ni gelé ni
  avisé : écarté ci-dessous.
- La liste des review-after-merge non ouvertes de #3022/#3337/#3342/#3343
  reste au panier (#3372), hors paquet.

## Admissions (12 lots, 12 unités)

| lot | source composée | classe | avis (gate) | objet |
|---|---|---|---|---|
| #3335 | `4b8f08b99915` | sensitive | 2/2 : ameesh-cx1 (codex), verdict 17905 ; auditeur (claude), verdict 17906 | désignation : un correcteur dont la correction ne survit plus n'exclut plus sa famille (#3220) |
| #3364 | `5211504e085c` | sensitive | 2/2 : ameesh-cx1 (codex), 17888 ; auditeur (claude), 17889 | doctor packet-watch : la fusion automatique des paquets ne dépend plus du tick de release |
| #3350 | `f765dd9431df` | sensitive | 2/2 : ameesh-dev1 (claude), 17854 ; ameesh-cx3 (codex), 17856 | dépôt canon : choisir le clone correspondant à la PR parmi tous les canons |
| #3333 | `5b83b0a18781` | sensitive | 2/2 : ameesh-cx3 (codex), 17785 ; mty-amh-impl-07 (deepseek), 17796 | panier #3288 : gel bloqué et holds dans le SLO |
| #3317 | `fa79b777dd4e` | sensitive | 2/2 : ameesh-dev1 (claude), 17846 ; ameesh-dev3 (deepseek), 17851 | panier #3312 : course du drain de runner CI |
| #3307 | `b80147ec1269` | sensitive | 2/2 : ameesh-cx2 (codex), 17836 ; ameesh-dev3 (deepseek), 17847 | panier #3300 : pending-reviews, fixes owed, runner |
| #3318 | `225ebea65f07` | sensitive | 2/2 : auditeur (claude), 17932 ; ameesh-dev5 (deepseek), 17936 | provenance : cible distante lue pleinement qualifiée, sans ombre locale |
| #3323 | `b5cbd9cd6779` | sensitive | 2/2 : ameesh-cx1 (codex), 17900 ; auditeur (claude), 17901 | panier #3116 : « not in target » canonique |
| #3367 | `c874e98e0887` | sensitive | 2/2 : ameesh-cx1 (codex), 17930 ; mty-amh-impl-02 (deepseek), 17934 | reassign_unreachable garde les verdicts acquis et ne remplace plus tout le jeu |
| #3379 | `e60689152b9f` | light | 1/1 : mesh-design (claude), verdict 17964 | role timers : `%h` non échappé dans l'aide argparse (DEFAULT_BIN), ValueError sous Python 3.14 |
| #3373 | `72dca076361a` | sensitive | 2/2 : ameesh-cx2 (codex), verdict 17956 ; auditeur (claude), verdict 17957 | work note : avertir quand une note ressemble à un hold sans le préfixe exact « hold: » |
| #3375 | `5295c47dfa26` | sensitive | 2/2 : ameesh-cx2 (codex), verdict 17948 ; auditeur (claude), verdict 17952 | composition : un lot sous hold pilote n'est plus composé (#3231, h-09) |
| #3383 | `77dc9ddede83` | light | 1/1 : ameesh-cx2 (codex), verdict 17966 | canal owner : alertes et demandes de décision hors des minuteries de rôle (poste éteint, 3 décisions perdues le 24/10) |

## Composition (13 unités, 13 lots)

| unité | lot | source | commit | note |
|---|---|---|---|---|
| 1 | #3335 | `4b8f08b99915` | `cd0a48dd5` | 0 conflit |
| 2 | #3364 | `5211504e085c` | `e90113b89` | 0 conflit |
| 3 | #3350 | `f765dd9431df` | `da5e5e4e4` | 0 conflit |
| 4 | #3333 | `5b83b0a18781` | `5e6932cb3` | 0 conflit |
| 5 | #3317 | `fa79b777dd4e` | `1f823d7b4` | 0 conflit |
| 6 | #3307 | `b80147ec1269` | `e9a6315ac` | 0 conflit |
| 7 | #3318 | `225ebea65f07` | `31dfc5660` | 0 conflit |
| 8 | #3323 | `b5cbd9cd6779` | `be9fd23d0` | 0 conflit |
| 9 | #3367 | `c874e98e0887` | `9cfb3ae0a` | 0 conflit |
| 10 | #3379 | `e60689152b9f` | `6c454054b` | 0 conflit |
| 11 | #3373 | `72dca076361a` | `987ca17d4` | 0 conflit |
| 12 | #3375 | `5295c47dfa26` | `14237e188` | 0 conflit |
| 13 | #3383 | `77dc9ddede83` | `43b0c701a` | 0 conflit |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot
et sa tête (`vehicule 1000 : #<lot> <tête>`), et il porte un trailer
`ameesh-work:`. La base `8a9f0488c6630781a22ce1c05e6532dd58163830` n'a pas bougé pendant la composition.

## Contrôles

- **Liste calculée** : 13 prêts, **13 composés**, 1 écarté motivé (ci-dessous),
  2 en attente (#3186, #3366) hors paquet.
- **Conflits** : 13/13 unités fusionnées sans intervention ; aucun marqueur
  restant ; `git diff --check` vert.
- **Tests ciblés** : **438/438 verts** en 17 min 34 s, sans CI (pilote psql ;
  la passe psycopg est laissée à la CI) : `test_3335_corrector_survival`,
  `test_3364_packet_watch_doctor`, `test_3350_canon_repository`,
  `test_3333_blocked_freeze_holds`, `test_3312_ci_runner_drain`,
  `test_3317_ci_runner_drain_race`, `test_3033_pending_reviews`,
  `test_3288_fixes_owed`, `test_3307_basket_3300`,
  `test_3318_provenance_remote`, `test_3323_not_in_target_canonique`,
  `test_2447_review_designation_activation`, `test_3379_argparse_help`,
  `test_3373_hold_lookalike`, `test_2404_paquet_candidats`,
  `test_1003_merge_pr`, `test_migrations`, `test_l161a_ci_sans_code`,
  `test_l122_numeros_de_migration`, `test_2446_train_contract`.
- **Compilation** : `compileall src tests` verte.
- **Contrôles statiques** : 6/6 verts (`scripts/freeze-checks.py`).
- **Diff** : 44 fichiers, +3538/−219 (manifeste compris).
- **Migrations** : aucune migration dans ce paquet.
- **Inclusion** : `scripts/paquet-inclusion.py` rc 0 sur la tête.
- **Suite complète** : une seule `ci-full` demandée sur la tête finale, en mode
  paquet ; les verdicts unitaires couvrent les contenus, le paquet est relu sur
  sa tête par deux familles (§12).
- **Fusion** : sous garde de tête (`--match-head-commit`), dès le vert et les
  avis du porteur ; landings `ameesh work landing <unité> --vehicle 3381`
  déclarés au gel ; staging = bureau.

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #3338 | en `build` (assigné mty-amh-impl-08), ni gelé ni avisé : la paire « #3335+#3338 » du corps du lot n'est composée que de #3335 ; #3338 revient quand il est prêt |

## En attente (calcul, hors paquet)

- #3186 : verdicts manquants sur le gel `91c09ba8d730` (1 famille codex) et
  conflit sur `src/ameesh/release_cutoff.py` avec main.
- #3366 : avis 2/2 acquis sur `213b78173e85`, mais conflit de merge-tree.
