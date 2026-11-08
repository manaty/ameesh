# Paquet integration/paquet-h-20261108-07 — composition (§12), sur main `09889efc24ac`

Base : `main` **`09889efc24ac44a1c63009989c65a76c2bb75aed`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261108-07`, cible `main`.
Porteur : **#6281**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (3 unités, 3 lots)

| lot | source composée |
|---|---|
| #6186 | `99634ecd6723` |
| #6276 | `bd6b69de91b3` |
| #6272 | `22b310e74508` |

## Composition (3 unités, 3 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6186 | `99634ecd6723` | `bf4223033e6e` | composition |
| 2 | #6276 | `bd6b69de91b3` | `055b1f4d9bc7` | composition |
| 3 | #6272 | `22b310e74508` | `585074902a5f` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 3 lot(s) prêt(s), 4 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 47.4 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
