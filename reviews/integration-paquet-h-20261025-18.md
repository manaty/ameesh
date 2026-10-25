# Paquet integration/paquet-h-20261025-18 — composition (§12), recomposition

Base : `main` **`f571ac5d484f611e1c4b674fc9d0ba8e0131073d`** (fusion du paquet
h-17, PR #2684, 25/10 09:06:39Z, admin-merge sur tête gelée).
Branche `integration/paquet-h-20261025-18`, cible `main`, PR #2699. Porteur :
**#3510** (créé par mesh-design, 25/10 09:08Z).
Composeur : mty-amh-impl-01.

**Recomposition (25/10 09:33Z)** : décision pilote **option C** (mesh-design,
mail #137688, 09:30Z). La première tête `27b3ba04865b` (14 unités) est
remplacée ; le run `40180614008` est annulé (§3, lot en attente) ; H5 #3472,
H6 #3473 et H7 #3474 sont retirés. Nouvelle composition : **11 unités,
11 lots**. Ses trois premières unités (#3505, H3 #3470, H4 #3471) sont les
commits d'origine — la porte les avait déjà passés avant de buter sur #3472 —
et les huit suivantes sont recomposées sur `da83973cf`. Le gel #18739, les
avis #18741 (mesh-design) / #18743 (cx1) et le feu #18744 portent sur l'ancienne
tête : ils ne sont pas reportés sur celle-ci ; cx1 et le pilote relisent le
delta de composition, puis CI, puis feu.

## Sources

- mesh-design, 25/10 09:09Z : périmètre et ordre de #3510 — #3505 en premier
  (il corrige la cause du rouge psycopg 7/8 de h-17), puis la chaîne Handoff
  H3 #3470, H4 #3471 avec la résolution H4×H3 `f94603aeb`, H5 #3472, H6 #3473,
  puis #3500, les trois retards de SLO (#3488, #3489, #3497), enfin #3498,
  #3502, #3503, #3504. « Tous sont relus. Pour la chaîne H, je confirme les
  avis sur les seuls deltas (désignations manuelles, provenance héritée). »
  Un lot en conflit passe à h-19 sans retenir les autres.
- mesh-design, 25/10 09:10Z : H7 #3474 (`57763fa2`) est conditionnel — à
  composer juste après H6 si l'avis DeepSeek d'impl-07 arrive avant le gel,
  sinon h-19 ; ne pas retarder le gel pour lui. L'avis est arrivé **avant le
  gel** : verdict #18732 d'impl-07 sur `57763fa2`, et le courrier du pilote
  09:22:30Z le confirmait déjà (la mention « 09:50Z » du premier manifeste
  était une coquille, relevée par cx1 et corrigée ici). H7 est néanmoins
  **hors paquet** par la décision C ci-dessous.
- mesh-design, 25/10 09:14Z : la composition de h-17 est enregistrée
  (`ameesh work packet record 3499`), ses 6 unités sont fusionnées ; lancer
  `ameesh work packet record 3510` dès la branche poussée.
- mesh-design, 25/10 09:30Z, décision **option C** : l'option A (admin-merge
  après CI verte) est exclue — forcer la fusion contournerait la porte
  d'indépendance (§2 : l'indépendance ne se présume jamais ; §7 : une porte
  codée ne se contourne pas par jugement d'agent), et l'owner est absent. Le
  coût de C est nul sur le chemin critique (l'enrôlement attend la décision
  H8 #137536). À faire : annuler le run `40180614008`, recomposer sur
  `f571ac5d4` sans #3472/#3473/#3474, geler, `packet record`, dire la tête ;
  cx1 et le pilote relisent le delta de composition, puis CI, puis feu. H5 à
  H7 iront dans h-19, quand H3 et H4 seront dans `main`.
- Liste calculée `packet_candidates.inventory` du 25/10 09:16Z sur la base
  `f571ac5d4`, projet `ameesh`/`main` explicites : **10 lots prêts**, 3 en
  attente, 3 erreurs (lecture de provenance, voir plus bas). Prêts : #3502
  (`21dcef37bf0f`), #3503 (`912a50fa0856`), #3470 (`34331d80606d`), #3488
  (`00b550ef37ef`), #3489 (`6e46a4a543e5`), #3497 (`927b614d29ac`), #3498
  (`f0d1484767a2`), #3500 (`96255cefab3a`), #3504 (`fd992f24fc8f`), #3505
  (`d0ade97c53f1`). #3471 entre par sa résolution (`f94603aeb`).
- Écart d'hôte déjà connu pour `scripts/paquet-candidats.py --project
  ameesh` (`project 'ameesh' is not declared in radar.projects`) : la liste
  est produite par le même module, en lecture seule, cible et base explicites
  — même écart que les manifestes h-16 et h-17.

## Admissions (11 lots, 11 unités)

| lot | source composée | avis au gel | objet |
|---|---|---|---|
| #3505 | `d0ade97c53f1` | 1 requis, 2 rendus : ameesh-dev3 (deepseek) #18687 ; mty-amh-impl-01 (deepseek) #18688 — auteur codex3 (codex) | Banc : rouvrir le journal asynchrone partagé après les tests L77 (3 lignes test-only, cause racine du rouge de h-17) |
| #3470 | `34331d80606d` | 2/2 : ameesh-cx1 (codex) #18675 ; auditeur (claude) #18676 | Handoff réel H3 : activation, santé et réconciliation |
| #3471 | `8564951b85e8` + résolution `f94603aeb3fb` | 2/2 : mesh-design (claude) #18623 ; mty-amh-qa-01 (deepseek) #18695 | Handoff réel H4 : pont secrets L232c et santé des comptes |
| #3500 | `96255cefab3a` | 2/2 : mty-amh-impl-05 (deepseek) #18702 ; mesh-design (claude) #18701 | `ameesh agent retire` : refuser un agent exécutant externe déclaré d'un lot ouvert |
| #3488 | `00b550ef37ef` | 2/2 : ameesh-dev3 (deepseek) #18646 ; auditeur (claude) #18641 | Panier « #3483 » : défauts non bloquants |
| #3489 | `6e46a4a543e5` | 2/2 : ameesh-dev3 (deepseek) #18636 ; auditeur (claude) #18634 | Panier « #3467 » : défauts non bloquants |
| #3497 | `927b614d29ac` | 1 requis, 2 rendus : ameesh-cx2 (codex) #18647 ; ameesh-cx1 (codex) #18644 | Débit : lot fusionné puis retenu pour déploiement compté hors `qa_merge` |
| #3498 | `f0d1484767a2` | 1 requis : ameesh-cx2 (codex) #18677 (aussi #18640) | Hold en attente du bureau : débit en attente externe |
| #3502 | `21dcef37bf0f` | 2/2 : ameesh-cx1 (codex) #18673 ; auditeur (claude) #18672 | Notifier : `estimate_bias` stable (un épisode par estimateur) |
| #3503 | `912a50fa0856` | 2/2 : ameesh-cx1 (codex) #18674 ; auditeur (claude) #18680 | Base : `jit=off` à l'ouverture de session, contrôle doctor |
| #3504 | `fd992f24fc8f` | 1 requis : ameesh-cx2 (codex) #18679 | Courrier : `agent-mail send` lit le corps sur l'entrée standard |

Les quatre lots à un seul avis requis (#3505, #3497, #3498, #3504) sont des
changements légers au sens du gel (banc test-only, débit, courrier) ; le gel a
désigné un relecteur d'une autre famille que l'auteur, verdict rendu.

## Composition (11 unités, 11 lots)

| unité | lot | source | commit | note |
|---|---|---|---|---|
| 1 | #3505 | `d0ade97c53f1` | `418e1711ae76` | 0 conflit, commit de la première tête conservé |
| 2 | #3470 | `34331d80606d` | `75f6bf24ecb3` | H3, 0 conflit, commit conservé |
| 3 | #3471 | `f94603aeb3fb` (résolution H4×H3) | `da83973cfca1` | 0 conflit, voir ci-dessous, commit conservé |
| 4 | #3500 | `96255cefab3a` | `81aca42c7fe5` | 0 conflit, recomposé |
| 5 | #3488 | `00b550ef37ef` | `b5600848bebd` | 0 conflit, recomposé |
| 6 | #3489 | `6e46a4a543e5` | `922a9cddae5d` | 0 conflit, recomposé |
| 7 | #3497 | `927b614d29ac` | `a8cd0a479b9f` | 0 conflit, recomposé |
| 8 | #3498 | `f0d1484767a2` | `40a485ce7f92` | 0 conflit, recomposé |
| 9 | #3502 | `21dcef37bf0f` | `d0722ecd8d9f` | 0 conflit, recomposé |
| 10 | #3503 | `912a50fa0856` | `8a660969d471` | 0 conflit, recomposé |
| 11 | #3504 | `fd992f24fc8f` | `f29c5faa0dcd` | 0 conflit, recomposé |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot
et sa source (`vehicule 1800 : #<lot> <source>`) et il porte un trailer
`ameesh-work:`. La base n'a pas bougé pendant la composition.

### H4 (#3471) — composition par la résolution préparée

Le conflit H4×H3 est résolu d'avance par le commit de résolution
`f94603aeb3fb7a38c9c6343bd2890e6cb4136ea7` (`Resolution: #3471 with #3470`,
parents `8564951b85e8` H4 et `34331d80606d` H3, conflits
`deploy/mise-a-jour/fabrique-handoff.py` et `src/ameesh/handoffs.py`).
L'unité 3 compose cette résolution comme source : aucun conflit, aucune
résolution manuelle sur le véhicule. H5 (`4c6063a7`), son child direct, n'est
pas composé (décision C).

## Conflits

Aucun. Les 11 unités se sont fusionnées sans intervention sur le véhicule ;
`git diff --check` est vert avant le gel. Le delta de la nouvelle tête contre
l'ancienne (`27b3ba04..HEAD`) ne retire que le contenu de H5, H6, H7 et
l'ancien manifeste — aucune autre unité n'est modifiée par le retrait.

## Provenance héritée — ce que la porte a refusé, et la décision C

Le gel de l'ancienne tête a passé les unités #3505, #3470 et #3471, puis la
porte des membres a retenu #3472 : `author provenance unknown for frozen
commit(s)` sur `ec2b0b38f15e`, `26bc85160ef6`, `3025e74545aa` (H3, auteur
mty-amh-impl-04) et `a9a7775a02be` (H4, auteur ameesh-cx2).

- Ces commits sont dans la plage gelée de H5/H6/H7 parce que leur branche est
  empilée sur H3/H4, mais leur reçu de provenance est porté par #3470/#3471,
  pas par le lot enfant. La porte lit le delta du membre depuis la base du
  paquet, pas depuis son parent : elle ne peut donc pas exclure qu'un auteur
  ait donné son propre avis ni qu'un relecteur ait corrigé lui-même
  (§2 : l'indépendance ne se présume jamais). Ce n'est pas une relecture
  manquante : les têtes de H5/H6/H7 portent bien leurs deux verdicts de
  familles.
- Décision pilote 09:30Z (option C) : ni admin-merge (A), qui contournerait
  une porte codée par jugement d'agent (§7), ni correction de la porte à
  chaud. H5/H6/H7 sortent du paquet et iront à h-19, quand H3 et H4 seront
  dans `main` : leur plage gelée repartira alors de `main` et sera lisible.
  Le défaut mécanique de la porte des lots empilés (`packet_reviews`, delta du
  membre lu depuis la base du paquet) part dans un lot de cause racine, sur
  instruction du pilote.
- H7 est exclu par cette décision, pas par un avis tardif : son verdict
  #18732 était acquis avant le gel (courrier du pilote 09:22:30Z).

## Contrôles

- **Liste calculée** : 10 prêts, 3 en attente, 3 erreurs. 11 composés =
  10 prêts + #3471 (conflit résolu d'avance `f94603aeb`). Écartés : #3472,
  #3473, #3474 (décision C, h-19), #3457 (branche non publiée), #3501
  (`no-verdict`), #3496 (témoin du drill #3439, hors paquet).
- **Contrôles statiques du gel** (`scripts/freeze-checks.py`) : 6 contrôles
  (garde mécanique #2449, compilation, attribution #1632, lint L63, alert
  types #1060, doc readers L161a), rejoués par le gel sur la tête gelée.
- **Suite complète** : une seule CI de la PR sur la tête gelée, demandée
  **après** la relecture du delta de composition par cx1 et le pilote
  (décision C). Le run `40180614008` de l'ancienne tête est annulé.
- **Prevalidation** : plus de Python hors `src/ameesh/` depuis le retrait de
  H7 (`deploy/mise-a-jour/canari-handoff.py`) ; la mesure du gel devrait être
  disponible, sinon le motif sera journalisé et cité au bilan.
- **Porte de fusion** : l'auto-merge n'est pas armé à ce gel ; le pilote
  donne le feu après les avis et la CI verte (§2, décision C).
