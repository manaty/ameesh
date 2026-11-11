# Paquet integration/paquet-h-20261111-04 — composition (§12), sur main `700a90f94428`

Base : `main` **`700a90f9442854f9ba5726584a0bbd3fac070276`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261111-04`, cible `main`.
Porteur : **#6815**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (6 unités, 6 lots)

| lot | source composée |
|---|---|
| #6776 | `abde901068e9` |
| #6799 | `68be4e9c5f4f` |
| #6802 | `65b7864cd9d5` |
| #6803 | `3572fc5f5e67` |
| #6809 | `5bc878f9d7ea` |
| #6771 | `b654afa6a65e` |

## Composition (6 unités, 6 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6776 | `abde901068e9` | `84b9cb462105` | composition |
| 2 | #6799 | `68be4e9c5f4f` | `db98b47b3e33` | composition |
| 3 | #6802 | `65b7864cd9d5` | `89d0055a8c23` | composition |
| 4 | #6803 | `3572fc5f5e67` | `69a973fdbb5e` | composition |
| 5 | #6809 | `5bc878f9d7ea` | `482b7dab9944` | composition |
| 6 | #6771 | `b654afa6a65e` | `bd8a09951cd9` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 6 lot(s) prêt(s), 1 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 55.4 s.
- Types clients : types clients : inchangés.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
