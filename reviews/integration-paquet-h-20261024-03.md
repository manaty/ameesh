# Paquet integration/paquet-h-20261024-03 — composition (§12)

Base : `main` **`61287c4ab2b3`** (fusion du paquet h-02, PR #2457, 02:25Z).
Branche `integration/paquet-h-20261024-03`, cible `main`. Porteur : **#3282**.
Composeur : mty-amh-impl-05. Une seule suite complète est demandée sur la tête
finale (CI de la PR).

## Sources

- Consigne h-03 (acquittée le 24/10 01:05Z, ids `131176`/`131177`) : #3003
  plus tout lot qa à 2 avis acquis sur sa tête gelée, hors #3220 (jusqu'à
  nouvelle tête de dev5, B12) et #2987 (hold bureau) ; composition sur `main`
  après la fusion de h-02, avant la release 1.21.0 (09:00Z).
- orchestrateur, message #131367 (24/10 02:07Z) : ajouter #3175 (gel
  `9a26c9c1`, OK cx1 codex + dev3 deepseek) et #3274 (gel `c8074ed3`, OK
  auditeur — vérifier le nombre d'avis requis) ; h-02 en cours.
- mesh-design, message #131405 (02:22Z) : ajouter #3277 (gel `82f71e51`,
  avis codex + deepseek OK) s'il passe sans conflit après h-02 ; résolution
  avec #3226 prête (`0383bdcb3`) si les deux entrent.
- mesh-design, message #131382 (02:12Z) : #3120 sort de h-03 (verdict
  `BLOCKED` B2/P1 d'ameesh-cx1 sur `23a59e56`), à corriger par impl-08 puis
  regeler.
- Calcul `scripts/paquet-candidats.py` (24/10 02:30Z, base `61287c4ab`, **code
  du déploiement 1.20.0** : le code de `main` lit `election_brief` (0477) que
  la base 1.20.0 n'a pas encore — même méthode que h-01/h-02) : **8 lots
  prêts** (#3003, #3175, #3220, #3266, #3273, #3274, #3277, #3279), 12 en
  attente, 1 lecture impossible (#2914). **6 composés**, 2 écartés avec motif
  ci-dessous.

## Admissions (6 lots, 6 unités)

| lot | source composée | classe | avis (gate) | objet |
|---|---|---|---|---|
| #3003 | `8c97c71f2bf0` | sensitive | 2/2 : ameesh-cx3 (codex), mty-amh-impl-01 (deepseek) | porte #2924 : la preuve de réordre est projetée sur la tête, un réordre qui survit avec une ligne insérée garde son auteur |
| #3175 | `9a26c9c122db` | sensitive | 2/2 : ameesh-cx1 (codex), ameesh-dev3 (deepseek) | fusion auto : un refus du passage est consigné au lot, la marque d'armement survit aux notes `held` |
| #3266 | `dff1dcbfd933` | sensitive | 2/2 : ameesh-cx3 (codex), auditeur (claude) | ménage : attribuer worktrees et tmp par agent et lot, relire la branche avant tout retrait |
| #3273 | `a2d8f9c3ba4f` | sensitive | 1/1 : mty-amh-impl-01 (deepseek) | reprise : le prompt idle nomme le pilote de l'équipe de l'agent, plus l'orchestrateur en dur |
| #3274 | `c8074ed3a5c5` | light | 1/1 : auditeur (claude) | panier ameesh du 24/10 : défauts non bloquants (plan_underused, hold d'assigné) |
| #3279 | `402465c03570` | sensitive | 2/2 : auditeur (claude), ameesh-cx3 (codex) | la marque d'armement ne dépend plus de la fenêtre des 200 événements |

## Composition (6 unités, 6 lots)

| unité | lot | source | commit | note |
|---|---|---|---|---|
| 1 | #3003 | `8c97c71f2bf0` | `77f2bb1eb` | 0 conflit |
| 2 | #3175 | `9a26c9c122db` | `58f4f6c50` | 0 conflit |
| 3 | #3266 | `dff1dcbfd933` | `5e45ddb6c` | 0 conflit |
| 4 | #3273 | `a2d8f9c3ba4f` | `5633fcef2` | 0 conflit |
| 5 | #3274 | `c8074ed3a5c5` | `03af1779b` | 0 conflit |
| 6 | #3279 | `402465c03570` | `c0eab9eb6` | 0 conflit, chevauchement connu avec #3175 sur `packet_automerge.py` — `ort` fusionne les hunks, vérifié sémantiquement (voir Contrôles) |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot
et sa tête, et il porte un trailer `ameesh-work:`. Aucun commit de `main`
n'est venu pendant la composition.

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #3220 | prêt sur sa nouvelle tête `d8f57e86` (gate 2/2 auditeur + cx1, merge-tree propre) mais la résolution avec #3003 (`a2ae5f7d`, 00:42Z) est bâtie sur l'ancienne tête #3220 `6ef4a48e` : à refaire sur `d8f57e86` et à relire sur le delta. Reporté à h-04 ; aucun des deux lots n'est bloqué. |
| #3277 | ne passe pas « sans conflit après h-02 » : conflit **sémantique** avec #3266 sur l'attribution du ménage. Ses 4 témoins de sûreté (`test_3277_tmp_over_quota` : montage, écriture profonde, clé antérieure active/verrouillée) échouent sur l'arbre intégré alors qu'ils sont verts seuls sur `main` + #3277 (le motif de conservation devient « no lot named » et l'entrée d'un lot clos n'est plus retirée). À re-préparer sur #3266 fusionné (h-04). |
| #3275 | conflit avec `main` `61287c4ab` sur `src/ameesh/lot_measures.py` et `src/ameesh/review_lot.py` (fichiers touchés par h-02), aucune résolution préparée ; à rafraîchir par dev4 puis h-04. |
| #3120 | verdict `BLOCKED` (B2/P1 reproduit par ameesh-cx1 sur `23a59e56`) : sortie décidée par mesh-design #131382 ; correction par l'auteur impl-08 puis regel. |
| #3226 | aucun verdict sur sa tête (`3b11550d`, gate non satisfaite) ; sa résolution avec #3277 reste inutilisée ce véhicule. |
| #2987 | hold bureau (porte de production §5), hors paquet. |
| #2914 | lecture impossible : gel antérieur `66a06cf0` absent du dépôt, porte non lisible ; non bloquant. |

Les autres lots qa du calcul sont en attente (verdicts manquants, tête gelée ≠
tête publiée ou hold) : aucun n'était prêt à la coupure.

## Contrôles

- **Liste calculée** : 8 prêts, **6/6 composés**, 2 écartés motivés (#3220,
  #3277) ; une lecture impossible (#2914), non bloquante.
- **Conflits** : 6/6 unités fusionnées par `ort` sans intervention manuelle.
  #3175 × #3279 (même `packet_automerge.py`) : les hunks sont distincts et
  l'union est cohérente — `marks()` lit désormais les seules notes
  `auto-merge …` par préfixe, sans fenêtre d'événements (#3279), et
  `_held()` déduplique sur ces mêmes lignes (`MARK_RE` inclut `held`, #3175).
  Témoins 215/215 verts sur `test_2813_packet_auto_merge` +
  `test_l161g_ci_gel`.
- **Tests ciblés de l'arbre intégré** : 482/482 verts (tests neufs de #3003,
  #3175, #3266, #3273 et #3274, plus les fichiers voisins).
- **Diff** : 26 fichiers, +1479/−92 sur `c0eab9eb6` ; `git diff --check`
  propre ; les six commits de composition portent leur trailer `ameesh-work:` ;
  aucun marqueur de conflit ni de saut de CI.
- **Migrations** : aucune (aucun fichier SQL, `deploy/`, migration) ; aucun
  geste de base dans ce paquet.
- **Prévalidation** : indisponible (`card-stale`, carte de sélection de plus
  de 48 h), non bloquant (§3), motif consigné.
- **Suite complète** : demandée une fois sur la tête poussée, par la CI de la
  PR (§12), `--avis 2` (le diff touche la porte de fusion : `review_authors.py`,
  `packet_automerge.py`, `packet_watch.py`). Aucune CI de lot.
