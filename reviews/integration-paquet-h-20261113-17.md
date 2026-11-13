# Paquet integration/paquet-h-20261113-17 — composition (§12), sur main `d569347094cc`

Base : `main` **`d569347094cc543eccc1ca369c772cdc1cd089c9`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261113-17`, cible `main`.
Porteur : **#7275**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #7034 | `789ab38f1d71` |
| #7269 | `e2f1f0626f51` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #7034 | `789ab38f1d71` | `5c9a66419529` | composition |
| 2 | #7269 | `e2f1f0626f51` | `ad83f87bc30b` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 2 lot(s) prêt(s), 0 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 45.3 s.
- Types clients : types clients : inchangés.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
