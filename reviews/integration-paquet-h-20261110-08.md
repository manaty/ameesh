# Paquet integration/paquet-h-20261110-08 — composition (§12), sur main `24523226f2e0`

Base : `main` **`24523226f2e0f35e8192aca82e822af2fa6b8b12`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261110-08`, cible `main`.
Porteur : **#6714**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (6 unités, 6 lots)

| lot | source composée |
|---|---|
| #6643 | `1b42db8189d9` |
| #6660 | `ea77b7e8aaa3` |
| #6661 | `8679ed6f498f` |
| #6664 | `927316b2c995` |
| #6684 | `02be704ec683` |
| #6626 | `fa248954fe36` |

## Composition (6 unités, 6 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6643 | `1b42db8189d9` | `c840e4109677` | composition |
| 2 | #6660 | `ea77b7e8aaa3` | `6460c403bc77` | composition |
| 3 | #6661 | `8679ed6f498f` | `a15bfad834fd` | composition |
| 4 | #6664 | `927316b2c995` | `603405c3a065` | composition |
| 5 | #6684 | `02be704ec683` | `cd47092fb915` | composition |
| 6 | #6626 | `fa248954fe36` | `cff85d584df4` | composition |

## Écartés

| lot | motif |
|---|---|
| #6649 | rouge déterministe en CI (run 42820964962) reproduit en local : tests.test_generic_core test_core_does_not_import_module_implementations ; packet_automerge.py:144 importe modules.dev.plan_github.Gh ; vert sur main 24523226f2e0, rouge sur 3f8a940556bf — --exclude, recomposition sans lui |
| #6676 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #6677 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |

Liste calculée : 8 lot(s) prêt(s), 3 en attente, 3 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 56.1 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
