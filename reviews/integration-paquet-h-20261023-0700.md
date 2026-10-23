# Paquet integration/paquet-h-20261023-0700 — composition (§12)

Base : `main` **`3fbc7849f90c`** (fusion du paquet h-20261023-0500, PR
manaty/ameesh#2336, tête `3dfb438724e6`). Branche
`integration/paquet-h-20261023-0700`, cible `main`, lot porteur **#3077**.
Une seule suite complète sur la tête finale.

Composeur : mty-amh-impl-01, sur sa délégation de composition (canon,
20/10/2026) et la liste calculée `scripts/paquet-candidats.py` du 23/10
05:07Z sur la base `3fbc7849f` ; canon §11 bis et §12.

## Unités (11 unités, 11 lots portés)

| lot | source | classe | avis (familles) | auteur | objet |
|---|---|---|---|---|---|
| #3010 | `40f569adadfc` (remise) | sensitive | 2/2 ok (claude ameesh-dev1, codex ameesh-cx3) | mty-amh-impl-04 | EP1 : élection et candidats sur les fiches Agent ; réclamation du bail entre hôtes candidats |
| #2954 | `c992271bf92c` | sensitive | 2/2 ok (codex ameesh-cx2, claude ameesh-dev7) | mty-amh-impl-08 | Panier « ameesh » du 2026-10-21 : défauts non bloquants (N, P2, P3) |
| #2955 | `79be0a193833` | light | 1/1 ok (claude auditeur) | mty-amh-impl-08 | Veille GitHub : saut de pagination quand la dernière ligne d'une page pleine est en retard d'index |
| #3036 | `c3b49d1ca9c1` | light | 1/1 ok (codex ameesh-cx3) | mty-amh-impl-07 | Alertes et escalades routées par rôle sans vivacité |
| #3042 | `7da843cf9546` | sensitive | 2/2 ok (codex ameesh-cx2, claude auditeur) | ameesh-dev5 | L68a macOS : TMPDIR réel, findutils, préambule pip du banc 1252 |
| #3044 | `baebdb693403` | light | 1/1 ok (claude auditeur) | mty-amh-impl-05 | Jauges Codex : un compte team au repos reste utilisable |
| #3047 | `ed69ca107520` | sensitive | 2/2 ok (codex ameesh-cx2, claude auditeur) | mty-amh-impl-02 | Gel : savepoint du cast ::jsonb de `_loads` |
| #2771 | `040eece279ee` | sensitive | 2/2 ok (codex ameesh-cx2, deepseek ameesh-dev3) | ameesh-dev7 | Panier « ameesh » du 2026-10-19 : défauts non bloquants (N, P2, P3) |
| #2940 | `5970bda6039f` | light | 1/1 ok (claude ameesh-dev7) | mty-amh-impl-08 | Panier « #286 » du 2026-10-21 : borne des actifs non lus |
| #3041 | `a7e8e2170a29` | sensitive | 2/2 ok (codex ameesh-cx2, deepseek ameesh-dev3) | ameesh-dev2 | Panier « #3001 » du 2026-10-23 : origine relative |
| #3049 | `954407f379f8` | light | 1/1 ok (codex ameesh-cx2) | ameesh-dev1 | Panier « #3039 » du 2026-10-23 : délai de staging |

## Admissions

#3010, #2954, #2955, #3036, #3042, #3044, #3047, #2771, #2940, #3041, #3049

## Composition (11 unités)

| unité | lot | source | commit | note |
|---|---|---|---|---|
| 1 | #3010 | `40f569adadfc` | `a0587829f1f7` | fusion de la remise, 0 conflit |
| 2 | #2954 | `c992271bf92c` | `1d765975faec` | fusion de la tête gelée, 0 conflit |
| 3 | #2955 | `79be0a193833` | `224a5ce62473` | fusion de la tête gelée, 0 conflit |
| 4 | #3036 | `c3b49d1ca9c1` | `3d04ed2a059e` | fusion de la tête gelée, 0 conflit |
| 5 | #3042 | `7da843cf9546` | `763eb84b8ea1` | fusion de la tête gelée, 0 conflit |
| 6 | #3044 | `baebdb693403` | `cabe81d9170c` | fusion de la tête gelée, 0 conflit |
| 7 | #3047 | `ed69ca107520` | `516362f36616` | fusion de la tête gelée, 0 conflit |
| 8 | #2771 | `040eece279ee` | `f3346e74b45c` | fusion de la tête gelée, 0 conflit |
| 9 | #2940 | `5970bda6039f` | `a8286712a947` | fusion de la tête gelée, 0 conflit |
| 10 | #3041 | `a7e8e2170a29` | `c9d143548fdc` | fusion de la tête gelée, 0 conflit |
| 11 | #3049 | `954407f379f8` | `90160aef577e` | fusion de la tête gelée, 0 conflit |

Arbre composé : 70 fichiers, +4966/−470 depuis `3fbc7849f90c`.

## Écartés (ce véhicule)

| lot | source | motif |
|---|---|---|
| #3048 | `9c1ca42535e4` | prêt, mais **conflit** avec main sur `src/ameesh/train_contracts.py` (même ligne d'épinglage que #3042) ; la résolution `43b33f87ae6d` existe, prochain paquet après #3042. |
| #3007 | `0a8a5747d03a` | **conflit** avec main sur `docs/EXECUTEUR-MEDIEE.md` ; remise refusée (gel antérieur `3a0842b5833f` absent, indépendance non prouvable) ; hors véhicule (consigne pilote). |
| #3035 | `d6d820500c6a` | verdict **cx2 manquant** au calcul 05:07Z (auditeur OK) ; entre au paquet suivant dès le 2/2. |
| #3066 | `05eae2b0c265` | verdict **dev3 manquant** (auditeur OK) ; porte de fusion des paquets, suivie dès le 2/2. |
| #2994 | — | **erreur de lecture** de la porte : gel antérieur `5158130ae2da` absent du dépôt, indépendance non prouvable ; regel par l'auteur requis. |
| #2914 | — | **erreur de lecture** de la porte : gel antérieur `66a06cf09d5c` absent du dépôt ; regel par l'auteur requis. |

## En attente au calcul (non admis)

| lot | motif |
|---|---|
| #114 | verdicts-missing (2) + conflit `src/ameesh/exploitation.py` |
| #390 | verdicts-missing (2) + conflit `.github/workflows/tests.yml`, `site/docs/guides/install.md` |
| #2614 | verdicts-missing (un seul avis codex) |
| #2987 | branch-not-published (`canon/2987-porte-prod-main`) |
| #2993 | verdicts-missing (aucun avis) |
| #3008 | verdicts-missing (un seul avis codex) |
| #3023 | verdicts-missing (aucun avis) |
| #3033 | verdicts-missing (aucun avis) |
| #3038 | verdicts-missing (aucun avis) |
| #3054 | no-verdict (gel effectif refusé) |
| #3063 | missing-branch (rien à composer) |

## Contrôles

- Calcul du 23/10 05:07Z sur `3fbc7849f` : **11 lots prêts**, 15 en attente,
  **2 erreurs de lecture** (#2994, #2914). Le script sort 1 et retire son
  export `--ready-out` dès qu'une lecture échoue (#2404) ; les deux erreurs
  sont **localisées** à deux lots dont la porte ne peut pas prouver
  l'indépendance (gel antérieur absent) et qui exigent un regel de leur
  auteur. Tous les autres lots ont été évalués sans erreur ; les 11 prêts
  sont complets. Décision de composer consignée ici et au journal du porteur.
- Essais de fusion : **11/11 propres**, 0 conflit.
- Contrôle d'inclusion (`scripts/paquet-inclusion.py --manifest … --head
  `90160aef577e` --ready-file prets-0700.txt --no-expected`) : **aucun
  défaut** — 11 entrées de composition, verdicts « présent » 11/11, 11 lots
  attendus, 11 prêts, 11/11 contenus à la tête ; exclusions #3048, #3007,
  #3035, #3066, #2994, #2914 consignées.
- Contrôles statiques du gel (`scripts/freeze-checks.py`) : 6/6 verts sur
  `90160aef577e` ; rejoués sur la tête finale qui porte ce manifeste.
- Tests ciblés : les auteurs les ont rendus verts avant gel ; la suite
  complète de la CI sur cette tête fait foi (§3, §11 bis).

## CI

Suite complète unique demandée sur la tête finale qui porte ce manifeste
(`ci-full-<sha>`), après le gel et la poussée ; le verdict est consigné au
journal du porteur #3077. Aucune CI de lot entre deux paquets (§11 bis).
