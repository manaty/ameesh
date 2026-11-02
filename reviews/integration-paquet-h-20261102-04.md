# Paquet integration/paquet-h-20261102-04 — composition (§12), sur main `2f8b476e28fb`

Base : `main` **`2f8b476e28fb7a3f8e339f81af8e4ac1ca25885a`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261102-04`, cible `main`.
Porteur : **#5441**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (3 unités, 3 lots)

| lot | source composée |
|---|---|
| #5425 | `631ec7945938` |
| #5426 | `f0c6a47a1a49` |
| #5430 | `b25063ce0045` |

## Composition (3 unités, 3 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5425 | `631ec7945938` | `1596547d48e0` | composition |
| 2 | #5426 | `f0c6a47a1a49` | `0b4de2518663` | composition |
| 3 | #5430 | `b25063ce0045` | `7902c3bb5c69` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 3 lot(s) prêt(s), 3 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 29.4 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
