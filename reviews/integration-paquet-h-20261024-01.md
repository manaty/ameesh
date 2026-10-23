# Paquet integration/paquet-h-20261024-01 — composition (§12)

Base : `main` **`451ebc443c63`** (fusion du paquet h-23, PR #2447).
Branche `integration/paquet-h-20261024-01`, cible `main`. Porteur : **#3252**.
Composeur : mty-amh-impl-05. Une seule suite complète est demandée sur la tête
finale (CI de la PR).

## Sources

- mesh-design, message #130651 (22:35Z) : prochain paquet confié à
  mty-amh-impl-05 ; candidats à prendre selon les avis acquis à la coupure :
  #2994 (2/2 sur `8c861f31`, prioritaire : #3220 attend sa fusion), #3242,
  #3244 (si dev2 a rendu), #3026 (si auditeur et cx1 ont rendu), #3116 (remise
  `f4656934`, si dev2 a rendu) ; hors paquet #3220 et les témoins EP7
  #3223/#3231.
- Calcul `scripts/paquet-candidats.py` (22:49Z, base `451ebc44`, code du
  déploiement 1.20.0, la base étant à `0476` — la `0477` n'est pas appliquée) :
  3 lots prêts (#2994, #3026, #3242), 17 en attente, 1 lecture impossible
  (#2914). Les trois prêts sont composés ; aucun lot prêt laissé hors paquet.

## Admissions (3 lots, 3 unités)

| lot | source composée | classe | avis (gate) | objet |
|---|---|---|---|---|
| #2994 | `8c861f3121a2` | sensitive | 2/2 : ameesh-cx1 (codex), mty-amh-docs-01 (deepseek) | porte #2924 : un commit ne survit que par ses lignes propres ; témoins réordre/préfixe/binaire |
| #3026 | `ce1a2cb7e7f6` | sensitive | 2/2 : auditeur (claude), ameesh-cx1 (codex) | L68b : `platform-macos` requis après la première suite complète verte |
| #3242 | `0034d01f089d` | light | 1/1 : ameesh-cx2 (codex) | test_l30 : les faux harnais jouent sous l'interpréteur du banc, `IndexError` disparu |

## Composition (3 unités, 3 lots)

| unité | lot | source | commit | note |
|---|---|---|---|---|
| 1 | #2994 | `8c861f3121a2` | `37e6e4891db2` | 0 conflit |
| 2 | #3026 | `ce1a2cb7e7f6` | `b98bb4e8635f` | 0 conflit, branche déjà fusionnée sur h-23 |
| 3 | #3242 | `0034d01f089d` | `da3e260dfa43` | 0 conflit |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot
et sa tête, et il porte un trailer `ameesh-work:`. Aucun commit de `main` n'est
venu pendant la composition. Aucune résolution manuelle : 3/3 unités
fusionnées proprement.

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #3116 | avis manquant sur la tête `64d56064` (seul dev2, claude ; 2 requis en sensible) et remise `f4656934` en conflit avec `main` sur `src/ameesh/review_authors.py` (base f133b837, avant h-23) ; au paquet suivant après rebase |
| #3244 | verdict dev2 **bloqué** sur la tête `f328bd9f` ; le blocage lève le feu, aucun avis acquis |
| #3220 | verdict dev2 **bloqué** sur la tête `4f72492b` ; en relecture, attend la fusion de #2994 puis rebase |
| #3231 | témoin EP7 : hors paquet (mesh-design #130651, même réserve que #129908) |
| #3223 | témoin EP7 (doc seule) : hors paquet (idem) |
| #2914 | lecture impossible : gel antérieur `66a06cf0` absent du dépôt, porte non lisible (migration `0477` non appliquée ici) ; non bloquant, comme au paquet h-23 |

## Contrôles

- **Liste calculée** : 3 prêts, **3/3 composés**. Une lecture impossible
  (#2914), non bloquante pour ce véhicule, avec son motif ci-dessus.
- **Contrôles statiques** : `scripts/freeze-checks.py` **6/6** verts.
- **Diff** : 17 fichiers, +433/−88 ; `git diff --check` propre ; les trois
  commits de composition portent leur trailer `ameesh-work:` ; aucun marqueur
  de conflit ni de saut de CI.
- **Migrations** : aucune ajoutée par le paquet ; la base 1.20.0 porte
  `0475`–`0476`, la `0477` sera appliquée par le déploiement de la release
  avant de relancer les exécuteurs.
- **Prévalidation** : indisponible (`card-stale`, carte de sélection de plus
  de 48 h), non bloquant (§3) ; motif consigné.
- **Suite complète** : demandée une fois sur la tête poussée, par la CI de la
  PR (§12). Aucune CI de lot.
