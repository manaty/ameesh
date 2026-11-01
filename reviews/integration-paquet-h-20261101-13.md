# Paquet integration/paquet-h-20261101-13 — composition (§12), sur main `ae29029ec3bc`

Base : `main` **`ae29029ec3bc68a14a9ae324efb2be85e76f3b8f`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261101-13`, cible `main`.
Porteur : **#5324**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (3 unités, 3 lots)

| lot | source composée |
|---|---|
| #5314 | `6985e00f2b8f` |
| #5321 | `04213139f0b3` |
| #5305 | `dc1c673923f9` |

## Composition (3 unités, 3 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5314 | `6985e00f2b8f` | `514def8e249f` | composition |
| 2 | #5321 | `04213139f0b3` | `9c028b1ffa00` | composition |
| 3 | #5305 | `dc1c673923f9` | `f39ce4eb92aa` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 3 lot(s) prêt(s), 1 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 26.2 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
