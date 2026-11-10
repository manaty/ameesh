# Paquet integration/paquet-h-20261110-12 — composition (§12), sur main `53904895854e`

Base : `main` **`53904895854edb29cd17a4cab28682a3dc531d47`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261110-12`, cible `main`.
Porteur : **#6744**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (4 unités, 4 lots)

| lot | source composée |
|---|---|
| #6668 | `413d54c4b210` |
| #6669 | `11247b9bfdca` |
| #6733 | `3b9168328dbe` |
| #6734 | `fde66a1563a4` |

## Composition (4 unités, 4 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6668 | `413d54c4b210` | `bb369013bf30` | composition |
| 2 | #6669 | `11247b9bfdca` | `1240d6946b96` | composition |
| 3 | #6733 | `3b9168328dbe` | `adbc408b38e6` | composition |
| 4 | #6734 | `fde66a1563a4` | `83577d7dc409` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 4 lot(s) prêt(s), 3 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 59.4 s.
- Types clients : types clients : inchangés.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
