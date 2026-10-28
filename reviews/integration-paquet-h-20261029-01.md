# Paquet integration/paquet-h-20261029-01 — composition (§12), sur main `ac8901c63a67`

Base : `main` **`ac8901c63a67cf7fc504cc5fcc9957e4f0907ad4`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261029-01`, cible `main`.
Porteur : **#4420**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (1 unités, 1 lots)

| lot | source composée |
|---|---|
| #4416 | `71cc3957132bc8b6152bcb6ef540260445b7caa7` |

## Composition (1 unités, 1 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #4416 | `71cc3957132b` | `974278879275` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 1 lot(s) prêt(s), 12 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 20.9 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
