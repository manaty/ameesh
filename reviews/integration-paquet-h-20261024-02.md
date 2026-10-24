# Paquet integration/paquet-h-20261024-02 — composition (§12)

Base : `main` **`f71414d8eb2d`** (fusion du paquet h-01, PR #2454).
Branche `integration/paquet-h-20261024-02`, cible `main`. Porteur : **#3269**.
Composeur : mty-amh-impl-05. Une seule suite complète est demandée sur la tête
finale (CI de la PR).

## Sources

- orchestrateur, message #131084 (24/10 00:32Z) : composer h-02 sur la tête de
  `main` (`f71414d8e`) avec les lots prêts, au minimum #3207 et #3250, plus
  tout autre lot qa à 2 avis acquis sur sa tête gelée, sans conflit ; hors
  paquet #2987 (hold bureau, porte prod §5) et #3220 (nouvelle tête attendue
  de dev5). Une seule CI, fusion au vert, avant la release 1.21.0 (09:00Z).
- Calcul `scripts/paquet-candidats.py` (24/10 00:40Z, base `f71414d8e`,
  **code du déploiement 1.20.0** : le code de `main` lit `election_brief`
  (migration 0477) que la base 1.20.0 n'a pas encore — même méthode que h-01) :
  **7 lots prêts** (#3185, #3129, #3130, #3207, #3250, #3251, #3056),
  13 en attente, 1 lecture impossible (#2914). Les 7 prêts sont composés ;
  aucun lot prêt laissé hors paquet.

## Admissions (7 lots, 7 unités)

| lot | source composée | classe | avis (gate) | objet |
|---|---|---|---|---|
| #3251 | `330ce1a8f22e` | sensitive | 2/2 : auditeur (claude), ameesh-cx1 (codex) | agrégat `tests` : une annulation interne ne rougit plus l'agrégat, un run annulé reste rouge (décision owner #128512) |
| #3207 | `ac64bc98e025` | sensitive | 2/2 : ameesh-cx1 (codex), ameesh-dev1 (claude) | `macos.yml` : retirer `cache: pip` de `setup-python` sous `pull_request_target` |
| #3130 | `0f77b9d8cd16` | sensitive | 2/2 : ameesh-cx3 (codex), mty-amh-impl-01 (deepseek) | macOS : formule `flock` (Homebrew) pour le verrou de `staging.sh` |
| #3129 | `4207a89006cd` | light | 1/1 : ameesh-cx1 (codex) | faux `cat` des bancs : exécuter le vrai `cat` résolu (`/bin/cat` sur macOS) |
| #3056 | `643f83c865ad` | sensitive | 2/2 : ameesh-cx1 (codex), ameesh-dev3 (deepseek) | panier #3038 : un échec de `git diff` n'efface pas la taille, doute décidé avant la CI |
| #3250 | `dba5983c79c8` | sensitive | 2/2 : mty-amh-impl-03 (deepseek), auditeur (claude) | porte de gel : rafraîchir et épingler la cible exacte avant la plage de provenance |
| #3185 | `6cce844c1e13` | sensitive | 2/2 : ameesh-cx2 (codex), mty-amh-docs-01 (deepseek) | désignation : relecteurs déclarés hors équipe (`relit:`), migration 0479 |

## Composition (7 unités, 7 lots)

| unité | lot | source | commit | note |
|---|---|---|---|---|
| 1 | #3251 | `330ce1a8f22e` | `e7004b24a` | 0 conflit |
| 2 | #3207 | `ac64bc98e025` | `1bd13dd14` | 0 conflit |
| 3 | #3130 | `0f77b9d8cd16` | `faac3db2b` | 0 conflit (même `macos.yml` que #3207, hunks distincts) |
| 4 | #3129 | `4207a89006cd` | `0d85c8e25` | 0 conflit |
| 5 | #3056 | `643f83c865ad` | `b995e9546` | 0 conflit |
| 6 | #3250 | `dba5983c79c8` | `01bf1a03c` | 0 conflit |
| 7 | #3185 | `6cce844c1e13` | `bf82c105e` | 0 conflit, base ancienne (d93b3a46) fusionnée proprement |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot
et sa tête, et il porte un trailer `ameesh-work:`. Aucun commit de `main` n'est
venu pendant la composition. Aucune résolution manuelle : 7/7 unités
fusionnées proprement.

## Correction après CI rouge (tête `a4827fcce410`, run `40046106347`)

La suite complète a rendu deux échecs, le même garde dans les parts
`psycopg 3/8` et `psql 1/16` :
`tests.test_cluster.SuperviseurRoleTest.test_chaque_colonne_est_accordee_ou_exclue`
— `agent_registry.relit` (migration `0479`, #3185) n'était ni accordée ni
exclue au contrat des rôles superviseur.

Correctif `53c348f66` (`deploy/sql/role-superviseur.sql`) : `relit` rejoint
les colonnes lisibles du rôle de base, comme `projects` (0388) et `roles`
(0427) — des noms de projets déclarés, pas de contenu. Aucun GRANT retiré ;
aucune autre colonne ne bouge. Avis delta SQL (deux familles) requis par le
§12 avant fusion.

Ciblés verts sur `53c348f66` : `tests.test_cluster.SuperviseurRoleTest`
16/16, `tests.test_420_qa_role` 33/33.

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #2987 | hold bureau : porte de production §5, hors paquet (orchestrateur #131084) |
| #3220 | nouvelle tête attendue de dev5 ; relecture en cours (orchestrateur #131084) |
| #2914 | lecture impossible : gel antérieur `66a06cf0` absent du dépôt, porte non lisible ; non bloquant, comme aux paquets h-23 et h-01 |

Les 13 autres lots qa du calcul sont en attente (verdicts manquants, tête
gelée ≠ tête publiée, conflit ou hold) : aucun n'était prêt à la coupure.

## Contrôles

- **Liste calculée** : 7 prêts, **7/7 composés**. Une lecture impossible
  (#2914), non bloquante pour ce véhicule, avec son motif ci-dessus.
- **Contrôles statiques** : `scripts/freeze-checks.py` **6/6** verts.
- **Diff** : 41 fichiers, +1994/−263 sur `53c348f66` (correctif de contrat
  inclus) ; `git diff --check` propre ; les sept commits de composition
  portent leur trailer `ameesh-work:` ; aucun marqueur de conflit ni de saut
  de CI.
- **Migrations** : la base 1.20.0 porte `0475`–`0476` ; `main` porte déjà
  `0477_election_brief` et `0478_role_handover`, et #3185 ajoute
  `0479_agent_relit` — toutes additives. La release 1.21.0 les applique avant
  de relancer les exécuteurs ; aucun geste de base dans ce paquet.
- **Prévalidation** : indisponible (`card-stale`, carte de sélection de plus
  de 48 h), non bloquant (§3) ; motif consigné.
- **Suite complète** : demandée une fois sur la tête poussée, par la CI de la
  PR (§12). Aucune CI de lot.
