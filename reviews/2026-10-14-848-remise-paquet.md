# #848 — remise locale au paquet (préparation pilote)

mty-amh-docs-01, 14/10/2026 20:1xZ. Événement stale #848 (20:05Z) et demande de
l'auditeur (20:06Z). **Aucun geste distant** : ni push, ni run, ni CI, ni
close/reopen, ni fusion. Canon §12 : la qualification CI se fait sur la branche
d'intégration du paquet ; aucune CI individuelle n'est demandée.

## 1. Identité et gel

- Lot #848 (qa, **sensible §8** : `src/ameesh/progress_serve.py` = prod ; aussi
  `ci.py`, `plan_github.py`) — PR manaty/ameesh#644, branche
  `lot/848-github-api-quota`.
- Tête gelée : `3a4878b6506d91317ad28139e6aa1d71fad5df8a`, arbre
  `0a96397ef4b1f2a73ce12d63bcbc28f2d8cdb525` — gel du 14/10 13:57:25Z (jalon
  4231, posé par l'auteur).
- Rafraîchissement : fusion `--no-ff` de main `c4fa14a2` dans la branche
  (parents `d9794f61` + `c4fa14a2`), sans réécriture ; base de lecture du delta
  = `c4fa14a2`.
- Main courante au relevé unique 20:0xZ :
  `0532faed4bba6a683073b9c7f5b9deca64abb2bd` (arbre `08db28e6`), vérifiée par
  `git ls-remote`.
- PR : OPEN / MERGEABLE / `mergeStateStatus` BLOCKED / `mergedAt: null`.
  **Aucune fusion hors PR** — rien à clore.

## 2. Constance du contenu relu

- `git patch-id --stable -U0` du delta du lot : `c3e2b44606a6bffe30bd060c35e7c608d95a00a4`
  — identique sur `d9794f61` (base `afa32932`) et sur `3a4878b65` (base
  `c4fa14a2`) : le seul apport du rafraîchissement est la fusion de main.
- Diffstat constant : **8 fichiers, +1199/−50** — `docs/EXPLOITATION.md`,
  `src/ameesh/ci.py`, `src/ameesh/plan_github.py`, `src/ameesh/progress.py`,
  `src/ameesh/progress_serve.py`, `src/ameesh/versions.py`,
  `tests/fake_github.py`, `tests/test_l848_github_quota.py`.
- Le jeton n'est ni écrit ni journalisé (contrainte du lot).

## 3. Avis relus

- **R1** mty-amh-qa-01 (DeepSeek, autre chantier/session) : GO **0 B / 2 N**
  (N1 horloge wall non transmise à `client()` ; N2 doc « et réciproquement »
  non implémentée par le pré-contrôle), 13/10 13:06Z sur `d9794f61`, preuves
  `.qa/qa-848/`.
- **R2** bureau (Claude 3) : OK **0 B / 4 N** (clé de cache = chemin REST seul ;
  `reads.blocked` limité à core ; limite secondaire 403 sans `Retry-After` non
  retenue ; invalidation de statut non explicite, borne `ci_ttl` 60 s), 14/10
  02:06:08Z, jalon verdict `ok` #3606 sur `d9794f61`. N4 → lot de suite #1185.
- Les deux avis portent le **contenu constant** ; leur report sur la tête
  rafraîchie relève du pilote (question du **feu adapté** posée le 14/10 13:56Z,
  toujours due). C'est la porte actuelle.

## 4. Dépendances

- #880 (plafond psql 45 min), #925 (blob `test_653`), #932 (clé de cache venv
  par `runner.name`) : **fusionnés, satisfaits** (contenus vérifiés dans main).
- Aucune dépendance bloquante ouverte.

## 5. Qualification CI

- Run applicable à la tête : `38576187834` (`pull_request`, créé 13:57:09Z,
  modèle #1211) — **terminal `cancelled` à 19:26:52Z** (vague d'annulations) :
  aucun verdict, aucun run vif sur la tête. Les check-runs affichés « fail »
  appartiennent à ce run annulé.
- Dernier vert : `38560950470` (12:23:00Z, 12/13, agrégat `tests` SUCCESS) sur
  la tête **précédente** `d9794f61`, sous le modèle CI **d'avant** #1211 →
  **non transférable** (§12).
- La qualification applicable est celle de la CI complète de la branche
  d'intégration du paquet ; la tête s'y intègre proprement (§6). Aucune CI
  individuelle demandée.

## 6. Admission au paquet — preuve locale

- `git merge-tree --write-tree 0532faed4 3a4878b65` : **rc=0**, arbre fusionné
  `1e1da1be44b1f654e67008da24a3443f211ad644` — aucun conflit.
- Réf atelier locale : `refs/atelier/848/union-0532faed` @
  `03e10b9633f56ad5b4c97c3671efd7a7299bcea2` (commit de fusion, parents
  `0532faed4` + `3a4878b65`, arbre `1e1da1be`).
- Preuves ciblées déjà disponibles : `tests.test_l848_github_quota` **22/22 OK**
  (gel 13:57Z, sans réseau ni base) ; 207 tests ciblés OK (13/10, 17 sautés
  faute de base locale).

## 7. Porte actuelle et action attendue

Rien n'est dû de mon côté : avis livrés, contenu constant, PR ouverte, aucune
dépendance. Les trois plans à distinguer :

1. **Verdict** — livré (R1 + R2) ; le registre affiche encore « verdict by
   bureau » parce que le jalon verdict (02:06Z) précède le dernier gel
   (13:57Z) : artefact de jalons, pas une relecture en attente. Report sur la
   tête rafraîchie = décision du pilote (feu adapté).
2. **Qualification** — par la CI du paquet, à l'admission ; pas de CI
   individuelle (§12).
3. **Admission / fusion** — admission de #848 à un paquet par le pilote, puis
   fusion auteur `--match-head-commit` au feu, puis coordination impl-04 /
   deepseek1 (déploiement et vérification).

Ni abandon ni supplantation : le lot reste assigné, actif, en attente de
l'arbitrage du pilote.
