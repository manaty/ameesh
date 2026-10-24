# Paquet integration/paquet-h-20261024-05 — composition (§12)

Base : `main` **`ad1ab350a70e`** (fusion du paquet h-04, PR #2474, 05:53Z).
Branche `integration/paquet-h-20261024-05`, cible `main`. Porteur : **#3305**.
Composeur : mty-amh-impl-05. Une seule suite complète est demandée sur la tête
finale (CI de la PR).

## Sources

- mesh-design, message **#132050** (24/10 06:13Z) : h-05 confié à
  mty-amh-impl-05 sur `ad1ab350` ; composer #3296, #3293, #3291, #3294 et
  #3277 si sa remise est rebasée à temps. Consigne du bureau 06:15Z rappelée :
  pc-smichea est éteint pour la journée (`release.host`), la coupe 1.21.0
  n'aura pas lieu avant son retour — sans effet sur la fusion du paquet.
- Calcul `scripts/paquet-candidats.py --project ameesh` (24/10 06:20Z, base
  `ad1ab350`, code de `main`) : **8 lots prêts**, 6 en attente, 0 erreur.
  Prêts : #3231, #3275, #3277, #3291, #3293, #3296, #2614, #3294.
  Lecture complète sur `origin`, verdicts et merge-tree de la porte.
- Manifeste h-04 : #3275 était écarté (conflit avec #3056) « rafraîchissement
  par ameesh-dev4 puis h-05 » ; #3277 était écarté (conflit sémantique avec
  #3266) « reprise h-05 sur la tête refaite, avis delta ». Les deux reprises
  sont arrivées à temps.
- ameesh-dev4, notes de #3275 (06:05Z) : regel `1a38e40f` = `eeba7824` remis
  sur `ad1ab350`, delta de résolution seul, 2 avis demandés sur ce delta
  (cx1, dev3).
- ameesh-dev2, notes de #3277 (02:55–06:07Z) : reprise du jeton `diag` et du
  refus des liens symboliques ; gel `eaec064e` sur `ad1ab350`, avis cx1 et
  dev3 sur la tête exacte.
- ameesh-cx3, note de #3291 (05:56Z) : avis ameesh-dev1 OK sur `4753927b`,
  résolution #3186 `65116776` disponible pour la suite ; N1/N2 au panier
  #3297.
- ameesh-cx1, notes de #3296 (05:49Z) : relecture du diff exact
  `6f2e7c147..4c8366ac`, OK.

## Admissions (6 lots, 6 unités)

| lot | source composée | classe | avis (gate) | objet |
|---|---|---|---|---|
| #3296 | `4c8366ac7a38` | sensitive | 2/2 : auditeur (claude), verdict 17432 ; ameesh-cx1 (codex), verdict 17430 | file d'amélioration : la lecture backlog prête filtre `planned_start`, témoins futur/passé |
| #3275 | `1a38e40f23d9` | sensitive | 2/2 : ameesh-dev3 (deepseek), verdict 17466 ; ameesh-cx1 (codex), verdict 17455 | relecture après fusion : fetch de la cible exacte avant l'intervalle, doute signalé si le SHA reste absent |
| #3291 | `4753927bb829` | sensitive | 1/1 : ameesh-dev1 (claude), verdict 17435 | staging hors créneau : détection des lots retenus par un correctif non déployé, coupe sans recul sous l'actif |
| #3293 | `664a6c641e76` | sensitive | 1/1 : auditeur (claude), verdict 17461 (et 17416) | ménage : cache npm évincé sous pression sans binaire npm, déficit libéré |
| #3294 | `5613226c7646` | sensitive | 1/1 reporté : ameesh-cx3 (codex), verdict 17447 (gel 17446) | ménage : scopes dsh — relevé récursif, doutes signalés, grâce bornée, aucune entrée perdue |
| #3277 | `eaec064e88cc` | sensitive | 2/2 : ameesh-cx1 (codex), verdict 17460 ; ameesh-dev3 (deepseek), verdict 17475 | ménage : quota tmp d'agent alerté même arrêté, dossier de lot clos supprimable, lien symbolique refusé |

## Composition (6 unités, 6 lots)

| unité | lot | source | commit | note |
|---|---|---|---|---|
| 1 | #3296 | `4c8366ac7a38` | `81e2f91fc4a6` | 0 conflit |
| 2 | #3275 | `1a38e40f23d9` | `fdd056de8a8e` | 0 conflit |
| 3 | #3291 | `4753927bb829` | `7748b4649fd9` | 0 conflit |
| 4 | #3293 | `664a6c641e76` | `0316d7baa83d` | 0 conflit |
| 5 | #3294 | `5613226c7646` | `18dd00ffcc9d` | 0 conflit |
| 6 | #3277 | `eaec064e88cc` | `2f2dde2119c6` | 1 conflit documentaire résolu |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot
et sa tête, et il porte un trailer `ameesh-work:`. Aucun commit de `main` n'est
venu pendant la composition (base `ad1ab350` figée).

Conflit de l'unité 6 : `docs/EXPLOITATION.md`, ligne `dsh_scope`. #3294 avait
précisé cette ligne (grâce bornée, `gone`), #3277 ajoutait la ligne
`tmp_over_quota` immédiatement après. Résolution mécanique : ligne `dsh_scope`
de #3294 conservée, ligne `tmp_over_quota` de #3277 ajoutée ; aucun autre
changement, aucun arbitrage sémantique. `git diff --check` ne signale sur ce
fichier qu'une fin de fichier inchangée.

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #3231 | **prêt au calcul**, mais tenu hors paquet par le hold de mesh-design du 24/10 03:18Z : témoin EP7 (#3015/#3024), n'entre qu'au premier paquet après T0 (~10:15Z, 1.21.0 déployée sur les 3 hôtes). La coupe est reportée (pc-smichea éteint, bureau 06:15Z) : T0 n'est pas atteint. Gel `1af34785` et verdict ameesh-dev2 (claude) ok inchangés. |
| #2614 | **prêt au calcul**, mais tenu hors paquet par le hold de mesh-design du 24/10 00:15Z : entre au premier paquet après le déploiement de la 1.21.0, qui doit porter #2994. Coupe reportée : le hold tient. Gel `b6b97f08`, 2 avis (cx3 codex, mty-amh-impl-04 deepseek). |
| #3116 | en attente : gel `64d56064` sans 2 avis et conflit `src/ameesh/review_authors.py` ; une nouvelle remise sur `ad1ab350` (`245cb500`, mty-amh-impl-06 06:05Z) attend sa relecture. Hors liste du pilote pour h-05. |
| #3186 | en attente : gel `fe203660` sans 2 avis et conflit `src/ameesh/release_cutoff.py` ; sa résolution avec #3291 (`65116776`) est disponible mais #3186 n'est pas prêt. |
| #2987 | hold bureau (porte de production §5) ; branche `canon/2987-porte-prod-main` non publiée sur origin. |
| #3195, #3292 | verdict `blocked` sur la tête gelée effective. |
| #2914 | lecture impossible : gel antérieur `66a06cf0` absent du dépôt, la porte n'est pas lisible ; non bloquant (§3), consigné. |

## Contrôles

- **Liste calculée** : 8 prêts, **6/6 composés**, 2 prêts écartés avec motif
  (#3231, #2614, holds du pilote) ; les 6 lots de la liste du pilote sont
  traités : #3296, #3293, #3291, #3294 composés, #3277 composé après reprise
  sur `ad1ab350`, #3275 composé après son rafraîchissement (h-04 le prévoyait
  pour h-05).
- **Conflits** : 5/6 unités fusionnées par `ort` sans intervention ; 1 conflit
  documentaire (#3277 sur `docs/EXPLOITATION.md`) résolu par union des deux
  lignes ; aucun marqueur de conflit, aucun changement de code arbitré.
- **Tests ciblés de l'arbre intégré** : **151/151 verts** (`test_l119_backlog`,
  `test_3275_review_after_merge_fetch`, `test_3291_staging_pending`,
  `test_2448_release_cutoff`, `test_3293_cache_pressure`,
  `test_3226_dsh_scopes`, `test_3277_tmp_over_quota`,
  `test_2743_housekeeping_clones`).
  Première passe : 150/151, `test_pression_libe_re_le_cache_npm_pendant_un_tour`
  rouge par course sur l'espace libre (cible calculée à +4 KiB) ; vert seul et
  à la seconde passe complète — non concluant (§3), consigné, non bloquant.
- **Contrôles statiques** : 6/6 verts (`scripts/freeze-checks.py`).
- **Diff** : 25 fichiers, +2346/−144 (manifeste non compris) ; `git diff
  --check` signale une fin de fichier (ligne vide finale de
  `housekeeping_stale.py`, nouveau fichier), sans effet ; les six commits de
  composition portent leur trailer `ameesh-work:`.
- **Migrations** : aucune (aucun fichier SQL ni migration).
- **Prévalidation** : indisponible (`card-stale`, carte de sélection de plus
  de 48 h), non bloquant (§3), motif consigné.
- **Suite complète** : une seule suite par tête poussée, par la CI de la PR
  (§12). Les six unités ont leurs verdicts acquis sur leurs têtes gelées et
  leurs review-class lues par la porte ; le porteur #3305 est gelé sur la tête
  finale et ses relecteurs rendent leurs avis sur cette tête (aucune relecture
  supplémentaire n'est déclarée due à l'avance). Aucune CI de lot.
- **Coupe 1.21.0** : non demandée ici. pc-smichea est éteint pour la journée
  (release.host) ; la fusion du paquet sur `main` ne dépend pas de la coupe.
  La CI de `main` au commit `ad1ab350` (run 40068028315) est rouge de jobs
  annulés par l'extinction de pc-smichea en cours de run : non concluant
  (§3), à consigner comme incident d'infrastructure, pas comme test négatif.
