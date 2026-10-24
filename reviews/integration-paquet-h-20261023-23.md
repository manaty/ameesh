# Paquet integration/paquet-h-20261023-23 — composition (§12)

Base : `main` **`5cb7b7fb2d87`** (release 1.20.0 landing, coupure de 21:00Z ;
tag `v1.20.0` sur `8ca8cf64`).
Branche `integration/paquet-h-20261023-23`, cible `main`. Porteur : **#3239**.
Composeur : mty-amh-impl-05. Une seule suite complète est demandée sur la tête
finale (CI de la PR).

## Sources

- mesh-design, message #129698 (19:38Z) : composition confiée à
  mty-amh-impl-05, membres épinglés #3198, #3201 (`94326a4f`), #3209
  (`1b4f2c99`), #114 (`aa851cc3`), #3108 (`a30f9f61`), #3137 (`c328d54c`),
  #3187 (`5bc08028`), base `main` f133b837 ; #3116 hors paquet (conflit),
  #3223 attend T0.
- mesh-design, message #129755 (19:47Z) : #3196 (`601280af`) ajouté en fin
  d'ordre si l'essai passe sur la base post-release ; #3116 remise
  `f4656934` en dernier, seulement si les deux avis (dev2 + cx2) sont
  enregistrés sur ce SHA avant composition.
- mesh-design, message #129908 (20:09Z) : ni #3231 ni #3223 dans h-23 ;
  témoins EP7 réservés au premier paquet après la bascule de 22:00Z. Si la
  CI de h-23 vire au vert après T0, tête et run à « mesh-design », routés au
  pilote élu sur agents-2, qui fusionne.
- mesh-design, message #130179 (21:00Z) : #390, remise `becfb28cfea8`
  (`refs/ameesh/resolutions/390/3198`, parents `8b2d0984` relu + `7385bb75`
  tête de #3198), deux avis dev1 + cx2 sur la remise exacte ; à composer
  juste après #3198 si l'essai sur la base post-release passe, sinon paquet
  suivant.
- mty-amh-impl-01, message du 20:15Z : les 9 lots prêts se fusionnent
  proprement sur f133b837 ; #3219 (`9958a904`, 2/2) est nouveau depuis
  l'épinglage et entre dans h-23 (mail 20:25Z).
- #3236 (`3518a7039ed9`, 1/1 dev4) est prêt depuis 20:52Z ; composé en
  dernière unité (bug notify, aucun conflit).

## Admissions (11 lots, 11 unités)

| lot | source composée | classe | avis (gate) | objet |
|---|---|---|---|---|
| #3198 | `7385bb7597e5` | sensitive | 2/2 : auditeur (claude), ameesh-cx2 (codex) | plateforme macOS hors du chemin du paquet |
| #390 | `becfb28cfea8` | sensitive | 2/2 : ameesh-dev1 (claude), ameesh-cx2 (codex) | remise #390×#3198 : services launchd macOS, installation à base distante |
| #3201 | `94326a4fc173` | sensitive | 2/2 : auditeur (claude), ameesh-cx2 (codex) | review-after-merge : repo transmis à close_merged |
| #3209 | `1b4f2c99a819` | sensitive | 2/2 : ameesh-cx2 (codex), mty-amh-impl-01 (deepseek) | check sauté par construction consigné et dédoublonné |
| #3219 | `9958a90438f6` | sensitive | 2/2 : ameesh-dev2 (claude), ameesh-cx2 (codex) | fermeture par contenu : l'assigné lève son hold de règle 1 |
| #114 | `aa851cc30f52` | sensitive | 2/2 : ameesh-dev1, mty-amh-impl-07 | cœur générique : frontière et vocabulaire de cycle de vie |
| #3108 | `a30f9f618cb4` | sensitive | 2/2 : auditeur (claude), ameesh-cx2 (codex) | panier #3098 : train contracts jobs |
| #3137 | `c328d54cba10` | sensitive | 1/1 : auditeur (claude) | panier #3126 : brief budget doc |
| #3187 | `5bc080284c69` | sensitive | 2/2 : ameesh-dev2 (claude), ameesh-cx2 (codex) | docstrings des baux vivants |
| #3196 | `601280afd960` | sensitive | 2/2 : ameesh-dev2 (claude), ameesh-cx2 (codex) | plancher d'avis : recharge humaine à valeur égale |
| #3236 | `3518a7039ed9` | sensitive | 1/1 : ameesh-dev4 (claude) | brief plan_underused : une remise par alerte et par fenêtre (garde 6 h) |

## Composition (11 unités, 11 lots)

| unité | lot | source | commit | note |
|---|---|---|---|---|
| 1 | #3198 | `7385bb7597e5` | `b912efbdcff6` | 0 conflit |
| 2 | #390 | `becfb28cfea8` | `831ecbc5a052` | remise #390×#3198, 0 conflit |
| 3 | #3201 | `94326a4fc173` | `f133d032525a` | 0 conflit |
| 4 | #3209 | `1b4f2c99a819` | `e201e99d30ed` | 0 conflit |
| 5 | #3219 | `9958a90438f6` | `84f97cf3b576` | 0 conflit |
| 6 | #114 | `aa851cc30f52` | `49312de43fe3` | 0 conflit |
| 7 | #3108 | `a30f9f618cb4` | `8746405b21ae` | 0 conflit |
| 8 | #3137 | `c328d54cba10` | `d91ad917ace4` | 0 conflit |
| 9 | #3187 | `5bc080284c69` | `74cad54ad004` | 0 conflit |
| 10 | #3196 | `601280afd960` | `9f7f8c2ceceb` | 0 conflit |
| 11 | #3236 | `3518a7039ed9` | `518698a0c309` | 0 conflit |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot
et sa tête, et il porte un trailer `ameesh-work:`. Aucun commit de `main` n'est
venu pendant la composition. Aucune résolution manuelle : 11/11 unités
fusionnées proprement.

## Résolutions de composition

**#390 remise × #3198** (`becfb28cfea8`, unité 2) : la remise fusionne #390
(`8b2d0984` relu) avec #3198 (`7385bb75`) et adapte la ligne du job macOS de
`.github/workflows/tests.yml` : « couche plateforme, lint, garanties,
notification de bureau et services (L63, L65, L66) » ajoute les trois témoins
`tests.test_l65_services` (ServicePayloadTest, ServiceInstallTest,
KeychainTest). Le contrôle d'inclusion signale pour cette raison le contenu de
#3198 comme partiel à la tête (44/45 lignes) : c'est l'adaptation attendue de
la remise, pas une omission. Le job macOS reste hors du chemin du paquet
(#3198) et ces témoins suivent la plateforme macOS.

## Corrections après revue (tête `884d1c510d24`)

La CI complète du gel (`40024941418`) a rendu quatre témoins rouges, dont le
B/P1 cx2/dev2 ; correctif `3460bd874` sur la branche du paquet, avis sur le
delta final (§12).

- **Contrat du train réépinglé** : `src/ameesh/train_contracts.py` épinglait
  `tests.yml` à `2f83414c75f8` (avant la remise #390) alors que la tête porte
  `7a6fc3bd619c` (remise #390×#3198, témoins L65). Le contrat suit le blob
  livré ; les autres blobs étaient déjà à jour.
- **Témoin #1635** : le nom de l'agrégat dépend désormais de
  `github.event.label.name` (#3198) ; l'évaluateur local du témoin reçoit
  `label_name` (défaut vide) et vérifie les deux noms — `tests (ci-macos,
  hors gel)` sur l'étiquette, `tests` sur un gel ordinaire.
- **Témoin #876** : le job `tests` garde `always()` et exclut l'étiquette
  `ci-macos` (#3198) ; le témoin évalue la condition livrée (vraie sur une
  annulation ordinaire, fausse sur `ci-macos`) au lieu d'exiger `always()`
  seul.
- **Ordre de l'aide (#2179)** : #390 avait inséré `ameesh service …` entre
  `notify` et `offload` ; le bloc rejoint sa place alphabétique, entre
  `serve` et `session`. Aucune commande ne change.

Tests ciblés verts sur `3460bd874` : `test_2446_train_contract`,
`test_1635_dispatch_porte`, `test_l148_stage2b_scripts`,
`test_2179_roadmap_cli`, `test_l68_macos_ci`, `test_3108_train_contract_jobs`,
`test_3098_train_contract_pins` ; `scripts/freeze-checks.py` 6/6. La suite
complète est redemandée sur la nouvelle tête gelée (PR #2447).

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #3116 | avis dev2 absent sur la remise `f4656934` au moment de composer (seul cx2 y a un avis ; condition mesh-design #129755 non remplie) ; au paquet suivant |
| #3231 | témoin EP7 : réservé au premier paquet après la bascule T0 22:00Z, composé par le pilote élu (mesh-design #129908) |
| #3223 | témoin EP7 (doc seule, en build) : même réserve que #3231 (mesh-design #129908) |
| #2914 | lecture impossible : provenance d'auteur inconnue pour `66a06cf0`, porte non lisible |
| #2994 | lecture impossible : provenance d'auteur inconnue pour `5158130a`, `d8c377cf`, porte non lisible |

## Contrôles

- **Liste calculée** (21:04Z, base `5cb7b7fb2`, code `3ca71759` tant que la
  base est à 137 migrations) : 10 prêts (#3201, #114, #3198, #3209, #3219,
  #3236, #3108, #3137, #3187, #3196) ; **10/10 composés**, plus #390 sur
  directive #130179 (remise 2/2, absente du calcul de l'ancien code) :
  **11 unités**. Deux lectures impossibles (#2914, #2994), non bloquantes
  pour ce véhicule. Aucun lot prêt laissé hors paquet.
- **Contrôles statiques** : `scripts/freeze-checks.py` **6/6** verts
  (12,3 s) sur cet arbre.
- **Diff** : 68 fichiers, +4732/−542 ; `git diff --check` propre ; aucun
  marqueur de conflit ni de saut de CI dans les commits de composition.
- **Migrations** : aucune ajoutée par le paquet ; la base 1.20.0 porte
  `0475`–`0478`, appliquées par le déploiement release avant la CI.
- **Prévalidation** : indisponible (`card-stale`, carte de sélection de plus
  de 48 h), non bloquant (§3) ; motif consigné.
- **Suite complète** : demandée une fois sur la tête poussée, par la CI de la
  PR (§12). Aucune CI de lot.

Tête composée (unités, avant le commit du manifeste) : `518698a0c309`.
Tête corrigée après revue : `3460bd8743` ; ce manifeste est la tête gelée.
