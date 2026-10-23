# Paquet integration/paquet-h-20261023-1900 — composition (§12)

Base : `main` **`c14f9095c1da`** (fusion de la PR #2415, paquet h-1700, 17:09Z).
Branche `integration/paquet-h-20261023-1900`, cible `main`. Porteur : **#3101**.
Composeur : mty-amh-impl-01. Une seule suite complète est demandée sur la tête
finale (CI de la PR).

## Sources

- mesh-design, message #128962 (17:09Z) : paquet 1700 fusionné à la main
  (`c14f9095`), composer le suivant sur cette tête ; #3126 (déjà fusionné),
  #3007 (`0c9ef519`) et #3114 en tête, puis tous les prêts.
- mesh-design, message #128997 (17:16Z) : #3101 est prêt (tête `130e1c6f`,
  avis cx2 et dev5, deux familles indépendantes), à mettre dans le paquet en
  tête avec #3085 ; il conditionne la release de 21:00Z.
- Liste calculée `scripts/paquet-candidats.py` à 17:35Z sur `c14f9095` :
  **8 prêts, 16 en attente, 2 lectures impossibles** (#2914, #2994). Aucun
  nouveau prêt par rapport au calcul de 17:19Z ; les 8 prêts sont composés.
  #3007 et #3085, complets en avis mais en conflit avec `main`, sont composés
  sur directive du pilote et résolus (ci-dessous).

## Admissions (10 lots, 10 unités)

| lot | source composée | classe | avis (gate) | objet |
|---|---|---|---|---|
| #3101 | `130e1c6f8c0e` | sensitive | 2/2 : ameesh-cx2 (codex), ameesh-dev5 (deepseek) | EP1 : une admission de rôle élu ne compte qu'une fois dans `max_agents` |
| #3114 | `7206d9394f94` | sensitive | 2/2 : ameesh-dev2 (claude), ameesh-dev5 (deepseek) | `vm.sh redemarrer` : les drains s'additionnent, chaque arrêt attend son tour |
| #3183 | `e926c8f38846` | sensitive | 2/2 : mty-amh-docs-01 (deepseek), ameesh-cx2 (codex) | porte d'avis : ajouter une seconde famille ne relève pas le compte requis |
| #3180 | `8424aefac0ca` | sensitive | 2/2 : ameesh-cx2 (codex), auditeur (claude) | `host undrain` : option `--actor` pour un terminal humain |
| #3123 | `57c808b096da` | sensitive | 2/2 : auditeur (claude), ameesh-cx2 (codex) | bail d'un pilote volatil gardé à 300 s après un geste |
| #3181 | `6aa2ac08ac73` | sensitive | 2/2 : ameesh-cx2 (codex), mty-amh-docs-01 (deepseek) | clones git après coupure : objets et refs vides lus par la file |
| #3166 | `2adce6a69acb` | sensitive | 2/2 : auditeur (claude), ameesh-cx2 (codex) | image ameesh-executor : construire/pousser, relever le digest |
| #3104 | `5b9c6041bdf5` | sensitive | 2/2 : ameesh-dev5 (deepseek), ameesh-dev2 (claude) | verdict sans reçu : avertir ou refuser quand le sha ne se résout pas |
| #3007 | `0c9ef519ed99` | sensitive | 2/2 : ameesh-cx1 (codex), ameesh-dev2 (claude) | notifier humain : repli quand pc-smichea tombe, décision owner délivrée |
| #3085 | `3dff4945a148` | sensitive | 2/2 : ameesh-cx2 (codex), auditeur (claude) | portes de paquet/release : distinguer test négatif et non concluant |

#3101 porte en plus un avis indépendant `mty-amh-impl-01` (deepseek) rendu sur
la tête gelée, après le calcul de la liste : la porte restait satisfaite par
les deux familles acquises.

## Composition (10 unités, 10 lots)

| unité | lot | source | commit | note |
|---|---|---|---|---|
| 1 | #3101 | `130e1c6f8c0e` | `da840c455` | 0 conflit |
| 2 | #3114 | `7206d9394f94` | `c588b4099` | 0 conflit |
| 3 | #3183 | `e926c8f38846` | `df1c4c7de` | 0 conflit |
| 4 | #3180 | `8424aefac0ca` | `0cf2abd53` | 0 conflit |
| 5 | #3123 | `57c808b096da` | `981745212` | 0 conflit |
| 6 | #3181 | `6aa2ac08ac73` | `e8bf4e83c` | 0 conflit |
| 7 | #3166 | `2adce6a69acb` | `64afe30c8` | 0 conflit |
| 8 | #3104 | `5b9c6041bdf5` | `3d18ad035` | 0 conflit |
| 9 | #3007 | `0c9ef519ed99` | `7a9c2d855` | conflit `docs/EXECUTEUR-MEDIEE.md`, `src/ameesh/notify.py` résolu |
| 10 | #3085 | `3dff4945a148` | `5c5f2748e` | conflit `src/ameesh/merge_queue.py`, `src/ameesh/packet_automerge.py` résolu |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot
et sa tête, et il porte un trailer `ameesh-work:`. La composition part de
`c14f9095` ; aucun commit de `main` n'est venu pendant la composition.

## Résolutions de composition

**#3007 × main** (`7a9c2d855`) : `notify.py` garde la passe restreinte d'une
minuterie de rôle (#3057, retour avant observation) et place le battement de
#3007 **avant** elle : il prouve le notificateur, pas le périmètre observé.
`docs/EXECUTEUR-MEDIEE.md` : les deux comptes en conflit sont remplacés par
ceux du contrat fusionné (72 opérations, 425 refusées, interface 497).
Témoins : `tests.test_3007_notify_fallback` + `tests.test_3057_panier_vivacite`
**37/37**.

**#3085 × main** (`5c5f2748e`) : `merge_queue._required_ci` réunit la
signature (#3080 : `branch`, message `requirement`) et l'option
`allow_inconclusive` de #3085 (lecture `acceptable` pour la seule porte
feu/paquet). `packet_automerge` conserve le filtre `_required_ci` de #3066
(rollup réduit aux checks exigés, preuve `paquet`) et passe `report` : les
non concluants consignés entrent dans la note de fusion. Témoins :
`test_3085_nonconclusive_gates`, `test_3066_packet_required_checks`,
`test_2813_packet_auto_merge`, `test_2876_packet_replacement`,
`test_2286_queue_required_checks`, `test_1651_ci_parts` —
**206/206**, 2 sautés (non concluants, §3).

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #2914 | lecture impossible : provenance d'auteur inconnue pour `66a06cf0`, porte non lisible |
| #2994 | lecture impossible : provenance d'auteur inconnue pour `5158130a`, `d8c377cf`, porte non lisible |
| #3175 | `no-verdict` ; conflit `src/ameesh/packet_automerge.py` |
| #3185 | `verdicts-missing` (1/2 : cx2) |
| #3116 | `verdicts-missing` ; conflit `src/ameesh/review_authors.py` |
| #3120 | `verdicts-missing` (1/2 : mesh-design) |
| #3129 | `no-verdict` |
| #3130 | `no-verdict` ; conflit `src/ameesh/train_contracts.py` |
| #2987 | branche `canon/2987-porte-prod-main` non poussée sur la forge |
| #3056 | `blocked-verdict` |
| #3033 | `blocked-verdict` ; conflit `src/ameesh/reprise.py`, `src/ameesh/review_designation_activation.py` |
| #390 | `verdicts-missing` ; conflit `.github/workflows/tests.yml`, `site/docs/guides/install.md` |
| #2614 | `verdicts-missing` (1/2 : cx3) |
| #114 | `verdicts-missing` (1/2 : impl-07) ; conflit `src/ameesh/exploitation.py` |

#3126 (demandé par le pilote) est déjà fusionné dans `main` (`214505e2`, paquet
h-1700) ; il n'est pas recomposé.

## Contrôles

- **Liste calculée** : 8 prêts, 16 en attente, 2 lectures impossibles ;
  **8/8 prêts composés**, plus #3007 et #3085 sur directive (avis complets,
  conflits résolus). Aucun lot prêt laissé hors paquet.
- **Contrôles statiques** : `scripts/freeze-checks.py` **6/6** verts.
- **Tests ciblés des résolutions** : 37/37 (#3007/#3057) et 206/206
  (#3085 et portes de CI), 2 sautés non concluants.
- **Prévalidation** : indisponible (`card-stale`, carte de sélection de plus de
  48 h), non bloquant (§3) ; motif consigné.
- **Migrations** : `0474_notify_liveness.sql` seule ajoutée (additive), aucun
  numéro en collision.
- **Diff** : 65 fichiers, +14835/−244 ; `git diff --check` propre ; aucun
  marqueur de conflit.
- **Suite complète** : demandée une fois sur la tête poussée, par la CI de la
  PR (§12) ; le composeur n'en lance pas une seconde.
