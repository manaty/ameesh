# Paquet integration/paquet-h-20261025-21 — composition (§12), recomposition par le pilote

Base : `main` **`47bf1fe68fa8cdde9cda2fd41c908d66ab79a092`** (fusion du paquet
témoin h-20, PR #2751, 25/10 14:35:31Z). Branche
`integration/paquet-h-20261025-21`, cible `main`, PR #2759. Porteur : **#3573**.
Composeur initial : mty-amh-impl-01 ; recomposition : mesh-design (pilote,
agents-1), 25/10 14:5xZ.

**Recomposition** : la tête `0c283d0a6df9` (18 unités, gel #19179, OK dev2 +
cx3) est remplacée. Le feu y est refusé : H6 #3473, H7 #3474, H8 #3509 et #3506
portent encore, au-dessus de `main`, les commits pré-rebase de H5 (impl-07,
deepseek ; `main` porte H5 rebasé depuis h-19). La porte compte donc deepseek
parmi leurs auteurs, avec codex (cx2, codex1, cx1) : seule la famille claude
reste éligible, deux familles sont requises (sensible). Ces quatre lots sont
écartés vers h-22, après rebase sur `main` par leurs auteurs (même patch,
report des avis). Les 14 autres sont recomposés dans le même ordre, sans
conflit. Le run `40204065236` n'a rien exécuté (merge-ref encore sur
`771e4479a`).

## Admissions (14 lots, 14 unités)

| lot | source composée | avis au gel | objet |
|---|---|---|---|
| #3540 | `9b7c700b73ea` | 2/2 (cx3 codex, dev4 claude) | pc-smichea : git des agents en HTTPS par gh, SSH fermé |
| #3521 | `465650887a9f` | 1/1 (cx3 codex) | Témoins psycopg (correctif du lot 3503) hors de la part psql |
| #3501 | `ac8ed069dd59` | 2/2 (cx1 codex, impl-05 deepseek) | Panier « ameesh » : estimates et publication |
| #3529 | `178c55b562db` | 1/1 (impl-07 deepseek) | Coupe Nexlink déterministe (develop, tag, staging) |
| #3539 | `594fb30b48b7` | 2/2 (dev1 claude, dev5 deepseek) | Compagnon #3529 : configuration et unités staging |
| #3533 | `ae2379c6243f` | 2/2 (dev4 claude, dev3 deepseek) | Porte de provenance : retours de main hors auteurs |
| #3532 | `446ff1987065` | 2/2 (cx1 codex, dev3 deepseek) | Réservation de la cible ignorée par la file |
| #3534 | `0d906da9d122` | 2/2 (dev4 claude, cx3 codex) | Fusion auto : cible avancée pendant la CI |
| #3512 | `ccb87cac4e2d` | 2/2 (dev4 claude, dev3 deepseek) | Notify : alerte `slo_breach` sur lot fusionné |
| #3516 | `421b9a1c3fa3` | 2/2 (impl-05 deepseek, cx3 codex) | Membres : ordre topologique de la porte |
| #3556 | `b3b1f64e2a3a` | 2/2 (dev4 claude, dev3 deepseek), 1 requis | Prise automatique : un lot sous hold pilote n'est pas pris |
| #3554 | `5c0f2be01ab9` | 2/2 (cx3 codex, dev2 claude) | Porte : une baisse d'exigence au canon demande deux avis |
| #3560 | `17d2f07d9282` | 2/2 (dev3 deepseek, cx3 codex) | Train : une ligne « ejected » (ci_required) ne bloque plus l'inscription ; porte aussi la livraison #3569 |
| #3555 | `08ef386a2979` | 1/1 (dev1 claude) | SLO : le temps parqué sous un hold de pilote ne compte plus dans « review GO → merge » |

## Avis au gel

| lot | gel | verdicts |
|---|---|---|
| #3540 | 18951 | 18952 cx3 (codex) ok, 18954 dev4 (claude) ok |
| #3521 | 18991 | 18995 cx3 (codex) ok — 1 requis (light) |
| #3501 | 18911 | 18920 impl-05 (deepseek) ok, 18921 cx1 (codex) ok |
| #3529 | 18893 | 18925 impl-07 (deepseek) ok — 1 requis |
| #3539 | 18895 | 18908 dev1 (claude) ok, 18926 dev5 (deepseek) ok |
| #3533 | 18881 | 18887 dev4 (claude) ok, 18930 dev3 (deepseek) ok |
| #3532 | 18882 | 18885 cx1 (codex) ok, 18957 dev3 (deepseek) ok |
| #3534 | 18941 | 18943 dev4 (claude) ok, 18944 cx3 (codex) ok |
| #3512 | 18978 | 18983 dev4 (claude) ok, 18987 dev3 (deepseek) ok |
| #3516 | 19066 | 19079 impl-05 (deepseek) ok, 19132 cx3 (codex) ok (19142 impl-05, re-confirmation) |
| #3556 | 19057 | 19041 dev4 (claude) ok, 19082 dev3 (deepseek) ok |
| #3554 | 19077 | 19083 cx3 (codex) ok, 19084 dev2 (claude) ok |
| #3560 | 19154 | dev3 (deepseek) ok, cx3 (codex) ok |
| #3555 | 19032 | 19151 dev1 (claude) ok — 1 requis |

## Composition (14 unités, 14 lots)

| unité | lot | commit |
|---|---|---|
| 1 | #3540 | `241da727f` |
| 2 | #3521 | `dee45a129` |
| 3 | #3501 | `75d0405b3` |
| 4 | #3529 | `afd15582d` |
| 5 | #3539 | `17942d28e` |
| 6 | #3533 | `8bbdc3590` |
| 7 | #3532 | `6796ef104` |
| 8 | #3534 | `b86fcb0bb` |
| 9 | #3512 | `1e67c3a02` |
| 10 | #3516 | `7b234caee` |
| 11 | #3556 | `4d54901c5` |
| 12 | #3554 | `661c378e8` |
| 13 | #3560 | `809544d69` |
| 14 | #3555 | `a91870c32` |

## Écartés (ce véhicule)

- #3473 (H6), #3474 (H7), #3509 (H8), #3506 : plage d'auteurs contenant les
  commits pré-rebase de H5 (voir ci-dessus) → h-22 après rebase sur `main`.
- Déjà hors h-21 : #3519 et #3526 (après H8), #3546, #3549, #3558 (conflit
  avec #3534), #3542, #3557 → h-22.

## Contrôles

- Composition : 14/14 sans conflit sur `47bf1fe68fa8`.
- Une seule suite complète sur la tête gelée, après le gel ; #3521 (dans ce
  paquet) corrige les 3 témoins psycopg hérités de `test_3503_jit`.
- Après fusion : staging hors créneau (§11 bis) pour #3560 (Nexlink #3537,
  #3561), #3540 (git HTTPS de pc-smichea) et les portes #3534/#3554/#3556.

## Correction après CI rouge (25/10 15:0xZ)

Le run `40204539739` (tête `4173808ac`) a rougi 6 fois, toutes dans
`tests.test_l234a_auditor_canon_guard.CanonGuardTest` : chaque prise passe
désormais par `backlog._take_holds` (#3556), qui lit
`storage.of(db).progress.lot_hold_events`, alors que la fixture
`tests.test_l234_auditor.AuditorTest.take` ne fournissait que `work`,
`operations` et `hosts`. Correctif `4f47c9d9f8b5` : la fixture fournit un
`progress` sans hold. Aucune unité modifiée ; témoin seul (adaptation de
test, §2 — aucun avis dû). Ciblés locaux verts :
`test_l234a_auditor_canon_guard` + `test_l234_auditor` (20 OK),
`test_3556_auto_take_holds` (7 OK). Nouvelle tête gelée et suite `ci-full`
relancée ; le feu est à redonner par le pilote.
