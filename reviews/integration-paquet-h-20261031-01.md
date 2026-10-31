# Paquet integration/paquet-h-20261031-01 — composition (§12), sur main `782aba5ca1d0`

Base : `main` **`782aba5ca1d05eb0501ad6bcfee503f31ef433d2`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261031-01`, cible `main`.
Porteur : **#5023**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (8 unités, 8 lots)

| lot | source composée |
|---|---|
| #5005 | `9e1e9c23cbdc` |
| #5013 | `39eee48ceab9` |
| #4757 | `8618063390f0` |
| #4898 | `b9c337718248` |
| #4621 | `59055ecc7ff2` |
| #4957 | `cbac73552e01` |
| #4995 | `6d686ab36242` |
| #5001 | `30e3ea47e933` |

## Composition (8 unités, 8 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5005 | `9e1e9c23cbdc` | `518623db7612` | composition |
| 2 | #5013 | `39eee48ceab9` | `cf8da9a9d8aa` | composition |
| 3 | #4757 | `8618063390f0` | `6fa69e51785a` | composition |
| 4 | #4898 | `b9c337718248` | `ce7c1cf13eb3` | composition |
| 5 | #4621 | `59055ecc7ff2` | `5c0f38f434df` | composition |
| 6 | #4957 | `cbac73552e01` | `9a5619afc658` | composition |
| 7 | #4995 | `6d686ab36242` | `a75c0299f820` | composition |
| 8 | #5001 | `30e3ea47e933` | `1520800f9c52` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 8 lot(s) prêt(s), 8 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 21.0 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
