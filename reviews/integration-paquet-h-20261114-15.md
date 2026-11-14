# Paquet integration/paquet-h-20261114-15 — composition (§12), sur main `332ddc65a767`

Base : `main` **`332ddc65a7679452475efd82e86426cb31e17ef6`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261114-15`, cible `main`.
Porteur : **#7396**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (1 unités, 1 lots)

| lot | source composée |
|---|---|
| #7392 | `2ebe472cbda6` |

## Composition (1 unités, 1 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #7392 | `2ebe472cbda6` | `7e7ee5c63b88` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 1 lot(s) prêt(s), 1 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 51.9 s.
- Types clients : types clients : inchangés.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
