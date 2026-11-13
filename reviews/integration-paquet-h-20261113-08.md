# Paquet integration/paquet-h-20261113-08 — composition (§12), sur main `4e681c813776`

Base : `main` **`4e681c81377699a54e270e9b665ac2418c5c3f93`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261113-08`, cible `main`.
Porteur : **#7210**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (4 unités, 4 lots)

| lot | source composée |
|---|---|
| #7106 | `94a037bcd0ac` |
| #7120 | `7da642244d93` |
| #7195 | `ccba45975f87` |
| #7093 | `2cd948f5dad1` |

## Composition (4 unités, 4 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #7106 | `94a037bcd0ac` | `fbe1c65f7e5f` | composition |
| 2 | #7120 | `7da642244d93` | `3881cf136bbf` | composition |
| 3 | #7195 | `ccba45975f87` | `97bd59f251bf` | composition |
| 4 | #7093 | `2cd948f5dad1` | `6b2a3a3fb1ba` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 4 lot(s) prêt(s), 0 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 64.3 s.
- Types clients : types clients : inchangés.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
