# Paquet integration/paquet-h-20261113-02 — composition (§12), sur main `981932467e6b`

Base : `main` **`981932467e6b977f359af1fea5e30882b9cca9ea`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261113-02`, cible `main`.
Porteur : **#7160**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #7059 | `5f0f67123ac7` |
| #7117 | `c816a4b0bf68` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #7059 | `5f0f67123ac7` | `fd2f617c82f5` | composition |
| 2 | #7117 | `c816a4b0bf68` | `a9bcb5b70327` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 2 lot(s) prêt(s), 13 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 56.8 s.
- Types clients : types clients : inchangés.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
