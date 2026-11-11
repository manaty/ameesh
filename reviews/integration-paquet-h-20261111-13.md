# Paquet integration/paquet-h-20261111-13 — composition (§12), sur main `3e92c08b2e35`

Base : `main` **`3e92c08b2e35b414af6515f0b7064317e152b7b8`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261111-13`, cible `main`.
Porteur : **#6896**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (6 unités, 6 lots)

| lot | source composée |
|---|---|
| #6873 | `5eeaf9fe91d5` |
| #6877 | `adef2523a2e0` |
| #6868 | `3e1c17e157f1` |
| #6871 | `2b7d8e54f211` |
| #6888 | `51b3c5ae0c39` |
| #6891 | `2bba5ba59bf0` |

## Composition (6 unités, 6 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6873 | `5eeaf9fe91d5` | `878b47232ed9` | composition |
| 2 | #6877 | `adef2523a2e0` | `bea0e5a28ac3` | composition |
| 3 | #6868 | `3e1c17e157f1` | `ab672f22260d` | composition |
| 4 | #6871 | `2b7d8e54f211` | `f90951212ab3` | composition |
| 5 | #6888 | `51b3c5ae0c39` | `9ba7bca27a45` | composition |
| 6 | #6891 | `2bba5ba59bf0` | `47c3a96fe2ad` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 6 lot(s) prêt(s), 2 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 56.3 s.
- Types clients : types clients : inchangés.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
