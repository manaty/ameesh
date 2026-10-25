# Paquet integration/paquet-h-20261025-15 — composition (§12)

Base : `main` **`874a173802e2cb069016d0f3d4dfec40855345c7`** (fusion du paquet
h-14, PR #2617, 25/10 02:20:32Z, CI complète verte).
Branche `integration/paquet-h-20261025-15`, cible `main`. Porteur : **#3461**
(créé par mesh-design, fil du lot, 25/10 01:34Z).
Composeur : mty-amh-impl-01. Une seule suite complète est demandée sur la tête
finale (CI de la PR).

## Sources

- mesh-design, message du 25/10 01:34Z : h-15 à composer dès la fusion de h-14
  (#3431), base = tête de `main` après h-14 ; candidats à confirmer par
  `paquet-candidats.py` : #3440 en premier, #3437 si avis acquis, #3430 si
  prêt et code ameesh, #3449 si gelé avec ses deux avis. Consignes : fetch
  d'`ameesh-packets` sur agents-2 après chaque poussée, aucune annulation de
  run, un rouge se traite en retirant le lot fautif.
- mesh-design, message du 25/10 01:50Z : candidat supplémentaire **#3444**
  (sensible, gel `e99e0945575f`, avis OK de cx3, dev2 et de l'auditeur,
  merge-tree propre sur 52c74fd1c) ; « sa fusion ne vaut pas livraison. Le lot
  reste ouvert après le paquet (garde-fou seulement, frontière en décision
  owner #135872) ».
- mesh-design, message du 25/10 02:37Z : h-14 a fusionné, `main` =
  `874a17380` ; composer h-15 maintenant, dans l'ordre de priorité #3440,
  #3449 (`a236ca5af25a`, avis cx2 et auditeur), #3444, #3437, #3450 ;
  ajouter #3453 si ses avis sont acquis ; pousser dès que c'est composé ;
  rien n'est réservé sur `main`.
- auditeur, message du 25/10 02:38Z : h-14 fusionné à 02:20:32Z (PR #2617,
  `main` 874a1738, CI complète verte) ; la condition du hold est levée ;
  #3440 et #3437 accusent 1 h 04 de retard SLO, #3449 est la porte de fusion.
- Calcul `scripts/paquet-candidats.py --repo . --target main` sur la base
  ci-dessus, 25/10 02:40Z : **6 lots prêts**, 5 en attente, 0 erreur. Prêts :
  #3440 (`e808be66f931`), #3449 (`a236ca5af25a`), #3444 (`e99e0945575f`),
  #3450 (`ae7b0f0e28cf`), #3437 (`df16a441b88f`), #3452
  (`6f976c201410`). En attente : #3451 (`verdicts-missing`), #3453
  (`conflict` sur `src/ameesh/review_lot.py`), #3454 et #3467
  (`blocked-verdict`), #3457 (`branch-not-published`).

## Admissions (5 lots, 5 unités)

| lot | source composée | classe | avis (gate) | objet |
|---|---|---|---|---|
| #3440 | `e808be66f931` | light | 1/1 : auditeur (claude), verdict 18310 | Doc : la procédure EP7 emploie le **nom d'agent élu**, pas le rôle (exemple mesh-design) ; `role status` et le standby sont corrigés |
| #3449 | `a236ca5af25a` | sensitive | 2/2 : ameesh-cx2 (codex) ; auditeur (claude), avis du 02:01Z | CI : une part Linux planifiée annulée, expirée ou absente rougit l'agrégat `tests` ; 15/16 n'est plus vert (décision 128512) |
| #3444 | `e99e0945575f` | sensitive | 2/2 : ameesh-cx3 (codex), verdict 18373 ; ameesh-dev2 (claude), verdict 18371, + auditeur | pc-smichea : les exécuteurs d'agents ne lisent plus les clés SSH de l'owner — **garde-fou** (décision 133454 a non satisfaite) |
| #3437 | `df16a441b88f` | light | 1/1 : ameesh-cx1 (codex), avis du 00:42Z | Débit : un lot `qa` ciblant la branche d'un autre lot ouvert attend ce lot, plus de `qa_merge` propre |
| #3452 | `6f976c201410` | sensitive | 2/2 : ameesh-cx3 (codex) ; auditeur (claude), avis du 02:40Z | Panier mesures du 25/10 : hôte périmé (âge, `stale`), `idle_with_mail` hors externes, troncature `cost turns`, `inputs` des tours, imputation des tours du pilote |

Les têtes composées sont les têtes **publiées** lues par le calcul (gel effectif
= tête publiée, verdicts acquis sur cette tête). #3452 est prêt mais non cité
par le pilote au 02:37Z (son second avis est arrivé à 02:40Z) : il est composé
plutôt qu'écarté, conformément au calcul (`paquet-candidats.py`) et au §12.
#3444 est composé comme garde-fou : sa fusion **ne vaut pas livraison**, le lot
reste ouvert après le paquet (frontière en décision owner 135872).

## Composition (5 unités, 5 lots)

| unité | lot | source | commit | note |
|---|---|---|---|---|
| 1 | #3440 | `e808be66f931` | `e1df39cbbcfa` | 0 conflit |
| 2 | #3449 | `a236ca5af25a` | `2450fdbf8c97` | 0 conflit |
| 3 | #3444 | `e99e0945575f` | `013b8906ea87` | 0 conflit |
| 4 | #3437 | `df16a441b88f` | `2c2d22584604` | 0 conflit |
| 5 | #3452 | `6f976c201410` | `adbec0e012fe` | 0 conflit |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot
et sa tête (`vehicule 1500 : #<lot> <tête>`), et il porte un trailer
`ameesh-work:`. La base n'a pas bougé pendant la composition.

## Contrôles

- **Liste calculée** : 6 prêts, **5/5 composés**, 1 écarté (#3450, motivé
  ci-dessous), 0 erreur.
- **Conflits** : 5/5 unités fusionnées sans intervention ; chaque arbre de
  composition est identique à `git merge-tree --write-tree` de ses parents
  (vérifié commit par commit) ; aucun marqueur de conflit, aucun arbitrage de
  code ; `git diff --check` vert.
- **Tests ciblés de l'arbre intégré** : **465/465 verts** (5 sautés) en 489 s
  sur les modules des unités et sur le contrat CI — `test_2446_train_contract`,
  `test_3449_agregat_run_annule`, `test_3251_agregat_annulation`,
  `test_940_agregat_bash`, `test_carte_parts_completes`, `test_3344_map_stale`,
  `test_l161f_plan`, `test_3444_cles_owner`, `test_3437_debit_dependance`,
  `test_3292_debit`, `test_288_cost_per_message`, `test_l13_budget`,
  `test_l26_exploitation`, `test_l31_resources`.
- **Compilation / contrôles statiques** : 6/6 verts (`scripts/freeze-checks.py` :
  garde mécanique #2449, compilation, attribution #1632, lint L63, alert types
  #1060, doc readers L161a).
- **Prévalidation** : à lire au journal du gel (sélection possiblement non
  sûre car `.github/workflows/tests.yml` est modifié par #3449) —
  non bloquante, motif exact consigné au lot et dans la demande de CI.
- **Diff** : 26 fichiers, +2192/−166 (manifeste compris). Classe sensible :
  `.github/workflows/tests.yml`, `scripts/ci-agregat.py`, `tests/parts/`,
  `src/ameesh/train_contracts.py` (contrat #2446 re-pinné par #3449),
  `deploy/poste/` et `deploy/systemd/`, `docs/EXPLOITATION.md` (doc de
  déploiement). Aucune migration SQL.
- **Inclusion** : `scripts/paquet-inclusion.py` rc 0 sur la tête (attendus =
  les 5 admis ; prêts = les 6 du calcul, #3450 écarté avec motif ; 0 omission).
- **Suite complète** : demandée sur la tête finale, en mode paquet ; les
  verdicts unitaires couvrent les contenus, le paquet est relu sur sa tête par
  les familles du porteur (§12).
- **Fusion** : sous garde de tête, dès le vert et les avis du porteur ;
  landings `ameesh work landing <unité> --vehicle 3461` déclarés au gel
  (#3444 excepté : garde-fou, lot à laisser ouvert).

## Têtes précédentes (retirées avant gel final)

- `1492fbeee54a` : rouge CI sur #3450 — 5 tests `StaleMapLotTest` échouaient
  sous `init.defaultBranch=main` (le premier commit du dépôt jetable crée déjà
  `main`, `git checkout -b main` renvoie 128). Adapté en `checkout -B main`.
- `84a40dccbd79` : rouge CI sur le contrat #2446 — #3450 modifie
  `scripts/test-plan.py` sans re-pinner le blob dans `src/ameesh/train_contracts.py`
  (contrat audité, deux familles). **#3450 est retiré du paquet** plutôt que de
  modifier le contrat dans le véhicule ; il repartira en paquet suivant après
  re-pin et relecture.

## En attente (calcul, hors paquet)

| lot | motif |
|---|---|
| #3451 | `verdicts-missing` : gel `da667e031d12`, un seul avis (cx3), il en faut deux (sensible) |
| #3453 | `conflict` avec `main` sur `src/ameesh/review_lot.py` (et avis non acquis) — à recomposer sur la nouvelle tête |
| #3454 | `blocked-verdict` : gel refusé |
| #3457 | `branch-not-published` : branche `auditeur/session-taille-pilotes` absente d'origin |
| #3467 | `blocked-verdict` : gel refusé |

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #3450 | Retiré après le rouge du contrat #2446 : le blob de `scripts/test-plan.py` n'est pas re-pinné dans `src/ameesh/train_contracts.py` ; un changement de contrat demande une relecture à deux familles, hors de ce véhicule — retour en paquet suivant après re-pin |
| #3430 | App nexlink (cible `develop`, hold pilote jusqu'au 11:30Z) : hors du `main` d'ameesh |
| #3453 | Conflit sur `src/ameesh/review_lot.py` avec `main` + avis non acquis ; à recomposer et relire |

Les 6 lots prêts du calcul de 02:40Z sont traités : 5 composés, #3450 écarté
avec motif ; aucun lot prêt n'est écarté sans motif.
