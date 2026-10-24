# Paquet integration/paquet-h-20261024-04 — composition (§12)

Base : `main` **`6f2e7c1470c8`** (fusion du paquet h-03, PR #2464, 04:05Z ;
CI full `40058036839` verte sur `5571dbd13`).
Branche `integration/paquet-h-20261024-04`, cible `main`. Porteur : **#3295**.
Composeur : mty-amh-impl-05. Une seule suite complète est demandée sur la tête
finale (CI de la PR).

## Sources

- mesh-design, message **#131728** (24/10 03:53Z) : composer h-04 dès la
  fusion de h-03 sur la nouvelle tête de `main`, priorité #3275, #3220 (remise
  `de1c3f59`, OK claude) et #3116 (remise `f0cfa296`, exception étendue par la
  décision **#131656**) ; consigner au lot le motif de tout écart.
- Calcul `scripts/paquet-candidats.py` (24/10 04:13Z, base `6f2e7c147`, code
  du déploiement 1.20.0) : **6 lots prêts** (#3244, #3220, #3226, #3231,
  #3277, #3120), 12 en attente, 1 lecture impossible (#2914).
- mty-amh-impl-08, message du 24/10 04:13Z : #3120 prêt sur `bf22b5cd`, deux GO
  (cx1 codex, dev1 claude), merge-tree propre sur `6f2e7c147`.
- ameesh-dev5, notes de #3220 (24/10 02:55–03:09Z) : remise `de1c3f59`
  (`refs/ameesh/resolutions/3220/main-8c97c71f`, parents `8c97c71f` et
  `d8f57e86`) prête, couverte par les verdicts auditeur + cx1 une fois #3003
  dans `main`.
- Décisions signées **#127150** et **#131656** (human:smichea, preuve VALID) :
  l'exception de la porte vaut pour la remise `f0cfa296` de #3116. Elle n'est
  **pas consommée** dans ce véhicule : `f0cfa296` ne fusionne pas sur la
  nouvelle tête (voir Écartés) ; une nouvelle remise devra être relue.

## Admissions (4 lots, 4 unités)

| lot | source composée | classe | avis (gate) | objet |
|---|---|---|---|---|
| #3244 | `7fae34067967` | light | 1/1 : ameesh-dev3 (deepseek), verdict 17384 | release_cutoff : un job facultatif non conclu (annulé, en cours, expiré) ne retire plus un run de main des candidats |
| #3220 | `de1c3f5938c3` | sensitive | 2/2 : auditeur (claude), verdict 17329 ; ameesh-cx1 (codex), verdict 17331 — remise `refs/ameesh/resolutions/3220/main-8c97c71f`, couverture `verdict` | provenance 2924 B8 : un auteur d'un gel antérieur ne compte plus quand son contenu ne survit pas dans la tête (fichier supprimé) |
| #3226 | `f4c45a7e35dc` | sensitive | 2/2 : ameesh-cx1 (codex), verdict 17386 ; ameesh-dev3 (deepseek), verdict 17390 | ménage : les scopes systemd `dsh-subprocess-*` sont récoltés par le drain ; signal par pidfd tenu avant tout envoi, scope gardé et signalé sinon |
| #3120 | `bf22b5cd4a5f` | sensitive | 2/2 : ameesh-cx1 (codex), verdict 17358 ; ameesh-dev1 (claude), verdict 17360 | drain hôte : la barrière d'une pose incertaine tient malgré un succès rapporté |

## Composition (4 unités, 4 lots)

| unité | lot | source | commit | note |
|---|---|---|---|---|
| 1 | #3244 | `7fae34067967` | `06cf47e4e` | 0 conflit |
| 2 | #3220 | `de1c3f5938c3` | `40777e970` | 0 conflit (remise) |
| 3 | #3226 | `f4c45a7e35dc` | `54693dc9a` | 0 conflit |
| 4 | #3120 | `bf22b5cd4a5f` | `fca3a89cc` | 0 conflit |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot
et sa tête, et il porte un trailer `ameesh-work:`. Aucun commit de `main`
n'est venu pendant la composition.

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #3231 | **prêt au calcul**, mais **tenu hors paquet** par le hold de mesh-design du 24/10 03:18Z : témoin EP7 (#3015/#3024), n'entre qu'au premier paquet après T0 (~10:15Z, 1.21.0 déployée sur les 3 hôtes). Gel `1af34785` inchangé, verdict ameesh-dev2 (claude) ok le 03:18. |
| #3277 | **prêt au calcul** (merge-tree propre sur `6f2e7c147`) mais **conflit sémantique réel avec #3266**, fusionné par h-03 : sur l'arbre intégré, 4 témoins de sûreté de `tests/test_3277_tmp_over_quota.py` échouent (`a_bind_mount…`, `a_deep_write…`, `former_key_keeps…`, `former_key_removes…`), le motif de conservation devenant « no lot named ». Le correctif prévu par ameesh-dev2 (jeton `diag` dans `NAME_TOKEN`) n'est pas encore poussé ; reprise h-05 sur la tête refaite, avis delta. |
| #3275 | conflit avec la nouvelle tête de `main` sur `src/ameesh/lot_measures.py` et `src/ameesh/review_lot.py` (`PUBLISHED_TIP_TTL_S` de #3056 contre le rafraîchissement de la cible de #3275). Aucune remise préparée ni relue sur `6f2e7c147` ; la tête gelée `eeba7824` et ses 2 avis (cx3, dev3) restent acquis. Rafraîchissement par ameesh-dev4 puis h-05. |
| #3116 | en attente au calcul (gel sans 2 avis) et sa remise `f0cfa296` **ne fusionne plus** sur `6f2e7c147` : conflit sur `src/ameesh/review_authors.py` avec #3003, fusionné par h-03. L'exception signée #131656 nomme cette remise, pas une nouvelle résolution : une remise sur la nouvelle tête doit être préparée et relue (porte de fusion, 2 familles) avant h-05. Les avis dev2 #17372 et cx2 #17382 sur `f0cfa296` restent la trace de la relecture passée. |
| #2987 | hold bureau (porte de production §5), hors paquet. |
| #3033, #3195 | verdict `blocked` sur la tête gelée effective. |
| #2614, #3186, #3291, #3293 | verdicts requis manquants sur la tête gelée. |
| #3003, #3266, #3279 | déjà dans la cible (tête gelée ancêtre de `main`) ; `sync-merges` doit les clore. |
| #2914 | lecture impossible : gel antérieur `66a06cf0` absent du dépôt, la porte n'est pas lisible ; non bloquant (§3), consigné. |

## Contrôles

- **Liste calculée** : 6 prêts, **4/4 composés**, 2 prêts écartés avec motif
  (#3231 hold, #3277 conflit sémantique) ; les 4 lots prioritaires cités par
  mesh-design sont traités : #3220 composé, #3275 et #3116 écartés avec motif
  sur leur fiche et ici, #3277 écarté par le témoin.
- **Conflits** : 4/4 unités fusionnées par `ort` sans intervention manuelle ;
  aucun marqueur de conflit.
- **Tests ciblés de l'arbre intégré** : 234/234 verts sur la tête
  `fca3a89cc` (`test_3244_release_optional_jobs`,
  `test_3220_b8_2994_reconciliation`, `test_3220_deleted_prior_content`,
  `test_2924_history_rewrite_provenance`,
  `test_2977_inherited_content_lines`,
  `test_2994_reorder_and_prefix_survival`,
  `test_3003_reorder_relative_order`, `test_3226_dsh_scopes`,
  `test_3114_host_wide_drain`, `test_503_agent_host`).
- **Diff** : 16 fichiers, +3701/−135 (manifeste non compris) ; `git diff
  --check` propre ; les quatre commits de composition portent leur trailer
  `ameesh-work:`.
- **Migrations** : aucune (aucun fichier SQL ni migration ; le script de
  déploiement de #3120 ne touche pas la base).
- **Prévalidation** : indisponible (`card-stale`, carte de sélection de plus
  de 48 h), non bloquant (§3), motif consigné.
- **Suite complète** : une seule suite par tête poussée, par la CI de la PR
  (§12), `--avis 0` — la revue unitaire du paquet est ce manifeste (#2240) et
  les quatre unités ont leurs verdicts acquis sur leurs têtes gelées. La
  désignation automatique du porteur a rendu 2 relecteurs (cx1, dev3) parce
  qu'elle lit le contenu des unités dans la plage `base..tête` ; le delta
  propre du porteur est le seul manifeste (documentation), donc aucune
  relecture supplémentaire n'est due et la demande de CI porte `--avis 0`,
  comme h-03. Aucune CI de lot.
