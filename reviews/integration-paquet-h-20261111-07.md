# Paquet integration/paquet-h-20261111-07 — composition (§12), sur main `e1c5abe02616`

Base : `main` **`e1c5abe0261685e513ce8f8283196af435a08f4f`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261111-07`, cible `main`.
Porteur : **#6838**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (3 unités, 3 lots)

| lot | source composée |
|---|---|
| #6825 | `f1132b482602` |
| #6826 | `17c279b5f756` |
| #6833 | `88bd8f8054c6` |

## Composition (3 unités, 3 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6825 | `f1132b482602` | `3a149464a18d` | composition |
| 2 | #6826 | `17c279b5f756` | `f781033108ce` | composition |
| 3 | #6833 | `88bd8f8054c6` | `96eefaabe06c` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 3 lot(s) prêt(s), 4 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 56.2 s.
- Types clients : types clients : inchangés.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
