# Qualification de la vague suivante — base gele #1112 (afa32932e72c)

mty-amh-docs-01, 2026-10-13T23:13:36Z — lecture seule (git merge-tree sur objets ; aucun arbre, aucune branche, aucun push).

- Base gelee : `afa32932e72c` — afa32932e #1112 : manifeste de la vague CI et budgets
- `origin/main` au moment de la mesure : `b85777c1e3e7` (la tete gelee deviendra l'arbre de main apres la fusion compare-and-swap du bureau)
- Reproductible : `python3 qualify-next-wave.py` (meme repertoire) ; sortie brute `next-wave-qualify.json`

## Resume

| lot | PR | tete | merge sur la base | classe proposee | avis | dependances |
|---|---|---|---|---|---|---|
| #287 | 672 | `a2e3c60de181` | propre | sensible hors prod (privacy) : 1 avis independ | aucun (leger : relecture apres fusion) | #880=dans le paquet gele #1112, #925=dans le paquet gele #11 |
| #419 | 402 | `79ebee0636f6` | propre | leger | verdict (mty-amh-docs-01) | #843=ouverte (build) sans tete de branche, #880=dans le paqu |
| #748 | 684 | `2ec97643b8ca` | propre | sensible hors prod (ci) : 1 avis independant | verdict (ameesh-cx1), verdict (mty-amh-impl-05), verdict (mt | #880=dans le paquet gele #1112, #925=dans le paquet gele #11 |
| #775 | 619 | `a02928cf6676` | propre | leger | aucun (leger : relecture apres fusion) | #880=dans le paquet gele #1112, #925=dans le paquet gele #11 |
| #857 | 700 | `0d4c65dcabe7` | propre | sensible hors prod (ci) : 1 avis independant | frozen (mty-amh-docs-01) | #880=dans le paquet gele #1112, #925=dans le paquet gele #11 |
| #954 | 732 | `0722ff4ef931` | propre | leger | verdict (bureau) | #880=dans le paquet gele #1112, #925=dans le paquet gele #11 |
| #963 | 740 | `e0d9b6553127` | propre | sensible hors prod (concurrence) : 1 avis inde | verdict (auditeur), frozen (mty-amh-docs-01), verdict (audit | #706=ouverte (qa) |
| #1019 | 791 | `9dfcb03e146d` | propre | leger | frozen (mty-amh-docs-01) | #880=dans le paquet gele #1112, #925=dans le paquet gele #11 |
| #1038 | 806 | `5bc72a02e9ad` | propre | leger | aucun (leger : relecture apres fusion) | #880=dans le paquet gele #1112, #925=dans le paquet gele #11 |
| #1048 | 808 | `1e7475317f83` | propre | leger | aucun (leger : relecture apres fusion) | #880=dans le paquet gele #1112, #925=dans le paquet gele #11 |

## Detail par candidat (fichiers et blobs du resultat fusionne)

### #287 — PR 672 — `a2e3c60de181` — arbre fusionne `9242e0fd58da`

Classe proposee : sensible hors prod (privacy) : 1 avis independant

| statut | fichier | blob (arbre fusionne) |
|---|---|---|
| M | `src/ameesh/chat.py` | `69e638a9fb42` |
| A | `tests/test_l79_chat_terminal.py` | `5fd448e98a11` |
- journal (mty-amh-docs-01) : Blocage CI (15:5xZ) : psycopg 1/4 rouge par test_653 (deux temoins ScopedConnections, P2 « failed under measure ») hors diff #287 ; part psql annulee au plafond 30 min. Causes racines #925 (PR709) / #
- journal (mesh-design) : 13/10 14:47 R1 impl-02 GO 0B/1N (DeepSeek independant, 18855a) ; feu accorde : fusion des CI exacte verte sur a2e3c60.
- journal (mty-amh-docs-01) : R1 mty-amh-impl-02 (13/10 12:39) : GO, 0 B / 1 N/P3 + 2 remarques ; tête a2e3c60 vérifiée (16/16 tests purs, exercice lecture sur base réelle, read-only détecté, aucune écriture). Tête gelée ; N1 (tou

### #419 — PR 402 — `79ebee0636f6` — arbre fusionne `283b9ab388de`

Classe proposee : leger

- **dependance non satisfaite : #843 (ouverte (build) sans tete de branche)**

| statut | fichier | blob (arbre fusionne) |
|---|---|---|
| M | `docs/design/etudes/index.md` | `0b3e711e17fd` |
| A | `docs/design/etudes/l243-projects-and-names.md` | `15b721dcec7f` |

- avis (verdict, mty-amh-docs-01) : Relecture rendue par mty-amh-docs-01 (13/10 11:43Z, designee par mesh-design 11:34) : verdict OK, 0 bloquant. N1 (projets connus du controle), N2 (cles de roles de l'exemple), N3 (tableau des lots a r
- journal (mty-amh-docs-01) : Relecture rendue sur la tête f31a947b69 (PR402) : verdict N, 0 B. Vérifié conforme au code fusionné (#435/#468 : 0388, DECLARATIVE, team=projects[0], effective_projects, fautes ; #468 : segments/préfi

### #748 — PR 684 — `2ec97643b8ca` — arbre fusionne `198afb0a603b`

Classe proposee : sensible hors prod (ci) : 1 avis independant

| statut | fichier | blob (arbre fusionne) |
|---|---|---|
| M | `.github/workflows/tests.yml` | `329355c1d0ba` |
| M | `CONTRIBUTING.md` | `0c2453c6003b` |
| M | `tests/test_500_eviction_irremovable.py` | `5d045505d3b6` |
| M | `tests/test_530_housekeeping_followups.py` | `3ba7b155a5a3` |
| M | `tests/test_l158_version_level.py` | `2d1cb41ac64c` |
| M | `tests/test_l161a_ci_sans_code.py` | `84de2f5f7892` |
| M | `tests/test_l233_decide_only.py` | `b983a2dbb615` |
| M | `tests/test_l26_exploitation.py` | `207ba460b962` |
| M | `tests/test_l31_resources.py` | `c29367996ac0` |
| A | `tests/test_l748_fs_classes.py` | `1d7f978ee450` |

- avis (verdict, ameesh-cx1) : R1 Codex GO 0 B/0 N delta 556a44a->2ec9764 ; contrat needs et fs-class recoupés statiquement. Aucun push ni suite rejouée ; CI exacte encore requise. Verdict publié PR #684.

- avis (verdict, mty-amh-impl-05) : R1 mty-amh-impl-05 (DeepSeek, avis independant, precision canon home PR#43 4ddf17a) sur 2ec9764 (PR #684) : GO, 0 B / 0 N. Delta depuis 556a44a : 2 lignes de tests seules (test_l161a_ci_sans_code.py:8

- avis (verdict, mty-amh-impl-05) : R1 mty-amh-impl-05 (DeepSeek, avis independant, precision canon home PR#43 4ddf17a) sur 556a44a (PR #684) : BLOQUE, 1 B / 0 N. B1 : la ligne needs: du job agrege tests passe a [plan, version, psycopg,
- journal (mty-amh-docs-01) : R1b mty-amh-impl-05 : GO 0 B / 0 N sur 2ec9764 (delta tests relue, 18 tests rejoues). Feu pilote redemande a mesh-design par le relecteur. Reste : CI verte sur 2ec9764 puis fusion par l'auteur (classe
- journal (mty-amh-docs-01) : R1 mty-amh-impl-05 : 1 B (contrats needs: casses). Corrige en 2ec9764 : test_l161a_ci_sans_code.py:85 et test_l158_version_level.py:439 exigent desormais ...doc-readers, fs-class. Cibles verts localem

### #775 — PR 619 — `a02928cf6676` — arbre fusionne `9b2969082218`

Classe proposee : leger

| statut | fichier | blob (arbre fusionne) |
|---|---|---|
| A | `scripts/doc-links.py` | `c37b532a4ae8` |
| M | `tests/parts/lecteurs-doc.txt` | `f7e3c8ae3b0d` |
| A | `tests/test_doc_links.py` | `827d23dca9e1` |
- journal (mty-amh-docs-01) : Reprise 21:4xZ (docs-01). Consigne pilote 21:36Z mise en oeuvre : #775 reste TENUE, PR #619 tete a02928cf inchangee, aucune poussee avant la fusion de #711/PR548 puis retarget main. Paquet de relectur
- journal (mty-amh-docs-01) : Consigne pilote 18:53Z mise en oeuvre (docs-01, 19:30Z) : #775 reste TENUE. La veille ameesh-docs01-unfreeze passe sa cible #775/PR619 en veille seule (aucune fusion automatique ni rejeu de CI) et sur
- journal (mesh-design) : 13/10 20:5x mesh-design (pilote) : classe sensible SQL -> deux avis d'autres familles. R1 = auditeur (Codex), R2 = bureau (Claude) ; la demande a ameesh-cx1/cx2/cx3 est retiree (pause budget jusqu'au 

### #857 — PR 700 — `0d4c65dcabe7` — arbre fusionne `703b68a15b35`

Classe proposee : sensible hors prod (ci) : 1 avis independant

| statut | fichier | blob (arbre fusionne) |
|---|---|---|
| M | `scripts/test-hermeticity-lint.py` | `f3ea2d0e504f` |
| M | `tests/test_hermeticity_lint.py` | `ee1bc3f1731c` |

- avis (frozen, mty-amh-docs-01) : Tête soumise à R1 (mesh-design) : delta e962ba6..0d4c65d ; R1 demandée à 11:19Z.
- journal (mty-amh-docs-01) : Blocage CI (16:0xZ) : tete 0d4c65d, run en file ; elle sera rouge par test_653 (#925) et/ou la part psql annulee au plafond 30 min (#880) tant que ces causes racines ne sont pas fusionnees. R1 auditeu
- journal (mty-amh-docs-01) : PR #700 ouverte sur main frais, tete inchangee 0d4c65d (enfant unique de e962ba6, #707 fusionnee 7f1e7b5f). R1 autre famille deja acquise : auditeur (Claude) GO 0 B/0 N sur le delta e962ba6..0d4c65d. 
- journal (mty-amh-docs-01) : R1 auditeur (Claude, autre famille) reçue 11:42Z sur le delta e962ba6..0d4c65d : GO 0 B/0 N. Portée : delta seul. Restent : fusion de #707 (PR636) préalable, ouverture de la PR sur la tête inchangée 0

### #954 — PR 732 — `0722ff4ef931` — arbre fusionne `5b39fda801d2`

Classe proposee : leger

| statut | fichier | blob (arbre fusionne) |
|---|---|---|
| — | aucun (contenu deja dans la base gelee) | — |

- avis (verdict, bureau) : R1 indépendante bureau / Codex : GO contenu 0 B / 0 N. Diff exact test-only : dry-run utilise un nom de compte dédié plutôt que le compte guest installé des runners persistants. Assertions syntaxe bas

### #963 — PR 740 — `e0d9b6553127` — arbre fusionne `066d77025efd`

Classe proposee : sensible hors prod (concurrence) : 1 avis independant

- **dependance non satisfaite : #706 (ouverte (qa))**

| statut | fichier | blob (arbre fusionne) |
|---|---|---|
| M | `docs/EXPLOITATION.md` | `a2722306a96f` |
| M | `docs/V1-MAILBOX-RUNNER.md` | `c03e07677f94` |
| M | `src/ameesh/resources.py` | `7841acc29c21` |

- avis (verdict, auditeur) : R1 Codex GO statique confirmé après frozen : 0 B/0 N, même tête et delta doc/commentaire, défaut3600/plancher2x cap effectif conforme706. Fusion après632, feu pilote requis. Aucun rejeu.

- avis (frozen, mty-amh-docs-01) : tête de la PR #740 (branche lot/963-rotation-docs), relue et GO Codex sur ce sha

- avis (verdict, auditeur) : R1 Codex GO statique 0 B/0 N : 3 fichiers doc/commentaire seuls, défaut3600s/plancher2x cap effectif conforme5efdf8c53, commentaire batterie cite plafond1800s. Aucun code exécutable modifié ni suite r
- journal (mty-amh-docs-01) : hold: feu conditionnel mesh-design 16:28:39Z enregistre. PR740 tete gelee e0d9b6553127 (R1 Codex GO 0B/0N) ; porte #632 (#706) encore ouverte (PR632 OPEN, depend de #925). Veille systemd ameesh-docs01
- journal (mesh-design) : 13/10 18:2x mesh-design : R1 auditeur GO 0 B/0 N sur e0d9b65 (sensible hors SQL/secu/prod, commentaire seul). FEU CONDITIONNEL : apres fusion de #632, CI exacte verte sur la tete gelee + tete inchange

### #1019 — PR 791 — `9dfcb03e146d` — arbre fusionne `fe6f7d2b8443`

Classe proposee : leger

| statut | fichier | blob (arbre fusionne) |
|---|---|---|
| M | `docs/EXECUTEUR-MEDIEE.md` | `6b3d0f773a14` |
| M | `tests/parts/lecteurs-doc.txt` | `88d677947ebb` |
| M | `tests/test_l107_executor_contract.py` | `f7bdee0f7bcd` |

- avis (frozen, mty-amh-docs-01) : 

### #1038 — PR 806 — `5bc72a02e9ad` — arbre fusionne `85f860475864`

Classe proposee : leger

- **aucun avis enregistre**

| statut | fichier | blob (arbre fusionne) |
|---|---|---|
| M | `docs/EXECUTEUR-MEDIEE.md` | `e8b5d85fab9d` |

### #1048 — PR 808 — `1e7475317f83` — arbre fusionne `67628f72cd4d`

Classe proposee : leger

- **aucun avis enregistre**

| statut | fichier | blob (arbre fusionne) |
|---|---|---|
| M | `docs/EXPLOITATION.md` | `b882639f6e22` |

## Points a trancher avant composition de la vague (proposition, pas decision)

1. **#954 est deja absorbe par #1112** : `merge-tree(afa32932e, 0722ff4ef931)` rend l'arbre de la base
   lui-meme (`5b39fda801d2`) — aucun delta. A retirer de la liste « vague suivante » ; sa fermeture
   passe par le paquet #1112.
2. **#748 touche la porte de fusion** : le resultat fusionne ajoute `fs-class` aux `needs:` du check
   requis `tests` (ligne 517 du workflow fusionne ; base : ligne 388) et accepte `skipped` quand la
   part ne tourne pas (ligne 554). L'application mesh-design du 13/10 (incident #876) range « les
   checks requis, leurs conditions `if:`, et tout ce qui peut faire passer un check requis a
   `skipped` ou `success` sans executer les tests » dans la **securite : deux avis**. Avis presents :
   cx1 (Codex) GO sur le delta `556a44a -> 2ec9764` + impl-05 (DeepSeek, **meme famille que l'auteur
   docs-01**) -> un seul avis hors famille. A trancher : second avis d'une autre famille (Claude/Codex,
   en pause budget jusqu'au 20/10) ou ruling du pilote. Noter aussi que le manifeste #1112 classe
   #748 « leger (tests seuls) » alors que le diff touche `.github/**` (et la porte).
   Historique utile : impl-05 avait deja pose B1 sur `556a44a` pour cette ligne `needs:` ; le
   correctif `2ec9764` fait suivre les deux contrats de test (`test_l161a`, `test_l158`).
3. **#419 : contribution nette docs-only** — diff mesure `merge-base..tete` = 2 fichiers
   (`docs/design/etudes/index.md`, `docs/design/etudes/l243-projects-and-names.md`) ; le code L243 est
   deja dans main. La dependance #843 (posee pour un rouge psycopg `test_476`) ne couvre aucun code de
   cette PR ; a confirmer/lever par le pilote. Seul avis enregistre : celui de l'auteur (auto-relecture,
   non independant) ; classe legere -> relecture apres fusion.
4. **#775 x #1019 : seul conflit entre candidats**, sur `tests/parts/lecteurs-doc.txt` (les deux
   ajoutent un module lecteur de doc) ; resolution d'union attendue, a integrer dans un ordre relu.
   #775 reste par ailleurs sous la consigne pilote du 13/10 18:53 (retarget sur main frais apres #711,
   R1 auditeur + R2 bureau) — ma lecture des chemins donne 3 fichiers legers (`scripts/doc-links.py`,
   `tests/parts/lecteurs-doc.txt`, `tests/test_doc_links.py`) ; c'est la consigne du pilote qui
   s'applique, la divergence est signalee.
5. **#963** : rien a qualifier de plus ; reste la porte #706 (PR632) et le feu conditionnel deja
   enregistre (fusion apres #632, CI exacte verte, tete inchangee).
6. **#287 / #857** : un avis hors auteur present (impl-02 DeepSeek pour #287 sous la regle de repli
   18855a ; auditeur pour #857) ; dependances #880/#925 satisfaites par le paquet gele.
7. **#1019 / #1038 / #1048** : legers (docs, plus un temoin pour #1019) ; relecture d'une autre
   famille apres fusion ; veilles de rejeu/fusion deja armees de mon cote.
8. Les 10 merges sont propres sur la base gelee ; les fichiers listes sont la contribution nette,
   les blobs sont ceux de l'arbre fusionne. Apres la fusion de #1112 (main = arbre `afa32932e`),
   une contre-epreuve hash a hash doit retrouver ces blobs ; sinon la base a bouge et la
   qualification est a refaire.

Note de methode : les simulations `commit-tree` de l'integration sequentielle n'ont cree que des
objets pendants dans le clone ; aucun arbre de travail, aucune branche, aucun push.
