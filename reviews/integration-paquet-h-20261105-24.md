# Paquet integration/paquet-h-20261105-24 — composition (§12), sur main `adee4f3a5425`

Base : `main` **`adee4f3a5425b0ec4420845585af3606802f565a`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261105-24`, cible `main`.
Porteur : **#5948**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (3 unités, 3 lots)

| lot | source composée |
|---|---|
| #5937 | `fddf15d41263` |
| #5941 | `b09996670edb` |
| #5945 | `dad712dd902d` |

## Composition (3 unités, 3 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5937 | `fddf15d41263` | `b4d6fa6070e5` | composition |
| 2 | #5941 | `b09996670edb` | `7030fceb927b` | composition |
| 3 | #5945 | `dad712dd902d` | `3566beedb6b7` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 3 lot(s) prêt(s), 4 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 39.5 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
