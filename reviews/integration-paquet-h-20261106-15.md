# Paquet integration/paquet-h-20261106-15 — composition (§12), sur main `1aacb2ec55ac`

Base : `main` **`1aacb2ec55acdaa0d34b73c5316fb548871c3f24`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261106-15`, cible `main`.
Porteur : **#6056**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (1 unités, 1 lots)

| lot | source composée |
|---|---|
| #6044 | `4357f2196e8f` |

## Composition (1 unités, 1 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6044 | `4357f2196e8f` | `ed958b281af7` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 1 lot(s) prêt(s), 2 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 46.4 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
