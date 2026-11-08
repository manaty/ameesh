# Paquet integration/paquet-h-20261108-02 — composition (§12), sur main `780429439996`

Base : `main` **`78042943999615f0926fd925924f1c1302e92faa`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261108-02`, cible `main`.
Porteur : **#6248**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #6183 | `e4f5606cfc1f` |
| #6232 | `03e226599040` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6183 | `e4f5606cfc1f` | `dbad780b09bd` | composition |
| 2 | #6232 | `03e226599040` | `7a26b3fbd904` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 2 lot(s) prêt(s), 3 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 51.7 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
