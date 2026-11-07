# Paquet integration/paquet-h-20261107-29 — composition (§12), sur main `e256558523b3`

Base : `main` **`e256558523b3c354554ed69790db61a17607b65b`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261107-29`, cible `main`.
Porteur : **#6233**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (1 unités, 1 lots)

| lot | source composée |
|---|---|
| #6228 | `ca651174be89` |

## Composition (1 unités, 1 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6228 | `ca651174be89` | `47fffa35c745` | composition |

## Écartés

| lot | motif |
|---|---|
| #6183 | rouge CI complète h-29 (run 42421507044) : test_4322 lit la première note, devenue la projection des contrats de E ; correctif de test chez l'auteur — --exclude, recomposition sans lui |

Liste calculée : 1 lot(s) prêt(s), 4 en attente, 1 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 42.5 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
