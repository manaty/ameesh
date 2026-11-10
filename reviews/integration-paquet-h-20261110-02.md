# Paquet integration/paquet-h-20261110-02 — composition (§12), sur main `5888b20a4457`

Base : `main` **`5888b20a4457a1ad45bbb1e9f127d7e5288ac345`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261110-02`, cible `main`.
Porteur : **#6616**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (4 unités, 4 lots)

| lot | source composée |
|---|---|
| #6579 | `52ba429c3cb7` |
| #6606 | `4c5e3a5b9761` |
| #6608 | `f982f7560dd3` |
| #6610 | `4f102be2c796` |

## Composition (4 unités, 4 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6579 | `52ba429c3cb7` | `9f43b0eba5b2` | composition |
| 2 | #6606 | `4c5e3a5b9761` | `3697f32dc223` | composition |
| 3 | #6608 | `f982f7560dd3` | `5a2404ef6246` | composition |
| 4 | #6610 | `4f102be2c796` | `22d08e950813` | composition |

## Écartés

| lot | motif |
|---|---|
| #6609 | rouge déterministe reproduit en local (CI run 42740701943, psycopg 6/8) : tests.test_3687_gel_contrat_porte ScriptTest.test_cas_d_origine_complete_authors_change_refuse rend 0 au lieu de 1 ; main 5888b20a + seul tests/__init__.py de #6609 suffit à le rougir ; main seul vert. Cause : realpath(tests/../src) suit le lien symbolique tests du banc vers le dépôt réel et charge src réel au lieu de la copie mutée. — --exclude, recomposition sans lui |
| #6605 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |

Liste calculée : 5 lot(s) prêt(s), 4 en attente, 2 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 65.3 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
