# Paquet integration/paquet-h-20261029-04 — composition (§12), sur main `a7d962b4ccca`

Base : `main` **`a7d962b4ccca57d08bd649fd9f4d143bd2b53b6e`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261029-04`, cible `main`.
Porteur : **#4509**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (10 unités, 10 lots)

| lot | source composée |
|---|---|
| #4501 | `1004cd8ce0646c231473224e17e79dbef408a0e4` |
| #4504 | `c7180f0d50719834492a8c557cc306629344cf57` |
| #4064 | `e85b2d09a407be603fe7a73ea419efbc14130e9e` |
| #4174 | `f4461706a98beb84781b55241b2b3dafbe7cc255` |
| #4478 | `b21551dc4047130d1c38fc7ef96979d0e849c3e2` |
| #4487 | `d2a107ca008918d2c812822d0b76329e66882214` |
| #4489 | `719d621243ba496db28933a30a63e42cdb24154e` |
| #4491 | `46ae61b0c0e6f97db50b5994e4e2e339031ad656` |
| #4435 | `7479d99ae070fc9c608bdd5dad88ea64540fc85a` |
| #4438 | `e08c3f074a8fa0ccee5b58a6878c1d3260a45ff7` |

## Composition (10 unités, 10 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #4501 | `1004cd8ce064` | `43179fd67be3` | composition |
| 2 | #4504 | `c7180f0d5071` | `f86a7e70a12f` | composition |
| 3 | #4064 | `e85b2d09a407` | `28f976d926b9` | composition |
| 4 | #4174 | `f4461706a98b` | `64fca2fcb781` | composition |
| 5 | #4478 | `b21551dc4047` | `f0842a146556` | composition |
| 6 | #4487 | `d2a107ca0089` | `208c57556871` | composition |
| 7 | #4489 | `719d621243ba` | `dc81ea3af35e` | composition |
| 8 | #4491 | `46ae61b0c0e6` | `dc42be5bee94` | composition |
| 9 | #4435 | `7479d99ae070` | `7b395057ab88` | composition |
| 10 | #4438 | `e08c3f074a8f` | `95d67c01a405` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 10 lot(s) prêt(s), 10 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 63.3 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
