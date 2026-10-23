# Paquet integration/paquet-h-20261023-0900 — composition (§12)

Base : `main` **`3fbc7849f90c`** (fusion du paquet h-20261023-0500, PR
manaty/ameesh#2336, tête `3dfb438724e6`). Branche
`integration/paquet-h-20261023-0900`, cible `main`, lot porteur **#3090**
(issue manaty/ameesh#2356). Une seule suite complète sur la tête finale.

Composeur : mty-amh-impl-01, sur le courrier de mty-amh-impl-04 du 23/10
07:17Z (déblocage du mesh, #3010 en tête), la liste calculée du 23/10 07:2xZ
sur `3fbc7849f` et le canon §11 bis/§12.

**Correction v2 (23/10 07:4xZ)** : la première composition (10 unités)
portait #3044 ; la porte d'auto-fusion du véhicule reclasse #3044 en
sensible (36 fichiers « depuis main » : sa branche part d'une base ancienne)
alors que sa propre classe de revue est light à 1 avis claude, et exige un
second avis d'une famille différente — indisponible (Codex en échec). Le
véhicule est recomposé sans #3044 pour rendre la fusion possible ; #3044
reste prêt pour le paquet suivant après rafraîchissement de sa branche ou
second avis.

## Unités (9 unités, 9 lots portés)

| lot | source | classe | avis (familles) | auteur | objet |
|---|---|---|---|---|---|
| #3010 | `40f569adadfc` (remise) | sensitive | 2/2 ok (claude ameesh-dev1, codex ameesh-cx3) | mty-amh-impl-04 | EP1 : élection et candidats sur les fiches Agent ; réclamation du bail entre hôtes candidats |
| #2955 | `79be0a193833` | light | 1/1 ok (claude auditeur) | mty-amh-impl-08 | Veille GitHub : saut de pagination quand la dernière ligne d'une page pleine est en retard d'index |
| #3036 | `c3b49d1ca9c1` | light | 1/1 ok (codex ameesh-cx3) | mty-amh-impl-07 | Alertes et escalades routées par rôle sans vivacité |
| #3042 | `7da843cf9546` | sensitive | 2/2 ok (codex ameesh-cx2, claude auditeur) | ameesh-dev5 | L68a macOS : TMPDIR réel, findutils, préambule pip du banc 1252 |
| #3047 | `ed69ca107520` | sensitive | 2/2 ok (codex ameesh-cx2, claude auditeur) | mty-amh-impl-02 | Gel : savepoint du cast ::jsonb de `_loads` |
| #2771 | `040eece279ee` | sensitive | 2/2 ok (codex ameesh-cx2, deepseek ameesh-dev3) | ameesh-dev7 | Panier « ameesh » du 2026-10-19 : défauts non bloquants (N, P2, P3) |
| #2940 | `5970bda6039f` | light | 1/1 ok (claude ameesh-dev7) | mty-amh-impl-08 | Panier « #286 » du 2026-10-21 : borne des actifs non lus |
| #3041 | `a7e8e2170a29` | sensitive | 2/2 ok (codex ameesh-cx2, deepseek ameesh-dev3) | ameesh-dev2 | Panier « #3001 » du 2026-10-23 : origine relative |
| #3049 | `954407f379f8` | light | 1/1 ok (codex ameesh-cx2) | ameesh-dev1 | Panier « #3039 » du 2026-10-23 : délai de staging |

## Admissions

#3010, #2955, #3036, #3042, #3047, #2771, #2940, #3041, #3049

## Composition (9 unités)

| unité | lot | source | commit | note |
|---|---|---|---|---|
| 1 | #3010 | `40f569adadfc` | `512a78826` | fusion de la remise, 0 conflit |
| 2 | #2955 | `79be0a193833` | `9f9c59837` | fusion de la tête gelée, 0 conflit |
| 3 | #3036 | `c3b49d1ca9c1` | `2fa5ffa55` | fusion de la tête gelée, 0 conflit |
| 4 | #3042 | `7da843cf9546` | `adfdc6bcf` | fusion de la tête gelée, 0 conflit |
| 5 | #3047 | `ed69ca107520` | `877f2687c` | fusion de la tête gelée, 0 conflit |
| 6 | #2771 | `040eece279ee` | `a1cfd4380` | fusion de la tête gelée, 0 conflit |
| 7 | #2940 | `5970bda6039f` | `aa9bf83ad` | fusion de la tête gelée, 0 conflit |
| 8 | #3041 | `a7e8e2170a29` | `b7c520c41` | fusion de la tête gelée, 0 conflit |
| 9 | #3049 | `954407f379f8` | `783ca4c05` | fusion de la tête gelée, 0 conflit |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot
et son SHA, et il porte un trailer `ameesh-work: <lot>`. Arbre composé :
59 fichiers, +4253/−432 depuis `3fbc7849f`.

## Adaptations d'intégration (bancs seulement)

Les défauts d'intégration connus du véhicule h-0700 (PR #2348, retirée) sont
rejoués ici après retrait de #2954 et #3044 :

| lot | symptôme | correctif |
|---|---|---|
| #3010 | `test_420_qa_role` : `DECLARATIVE` compare désormais `releve`/`candidats`, le jeu déclaré ne les portait pas | ajout de `releve: "hote"`, `candidats: None` au jeu déclaré |
| #3036 | `test_l26_exploitation` « stale_lot_sans_role_par_historique » et `test_l317b_module_audit` (×2) : un brief n'est plus déposé à un agent sans bail vivant | les bancs simulent le runner qui tient l'agent (`lease_owner`, `lease_expires_at`) |

Aucun code produit modifié ; ce sont des adaptations de bancs (§11 bis), sans
avis supplémentaire. Commit `dc0af6fe9`.

## Écartés (ce véhicule)

| lot | source | motif |
|---|---|---|
| #2954 | `6ed210051663` | **test négatif** dans h-0700 (`test_l_interface_entiere_est_classee` : `WorkItems.set_type` ajouté à l'interface, non classé au contrat) ; la tête corrigée `6ed210051` n'a pas encore ses verdicts au calcul (verdicts-missing). Isolé, paquet suivant. |
| #3044 | `baebdb693403` | **refus de la porte d'auto-fusion** : reclassé sensible (36 fichiers « depuis main », base de branche ancienne) alors que sa classe de revue est light à 1 avis claude ; second avis d'une famille différente indisponible (Codex en échec). Recomposé sans lui ; à rafraîchir/revoir pour le paquet suivant. |
| #3035 | `b598739371f1` | prêt, mais **conflit** `docs/EXECUTEUR-MEDIEE.md` avec #3010 à l'essai de fusion (merge-tree rc=1) ; résolution à préparer, paquet suivant. |
| #3008 | `2af62cfad8e8` | prêt, première admission ; réservé au paquet suivant pour ne pas ajouter d'inconnue au véhicule de déblocage. |
| #3023 | `da2dcaa1e064` | prêt, première admission ; réservé au paquet suivant (idem). |

## En attente au calcul (non admis)

| lot | motif |
|---|---|
| #3048 | conflit `src/ameesh/train_contracts.py` avec la cible |
| #3007 | verdicts-missing sur la tête `3a0842b5833f` |
| #3066 | verdicts-missing sur la tête `05eae2b0c265` |
| #2994 | **erreur de lecture** de la porte : provenance inconnue des gels `5158130ae2da`, `d8c377cf31a4` ; regel requis |
| #2914 | **erreur de lecture** de la porte : provenance inconnue du gel `66a06cf09d5c` ; regel requis |

## Contrôles

- **Liste calculée** du 23/10 07:2xZ sur `3fbc7849f90c` : **13 lots prêts**
  (#3010, #3035, #2955, #3008, #3023, #3036, #3042, #3044, #3047, #2771,
  #2940, #3041, #3049), 2 erreurs localisées (#2994, #2914), le reste en
  attente motivée. `scripts/paquet-candidats.py` refuse de s'exécuter sur cet
  hôte : `project 'ameesh' is not declared in radar.projects (declared: none)`
  — la configuration de l'hôte n'a pas de clé `radar` et c'est un **écart
  signalé** ; la liste a été produite par le même module
  (`packet_candidates.inventory`) avec le projet `ameesh`/`main` explicite,
  sans aucune écriture. Aucune lecture globale n'a échoué.
- **Essais de fusion** : 9/9 propres, 0 conflit. Essais des admissions
  différées : #3008 et #3023 propres ; #3035 conflit doc ; #3044 refus de
  porte (classe), écarté.
- **Contrôle d'inclusion** (`scripts/paquet-inclusion.py --manifest … --head
  dc0af6fe9 --ready-file prets-0900.txt --no-expected`) : à consigner
  ci-dessous.
- **Contrôles statiques du gel** (`scripts/freeze-checks.py`) : **6/6 verts**
  sur la tête composée.
- **Tests ciblés** : 35 tests verts — `test_l165_election`,
  `test_l107_executor_contract.TableTest.test_l_interface_entiere_est_classee`,
  `test_420_qa_role.RolesPersistenceTest.test_roles_are_written_read_and_compared`,
  `test_l26_exploitation.AlertesTest.test_stale_lot_sans_role_par_historique`,
  `test_l317b_module_audit.AuditEvidenceTest`.

## CI

Suite complète unique portée par le run `pull_request` de la PR sur la tête
finale qui porte ce manifeste, étiquette `ci-full-<tête>` ; les runs d'une
tête remplacée sont annulés (composition v1 du 23/10 07:34Z). Aucune CI de
lot entre deux paquets (§11 bis).
