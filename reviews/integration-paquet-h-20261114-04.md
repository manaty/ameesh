# Paquet integration/paquet-h-20261114-04 — composition (§12), sur main `cbaf3c22ddd4`

Base : `main` **`cbaf3c22ddd41e5d403314e28ffdb8ec2f5d905c`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261114-04`, cible `main`.
Porteur : **#7321**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (4 unités, 4 lots)

| lot | source composée |
|---|---|
| #7051 | `0a39db44d45e` |
| #7310 | `b834e223f48d` |
| #7319 | `1ffd4369655a` |
| #7316 | `facea4a58ca3` |

## Composition (4 unités, 4 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #7051 | `0a39db44d45e` | `2b98e74cb705` | composition |
| 2 | #7310 | `b834e223f48d` | `bd63898672f4` | composition |
| 3 | #7319 | `1ffd4369655a` | `6fcbd8d6359d` | composition |
| 4 | #7316 | `facea4a58ca3` | `5f28019e0cf1` | composition |

## Écartés

| lot | motif |
|---|---|
| #7121 | types clients périmés : #7121 modifie docs/api/openapi-v1.json (agent.attach) sans régénérer ui/src/api/schema.d.ts (gen-api --check rc 1) ; seule unité de h-04 qui touche l'API — --exclude, recomposition sans lui |

Liste calculée : 4 lot(s) prêt(s), 3 en attente, 1 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 57.0 s.
- Types clients : types clients : inchangés.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
