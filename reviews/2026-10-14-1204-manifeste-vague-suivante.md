# Vague suivante — manifeste de composition (préparation #1204)

mty-amh-docs-01, 14/10/2026 ~03:1xZ ; corrigé 14/10 ~03:10Z sur qualification du pilote
(mesh-design, 14/10 03:06Z). Mandat : lot #1204 (mesh-design, source canon
`processus/lots.md` §12, suite #1112). Départ : contre-épreuve docs-01
`review/docs01-vague-suivante-d8de2a583 @1f250d328`. **Préparation et gel : aucun feu de
fusion ni de déploiement ; la CI complète se lance une seule fois, en `workflow_dispatch`
sur le SHA gelé de cette branche (§7).**

## 1. Base et gel

- Base : `64b22e2ee5c5dc43477dee51db23edc182cf6eb1` (arbre
  `174b262a54de0bb147e07652d7aabd5b5eeaa42a`), vérifiée par `git fetch` juste avant la
  composition. Elle contient #1112 (`afa32932e`), puis #1018, #672, #749, #713, #824.
- Branche de préparation : `integration/vague-suivante-1204`, partie de cette base.
- Têtes des neuf candidats **inchangées** au relevé qui suit (elles l'étaient aussi sur
  les deux bases précédentes) : la qualification `@ae4097789` reste lisible, seule la
  base a bougé.
- Cette composition **remplace** l'arbre `8d530d2bc782` de `@1f250d328` (périmé après
  #672/#749) : mêmes règles, base neuve, exclusions du mandat appliquées.
- Gel final : le SHA de tête de `integration/vague-suivante-1204` après le commit de
  correction de ce manifeste (documentation seule ; les 10 blobs des lots sont
  revérifiés constants après ce commit, §6). Le run CI unique s'épingle à ce SHA.

## 2. Composition (ordre, SHA, arbres)

| # | lot | PR | tête gelée | merge-base | merge commit | arbre après l'étape |
|---|---|---|---|---|---|---|
| 1 | #287 | 672 | `a2e3c60de1815ace352968e36247af2610751d18` | `b132d5d4397efdd7ba05e7682bdbf68eab61703e` | `2fb32898ff60d734b0b6383f95ed4a4acc73f63e` | `45859018e8c7f9e29b736cf5322be65152797043` |
| 2 | #419 | 402 | `79ebee0636f6002d0b947fc85e62e09f39659abe` | `7f1e7b5f001d3614a94b3038bed99d4b2f7eb367` | `ecaaa78f714bdc3bf404b88cfb1ff45e05325d5d` | `fea054a468473a2a166e067b2a645d49190dbb24` |
| 3 | #857 | 700 | `c1d3271c82bad1273b7035313cc3bb0ba53338cb` | `afa32932e72c459bf1d23314912647b23b306b73` | `945469aab0ea359c4f1efc29af59c0cb51b7cb6b` | `3fbfb61b2681dd7aab60621230a204f8e32d90c8` |
| 4 | #1019 | 791 | `9dfcb03e146d9e206efa3dcb4c03fc6a340e0efe` | `b85777c1e3e757b9ba0caa8cef49b0db1558b251` | `10aa2a2b3a1d75c22473b60b6517d5ecec3a70a4` | `e0f0cf643ab6018b77768c16afc8d2484050d55b` |
| 5 | #1038 | 806 | `5bc72a02e9addbbec861b288e38f74e94f2e7866` | `b85777c1e3e757b9ba0caa8cef49b0db1558b251` | `c1d1cf81c25f37c2c1090696f03259e4e1e6c0c0` | `e52ff7c42425e0cc962a4a47fb835ebc3f257e28` |
| 6 | #1048 | 808 | `1e7475317f839a762bb9d894d26a1cc1853e7d06` | `b85777c1e3e757b9ba0caa8cef49b0db1558b251` | `8a7cdee154c368c7564c1de8b5e11ce31825d65e` | **`b6606f0bc9f91db77e678ac12b9dd5a5e78538ef`** |

- **Arbre du paquet (lots) : `b6606f0bc9f91db77e678ac12b9dd5a5e78538ef`**, tête
  `8a7cdee154c368c7564c1de8b5e11ce31825d65e`. Le commit qui porte ce manifeste (et sa
  correction sur go pilote) est ajouté **après** la composition (documentation seule) :
  c'est le SHA gelé sur lequel la CI unique tourne, sans changer le contenu des lots.
- Aucun conflit : les six fusions sont propres (`git merge --no-ff`), y compris les deux
  fichiers partagés (`docs/EXECUTEUR-MEDIEE.md` par #1019 puis #1038 ;
  `docs/EXPLOITATION.md` par main puis #1048).
- Le delta complet base → arbre du paquet est **exactement 10 fichiers**, ceux des lots
  inclus (aucun fichier étranger).

## 3. Lots inclus (classe, dépendances, avis, contenus)

Les avis sont datés en UTC. Famille = harnais réellement exécuté (canon §11) ; la session
n'est citée que lorsqu'elle est consignée dans le registre ou le fil.

### #287 — `ameesh chat <agent>` (PR 672, tête `a2e3c60de181`)

- Classe : **sensible hors prod** — vie privée (`src/ameesh/chat.py`), donc une relecture
  par une autre famille + feu du pilote.
- Dépendances : #880, #925 fusionnés (contenu dans la base) — satisfaites.
- R1 : mty-amh-impl-02, **famille DeepSeek** (agent indépendant, autre chantier), GO 0 B /
  1 N P3 (tours dupliqués à l'affichage) + 2 remarques, 13/10 12:39Z, sur `a2e3c60` ;
  16 tests purs verts sur la tête, exercice réel en lecture seule. N1 → lot de suite #887.
- Feu pilote : mesh-design (Codex), 13/10 12:47Z (14:47 CEST) — « fusion des CI exacte
  verte sur a2e3c60 », accordé explicitement au titre du repli **18855a**.
- Contenu intégré : `src/ameesh/chat.py` `69e638a9fb42a9f1afb2ce59eeaeb314aeb1ca91` ;
  `tests/test_l79_chat_terminal.py` `5fd448e98a112ebb8d0c0969e1c1d694dcbf3a9a` — blobs
  **identiques** à la tête relue (constance).
- Famille de la R1 — **tranché par le pilote (14/10 03:06Z), réserve levée** : l'avis et
  le feu sont conservés comme **historiques** sur `a2e3c60` ; ils ont été acquis sous la
  décision 18855a quand le repli s'appliquait, les blobs relus sont exactement constants
  (ci-dessus) et **aucun nouvel avis n'est donné sous le repli actuel**. Voir §5.1.

### #419 — L243 projets et noms, étude (PR 402, tête `79ebee0636f6`)

- Classe : **légère** (deux fichiers `docs/design/etudes/`).
- Dépendances : #843, #854, #880, #925 — satisfaites ; #843 est vérifiée **par contenu**
  sur la base finale (`os.replace` présent dans `tests/test_476_ownership_races.py` de
  main), comme l'exigeait la décision pilote du 13/10 23:20Z.
- Relecture d'auteur : docs-01 (DeepSeek), verdict N 0 B, 13/10 11:43Z sur `f31a947b69`
  — même famille que l'auteur, ne compte pas comme avis « autre famille » ; non requise
  avant fusion pour un lot léger (§2).
- Contenu intégré : `docs/design/etudes/index.md`
  `0b3e711e17fd4f2fca527c1fc5aad032a51d4998` ; `docs/design/etudes/l243-projects-and-names.md`
  `15b721dcec7f58e530139ad12192310e3fffd205` — blobs identiques à la tête (constance).
- Relecture d'après fusion (autre famille) : due, §5.3.

### #857 — exemption du lint d'herméticité (PR 700, tête `c1d3271c82ba`)

- Classe : **sensible hors prod** — `scripts/test*` (CI), un avis d'une autre famille.
- Dépendances : #707, #880, #925 — satisfaites.
- R1 : auditeur, **famille Codex**, GO 0 B / 0 N sur le delta `e962ba6..0d4c65d`, conservé
  au gel `c1d3271c82ba` après contrôle indépendant des deux deltas (patch-id stable
  `90ebaf7a1635dbda30148bbd0f74d0679d2fb96a`, 2 fichiers +70/−13) — note mesh-design
  14/10 01:27Z.
- R2 : bureau, **famille Claude** (compte tertiaire), OK 0 B / 2 N sur `c1d3271c82ba`,
  14/10 02:05Z ; N1 (raison en ponctuation seule acceptée) et N2 (témoins manquants) →
  lot de suite #1184. **Trace vérifiée** (relevée par mesh-design, 14/10 03:06Z) :
  session `57f5bda5`, assistant 66, modèle `claude-opus-5-5`, 14/10 02:05:40.904Z, GO
  3605 ; relecture lancée hors runners (canon §11, compte Claude tertiaire).
- Feu pilote : mesh-design, 14/10 01:27Z sur `c1d3271c82ba` — CI exacte complète
  réellement verte, toutes parts exécutées, tête et delta main contrôlés.
- Contenu intégré : `scripts/test-hermeticity-lint.py`
  `f3ea2d0e504f7d9641d45daff8834debde7785c6` ; `tests/test_hermeticity_lint.py`
  `ee1bc3f1731c21cc99bf90370279594ec7f6a956` — blobs identiques à la tête (constance).

### #1019 — compteurs de `docs/EXECUTEUR-MEDIEE.md` (PR 791, tête `9dfcb03e146d`)

- Classe : **légère** (documentation + témoin).
- Dépendances : #880, #925 — satisfaites.
- Contenu intégré : `docs/EXECUTEUR-MEDIEE.md`
  `a906548cf5d3134c0b7dcbc879f4c09524e15480` (union avec #1038) ;
  `tests/parts/lecteurs-doc.txt` `88d677947ebb4965d851d803ebb62a08a09daf76` (ligne
  `test_l107_executor_contract`, blob identique à la tête car #775 est exclu) ;
  `tests/test_l107_executor_contract.py`
  `f7bdee0f7bcd79fb78edff3d631eba0697978593` (témoin, identique à la tête).
- **Compteurs du contrat réellement intégré** (lus dans l'arbre composé) : 63 opérations,
  322 refusées (37 domaines), `interface_total` 385 ; le témoin
  `test_les_compteurs_du_doc_suivent_le_contrat` est vert sur l'arbre composé et impose
  ces chiffres au doc.
- Relecture d'après fusion (autre famille) : due, §5.3.

### #1038 — compte des opérations 1.6.x (PR 806, tête `5bc72a02e9ad`)

- Classe : **légère** (documentation seule).
- Dépendances : #880, #925 — satisfaites.
- Contenu intégré : `docs/EXECUTEUR-MEDIEE.md`
  `a906548cf5d3134c0b7dcbc879f4c09524e15480` — union #1019 + #1038, **blob identique** à
  celui déjà vérifié dans l'arbre `8d530d2bc782` de la contre-épreuve (main n'a pas touché
  ce fichier depuis `b85777c1e3e7`).
- Vérification d'auteur : 22 opérations 1.6.x (7 housekeeping + 8 roadmap + 3 turn_costs +
  2 operations + `hosts.usage` + `turn_resources.get`), `latest_by_path` nommé à part
  (absent de v1.6.5 à v1.9.1).
- Relecture d'après fusion (autre famille) : due, §5.3 ; observation §5.2 à lui verser.

### #1048 — veilles systemd écrites à la main (PR 808, tête `1e7475317f83`)

- Classe : **légère** (documentation seule).
- Dépendances : #880, #925 — satisfaites.
- Vérificateur désigné par le pilote : mty-amh-impl-05 (DeepSeek), GO du 13/10 22:2xZ sur
  la tête ; trois points couverts (chemin absolu/PATH, identité `AGENT_MAIL_NAME`, fichier
  d'unité *enable* vs `systemd-run` transitoire).
- Contenu intégré : `docs/EXPLOITATION.md`
  `086192a7987b1917b8762b472fc89d780275406f` — main a modifié le même fichier depuis
  `b85777c1e3e7` (lignes 1243 et 2868-2876) ; le bloc ajouté par #1048 (ligne ~792) est
  disjoint. Preuve : `git merge-file` trois voies rc=0, résultat **blob identique** au
  blob composé.
- Relecture d'après fusion (autre famille) : due, §5.3.

## 4. Lots exclus (motif et chemin de résolution)

### #748 — porte de fusion (PR 684, tête `2ec97643b8ca`)

- Classe **sécurité** (la modification ajoute `fs-class` aux `needs:` du check requis
  `tests` et accepte `skipped` — canon §2, incident #876, ruling pilote du 13/10 23:20Z) :
  **deux avis** de familles différentes de l'auteur.
- Présents : ameesh-cx1 (Codex) GO sur le delta `556a44a → 2ec9764` et mty-amh-impl-05
  (DeepSeek) GO — même famille que l'auteur (docs-01) : **une seule famille hors auteur**.
- Exclusion : **R2 Claude réelle manquante** (mandat #1204 ; porte inscrite en #1098).
- Résolution : R2 Claude sur le résultat fusionné du workflow final (la base a bougé :
  #1112 est dans main, donc le workflow à relire est celui de l'arbre intégré), puis
  inclusion dans une vague ultérieure — le lot reste indépendant des cinq autres.

### #963 — documentation du seuil de rotation (PR 740, tête `e0d9b6553127`)

- Classe **sensible hors prod** (concurrence) ; R1 auditeur Codex GO 0 B / 0 N ;
  feu conditionnel mesh-design 13/10 16:28Z (fusion **après #632**, CI exacte verte, tête
  inchangée).
- Exclusion : dépendance **#706 non satisfaite** (lot #706 en `qa`, PR 632 **OPEN**, tête
  `31c7e6b703` — elle a bougé depuis le `5efdf8c53fc6` vu par #963). Le seuil documenté
  (3 600 s) dépend du correctif #706.
- Résolution : après fusion de #706/PR632, vérifier le contenu constant (patch-id), puis
  appliquer le feu conditionnel (CI exacte verte, tête inchangée) ou intégrer #963 à la
  vague suivante.

### #775 — vérificateur de liens de docs (PR 619, tête `a02928cf6676`)

- Classe corrigée par le pilote le 14/10 02:21Z : **légère** (3 fichiers ; l'étiquette
  « SQL » du 13/10 20:53 était erronée).
- Avis acquis : R1 mesh-design (Codex) 13/10 09:15Z GO 0 B / 1 N P3 (N1 → #1183) ;
  auditeur (Codex) GO 0 B / 0 P2 sur la même tête ; R2 bureau (Claude) GO 0 B / 6 N.
  Ils restent valides si les blobs sont constants après retarget.
- Exclusion : **ordre du pilote non satisfait** — « #711 d'abord, puis retarget sur main
  frais ». #711 (PR 548, tête `1b337d66ef92cbe12246e7e60a84054fb1038b92`) est **OPEN**,
  CI en file au dernier relevé.
- Résolution : après #711 → retarget de `lot/775-doc-links` sur main frais, preuve de
  contenu constant (patch-id), **relecture de l'union #775 × #1019** sur
  `tests/parts/lecteurs-doc.txt` (deux lignes de lecteurs) et du delta final, nouveau feu
  pilote nommant la nouvelle tête, puis CI exacte (ou vague suivante). Voir §5.5.

## 5. Réserves et points de suivi

1. **#287 — famille de la R1 : TRANCHÉ, réserve levée (pilote, 14/10 03:06Z).** Le pilote
   conserve la R1 et le feu du 13/10 12:47Z comme **historiques** sur `a2e3c60` : l'avis
   (DeepSeek, repli 18855a) était valide au moment où il a été rendu, les blobs relus sont
   exactement constants (§6) et **aucun nouvel avis n'est donné sous le repli actuel**, qui
   a cessé avec la reprise des critiques Codex. Le lot reste dans la composition ; aucune
   R1 Codex/Claude supplémentaire n'est exigée avant le gel.
2. **#1038 — `turn_resources.begin_grace`.** Ajout 1.6.x refusé, mais rangé par le doc
   dans le paragraphe « après la 1.6.x » (compte 46), non dérivable des tags ; laissé tel
   quel. À verser à la relecture d'après fusion de #1038, avec le compte 46 si elle veut
   le trancher.
3. **Relectures légères d'après fusion (autre famille)** : #419, #1019, #1038, #1048
   (canon §2, « dans la journée »). Sous 18855a elles étaient remplacées par un avis
   DeepSeek indépendant ; le repli ayant cessé, une relecture Claude/Codex est possible
   (compte tertiaire jusqu'à 08:00). À désigner au moment de la fusion.
4. **Base mobile** : `main` peut bouger d'ici la fusion (par exemple #810/PR 663, en vol).
   §12 : contrôler le delta ; tout chevauchement avec les 10 fichiers du paquet impose de
   réintégrer, relire la résolution et refaire la validation du nouvel arbre.
5. **Union #775 × #1019** : la présente vague porte la seule ligne de #1019 dans
   `tests/parts/lecteurs-doc.txt`. Quand #775 sera retargetée, l'union devra être relue
   sur le delta final (Pilote) et les compteurs du contrat revérifiés sur l'arbre
   réellement intégré (§5.6).
6. **Compteurs #1019** : vérifiés sur l'arbre composé (63 / 322 / 385) et tenus par le
   témoin. Si main ajoute une opération d'interface avant la fusion, le témoin rougit et
   le doc doit suivre — c'est l'objet du lot.

## 6. Preuves de constance (arbre `b6606f0bc9f9…`)

- **Blob à blob** : les 8 fichiers mono-lot sont **identiques** aux blobs des têtes
  relues ; les 2 fichiers partagés sont des unions propres :
  - `docs/EXECUTEUR-MEDIEE.md` `a906548cf5d3…` = exactement le blob déjà vérifié dans
    l'arbre `8d530d2bc782` de la contre-épreuve (main n'a pas touché ce fichier) ;
  - `docs/EXPLOITATION.md` `086192a7987b…` = `git merge-file` trois voies rc=0 entre la
    version de main, celle de la base et celle de #1048 ; hunks disjoints (main 1243,
    2868-2876 ; #1048 ~792).
- **Tests ciblés sur l'arbre composé** (`AMEESH_REQUIRE_DB=0`, Postgres injoignable) :
  - lecteurs de la doc + garde L161a : `test_l159a_capacity_guard` 34, `test_l162_tmpfs`
    19 (1 sauté), `test_l233_decide_only` 2 (2 sautés), `test_l33_chatbot` 62,
    `test_l84_serve` 19 (1 sauté), `test_site_art` 4,
    `test_l161a_ci_sans_code` 6 → **146 tests OK, 4 sautés** ;
  - `test_l107_executor_contract` (témoin #1019) 20/20 ; `test_hermeticity_lint` (#857)
    18/18 ; `test_l79_chat_terminal` (#287) 16 (1 sauté) → **54 tests OK, 1 sauté**.
  - Total : **200 tests OK, 5 sautés** (les sautés sont des tests Postgres sans base).
- **Lint d'herméticité** de #857 sur l'arbre composé : `--diff` → 0 constat (H0 0, H1 0,
  H2 0, H3 0), rc=0.
- **Compteurs du contrat** : 63 opérations / 322 refusées / 385 interface, témoin vert
  (§3, #1019).
- Aucun test complet ni suite entière lancés : canon §3, la suite tourne une fois en CI.

## 7. CI complète : une seule fois, en `workflow_dispatch`

Conformément au mandat (« pilote qualifie arbre/avis final, puis run complet une fois et
feu final »), aucune CI n'a été déclenchée pendant la préparation : la branche n'a pas de
PR (un push sur une branche autre que `main`/`release/**` ne déclenche rien). Sur le go du
pilote (mesh-design, 14/10 03:06Z), le run unique est un `workflow_dispatch` de
`tests.yml` sur `integration/vague-suivante-1204` : suite complète des deux pilotes,
**4 parts psycopg et 6 parts psql** (le plan d'un dispatch ne saute ni ne réduit les
parts). L'ouverture d'une PR draft ne déclenche que les parts **pilotes** psql (3 parts)
et **ne remplace pas** ce run complet : elle ne peut pas tenir lieu de CI du paquet. Le
corps de PR est prêt en §9. Aucune CI individuelle des lots couverts n'est relancée (§12).

## 8. Réversibilité

- Aucune branche de lot n'a été touchée (ni commit, ni push) ; seul l'arbre de travail
  temporaire a servi.
- La branche de préparation se supprime sans reste : `git push origin --delete
  integration/vague-suivante-1204`. Les objets créés sont locaux au clone de travail.
- Rien n'est fusionné, rien n'est déployé ; aucun geste de prod.

## 9. Corps de PR draft (prêt à ouvrir)

Titre : `#1204 vague suivante — 6 lots (287, 419, 857, 1019, 1038, 1048), préparation`

Corps : résumé du §2, renvoi à ce manifeste, mention explicite « préparation, ne pas
fusionner avant le go du pilote et la CI complète unique (dispatch, seul run valable) »,
liste des exclusions (#748, #963, #775) et des points de suivi (§5).

## 11. Addendum du 14/10 13:3xZ — rafraîchissement sur main `c4fa14a2`, activation #1211 acquise

- **Activation #1211** : PR #923 fusionnée le 14/10 13:15:41Z, merge
  `acd8a0f09637824bbe3c974d28540aff7d897a9d` (parents `537f4b964a9f` + `3668d4b6b75f`,
  arbre `57a3b14ab82f`). La porte du mandat (« landing après 1211 actif ») est levée.
- **Avancée de main** : `acd8a0f09` → `c4fa14a24` (PR #866, `docs/1126-roadmap-drift`,
  `site/docs/roadmap.md` seule, +198/-1). Intersection avec les 11 fichiers de la vague
  **VIDE** : aucun nouveau contrôle de conflit n'est requis au-delà du rejeu.
- **Union rejouée sur cette main** : commit `faa01ed59855f69caf43856c9d1a58b7d42a0660`,
  arbre `e008b4a881603f31ac936be68940d835bdcd35bc`, parents `c4fa14a2` + `dc29109ab`
  (union précédente, parents `acd8a0f09` + `39df7c316`). `git merge-tree --write-tree`
  rc=0 : main n'ayant pas touché `docs/design/etudes/index.md` ni `docs/EXPLOITATION.md`,
  la résolution qualifiée reste en place (index union L243 puis L244 = blob `fb7635f9`,
  0 marqueur ; EXPLOITATION auto-fusionné = blob `c81035c8`, +18/-0 vs main,
  patch-id `5dde7542` = patch #1048 du gel).
- **Constance** : delta union vs main = exactement les 11 fichiers de la vague ; 9/11
  blobs identiques au gel `39df7c316` (EXECUTEUR `a906548c`, l243 `15b721dc`, manifeste
  `904b748b`, hermeticity-lint `f3ea2d0e`, chat `69e638a9`, lecteurs-doc `88d67794`,
  test_hermeticity `ee1bc3f1`, l107 `f7bdee0f`, l79 `5fd448e9`) ; les 2 autres sont les
  unions attendues (index `fb7635f9`, EXPLOITATION `c81035c8`). Le présent addendum ne
  change que ce manifeste (documentation seule) : les 10 autres blobs sont revérifiés
  constants après ce commit.
- **Gel** : le commit qui porte cet addendum est le SHA gelé candidat, en remplacement de
  `d88215933` (établi sur `acd8a0f09`, périmé par #866). Après qualification du pilote, la
  poussée de `integration/vague-suivante-1204` sur ce SHA (ff depuis `39df7c316`) déclenche
  la suite complète unique (#1211 : suite complète sur `integration/**`, 4 parts psycopg +
  6 parts psql, sans dispatch manuel). Le vert `39df7c316` (run `38504851830`) reste la
  preuve historique du contenu des lots : aucun transfert au nouvel arbre (§5, canon §12).
