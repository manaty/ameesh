# Paquet integration/paquet-h-20261111-06 — composition (§12), sur main `a39326fb3152`

Base : `main` **`a39326fb3152dc07cffcac0c60bca2a01132939b`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261111-06`, cible `main`.
Porteur : **#6829**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (3 unités, 3 lots)

| lot | source composée |
|---|---|
| #6806 | `835661f9573d` |
| #6824 | `f8ea4f45bfc6` |
| #6808 | `56106ca1b019` |

## Composition (3 unités, 3 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6806 | `835661f9573d` | `edf10f19c813` | composition |
| 2 | #6824 | `f8ea4f45bfc6` | `b3e3b266d0e3` | composition |
| 3 | #6808 | `56106ca1b019` | `6477a5495b79` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 3 lot(s) prêt(s), 2 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 47.4 s.
- Types clients : types clients : inchangés.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
