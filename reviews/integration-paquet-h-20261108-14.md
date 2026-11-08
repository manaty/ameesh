# Paquet integration/paquet-h-20261108-14 — composition (§12), sur main `52e615785d34`

Base : `main` **`52e615785d34520bc38ca6c0b20bfca8f8ccae2a`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261108-14`, cible `main`.
Porteur : **#6324**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (4 unités, 4 lots)

| lot | source composée |
|---|---|
| #6309 | `67f18b751385` |
| #6315 | `fc2f0d031236` |
| #6317 | `5400f143d8c3` |
| #6289 | `d872979caac9` |

## Composition (4 unités, 4 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6309 | `67f18b751385` | `ed839115f1bf` | composition |
| 2 | #6315 | `fc2f0d031236` | `826fca6b0717` | composition |
| 3 | #6317 | `5400f143d8c3` | `bf6f7ceea72c` | composition |
| 4 | #6289 | `d872979caac9` | `a9a252979df1` | composition |

## Écartés

| lot | motif |
|---|---|
| #6313 | rouge déterministe test_2179 (aide racine non alphabétique : repair-report inséré avant decide), rejoué rouge en local sur 73bd1592, vert sur main 52e61578 ; 2e tentative, la 1re tuée par mon timeout local de 900 s pendant la passe contrats — --exclude, recomposition sans lui |
| #6327 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |

Liste calculée : 5 lot(s) prêt(s), 4 en attente, 2 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 47.4 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
