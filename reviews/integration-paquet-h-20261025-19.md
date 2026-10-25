# Paquet integration/paquet-h-20261025-19 — composition (§12), recomposition

Base : `main` **`5372cf7504993d168e310f6363e8840edf65d74b`** (fusion du paquet
h-18, PR #2699, 25/10 10:57:22Z).
Branche `integration/paquet-h-20261025-19`, cible `main`, PR #2735. Porteur :
**#3538** (créé par mesh-design, 25/10 10:59Z). Composeur : mty-amh-impl-01.

**Recomposition (25/10 11:55Z)** : décision pilote mesh-design (mail 11:54Z).
La première tête `f8cb071b4a47` (9 unités, gel #18918, verdict #18933, feu
#18922) est remplacée : sa suite complète `40188289476` est rouge sur
`psql 9/16` et `psycopg 5/8` — **10 échecs négatifs et déterministes** de
`tests.test_3036_alert_liveness` (8) et `tests.test_3057_panier_vivacite` (2).
Le pilote a reproduit le rouge sur la tête de #3512 seule et bissecté : vert
sur `cb3cb3917` (#3492), rouge sur `1d20f1d83` (#3512). **#3512 est retiré du
paquet** (§11 bis/§12, lot fautif retiré sans relire le reste) ; le run
`40188289476` est annulé. Nouvelle composition : **8 unités, 8 lots**, sans
#3512. Le gel, le verdict et le feu de l'ancienne tête ne sont pas reportés :
le pilote donne son verdict et son feu sur la nouvelle tête.

## Sources

- mesh-design, 25/10 11:00Z, mail #138223 : ordre et motifs — #3513 puis
  #3518 en premier (correctifs de la porte d'indépendance qu'attendent le
  paquet Nexlink 174, #3526, #3529, H5 et d'autres), puis H5 #3472, puis
  #3530, #3522, #3527, puis #3492, #3512. Les 8 sont prêts selon
  paquet-candidats ; OK et feu du pilote sur la tête gelée, à prévenir au gel.
- Liste calculée `packet_candidates.inventory` du 25/10 11:08Z, base
  `5372cf750`, cible `main`, projet `ameesh` explicite : **9 lots prêts**,
  10 en attente, 0 erreur. Aux 8 du pilote s'ajoute **#3535**
  (`4fa98ac7e07d`, light, gel 11:05Z, verdict cx3 #18890), gelé après la liste
  du pilote : composé en dernier (#2404). #3512, prêt à l'inventaire, est
  retiré par la décision ci-dessus.
- mesh-design, 25/10 11:54Z, décision de recomposition : annuler
  `40188289476` ; recomposer sans #3512, 8 autres unités dans le même ordre,
  #3535 compris ; regeler, packet record, manifeste avec le motif ; relancer
  une seule CI complète. Le pilote pose son verdict et son feu sur la
  nouvelle tête.
- Écart d'hôte connu (`--project ameesh` : `project 'ameesh' is not declared
  in radar.projects`) : la liste est produite par le même module en lecture
  seule, cible et base explicites — même écart que les manifestes h-16, h-17
  et h-18.

## Admissions (8 lots, 8 unités)

| lot | source composée | avis au gel | objet |
|---|---|---|---|
| #3513 | `2004ab033be9` | 2/2 (docs-01 deepseek, dev4 claude) — auteur cx2 codex | Porte des membres : la plage relue exclut les têtes gelées des membres antérieurs admis |
| #3518 | `d60446ee863d` | 2/2 (dev1 claude, dev3 deepseek) — auteur cx2 codex | Porte d'avis : une correction de tests seuls laisse sa famille éligible pour le delta produit |
| #3472 | `2bff9eba2b42` | 2/2 reportés par alignement (cx1 codex, auditeur claude) — auteur impl-07 deepseek | Handoff réel H5 : raccord au runner (`_lifecycle_adapter`, readiness, advance_host) |
| #3530 | `add4fb27f177` | 2/2 (dev1 claude, cx3 codex) — auteur impl-06 deepseek | CI non concluante : clé de dédoublonnage sans « (état; URL) », parts « tests (flux) » comptées comme des jobs |
| #3522 | `352bbec157f8` | 1 requis (cx3 codex) — auteur impl-03 deepseek | psql : les témoins de durée de part ne dépendent plus du nombre de tests |
| #3527 | `3bf21cbceab3` | 1 requis, 2 rendus (dev4 claude, docs-01 deepseek) — auteur cx3 codex | Release : la fusion d'atterrissage du tick porte son identité git |
| #3492 | `02f988417676` | 2/2 (cx3 codex, dev4 claude) — auteur qa-01 deepseek | Panier « registry » : réveil d'un agent retiré, défauts non bloquants |
| #3535 | `4fa98ac7e07d` | 1 requis (cx3 codex) — auteur docs-01 deepseek | CI : `suite_tolerated` s'applique aussi aux checks « tests (flux) » d'une part tolérée |

## Avis au gel

| lot | gel | verdicts |
|---|---|---|
| #3513 | 18801 | 18807 mty-amh-docs-01 (deepseek), 18810 ameesh-dev4 (claude) |
| #3518 | 18797 | 18808 ameesh-dev1 (claude), 18821 ameesh-dev3 (deepseek) |
| #3472 | 18903 | 18904 ameesh-cx1 (codex), 18905 auditeur (claude), reportés par alignement depuis 4c6063a7 |
| #3530 | 18848 | 18850 ameesh-dev1 (claude), 18851 ameesh-cx3 (codex) |
| #3522 | 18825 | 18827 ameesh-cx3 (codex), 1 requis (light) |
| #3527 | 18820 | 18824 ameesh-dev4 (claude), 18826 mty-amh-docs-01 (deepseek) |
| #3492 | 18735 | 18742 ameesh-cx3 (codex), 18746 ameesh-dev4 (claude) |
| #3535 | 18884 | 18890 ameesh-cx3 (codex), 1 requis (light) |

## Composition (8 unités, 8 lots)

| unité | lot | source | commit | note |
|---|---|---|---|---|
| 1 | #3513 | `2004ab033be9` | `d09db8a37` | 0 conflit, recomposé |
| 2 | #3518 | `d60446ee863d` | `48da6a201` | 0 conflit, recomposé |
| 3 | #3472 | `2bff9eba2b42` | `be66d096c` | 0 conflit, recomposé ; voir rebase ci-dessous |
| 4 | #3530 | `add4fb27f177` | `af9c76de8` | 0 conflit, recomposé |
| 5 | #3522 | `352bbec157f8` | `7f825048d` | 0 conflit, recomposé |
| 6 | #3527 | `3bf21cbceab3` | `5006de67d` | 0 conflit, recomposé |
| 7 | #3492 | `02f988417676` | `cb3cb3917` | 0 conflit, recomposé |
| 8 | #3535 | `4fa98ac7e07d` | `b6f3ffce4` | 0 conflit, recomposé |

Chaque commit de composition porte le lot en second parent, son sujet nomme le
lot et sa source (`vehicule 1900 : #<lot> <source>`) et il porte un trailer
`ameesh-work:`. La base n'a pas bougé pendant la composition.

### H5 #3472 — tête rebasée pendant la composition

H5 a été rebasé mécaniquement sur `main` `5372cf750` à 11:13Z (H3/H4 désormais
dans `main`) : gel #18903 sur `2bff9eba2b42`, patch-id du delta identique à la
tête relue `4c6063a7`, verdicts cx1 et auditeur reportés par alignement
(#18904, #18905), ciblés 14/14 verts et statiques 6/6. La composition a
d'abord été faite sur `4c6063a7`, puis refaite sur `2bff9eba2b42` pour que la
source composée soit exactement la tête gelée publiée ; l'arbre avec les 9
unités était identique à la composition sur `4c6063a7`. La recomposition à 8
unités ne change que la présence de #3512.

## Conflits

Aucun. Les 8 unités se sont fusionnées sans intervention sur le véhicule ;
`git diff --check` est vert avant le gel. La recomposition sans #3512 n'a
produit aucun conflit.

## Écartés (ce véhicule)

| lot | état / motif calculé | motif |
|---|---|---|
| #3512 | qa, prêt à l'inventaire 11:08 | **retiré par la décision pilote 11:54Z** : suite complète 40188289476 rouge sur `psql 9/16` et `psycopg 5/8` (10 échecs test_3036/test_3057, reproduits sur sa seule tête, bissection vert #3492 / rouge #3512) ; retour à l'auteur, hors paquet |
| #3501 | qa, verdicts-missing | conflit `estimates.py`, retour à dev2 (pilote) |
| #3533 | qa, verdicts-missing | verdict bloquant, dépend de #3513 (pilote) |
| #3521 | build | en build (pilote) ; les témoins psycopg de `test_3503_jit` restent non bloquants (§3) |
| #3473 | qa, verdicts-missing (H6) | pour h-20 (pilote) |
| #3474 | qa, verdicts-missing (H7) | pour h-20 (pilote) |
| #3509 | qa, verdicts-missing (H8) | pour h-20 (pilote) |
| #3526 | freeze-not-head | pas prêt (gel non tête) |
| #3529 | freeze-after-verdict | pas prêt |
| #3457 | branch-not-published | pas prêt |
| #3506, #3532 | verdicts-missing | pas prêts |

## Contrôles

- **Liste calculée** : 9 prêts, 10 en attente, 0 erreur à 11:08Z. 8 composés ;
  #3512, prêt mais fautif, est écarté avec le motif ci-dessus. Les 7 unités du
  pilote sont dans l'ordre demandé ; #3535 (prêt non cité, light) est dernier.
- **Rouge isolé** : le run `40188289476` est annulé ; ses échecs sont
  strictement ceux de #3512 (test_3036/test_3057), reproduits par le pilote
  sur la tête de #3512 seule. Aucun autre test négatif n'y était observé.
- **Contrôles statiques du gel** (`scripts/freeze-checks.py`) : rejoués par le
  gel sur la nouvelle tête gelée (garde mécanique #2449, compilation,
  attribution #1632, lint L63, alert types #1060, doc readers L161a).
- **Prevalidation** : la carte est plus ancienne que 24 h ; si la sélection
  reste incertaine, le motif est journalisé par le gel — non bloquant (§3).
- **Suite complète** : une seule CI complète sur la nouvelle tête gelée,
  demandée après le verdict et le feu du pilote. #3512 n'y est plus.
- **Porte de fusion** : verdict et feu du pilote sur la nouvelle tête ; la
  fusion automatique part au vert, pendant ou après le drill #3439 (12:55Z),
  comme accepté par le pilote.
- **Après fusion** : staging hors créneau (§11 bis) pour porter #3513/#3518 au
  paquet Nexlink 174 et aux autres ; appliquer les migrations additives
  0482–0484 avant de relancer les exécuteurs (préalable #3510, 10:59Z).
