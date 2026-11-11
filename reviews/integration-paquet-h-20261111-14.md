# Paquet integration/paquet-h-20261111-14 — composition (§12), sur main `3e92c08b2e35`

Base : `main` **`3e92c08b2e35b414af6515f0b7064317e152b7b8`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261111-14`, cible `main`.
Porteur : **#6926**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (9 unités, 9 lots)

| lot | source composée |
|---|---|
| #6877 | `adef2523a2e0` |
| #6881 | `22afea018b6a` |
| #6918 | `42c899cba079` |
| #6868 | `3e1c17e157f1` |
| #6871 | `2b7d8e54f211` |
| #6888 | `51b3c5ae0c39` |
| #6891 | `2bba5ba59bf0` |
| #6901 | `ebb25632f6a6` |
| #6915 | `7dad40320574` |

## Composition (9 unités, 9 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6877 | `adef2523a2e0` | `756ec10c2705` | composition |
| 2 | #6881 | `22afea018b6a` | `ee26857d75b0` | composition |
| 3 | #6918 | `42c899cba079` | `aa918a3a4dcc` | composition |
| 4 | #6868 | `3e1c17e157f1` | `b7509353b35e` | composition |
| 5 | #6871 | `2b7d8e54f211` | `c9acdca096f1` | composition |
| 6 | #6888 | `51b3c5ae0c39` | `ee94c0ad6e71` | composition |
| 7 | #6891 | `2bba5ba59bf0` | `84086b7b0faf` | composition |
| 8 | #6901 | `ebb25632f6a6` | `249e558e49a8` | composition |
| 9 | #6915 | `7dad40320574` | `513fd3d5df7f` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 9 lot(s) prêt(s), 3 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 55.6 s.
- Types clients : types clients : inchangés.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
