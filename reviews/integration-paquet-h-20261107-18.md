# Paquet integration/paquet-h-20261107-18 — composition (§12), sur main `6f2b7a7e3adc`

Base : `main` **`6f2b7a7e3adcbc4bb55f3384e4cc4a0c3e06e48d`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261107-18`, cible `main`.
Porteur : **#6191**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (1 unités, 1 lots)

| lot | source composée |
|---|---|
| #6184 | `33520fb1aa55` |

## Composition (1 unités, 1 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6184 | `33520fb1aa55` | `550c2d38a9e8` | composition |

## Écartés

| lot | motif |
|---|---|
| #6179 | ci-full 42383697602 rouge : tests.test_2449_garde_mecanique.SchemaCoverageTest.test_the_guard_keys_are_the_reader_keys — mechanics._OVERRIDE_KEYS ajoute contract_selection, absent de _DOCUMENT_KEYS et de la garde (diff de #6179) — --exclude, recomposition sans lui |
| #6106 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #6145 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #6163 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |

Liste calculée : 4 lot(s) prêt(s), 5 en attente, 4 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 48.2 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
