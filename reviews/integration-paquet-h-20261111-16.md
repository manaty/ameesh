# Paquet integration/paquet-h-20261111-16 — composition (§12), sur main `6b178ed5cb44`

Base : `main` **`6b178ed5cb44b6f90463ce9b32b9b03e1377c540`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261111-16`, cible `main`.
Porteur : **#6948**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (3 unités, 3 lots)

| lot | source composée |
|---|---|
| #6860 | `3f1231b29fea` |
| #6925 | `d553a67918de` |
| #6931 | `ef51e1492c68` |

## Composition (3 unités, 3 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6860 | `3f1231b29fea` | `564ba8784e39` | composition |
| 2 | #6925 | `d553a67918de` | `45ed890c79dd` | composition |
| 3 | #6931 | `ef51e1492c68` | `78ee31778837` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 3 lot(s) prêt(s), 5 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 54.2 s.
- Types clients : types clients : inchangés.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
