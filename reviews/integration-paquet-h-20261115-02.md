# Paquet integration/paquet-h-20261115-02 — composition (§12), sur main `ef0ae4bfd862`

Base : `main` **`ef0ae4bfd86205278e4d7b5691d6527cf2401efa`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261115-02`, cible `main`.
Porteur : **#7444**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (4 unités, 4 lots)

| lot | source composée |
|---|---|
| #7435 | `201210812609` |
| #7436 | `c7c47eb69d3b` |
| #7437 | `cbfe1a9ab0e8` |
| #7440 | `ada0e91bbce3` |

## Composition (4 unités, 4 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #7435 | `201210812609` | `8326d979c582` | composition |
| 2 | #7436 | `c7c47eb69d3b` | `6c74c67fca2e` | composition |
| 3 | #7437 | `cbfe1a9ab0e8` | `8c5fb1054071` | composition |
| 4 | #7440 | `ada0e91bbce3` | `cdfb67d7463f` | composition |

## Écartés

| lot | motif |
|---|---|
| #7430 | retiré : préflight N-1 rouge déterministe (run 43665025258, test_5947 contrôle après retour arrière vers v1.55.0 : ameesh-carte.service/.timer out of date) ; #7430 change l'unité carte (OnCalendar horaire, ExecStart carte dispatch --horaire, option inconnue de la N-1) — --exclude, recomposition sans lui |
| #7439 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #7443 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |

Liste calculée : 6 lot(s) prêt(s), 2 en attente, 3 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 47.8 s.
- Types clients : types clients : inchangés.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
