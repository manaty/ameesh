# Paquet integration/paquet-h-20261031-08 — composition (§12), sur main `125d3677eb8a`

Base : `main` **`125d3677eb8a0387d53e29188c323ab75520016a`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261031-08`, cible `main`.
Porteur : **#5081**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #5080 | `2d3f0e04baaa` |
| #4414 | `d8737629ed8b` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5080 | `2d3f0e04baaa` | `502f18648f7f` | composition |
| 2 | #4414 | `d8737629ed8b` | `ba1bceaf1fb6` | composition |

## Écartés

| lot | motif |
|---|---|
| #5038 | CI h-08 run 41182365761 : garde d environnement #5038 installée en processus par main/mesh_cli, DSN et PGPASSWORD perdus par les enfants (55432, no password supplied), masqué en local — --exclude, recomposition sans lui |
| #5077 | empilé sur la tête de #5038, en contient les commits — --exclude, recomposition sans lui |
| #5082 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |

Liste calculée : 3 lot(s) prêt(s), 3 en attente, 3 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 22.3 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
