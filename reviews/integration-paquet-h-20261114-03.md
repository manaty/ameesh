# Paquet integration/paquet-h-20261114-03 — composition (§12), sur main `1646d0f25c3a`

Base : `main` **`1646d0f25c3ac7041f979e346129bfa8f6f60a69`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261114-03`, cible `main`.
Porteur : **#7313**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (3 unités, 3 lots)

| lot | source composée |
|---|---|
| #7299 | `0d370f45690e` |
| #7307 | `5843b299fd5f` |
| #7309 | `fc3d51574d33` |

## Composition (3 unités, 3 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #7299 | `0d370f45690e` | `a91d305eab6f` | composition |
| 2 | #7307 | `5843b299fd5f` | `f766025b7712` | composition |
| 3 | #7309 | `fc3d51574d33` | `8eebc3542a75` | composition |

## Écartés

| lot | motif |
|---|---|
| #7121 | rouge ci-full 43543273901 (psycopg 9/12) : tests.test_l84_serve.OpenApiTest — docs/api/openapi-v1.json non régénéré après l'ajout de agent.attach (events.py) ; reproduit seul sur 5271f0934c07 — --exclude, recomposition sans lui |
| #7051 | coupure suivante : tête regelée — composé 8535c5c2b549, tête actuelle 0a39db44d45e : pas de ci de paquet sur la nouvelle tête |
| #7310 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |

Liste calculée : 5 lot(s) prêt(s), 1 en attente, 3 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 66.0 s.
- Types clients : types clients : inchangés.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
