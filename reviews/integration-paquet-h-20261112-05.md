# Paquet integration/paquet-h-20261112-05 — composition (§12), sur main `ef5fbd699e88`

Base : `main` **`ef5fbd699e88460d8485dc0b8b12ffda3af1eb2f`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261112-05`, cible `main`.
Porteur : **#7064**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #7049 | `f4ebbc614dd9` |
| #7053 | `b3fb4fd6f7da` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #7049 | `f4ebbc614dd9` | `5611c4d86105` | composition |
| 2 | #7053 | `b3fb4fd6f7da` | `16aa364002a4` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 2 lot(s) prêt(s), 22 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 56.7 s.
- Types clients : types clients : inchangés.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
