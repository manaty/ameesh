# Paquet integration/paquet-h-20261024-06 — composition (§12)

Base : `main` **`6495881f4a05`** (fusion du paquet h-05, PR #2483, 07:41Z).
Branche `integration/paquet-h-20261024-06`, cible `main`. Porteur : **#3319**.
Composeur : mty-amh-impl-05. Une seule suite complète est demandée sur la tête
finale (CI de la PR).

## Sources

- mesh-design, message **#132349** (24/10 07:54Z) : h-06 confié à
  mty-amh-impl-05 sur `6495881f4a05` (h-05 fusionné) ; composer les lots prêts
  du calcul, relancer `scripts/paquet-candidats.py` au moment de composer et
  écarter tout conflit avec un motif au manifeste. #3306 corrige le témoin
  `test_3293` rougi en CI de h-05 ; #3316 est fermé et remplacé par #3306.
  Une CI complète sur la tête, puis fusion sous garde de tête dès le vert ;
  les rouges d'infra et les tests instables sont non concluants (§3) et ne
  retiennent pas le paquet.
- Calcul `scripts/paquet-candidats.py --project ameesh --base 6495881f4a05`
  (24/10 07:56Z, code de `main`) : **10 lots prêts**, 5 en attente, 0 erreur.
  Prêts : #2914, #3231, #3292, #3299, #3300, #3301, #3308, #2614, #3297,
  #3306. Neuf sont composés ; #3231 est écarté avec le motif ci-dessous.
- Le courrier du pilote citait 9 lots (sans #3297). #3297 a reçu son verdict
  `ok` de l'auditeur à 07:56Z (verdict 17577) : prêt au calcul, il est
  composé. #3231, cité par le pilote, reste tenu par le hold ci-dessous.
- #2614 : unhold de mesh-design du 24/10 06:42Z — la condition est remplie
  (la porte de fusion d'agents-2 tourne sur `venv-61287c4`, qui contient
  #2994 `37e6e489`) ; h-05 était déjà composé, il entre donc ici. Gel
  `b6b97f08` et verdicts cx3/impl-04 inchangés.
- #2914 : le gel `d9744d68` est la reprise demandée par l'arbitrage du pilote
  du 24/10 06:53Z (retrait du correctif cx2, branche de secours conservée) ;
  2/2 sur cette tête. #3313 (correctif cx2) reste à rebaser après la fusion.
- #3306 : #3316 fermé, remplacé par #3306 ; le témoin ne dépend plus de
  l'espace libre réel, il est vert en local sur l'arbre intégré.

## Admissions (9 lots, 9 unités)

| lot | source composée | classe | avis (gate) | objet |
|---|---|---|---|---|
| #3308 | `223e961ba49e` | sensitive | 2/2 : auditeur (claude), verdict 17538 ; ameesh-cx1 (codex), verdict 17537 | porte : la cible résolue se lit sans `baseRefOid` (gh 2.45), auto-merge réarmé |
| #3301 | `d06d5d380e7a` | sensitive | 2/2 : ameesh-dev1 (claude), verdict 17500 ; mty-amh-impl-01 (deepseek), verdict 17512 (blocage du même relecteur levé par l'avis final) | provenance : les commits non reçus d'un gel antérieur ne comptent plus comme inconnus |
| #2914 | `d9744d68968c` | sensitive | 2/2 : ameesh-cx1 (codex), verdict 17547 ; ameesh-dev3 (deepseek), verdict 17560 | train : le canon lu par la porte est comparé au pin, écart signalé en ombre et refusé en actif |
| #3300 | `2b7d5995a877` | sensitive | 2/2 : ameesh-cx1 (codex), verdict 17481 ; ameesh-dev3 (deepseek), verdict 17499 | avis dus : un seul calcul sans cast SQL, la reprise d'inactivité les nomme |
| #2614 | `b6b97f080099` | sensitive | 2/2 : ameesh-cx3 (codex), verdict 15289 ; mty-amh-impl-04 (deepseek), verdict 15294 | réveil programmé : claim tenu jusqu'au lancement, une ligne `wake:` par consommation |
| #3299 | `bf695c8c6970` | sensitive | 2/2 : auditeur (claude), verdict 17552 ; ameesh-cx1 (codex), verdict 17553 | agent move : déplacement manuel d'un `releve: hote`, bail source révoqué dans la même écriture |
| #3292 | `561541c93ab0` | light | 1/1 : ameesh-cx1 (codex), verdict 17488 | débit : mesuré sur les jalons, en tête de chaque passage de l'auditeur |
| #3297 | `bedd447770f2` | light | 1/1 : auditeur (claude), verdict 17577 | staging hors créneau : seules les attentes de déploiement réelles comptent |
| #3306 | `9189bcc21b1a` | light | 1/1 : auditeur (claude), verdict 17573 | témoin du cache npm : plus de dépendance à l'espace libre réel |

## Composition (9 unités, 9 lots)

| unité | lot | source | commit | note |
|---|---|---|---|---|
| 1 | #3308 | `223e961ba49e` | `2551907ff625` | 0 conflit |
| 2 | #3301 | `d06d5d380e7a` | `ab0d8aab1c21` | 0 conflit |
| 3 | #2914 | `d9744d68968c` | `9892ae3f2ddb` | 0 conflit |
| 4 | #3300 | `2b7d5995a877` | `64591b365813` | 0 conflit |
| 5 | #2614 | `b6b97f080099` | `6b384a4a40fa` | 0 conflit |
| 6 | #3299 | `bf695c8c6970` | `e926869de727` | 0 conflit |
| 7 | #3292 | `561541c93ab0` | `6557742084d5` | 0 conflit |
| 8 | #3297 | `bedd447770f2` | `42feef5ce6a6` | 0 conflit |
| 9 | #3306 | `9189bcc21b1a` | `ff7852d755be` | 0 conflit |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot
et sa tête, et il porte un trailer `ameesh-work:`. Aucun commit de `main` n'est
venu pendant la composition (base `6495881f4a05` figée).

## Contrôles

- **Liste calculée** : 10 prêts, **9/9 composés** ; 1 prêt écarté avec motif
  (#3231) ; 5 en attente hors paquet, motifs calculés ci-dessous.
- **Conflits** : 9/9 unités fusionnées par `ort` sans intervention ; aucun
  marqueur de conflit, aucun arbitrage de code.
- **Tests ciblés de l'arbre intégré** : **609/609 verts**
  (`test_3308_gh_base_ref`, `test_2813_packet_auto_merge`,
  `test_2876_packet_replacement`, `test_3301_prior_freeze_unknown`,
  `test_2914_train_canon_drift`, `test_2446_train_assembly` : 149 ;
  `test_3033_pending_reviews`, `test_789_slo`, `test_3292_debit`,
  `test_1466_relecture_config`, `test_l165c_sessions_par_lot` : 181 ;
  `test_l31_relocation`, `test_3291_staging_pending`,
  `test_2448_release_cutoff`, `test_3293_cache_pressure` : 108 ;
  `test_1617_review_designation`, `test_1617_review_designation_pg`,
  `test_2447_review_designation_activation`,
  `test_2924_history_rewrite_provenance` : 171).
- **Compilation** : `compileall src tests` verte.
- **Contrôles statiques** : 6/6 verts (`scripts/freeze-checks.py`).
- **Diff** : 49 fichiers, +5437/−272 (manifeste non compris) ;
  `git diff --check` vert ; les neuf commits de composition portent leur
  trailer `ameesh-work:`.
- **Migrations** : aucune (aucun fichier SQL ni migration).
- **Prévalidation** : demandée au gel ; carte périmée (`card-stale`) attendue,
  non bloquante (§3), motif consigné.
- **Suite complète** : une seule suite par tête poussée, par la CI de la PR
  (`ci-full-<sha>`) après les avis requis ; aucune CI de lot. Les neuf unités
  gardent leurs verdicts acquis sur leurs têtes gelées ; le porteur #3319 est
  gelé sur la tête finale et ses relecteurs rendent leurs avis sur cette tête.
- **Fusion** : sous garde de tête (`--match-head-commit`), dès le vert, par le
  pilote ou le bureau. Seuls les rouges d'infra ou les tests instables sont
  consignés et ne retiennent pas le paquet (§3) ; tout autre rouge isole le
  lot fautif sans relire le reste.
- **Coupe 1.21.0** : aucune coupe demandée par ce véhicule ; la fusion du
  paquet sur `main` ne dépend pas de la coupure.

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #3231 | **prêt au calcul et cité par le pilote**, mais tenu hors paquet par le hold de mesh-design du 24/10 03:18Z : témoin EP7 (#3015/#3024), il n'entre qu'au premier paquet après T0 (~10:15Z, 1.21.0 déployée sur les 3 hôtes, conditions confirmées par le bureau). T0 n'est pas atteint à 08:00Z ; le hold du 03:18Z et la note de tenue du 04:17Z sont inchangés, réveil de contrôle à 09:15Z. Gel `1af34785` et verdict ameesh-dev2 (claude) acquis, aucune action. |

## En attente (calcul, hors paquet)

| lot | motif |
|---|---|
| #2987 | hold bureau (porte de production §5) ; branche `canon/2987-porte-prod-main` non publiée sur origin. |
| #3116 | `verdicts-missing` (1 seule famille, ameesh-dev2) et conflit `src/ameesh/review_authors.py`. |
| #3186 | `freeze-after-verdict` et conflit `src/ameesh/release_cutoff.py`. |
| #3195 | `blocked-verdict` sur la tête gelée effective. |
| #3312 | `verdicts-missing` (1 seule famille, ameesh-dev5). |
