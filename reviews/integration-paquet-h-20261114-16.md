# Paquet integration/paquet-h-20261114-16 — composition (§12), sur main `3580cdf78649`

Base : `main` **`3580cdf78649a07f607465e798a5868abd02d2dc`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261114-16`, cible `main`.
Porteur : **#7409**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #7408 | `0e919376b0e6` |
| #7312 | `fdf0552f6f5f` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #7408 | `0e919376b0e6` | `739ead7fa589` | composition |
| 2 | #7312 | `fdf0552f6f5f` | `e392191761a0` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 2 lot(s) prêt(s), 2 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 46.4 s.
- Types clients : types clients : inchangés.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
