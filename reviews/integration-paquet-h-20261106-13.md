# Paquet integration/paquet-h-20261106-13 — composition (§12), sur main `29db40e57650`

Base : `main` **`29db40e57650a2a5ba9c662f79eaf61b33c69de6`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261106-13`, cible `main`.
Porteur : **#6048**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #6038 | `6292a0404716` |
| #6042 | `7cb3acd0f65e` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6038 | `6292a0404716` | `b1ad0fa9aa86` | composition |
| 2 | #6042 | `7cb3acd0f65e` | `130e088eca94` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 2 lot(s) prêt(s), 4 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 41.2 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
