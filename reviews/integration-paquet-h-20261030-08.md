# Paquet integration/paquet-h-20261030-08 — composition (§12), sur main `88ba89233bd2`

Base : `main` **`88ba89233bd22e658fb4f158622e65a8a900578a`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261030-08`, cible `main`.
Porteur : **#4815**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (5 unités, 5 lots)

| lot | source composée |
|---|---|
| #4788 | `6bc45a2eeea9` |
| #4801 | `22e4d1e86537` |
| #4803 | `bb6c465d1a24` |
| #4785 | `5df0882b5cee` |
| #4798 | `59e875496cc3` |

## Composition (5 unités, 5 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #4788 | `6bc45a2eeea9` | `2eca84b104fd` | composition |
| 2 | #4801 | `22e4d1e86537` | `f6f4e7163933` | composition |
| 3 | #4803 | `bb6c465d1a24` | `b6541c554b5f` | composition |
| 4 | #4785 | `5df0882b5cee` | `e2ea58eb0152` | composition |
| 5 | #4798 | `59e875496cc3` | `1cc44689922a` | composition |

## Écartés

| lot | motif |
|---|---|
| #4793 | rouge réel : test_503_agent_host (check et controler sortent en 1 sans stderr) ; reproduit en local au préfixe #4793 (76408329c), vert avant (e5fa8cb3e) — --exclude, recomposition sans lui |
| #4779 | rouge réel : test_2776_journal_designations (relecteur sans fiche refusé à l entrée en qa) ; reproduit en local au préfixe #4779 (ef3899982), vert sur main — --exclude, recomposition sans lui |
| #4791 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #4806 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #4810 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #4812 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #4814 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #4759 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |

Liste calculée : 11 lot(s) prêt(s), 4 en attente, 8 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 20.0 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
