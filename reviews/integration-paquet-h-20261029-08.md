# Paquet integration/paquet-h-20261029-08 — composition (§12), sur main `d1c59da2cb5a`

Base : `main` **`d1c59da2cb5a1a371c039a9d85777178f0ee33e5`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261029-08`, cible `main`.
Porteur : **#4598**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (17 unités, 17 lots)

| lot | source composée |
|---|---|
| #4571 | `8b1680fefcc5` |
| #4595 | `a792f54cacfa` |
| #4600 | `75ec7e00c934` |
| #4256 | `b0e7a3ec87ee` |
| #4386 | `0970583df8fa` |
| #4407 | `c12a0d495a12` |
| #4513 | `941f4c171865` |
| #4557 | `410a1dcb7519` |
| #4581 | `6fd6d20d9578` |
| #4582 | `fc95bebfef6d` |
| #4589 | `a9c047a4d110` |
| #4590 | `0754df8a6c99` |
| #4594 | `03e9752cba81` |
| #4601 | `fa30aef4728d` |
| #4505 | `c1d735293bcc` |
| #4572 | `930895863d5d` |
| #4584 | `8d5aca357a9b` |

## Composition (17 unités, 17 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #4571 | `8b1680fefcc5` | `4dbbe4f148bf` | composition |
| 2 | #4595 | `a792f54cacfa` | `34c94048b0b9` | composition |
| 3 | #4600 | `75ec7e00c934` | `d54c3b2159d5` | composition |
| 4 | #4256 | `b0e7a3ec87ee` | `6a519382fc8a` | composition |
| 5 | #4386 | `0970583df8fa` | `b9ff56f965cd` | composition |
| 6 | #4407 | `c12a0d495a12` | `15aeb8e3f95d` | composition |
| 7 | #4513 | `941f4c171865` | `12459b22b7b0` | composition |
| 8 | #4557 | `410a1dcb7519` | `9a9eadd811d0` | composition |
| 9 | #4581 | `6fd6d20d9578` | `3cb3d5812316` | composition |
| 10 | #4582 | `fc95bebfef6d` | `9fa7e224bd14` | composition |
| 11 | #4589 | `a9c047a4d110` | `659a25ea5ad5` | composition |
| 12 | #4590 | `0754df8a6c99` | `babcb8f1b364` | composition |
| 13 | #4594 | `03e9752cba81` | `5db01e1ec3f6` | composition |
| 14 | #4601 | `fa30aef4728d` | `194cf136bf08` | composition |
| 15 | #4505 | `c1d735293bcc` | `5544e8195fdf` | composition |
| 16 | #4572 | `930895863d5d` | `c2d60b79c41f` | composition |
| 17 | #4584 | `8d5aca357a9b` | `c8b85253c827` | composition |

## Écartés

| lot | motif |
|---|---|
| #4508 | test négatif en ci-full 40912241912 : tests.test_plan_git.RemoteBranchRevertTest.test_remote_head_reverted_does_not_close_stale_branch, reproduit en local sur 222d82b0, sur la remise c5e0a2ed seule et sur le gel 961a7b64 — --exclude, recomposition sans lui |

Liste calculée : 17 lot(s) prêt(s), 8 en attente, 1 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 29.2 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
