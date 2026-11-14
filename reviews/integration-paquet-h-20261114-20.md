# Paquet integration/paquet-h-20261114-20 — composition (§12), sur main `312f2ffd3e3c`

Base : `main` **`312f2ffd3e3c029dff4c26979a4788b517b80484`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261114-20`, cible `main`.
Porteur : **#7419**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #7166 | `1e33ffd8a6c9` |
| #7296 | `6fe58c9a8f2d` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #7166 | `1e33ffd8a6c9` | `050780fc6520` | composition |
| 2 | #7296 | `6fe58c9a8f2d` | `4d242ed774f7` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 2 lot(s) prêt(s), 1 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 43.2 s.
- Types clients : types clients : inchangés.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
