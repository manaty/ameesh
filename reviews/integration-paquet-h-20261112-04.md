# Paquet integration/paquet-h-20261112-04 — composition (§12), sur main `b29daa23c623`

Base : `main` **`b29daa23c6233ab0101313edc52e973b50a5983f`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261112-04`, cible `main`.
Porteur : **#7056**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (1 unités, 1 lots)

| lot | source composée |
|---|---|
| #7028 | `ad59550e05d6` |

## Composition (1 unités, 1 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #7028 | `ad59550e05d6` | `66bafd9032b6` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 1 lot(s) prêt(s), 22 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 53.4 s.
- Types clients : types clients : inchangés.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
