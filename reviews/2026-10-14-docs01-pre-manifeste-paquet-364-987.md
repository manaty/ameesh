# Pré-manifeste du paquet suivant #364 + #987 (distinct de #1204)

mty-amh-docs-01, 14/10/2026 ~06:2xZ. Consolidation autorisée par mesh-design (mails
06:04/06:07/06:10 ; lot **#1260**, 06:07Z, porté par ameesh-cx2). Pièces d'auteurs
consolidées : impl-04 `review/impl04-364-piece-constante @627baee0c`
(`reviews/2026-10-14-364-piece-constante.md`) et impl-05
`review/impl05-piece-987-paquet-suivant @39e3f2ded`
(`reviews/2026-10-14-impl05-piece-987-paquet-suivant.md`). Les chiffres d'union de cette
pièce sont revérifiés en première main (§4). Révisé 14/10 06:1xZ : recoupement indépendant
ameesh-cx2 (06:15Z) intégré (§4bis). Révisé 14/10 06:2xZ (corrections documentaires
demandées par cx2, 06:17Z) : le **cœur** (#364+#987, arbre `4b65a177…`, 77 fichiers) est
distingué de la **simulation avec compagnons** (annexe B) ; blobs propres au cœur en
**annexe C** ; §7 (patch-ids du cœur) et §8 (paquet à six lots = #1204) corrigés. Révisé
14/10 06:3xZ (relu et recoupé par cx2, mail 06:24Z) : **§5** consolidé avec les
identifiants primaires exacts (jalons `work_item_milestones`, feu `work_item_events`,
binding `session_bindings`) ; ces avis **delta/historiques** ne valent pas GO complet
nouveau et la qualification de famille reste réservée au pilote. Précision cx2 (mail
06:32Z) : le binding **enregistre** un harnais Claude ; il ne prouve ni l'exécution réelle
ni le modèle, à qualifier sur la ligne primaire (registre ≠ preuve réelle, canon §11).
Révisé 14/10 07:2xZ (mail cx2 07:21Z) : **ligne primaire retrouvée** pour #2696/#2739
(session `11783f10…`, modèle `claude-opus-5-5`, résultats `is_error:false`) — §5, §7.3 et
§8 actualisés ; cette pièce est citée **sous la main de cx2** et n'est pas revérifiable
depuis `ameesh-agents-2`.

**Aucun feu de fusion, de déploiement ni de CI ; aucune mutation d'une branche gelée.** Le
gel #1204 (`origin/integration/vague-suivante-1204 = 39df7c316c9e`, run 38504851830 intact)
n'est ni élargi ni touché.

## 1. Base, porte, périmètre

- Base de tous les calculs : `main = 7305957e88b7f511c2e853e64a6573f8e574832e` (#833
  fusionné à 06:07Z), relevé 14/10 06:1xZ.
- **Cœur du paquet : #364 + #987.** Compagnons **candidats**, non inclus par défaut :
  #974 (après #364) et #1002 (après #987), inclus seulement si leurs avis/dépendances
  sont qualifiés avant le gel. #1257 (cause racine carte, impl-06) reste suivi à part.
  Le « paquet à six lots » cité par le corps #1260 désigne **#1204, déjà gelé** ; #1260
  n'ajoute aucun sixième lot (correction cx2, 14/10 06:17Z).
- **Porte #1211** : non active. PR #923, tête
  `39f51fd9d0105b21911adb1d56c7df67fc3876eb` inchangée ; delta propre = 10 fichiers depuis
  le merge-base `229434d7b27353c042c968fec4989a3afd90edcc` ; `merge-tree` main × #1211 =
  **deux conflits** (`tests.yml`, `scripts/test-plan.py`) à résoudre avant activation
  (mesh-design 06:10Z, préparation locale binôme, sans CI aveugle).
- Les quatre têtes candidates sont **inchangées** au relevé (`git ls-remote`), aucun
  close/reopen, aucun réarmement.

## 2. Entrées immuables

| lot | PR | tête | arbre de la tête | base de lecture | classe | dépendances |
|---|---|---|---|---|---|---|
| #364 | 693 | `90aae6672b5d0dd624a503bdeb8943a7caaf7623` | `2039734bdb41369a4e1f330f4c9944bb579b8766` | merge-base `b132d5d4397efdd7ba05e7682bdbf68eab61703e` ; réf. testée `069e9fea9bd5` (parents `afa32932e` + tête) | sensible prod + SQL | #671, #1112 (ancêtres de main) ; CI du paquet sous #1211 |
| #974 | 824 | `7e65733c3fe46d5af4db144e6f54552d689c049c` | `310c994b530f680519a6d8a396ed5152fe03ec3c` | `90aae667…` (empilée sur #364) | légère (docs) | #364 (bloquant) |
| #987 | 763 | `2df0b80def30ea5f19a2ae2468038698aea1d39a` | `147cd70284a0cf5613f251c7597c705ee3b700cf` | `b85777c1e3e757b9ba0caa8cef49b0db1558b251` (base PR) | sensible hors prod (CI) | #1211 actif, #940, #876 |
| #1002 | 773 | `7aaa5650934f6f3e62a16b4803e4453677421acf` | `bf8301d40acfc926a52e77fda8d9c46a5c4c1740` | `2df0b80d…` (empilée sur #987) | sensible hors prod (CI) | #987 (bloquant) |

## 3. Deltas et empreintes des lots

### #364 — 73 fichiers, +13817/−99, 43 commits (`b132d5d4…` → `90aae667`)

- Contenu : 25 créations dans `deploy/mise-a-jour/`, 3 `deploy/sql/role-*.sql` modifiés +
  `role-release-controller.sql` créé, `deploy/serveur/*`, `docs/L232B-*.md`,
  `src/ameesh/{cli,db,migrations}.py`, migration
  `src/ameesh/migrations/0417_host_release_fences.sql`, `storage/postgres/registry.py`,
  `storage/postgres/release_fences.py`, 24 tests `test_l232b_*` + 4 tests existants.
- La table complète des 73 fichiers (blob git + SHA256) est dans la pièce impl-04 ;
  les empreintes du **cœur** sont en **annexe C**, celles de la **simulation avec
  compagnons** (hors cœur par défaut) en annexe B.

### #987 — 4 fichiers (`b85777c1e3e7` → `2df0b80d`)

| Fichier | Blob à la tête | patch-id |
|---|---|---|
| `.github/workflows/tests.yml` | `cabebe0032deb17befc065140cbdcd6f99b543a2` | `8356f937e3ccfc026b2949cfea9b7cf26e2f1841` |
| `.github/workflows/relance-hosted.yml` | `668b992b79395024964fb23a8fa22c47bf7a61ea` | `fa939cdbd0e830a2442ea91a9f57bd11344ffd4b` |
| `scripts/ci-relance-hosted.py` | `b6deec3246d5665f837d81add7c18d0d426c54db` | `9a903e61c264451265abb229cc39676ac842ee53` |
| `tests/test_987_relance_hosted.py` | `9492791fb9a48822c2fb2288d06eaf556dab8eaa` | `6df400d8101da51f655755310cb5787fcf57643e` |

### #1002 — 3 fichiers (`2df0b80d` → `7aaa5650`)

| Fichier | Blob à la tête | patch-id |
|---|---|---|
| `.github/workflows/relance-hosted.yml` | `9463b99f39298dc941df57a97f62c32104dffd3e` | `c23d6799eb403c3464dbed086816d582f4e2bea0` |
| `scripts/ci-relance-hosted.py` | `ae8a83ee05ff5966785ab4c75632b57757b07254` | `a5f7f706ce24ee008cccf6102d6584e017649075` |
| `tests/test_1002_relance_perimee.py` | `03060cf30f8b9413dce7fb28cdf05f0c9520c67c` | `aa016bd90865fbcc86fe67f6396d82e0d5e0593d` |

### #974 — 2 fichiers (`90aae667` → `7e65733c`), docs seules

| Fichier | Blob à la tête | patch-id |
|---|---|---|
| `docs/L232B-PRIVATE-BACKUPS.md` | `3868cd378a34f678a4f77f9e42d1c3e8d736e0d9` | `85046ee487181d9f0a18d971205f33347e4a5bf9` |
| `docs/L232B-RELEASE-CONTROLLER.md` | `a9ffa79b592ad216812c7d3bd51f72ee59f2f61d` | `5491edf4c5ee26b184856504ed1d2e4248911049` |

## 4. Unions provisoires sur main 7305957e (revérifiées en première main)

| étape | rc | arbre après l'étape | fichiers vs main |
|---|---|---|---|
| cœur : main + #987 | 0 | `37abd5a2538ef41558e004aebd0628e0f5d40f11` | 4 |
| cœur : puis + #364 | 0 | **`4b65a17719bb…` (cœur `#364+#987`)** | 77 |
| cœur + #974 (après #364) | 0 | `a00ae7b57c62…` | 77 |
| **hors cœur** : #987 + #1002 (sans #364) | 0 | `b44b0ef096b62b4420a8a84969b66c4097b57845` | 5 |
| simulation : #987+#1002 puis + #364 + #974 | 0 | **`581607ca974304da4af28d0ee46ece07147fc0c0`** | 78 |
| simulation, ordre inverse : #364, #974, #987, #1002 | 0 | **`581607ca…` (même arbre)** | 78 |

- **Composition commutative sur cette base** : les deux ordres testés (avec compagnons)
  donnent le même arbre final `581607ca…` ; aucun conflit à aucune étape. Le **cœur**,
  sans #974 ni #1002, reste `4b65a177…` (77 fichiers, annexe C).
- Delta union vs main = **exactement les fichiers des lots** (77 pour le cœur = 73 ∪ 4 ;
  78 avec #974 et #1002), **aucun fichier étranger**.
- **Fichiers adaptés** (blob d'union ≠ blob de la tête du lot, parce que main a modifié le
  même fichier depuis la base) : **9/73 pour #364** — `deploy/sql/role-executor.sql`,
  `deploy/sql/role-module-writers.sql`, `deploy/sql/role-superviseur.sql`,
  `src/ameesh/cli.py`, `src/ameesh/storage/postgres/registry.py`,
  `tests/parts/pilote-psql.txt`, `tests/support.py`, `tests/test_cluster.py`,
  `tests/test_l56_role_executor.py` ; **1/4 pour #987** (`.github/workflows/tests.yml`) ;
  **0/3 pour #1002**. Ces 10 fichiers sont à relire au gel (delta composé, §12).
- **Patch-ids** : #974 2/2, #987 4/4, #1002 3/3, #364 **70/73 en diff standard et 73/73
  en `-U0`** : les 3 écarts standard (`deploy/sql/role-module-writers.sql`,
  `tests/support.py`, `tests/test_cluster.py`) sont des **contextes** différents, les
  lignes changées sont identiques ; l'union garde les deux contributions (union → tête =
  exactement le delta de main sur ces fichiers).
- **Recouvrement avec #1211** (à refaire sur la main exacte après activation) :
  `.github/workflows/tests.yml` (#987/#1002) et `tests/parts/pilote-psql.txt` (#364).
- Objets d'union **locaux** (`git commit-tree`), non poussés ; aucune branche de lot
  modifiée (ni rebase, ni merge de main dedans).

## 4bis. Recoupement indépendant (ameesh-cx2, 14/10 06:15Z)

- Mêmes arbres, obtenus séparément : union #364 `64264ca6…` puis #987 `4b65a177…` ;
  ordre inverse #987 → #364 = **exactement `4b65a177`** ; delta 77 fichiers = 73 + 4,
  aucun étranger ni manquant.
- Blobs #364 constants **64/73** (les 9 adaptés du §4) ; `tests.yml` composé
  `00b374d7dbd3…`, identique aux annexes B et C.
- Patch-id `-U0` : **73/73** pour #364 (les 3 écarts standard sont des contextes) ;
  #987 4/4 dans les deux formes.
- **#974 et #1002 restent candidats, exclus par défaut** : inclusion seulement si
  qualifiés avant le gel, comme au §1.

## 5. Classes, avis, familles, feux

Identifiants **primaires** relevés en première main le 14/10 06:2xZ : jalons d'avis
(`work_item_milestones`), feux (`work_item_events`), binding de session
(`session_bindings`). Ces avis sont **delta/historiques** — chacun est borné au SHA
indiqué ; ils ne valent pas GO complet nouveau du futur paquet et restent à qualifier au
gel sur la main exacte (§7, point 3).

- **#364** — sensible prod + SQL (migration 0417, `deploy/sql/role-*.sql`, `deploy/**`,
  concurrence des fences). Auteur **mty-amh-impl-04** (harnais **DeepSeek**). Jalons :
  - **#2436** — auditeur, **Claude**, **blocked**, 13/10 14:37:26Z, sur `febbf0ad…` : R1
    **partiel** (volets contrôleur/SSH/santé) ; volets SQL et sauvegardes annoncés sur la
    même tête dans le même tour.
  - **#2638** — auditeur, **Codex**, **blocked**, 13/10 16:15:45Z, sur `5270b1e9…` :
    second tour R1, K2 (P2) restant ; rapport
    `docs/reviews/2026-10-13-364-r2-auditeur.md` @ `00f1cd5c3` (branche
    `origin/review/auditeur-364-r2`). Le rapport **réserve explicitement au pilote la
    qualification de famille des autres avis**, « notamment si l'auteur initial du lot
    est compté Codex ».
  - **#2647** — **mty-amh-qa-01**, **DeepSeek**, **blocked**, 13/10 16:21:57Z, sur
    `5270b1e9…` : R2 0 B / 1 P2 (K2).
  - **#2696** — **bureau**, **Claude**, **ok**, 13/10 16:45:24Z, sur `6db727ac…` : R2
    delta 0 B / 1 N (les inventaires antérieurs au changement, sans `source_bytes`, sont
    refusés à la restauration).
  - **#2713** — **ameesh-cx1**, **Codex**, **blocked**, 13/10 16:56:01Z, sur `6db727ac…` :
    R1 0 B / 1 P2 nouveau (capacité lue sans parents protégés ni `O_NOFOLLOW`/`fstat`).
  - **#2727** — **ameesh-cx1**, **Codex**, **ok**, 13/10 17:02:47Z, sur `90aae667…` :
    binôme ciblé, P2 levé, 0 B/N/n nouveaux (commentaire PR #693 `6150849386`).
  - **#2739** — **bureau**, **Claude**, **ok**, 13/10 17:08:39Z, sur `90aae667` : R2
    delta `6db727ac..90aae667` 0 B / 0 N.
  - **Ligne primaire des jalons #2696/#2739** (retrouvée en première main par
    **ameesh-cx2**, 14/10 07:21Z, sur l'hôte bureau) :
    `~/.claude/projects/-home-smichea-Work/11783f10-5492-408f-988b-8c1035bec79b.jsonl` —
    jalon `2696` : assistant `2900`, 13/10 16:45:22.571Z, modèle **`claude-opus-5-5`**,
    `toolu_011yBjm9aryaPzEcWRNv3KnF`, résultat `2908` 16:45:30.628Z `is_error:false` →
    `milestone364ok` ; jalon `2739` : assistant `2992`, 17:08:38.151Z, même modèle,
    `toolu_017qxw1pnhk6fA7T1uH7vyKR`, résultat `2994` 17:08:44.530Z `is_error:false` →
    `milestone364ok`. Binding `session_bindings` **#26** — session
    `11783f10-5492-408f-988b-8c1035bec79b`, harnais **claude**, agent **bureau**, hôte
    pc-smichea, liée 13/10 16:29:29Z, révoquée 13/10 20:39:44Z ; sa fenêtre couvre #2696
    (16:45Z) et #2739 (17:08Z). L'exécution réelle et le modèle sont donc **attestés par
    la ligne primaire**, sous la main de cx2 (hashes des quatre lignes et détail :
    `~/Work/ameesh-cx2-1260-recoupage.md`, hôte bureau, et note #1260 de cx2). Pièce
    **non revérifiable depuis `ameesh-agents-2`** (`/home/smichea` absent) ; auteur,
    indépendance et couverture finale du paquet restent à qualifier par le pilote au gel
    (§7, point 3).
  - **Feu historique** : `work_item_events` **#8263**, pilote mesh-design, 13/10
    17:17:39Z — FEU PILOTE conditionnel sur `90aae667…` : CI exacte verte sur cette tête,
    **toutes parts psql/psycopg COMPLETED/SUCCESS, aucune annulée** (#940), tête
    inchangée, fusion par l'auteur `--match-head-commit`. **Non satisfait** (§6) ; tête
    déplacée ou avis défavorable = feu caduc.
- **#974** — légère (2 docs). Auteur dev3. Aucun avis ni feu propre au relevé (seul jalon
  `requested` #2698, 13/10 16:46:13Z) ; PR empilée sur `lot/l232b`, à retarget sur main
  après #364. Inclusion seulement si qualifié avant gel.
- **#987** — sensible hors prod (CI). Auteur **DeepSeek** (impl-05). Jalon **#2824** —
  auditeur, **Codex**, **ok**, 13/10 18:05:03Z, sur `2df0b80d` : R1 GO 0 B / 1 N (P3 →
  #1002) ; image macOS standard, filtre des check runs confirmé, aucun chemin de boucle,
  **porte de fusion inchangée**. Feu `work_item_events` **#8610**, mesh-design 13/10
  18:13:38Z, conditionnel (CI exacte verte, toutes parts exécutées, aucune annulée, tête
  inchangée). **Non satisfait** (§6) ; le levier macOS est prouvé en conditions réelles
  (job `plateforme macOS` SUCCESS sur runner hébergé `1000025835`, run 38486341746).
- **#1002** — sensible hors prod (CI). Jalon **#2873** — auditeur, **Codex**, **ok**,
  13/10 18:31:01Z, sur `7aaa5650` : R1 delta `2df0b80d..7aaa5650`, 0 B / 0 N nouveau. Feu
  `work_item_events` **#8744**, mesh-design 13/10 18:33:46Z (20:33 Paris), subordonné à
  #987 : (1) #763 fusionnée d'abord, (2) CI exacte verte, (3) tête inchangée. Champ
  `waiting` du lot (« verdict by auditeur ») à réconcilier.
- **#1257** — cause racine carte (couverture sqlite corrompue), assignée impl-06 ; suivi
  distinct, inclusion seulement si contenu final et avis qualifiés (mandat #1260).

## 6. Anciens runs — tous terminaux, aucun vert applicable

| lot | run | conclusion | fait |
|---|---|---|---|
| #364 | 38435051980 | failure | psycopg 4/4 rouge (flake test_653, corrigé par #925) ; psql annulé au plafond 30 min (#880) |
| #364 | 38435052029 | cancelled | mesures vertes, mesure psql annulée |
| #364 | 38485229711 | cancelled 01:13Z | purge de flotte ; psql 1/3, psycopg 2/4 et 3/4 verts ; 4 parts annulées |
| #364 | 38485229713 | failure | seul le job carte rouge (couverture sqlite corrompue, infra, #1257) |
| #987 | 38443445528 | cancelled | flake #925 (depuis dans main via #1112) |
| #987 | 38486341746 | cancelled 01:18Z | macOS SUCCESS réel ; 5 parts PostgreSQL annulées sans runner ; agrégats purgés |
| #1002 | 38446988755 | cancelled | même flake #925 ; aucun verdict |

Aucun de ces runs n'est transférable au paquet ; aucune relance, aucun close/reopen, aucun
réjeu individuel.

## 7. Conditions de gel (canon §12)

1. **#1211 active** (PR #923 fusionnée) et ses deux conflits avec #833 résolus.
2. **main fraîche** : refaire les unions ; revérifier les patch-ids du **cœur** (73 pour
   #364 + 4 pour #987) ; n'ajouter 3 (#1002) + 2 (#974) que si les compagnons sont
   qualifiés et inclus ; relire les **10 fichiers adaptés** du §4 et les
   **2 recouvrements #1211** (`tests.yml`, `tests/parts/pilote-psql.txt`) ; tout écart =
   arrêt et signalement.
3. **Qualification pilote** : classes réelles, familles/sources des avis (y compris la
   réserve de famille du rapport auditeur `00f1cd5c` et la ligne primaire du jalon #2739,
   retrouvée par cx2, §5), dépendances, inclusion ou exclusion motivée des compagnons
   (#974, #1002) ; #1257 à part.
4. **Nouveau gel** : branche d'intégration distincte de `39df7c316c9e` + manifeste final
   (cette pièce en entrée).
5. **Une seule validation complète** `workflow_dispatch` sur le gel : 4 parts psycopg +
   6 parts psql + macOS + agrégats requis ; toutes les parts attendues exécutées, aucune
   annulée ni sautée indue. Aucun ancien run transféré.
6. **Feu fusion/staging distinct** du pilote, après qualification.

## 8. Points ouverts

- #1002 : `waiting` à réconcilier (R1 déjà GO).
- #974 : avis/feu propres non rendus ; PR à retarget après #364.
- Jalon #2739 : binding `#26` = harnais `claude` (bureau) enregistré, et **ligne primaire
  retrouvée par cx2** (14/10 07:21Z) : session `11783f10…`, modèle `claude-opus-5-5`,
  résultats `is_error:false` pour #2696 et #2739 (§5) — l'exécution réelle est attestée
  sous la main de cx2, **non revérifiable depuis `ameesh-agents-2`**. Reste au pilote :
  qualification auteur/indépendance/couverture finale du paquet (§7, point 3).
- Les 3 fichiers #364 non patch-constants sont à relire dans le delta composé (§4).

## 9. Réversibilité

- Aucune poussée sur `lot/l232b`, `fix/974-inventaire-source-bytes`,
  `lot/987-macos-hosted`, `lot/1002-relance-hosted-perime`, ni sur une branche gelée ;
  aucun close/reopen ; aucun run CI ; aucune fusion de main dans une branche de lot.
- Cette branche ne porte que ce document ; suppression :
  `git push origin --delete prep/paquet-364-987`.
- Les objets d'union sont locaux (`git commit-tree`), non poussés.

## Annexe A — reproduction

```sh
git fetch origin main lot/l232b fix/974-inventaire-source-bytes lot/987-macos-hosted lot/1002-relance-hosted-perime
M=origin/main
B364=90aae6672b5d0dd624a503bdeb8943a7caaf7623 ; B974=7e65733c3fe46d5af4db144e6f54552d689c049c
B987=2df0b80def30ea5f19a2ae2468038698aea1d39a ; B1002=7aaa5650934f6f3e62a16b4803e4453677421acf
U987=$(git merge-tree --write-tree $M $B987 | head -1) ; C987=$(git commit-tree $U987 -p $M -p $B987 -m u987)
U=$(git merge-tree --write-tree $C987 $B364 | head -1)  # coeur 4b65a177
C=$(git commit-tree $U -p $C987 -p $B364 -m u364)
U2=$(git merge-tree --write-tree $C $B974 | head -1)   # +974 -> a00ae7b5
git diff --name-status $M $U2                          # exactement 77 fichiers
# extension #1002 : meme arbre final 581607ca dans les deux ordres
git diff b132d5d4397efdd7ba05e7682bdbf68eab61703e $B364 -- <f> | git patch-id --stable
git diff $M $U -- <f> | git patch-id --stable          # 70/73 identiques
git diff -U0 $M $U -- <f> | git patch-id --stable      # 73/73 identiques (contextes ignores)
```

## Annexe B — simulation avec compagnons #974 + #1002 (arbre `581607ca…`, 78 fichiers)

**Cette annexe n'est pas le paquet final** : elle simule l'inclusion des deux compagnons
#974 et #1002, **exclus par défaut** (§1). Le cœur admissible en préparation est l'arbre
`4b65a177…` (77 fichiers) ; les 4 blobs qui diffèrent de cette simulation, et l'absence
du test #1002, sont en **annexe C**.

| Fichier | Blob |
|---|---|
| `.github/workflows/relance-hosted.yml` | `9463b99f39298dc941df57a97f62c32104dffd3e` |
| `.github/workflows/tests.yml` | `00b374d7dbd3d7b9d2c62e78a06f4f21e4fecc24` |
| `deploy/mise-a-jour/adaptateur-hote-release.py` | `4fae7c30f0e9f06d44803e22377259443236be90` |
| `deploy/mise-a-jour/autorite-release.py` | `eab7053d912a730bb655dd170675ff88449a0196` |
| `deploy/mise-a-jour/bootstrap-release.py` | `bc24d0c2484c6eae866cad344a69e6e90fbca0ea` |
| `deploy/mise-a-jour/compte-release.py` | `8448bb6e3fc1bae35fd45e6cda2f5fb1d4a017a3` |
| `deploy/mise-a-jour/configuration-release.py` | `57d20d6f88c4fb3084820b41bfbcd543e192f12e` |
| `deploy/mise-a-jour/controleur-release.py` | `92a1e23b19fb58edb02eb7fe114ca83c65bf07e4` |
| `deploy/mise-a-jour/delegation-release.py` | `f6d0493abae8e27e988e654c71567b5e20f1158e` |
| `deploy/mise-a-jour/environnement-versionne.py` | `fdb6e72c6afdae7d8dffd1c1418c364ee3678afe` |
| `deploy/mise-a-jour/fence-release.py` | `bd648f1ceb025e37f826b5d2aec2919959c2938b` |
| `deploy/mise-a-jour/fichiers-bootstrap.py` | `9a623d467316c43006b59009f85afd857c6587e8` |
| `deploy/mise-a-jour/helper-redemarrage.py` | `d1d611ae77c0d85586a89737ffbf1204497833d8` |
| `deploy/mise-a-jour/hote-release.py` | `cdca153a8b3f557d2c83571f3e42eb31e80ab550` |
| `deploy/mise-a-jour/installateur-bootstrap.py` | `109ab24691f8cf180ae4d6f8b99456353ecb9907` |
| `deploy/mise-a-jour/pilote-sante.py` | `41acd8deda9608f4878c2dd5ba28cca6627264d5` |
| `deploy/mise-a-jour/poste.sh` | `77aaab1747d85596cf8c4f6febe0724f38b60a2f` |
| `deploy/mise-a-jour/retention-privee.py` | `4fedd5fa2c20b175fcac6a1049683f9f7907f9a2` |
| `deploy/mise-a-jour/retention-scaleway.py` | `15e791dc559480fe76b31668ed6679aa7007121c` |
| `deploy/mise-a-jour/runtime-release.py` | `a210a7302f4b40fd78e3e19a96d1e1aa218269a9` |
| `deploy/mise-a-jour/sante-release.py` | `1fa9cb186f45c4a458072d063dce7105d46fbcf5` |
| `deploy/mise-a-jour/sauvegarde-release.py` | `9ca7b360b64564bb2edabeac212e3ee2d9acdec4` |
| `deploy/mise-a-jour/sauvegarder-prive.py` | `11fb8bf5dc1cb6114813a98b9cca78ca54cd3496` |
| `deploy/mise-a-jour/service-release.py` | `a7c42f6029d06f8adc9705aed188e207bf4e549b` |
| `deploy/mise-a-jour/transport-release.py` | `b92964a03daecd0f2711612a936a36e74c7ae510` |
| `deploy/mise-a-jour/unite-controleur.py` | `fe6e2f08e52dd0cca0d32c740821f40039ce7aa8` |
| `deploy/mise-a-jour/verification-release.py` | `6680a29f19a7bce11274063c59d65eddd188d3df` |
| `deploy/serveur/README.md` | `6b8941bc30e728b1b9b9ba55e63f44cc6663e1ef` |
| `deploy/serveur/installer-serveur.sh` | `b08941ee93cfb6e8fba3bf38ba28defff8ebcd98` |
| `deploy/serveur/sauvegarder-base.sh` | `d28a1a0f741175f128ce097c0589ef85d8cc0b50` |
| `deploy/sql/role-executor.sql` | `a309ffd37c8159a9fa756b4b24f257d57bd1a701` |
| `deploy/sql/role-module-writers.sql` | `8294030c5d5ad5613c99cb17db91565b12ecde99` |
| `deploy/sql/role-release-controller.sql` | `c132504b52eb6502eb2a7a4946973d5a556cd9e9` |
| `deploy/sql/role-superviseur.sql` | `d010d12bff168c9bf1cc31e60c9b657337c934d5` |
| `docs/BASCULE.md` | `62fb073525be23bcbc7b05e038e6baf42c736a1f` |
| `docs/L232B-PRIVATE-BACKUPS.md` | `3868cd378a34f678a4f77f9e42d1c3e8d736e0d9` |
| `docs/L232B-RELEASE-CONTROLLER.md` | `a9ffa79b592ad216812c7d3bd51f72ee59f2f61d` |
| `docs/L232B-VERSIONED-ENVIRONMENTS.md` | `9b34146e9d23c8e00efa7c318765f7b74bb91fc1` |
| `scripts/ci-relance-hosted.py` | `ae8a83ee05ff5966785ab4c75632b57757b07254` |
| `src/ameesh/cli.py` | `8fa6a7b9f2c5f980d693ca4729155d0ef0b7131c` |
| `src/ameesh/db.py` | `ecb56bd3599b7b817f87919ec21a18d16de7b3eb` |
| `src/ameesh/migrations.py` | `3c6393007a161d725d420bbc1db1efbba6af1dd2` |
| `src/ameesh/migrations/0417_host_release_fences.sql` | `4a04180c60d08c03a350f3dbb9ff52edd22a5ec6` |
| `src/ameesh/storage/postgres/registry.py` | `719714745811bd6ae1636bfbf0bfe0eb580ad201` |
| `src/ameesh/storage/postgres/release_fences.py` | `62764c2982867bd915db04c7d88e6669512f21b0` |
| `tests/parts/pilote-psql.txt` | `bd2f421a75d5002d2edc482b441eb62ea7e2fb43` |
| `tests/support.py` | `a240ddd947e35925de5dbf19f66288fea8fa30eb` |
| `tests/test_1002_relance_perimee.py` | `03060cf30f8b9413dce7fb28cdf05f0c9520c67c` |
| `tests/test_518_dsn_argv.py` | `1dd75282c92c1fae4bf020aca315e614addab0cc` |
| `tests/test_987_relance_hosted.py` | `9492791fb9a48822c2fb2288d06eaf556dab8eaa` |
| `tests/test_cluster.py` | `fec5af4e77c8a63b3bd76e4674f05a0f7dbeb375` |
| `tests/test_l232b_account.py` | `3874468789592fb5b1fc9d4b6d66b41b4215fe12` |
| `tests/test_l232b_backup_restore.py` | `1eb6ce3e2d28df561ea0f7b9101fdb35a057a5c4` |
| `tests/test_l232b_bootstrap.py` | `439b50909921aefa93fa8a8625e15091a4d0fe82` |
| `tests/test_l232b_bootstrap_files.py` | `b17d690ee3a4811c73134f5ef754790f5fe2eb2b` |
| `tests/test_l232b_bootstrap_installer.py` | `c177a0c5a67f22a4ab0a5b2e1f22e66439a0e50b` |
| `tests/test_l232b_delegation.py` | `af0dc3d963ecb610a37087f4731c43c97ba8204c` |
| `tests/test_l232b_health_turn.py` | `f667eae319d98fc36e370cc2a37d2213b15e8835` |
| `tests/test_l232b_host_factory.py` | `7e6edfab198e526f7a3db34e4a023e6cd25b2666` |
| `tests/test_l232b_migration_worker.py` | `576608f156c6373f666bf054b4fe0c8a533acb5f` |
| `tests/test_l232b_private_backup.py` | `732fe618398b4aceedad8f847350c3844ecb0aea` |
| `tests/test_l232b_release_authority.py` | `6c9a2fb896836ba004041612590fced5f48f26f1` |
| `tests/test_l232b_release_configuration.py` | `e3b13af590178e5c97991b4354f6c7be96ff7daa` |
| `tests/test_l232b_release_controller.py` | `778fcf25c4b7714710fd9b93788ffabb3b530e80` |
| `tests/test_l232b_release_fence.py` | `4c7a4343000e095a8998a6e90f464643c2be3e66` |
| `tests/test_l232b_release_health.py` | `f7568fbf27f9187ee238203d0dc29b40d210190c` |
| `tests/test_l232b_release_recovery.py` | `23caadb2a88aefeab5ad0a26a95680d37188a606` |
| `tests/test_l232b_release_scripts.py` | `1436ae7643e6459425e90b6010474144b91dfb1c` |
| `tests/test_l232b_release_service.py` | `0c06f9439a2ac13a8b754eec967acd0091821135` |
| `tests/test_l232b_release_verification.py` | `d568c050f8efb5a2f7df143a784de8cbea1ef87c` |
| `tests/test_l232b_restart_helper.py` | `d741d94eb3eb6e359ccb689ad96d000c59601fd2` |
| `tests/test_l232b_retention.py` | `5a1025f10ca2b31f26ce5802879923a847866421` |
| `tests/test_l232b_retention_provider.py` | `df99543ab07c0f57090e6b156fedf1a04edd3a2c` |
| `tests/test_l232b_runtime.py` | `a0b7541f797c345eb57a82ec90bcad16882b4a06` |
| `tests/test_l232b_scaleway_retention.py` | `5fd7ba99c063720bce077a9a38578e11cd7ae8a9` |
| `tests/test_l232b_ssh_transport.py` | `d92ddaa00313dfb7b4c54844d28b3b3c27c60c36` |
| `tests/test_l232b_versioned_environment.py` | `34c7e7fa10a7bc43b8a24316b38af5cc1691d693` |
| `tests/test_l56_role_executor.py` | `10db514ce956c98d5134279a82e15fd880ea004c` |

## Annexe C — cœur admissible en préparation (arbre `4b65a177…`, 77 fichiers)

Cœur = main `7305957e` + #987 + #364, **sans #974 ni #1002**. 73 des 77 blobs sont
identiques à l'annexe B ; seuls les 4 fichiers suivants diffèrent, et
`tests/test_1002_relance_perimee.py` (présent dans la simulation) est absent du cœur.
Union refaite et blobs comparés en première main le 14/10 ; recoupement indépendant
ameesh-cx2 (mail 06:18Z) : mêmes 4 blobs, `tests.yml` inchangé.

| Fichier | Blob cœur (`4b65a177`) | Blob simulation (`581607ca`) |
|---|---|---|
| `.github/workflows/relance-hosted.yml` | `668b992b79395024964fb23a8fa22c47bf7a61ea` (version #987) | `9463b99f39298dc941df57a97f62c32104dffd3e` (version #1002) |
| `scripts/ci-relance-hosted.py` | `b6deec3246d5665f837d81add7c18d0d426c54db` (version #987) | `ae8a83ee05ff5966785ab4c75632b57757b07254` (version #1002) |
| `docs/L232B-PRIVATE-BACKUPS.md` | `3ac6c3ab00f7cde8d9ba5e391d333688e63f6780` (version #364) | `3868cd378a34f678a4f77f9e42d1c3e8d736e0d9` (version #974) |
| `docs/L232B-RELEASE-CONTROLLER.md` | `36d9b2a028a4754e059c5f0edd58b5d36a50d924` (version #364) | `a9ffa79b592ad216812c7d3bd51f72ee59f2f61d` (version #974) |
| `tests/test_1002_relance_perimee.py` | absent | `03060cf30f8b9413dce7fb28cdf05f0c9520c67c` |

Les 73 autres blobs du cœur sont ceux de l'annexe B (dont `tests.yml` composé
`00b374d7dbd3…`, identique dans les deux arbres). Rapport détaillé du recoupement cx2 :
`/home/smichea/Work/ameesh-cx2-1260-recoupage.md`.
