# Paquet integration/paquet-h-20261109-22 — composition (§12), sur main `634e5a5f8fea`

Base : `main` **`634e5a5f8feae61d5c8fdca9e82faa8ebb75b3a2`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261109-22`, cible `main`.
Porteur : **#6590**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #6567 | `c2b23d2a905d` |
| #6584 | `2aa2298f5740` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6567 | `c2b23d2a905d` | `7ceb527180b4` | composition |
| 2 | #6584 | `2aa2298f5740` | `75dc66ed8914` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 2 lot(s) prêt(s), 2 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 84.9 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
