# Paquet integration/paquet-h-20261025-22 — composition (§12), sur main issu de h-21

Base : `main` **`0d8f1a0604c13ef081c5e4c6c13d210fd0369a1e`** (fusion du paquet
h-21, PR #2759, 25/10 16:20:37Z). Branche
`integration/paquet-h-20261025-22`, cible `main`, PR #2770. Porteur : **#3590**.
Composeur : mty-amh-impl-01 (deepseek, agents-1).

Consigne pilote du 25/10 16:06Z : #3542 en tête (correctif CRITICAL_SCOPE), puis
#3577, #3546, #3568 ; #3558 seulement s'il est prêt. Liste calculée
`paquet-candidats` recalculée sur `0d8f1a06` : les 6 unités ci-dessous sont
prêtes ; #3578 (gel #19263) et #3549 (gel #19116) sont entrés parce que prêts
sur cette tête et non écartés ; #3558 est en conflit avec `main`, il va en h-23.

## Admissions (6 lots, 6 unités)

| lot | source composée | avis au gel | objet |
|---|---|---|---|
| #3542 | `aa51432d6f4d` | 2/2 (cx3 codex, dev2 claude) | Porte d'avis : CRITICAL_SCOPE ne relève plus le nombre d'avis d'un chemin nommé |
| #3577 | `a91a3479d291` | 2/2 (dev1 claude, cx3 codex) | Porte d'avis : une correction de tests seuls sous chemin sensible garde sa famille éligible |
| #3546 | `bf52da1f541a` | 2/2 (cx3 codex, dev3 deepseek) | CI : purge et rétention des artéfacts, quota de l'org |
| #3568 | `f0c8efe88391` | 2/2 (auditeur claude, cx3 codex) | Élection : la passe alerts peut échoir à un détenteur sans route de push |
| #3578 | `daff373d9b4d` | 2/2 (cx3 codex, impl-06 deepseek) | Porte d'avis : la famille d'un verdict vient de son reçu, pas de la session courante |
| #3549 | `6ba2f53eaabb` | 2/2 (dev2 claude, cx3 codex) | pc-smichea : runners sous l'utilisateur Unix ameesh, runbook et script de migration réversible |

## Avis au gel

| lot | gel | verdicts |
|---|---|---|
| #3542 | 19048 | 19050 dev2 (claude) ok, 19061 cx3 (codex) ok |
| #3577 | 19217 | 19229 cx3 (codex) ok, 19239 dev1 (claude) ok |
| #3546 | 19171 | 19176 cx3 (codex) ok, 19200 dev3 (deepseek) ok, 19213 dev3 (deepseek) ok |
| #3568 | 19212 | 19225 cx3 (codex) ok, 19245 auditeur (claude) ok |
| #3578 | 19263 | 19272 impl-06 (deepseek) ok, 19274 cx3 (codex) ok |
| #3549 | 19116 | 19120 cx1 (codex) ok, 19122 dev2 (claude) ok, 19126 cx3 (codex) ok |

## Composition (6 unités, 6 lots)

| unité | lot | source | commit |
|---|---|---|---|
| 1 | #3542 | `aa51432d6f4d` | `589fe062a` |
| 2 | #3577 | `a91a3479d291` | `97348a6e3` |
| 3 | #3546 | `bf52da1f541a` | `f4c0199e8` |
| 4 | #3568 | `f0c8efe88391` | `a5485d797` |
| 5 | #3578 | `daff373d9b4d` | `7566ff4ba` |
| 6 | #3549 | `6ba2f53eaabb` | `148c807d8` |

## Écartés de h-22

| lot | motif |
|---|---|
| #3558 | conflit avec main sur `src/ameesh/packet_automerge.py` — rebase et avis sur le delta en cours ; ne retient pas h-22, va en h-23 |
| #3544 | verdicts manquants au gel 2171bca21263 (sensible) — en attente |
| #3565 | gel 242dcf7c6748 dépassé par la tête publiée 07b07f7c04a3, verdicts manquants — en attente |
| #3572 | gel 38020880b51e dépassé par la tête publiée b322f6646924, verdicts manquants — en attente |
| #3557 | verdicts manquants au gel 4a6c843d448e (sensible) — en attente |
| #3519 | verdicts manquants au gel a3fbebcc7774 et conflit avec main (handoff) — en attente |
| #3526 | verdicts manquants au gel 30a916ad44b0 et conflit avec main (handoff) — en attente |
| #3536 | verdicts manquants au gel 79dea9c449e6 ; attend #3578 (dans h-22) — en attente |
| #3473 | lecture de la porte incomplète (provenance d'auteur) ; chaîne hors h-22 : attend #3542 (dans h-22) + #3584 |
| #3474 | lecture de la porte incomplète (provenance d'auteur) ; chaîne hors h-22 : attend #3542 + #3584 |
| #3509 | verdicts manquants au gel ee50e13b1f08 ; chaîne hors h-22 : attend #3542 + #3584 |
| #3506 | lecture de la porte incomplète (provenance d'auteur) ; chaîne hors h-22 : attend #3542 + #3584 |

Les 14 unités de h-21 sont dans la base (`0d8f1a06`) ; les lots correspondants
encore en `qa` seront clos par `sync-merges`, ils ne sont pas des candidats.

## Contrôles

- Composition : 6/6 sans conflit sur `0d8f1a0604c13ef081c5e4c6c13d210fd0369a1e`.
- Liste calculée `paquet-candidats` sur `0d8f1a06` : 6 prêts, tous composés ; les
  erreurs de lecture (#3473, #3474, #3506) sont hors h-22 par consigne pilote.
- Une seule suite complète sur la tête gelée, après le gel.
- Après fusion : staging hors créneau (§11 bis) pour #3568 (migration additive
  0485, appliquée AVANT de relancer les exécuteurs), #3549 (runners pc-smichea),
  #3546 (workflows), #3542/#3577/#3578 (portes de fusion).
