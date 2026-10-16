# Paquet integration/paquet-h-20261016-1445 — composition scriptée (mty-amh-impl-01, 16/10/2026)

Base : `origin/main` `496505cc6` (paquet horaire 1200 `496505cc6`). Branche : `integration/paquet-h-20261016-1445`.
Tête de composition des unités : `1b907ade4` ; la tête poussée du paquet est le commit de ce manifeste.
Exécuteur désigné : **mty-amh-impl-01** (composeur et exécuteur de fusion).
Unités incluses : 33. PR : ouverte juste après ce commit (voir notification).

La colonne « commit de composition » porte la preuve d'inclusion : chaque lot a
son commit dans la branche (`git log 496505cc6..1b907ade4`), y compris **#498** et **#1813**,
remis par le relais impl-05/impl-03 du 16/10 (transport groupé, aucune revue
rejouée) et **#1994** / **#1270**, relevés en union sur cette base.

## Composition

| unité | lot | source (gel / remise) | commit de composition | note |
|---|---|---|---|---|
| 1 | #1772 | `f5e819e9cc1797575ef0ea878fa1792a49519be1` | `1a632565b` | #1749 N1 : plan_git.probe ne rapatrie plus un commit absent d'un clone partiel (GIT_NO_LA… — mty-amh-impl-03, remise, verdict ok de ameesh-cx2 |
| 2 | #2016 | `ba9369b7f41c3075b8223bd9da300ed0ff3109ee` | `4ea6f57bb` | work assign : refuser l'assignation si le titulaire a changé depuis le relevé — mty-amh-impl-07, remise, verdict ok de mesh-design |
| 3 | #2021 | `ed80d5fc593914c213df190134f435c7d0efe1ed` | `5803b6d1d` | Runs CI de paquet sans work_item_id dans ci_runs : aucune notification de fin de CI au co… — mty-amh-impl-07, remise, verdict ok de mesh-design |
| 4 | #1283 | `95243064b10e9df48ffcd0c7de5b83355c9ff389` | `3b685abb6` | CI #1236 : tests d'adoption voient une session fictive dans un processus DeepSeek réel — mty-amh-impl-08, remise, verdict ok de bureau |
| 5 | #1979 | `3304681473c8bb5c9db6b3f1ec4763bc7ae1e2b9` | `2c9fcf1a4` | ameesh work reviewer : pouvoir retirer une désignation (ou la marquer « verdict non requi… — mty-amh-impl-02, remise, verdict ok de ameesh-dev2 et ameesh-cx1 |
| 6 | #498 | `6a25bdce7cd0b7f10c73e9e3b3adeb43733e254c` | `024246a5c` | L76c: per-turn delegated cgroup v2 (harness, tools, reparented descendants): stable owner… — mty-amh-impl-03, remise, verdict ok de ameesh-dev1 |
| 7 | #365 | `339d5c07d0f8a77b338eead8f4f5bb0eeb5494d4` | `4422ee44c` | L232c: mesh secret delivery and rotation — ameesh-cx2, tête gelée, verdict ok de codex3 |
| 8 | #2031 | `81833ce8edd12b2106ab3e64ccc6d6a3e38f287b` | `4319d20aa` | L232c tranche 2 : transport secrets séparé et intention durable — ameesh-dev1, tête gelée, verdict ok de mty-amh-qa-01 |
| 9 | #1813 | `7bd49dd78c6d5a2e472ba3829bd05b8af75b06a7` | `663a6d703` | plan_github N3 : compteurs du cache écrits seulement à la requête réelle suivante dans un… — mty-amh-impl-03, tête gelée, verdict ok de mesh-design |
| 10 | #2012 | `135882573df51f260cad8b29726d1d66d2219744` | `02fec7ea5` | docker_storage_unavailable permanent : docker.io 29.1.3 n'a pas le plugin buildx (agents-… — mty-amh-impl-07, tête gelée, verdict ok de ameesh-dev1 |
| 11 | #1816 | `2e9754bce34e6da69e702b140c8069509ac10e80` | `235ddea39` | L76 résident : les événements system commands_changed de la sonde Claude peuvent tomber d… — ameesh-cx2, tête gelée, verdict ok de mesh-design |
| 12 | #2009 | `93730aa5707cd4105dec09cb7d34da7738018b87` | `739fdcf86` | Arrêt d'un runner : après le SIGTERM de « systemctl restart », le runner reprend des tour… — mty-amh-impl-06, tête gelée, verdict ok de ameesh-dev7 |
| 13 | #2003 | `7db21ef0ae53455f2c6e40696d4b3ae1893c2f53` | `0bfc4cd70` | Ménage des hôtes : volumes Docker inutilisés des bancs de test jamais purgés (agents-1 à… — mty-amh-impl-03, tête gelée, verdict ok de ameesh-cx2 |
| 14 | #2007 | `92bfb775a836b3cb7f7157d5227373bb37924055` | `ac5d65849` | Docs : les preuves de livraison (PLAN-DE-TRAVAIL.md, EXPLOITATION.md) ne disent pas le ga… — mty-amh-docs-01, tête gelée, verdict ok de mesh-design |
| 15 | #1610 | `8f3ebdf804282c3800147887bc515280a8af1327` | `b71fdb3c3` | #1517 N2 : radar, epreuve de couple — lire les attributs de fusion dans l'arbre de la cib… — ameesh-cx1, tête gelée, verdict ok (tête) |
| 16 | #1609 | `5877da32d8a2f6b07540da2139ea87c07a27aa46` | `e67475fb5` | #1517 N1 : doc_readers (version-only.py) ne dedoublonne pas la liste lecteurs-doc apres u… — mty-amh-impl-07, tête gelée, verdict ok de mesh-design |
| 17 | #1878 | `1b5d43364927d9ad5ed115baf4ca1d284bbe1cee` | `e2750c347` | test-plan : un module sys/os renommé par affectation (s = sys ; s.exit()) ou getattr(sys,… — ameesh-cx2, tête gelée, verdict ok de ameesh-dev4 |
| 18 | #1481 | `7acfd68214d7fcda10d5d5c70efdcee458fba1ee` | `6f9ccb7f2` | Sessions par lot : préciser le comportement quand les horloges hôte et base divergent — mty-amh-impl-02, tête gelée, verdict ok de ameesh-cx3 |
| 19 | #1971 | `19f1d44dc8cd37b88ee030fbe8b4512c35c7109b` | `7b609c523` | #1916 N1 : fenêtre de reprise du superviseur VM mesurée à l'horloge murale — ameesh-dev2, tête gelée, verdict ok de mty-amh-docs-01 |
| 20 | #1972 | `19f1d44dc8cd37b88ee030fbe8b4512c35c7109b` | `7b609c523` | #1916 N2 : journaliser l'entrée dans la fenêtre de reprise (garde périmée) — ameesh-dev2, tête gelée, verdict ok de mty-amh-docs-01 |
| 21 | #1981 | `b76efbc92bb7ff514cd19aa5e3f69c5c81b66d75` | `3af358e58` | #1968 R1 bureau N1+N2 : temoin tilde quote manquant ; _HOOK_VARIABLE accepte un nom comme… — mty-amh-impl-08, tête gelée, verdict ok de ameesh-cx3 |
| 22 | #1943 | `4ac74a89917f9c33b34de6c0c6dd0ffa50e38de3` | `dd8cff37a` | test_l160e StackTest.test_prepared_conflict_replayed_unprepared_skipped dépend du git con… — mty-amh-impl-08, tête gelée, verdict ok de ameesh-cx3 |
| 23 | #1789 | `e5937db3f6835f93346219304fe1eba2a5777769` | `d0620ff1a` | #1580 : N de la relecture Claude — ameesh-dev5, tête gelée, verdict ok de ameesh-cx1 |
| 24 | #1605 | `da7af616ab823d15c34a05a19038a4555cbd4458` | `1f37806d2` | #1567 : détails P3 du delta L122 — commentaire anglais dans work.merged, témoin already+c… — ameesh-cx2, tête gelée, verdict ok de mesh-design |
| 25 | #927 | `0afb4d04ab605562e40fefb91e5b10c3b8db581f` | `76259033b` | Suite #364 (R1 auditeur, n) : huit defauts non bloquants du controleur de release L232b — mty-amh-impl-04, tête gelée, verdict ok de ameesh-dev7 et ameesh-cx1 |
| 26 | #2025 | `f83399c1db1b5d081f62f93cc66c7ae0f51770cb` | `74b9d510b` | #927 compagnon K7 : recouper cible libpq et schéma de sauvegarde de release — mty-amh-impl-04, tête gelée, verdict ok de ameesh-dev7 |
| 27 | #1573 | `6bd83c8c9` | `7a387cd3b` | ci.py (#901 suites) : relance courte finie entre deux passages non rouverte par _catch_up… — ameesh-dev5, tête gelée, verdict ok de bureau |
| 28 | #1822 | `ee88f1f4` | `ba1979c95` | Attente fantome « verdict » sur les lots legers dont le pilote a route l'avis APRES fusion — mty-amh-docs-01, tête gelée, verdict ok de ameesh-dev4 |
| 29 | #1666 | `0ae520fdf` | `1c05937c4` | #1651 suites (N) : porte des parts CI — espaces de noms, coût d'extraction, isolation du… — deepseek4, tête gelée, verdict ok de ameesh-dev4 et ameesh-cx3 |
| 30 | #1463 | `440d49f9e` | `13d55ff25` | L87c : accusé de réception persistant d'une alerte (API et CLI) — ameesh-cx2, tête gelée, verdict ok de bureau |
| 31 | #831 | `aa0857321` | `1d4de3e20` | #802 follow-up: work_packages writes outside the sync lock (rebase_default), per-canon lo… — mty-amh-impl-01, remise, verdict ok de ameesh-dev1 |
| 32 | #1994 | `a090ae9d` | `201de92b6` | #1937 post-fusion : le raccord B/C perd l'acteur des jalons — tout verdict réel est ignor… — mty-amh-impl-08, remise, verdict ok de ameesh-cx1 |
| 33 | #1270 | `cb6174e9` | `afd184ca9` | Un 429 passager laisse un agent Codex bloqué sans reprise automatique — mty-amh-impl-08, remise, verdict ok de ameesh-cx2 |

## Écartés de ce véhicule

- #1516 `43c3c2b2` — retiré par le pilote (16/10 12:23, témoin #1437 rouge) ;
  la nouvelle tête impl-06 `c44f57c4` n'est pas inscrite au registre du lot
  (branche vue `43c3c2b2`), admission à reprendre avant composition.
- #2008 `bd6931cb4` — candidat, relecture dev1 non rendue (pas admis).

## Contrôles de composition

- `git merge-tree --write-tree` rc=0 contre chaque unité avant fusion ; aucun
  marqueur de conflit restant ; `git diff --check` propre.
- Compteurs du contrat médié dérivés sur l'arbre composé (#1517 : aucune clé
  `interface_total`) : 64 opérations / 363 refusées / 427 au total,
  `tests/test_l107_executor_contract.py` vert.
- Une seule CI complète, sur cette tête gelée (§12) ; aucune CI de lot.

## Tête finale après la CI rouge du 16/10 — correctifs groupés (pilote 14:05)

La CI du run `38904431472` (tête `c8871afea`) a relevé quatre causes distinctes.
La tête finale les corrige **sans élargir le périmètre des 33 unités** :

- **raccord composeur `b9aaf6f2a`** (branche `prep/paquet-h-20261016-1445-raccord-ci`,
  poussée inerte) : `tests.test_l161c_failures_realtime` lit le `run_attempt` réel,
  `tests.test_l56_role_executor` emploie `attendu()`, `ci_parts.py` résout `PROC`
  via `platform` (+ réexport `platform/__init__.py`), témoin SandboxTest recalculé.
- **#2025 K7 `89a24fa80`** (GO dev7/Claude 13:58, jalon 9106, couplage #927+#2025
  conservé) : `LibpqParserUnavailable` — refus nommé et fail-closed du recoupement
  de cible quand `psycopg.conninfo` manque sous le pilote psql.
- **#498 `c2d1cfa50b`** (cause prouvée du rouge psql 9/9 — dev7 13:57, avis Codex
  pilote 0B/0N consigné) : `turn_cgroup_root` journalise en `log_async` ; le
  démarrage de `run()` ne bloque plus sur un puits plein et traite le SIGTERM.
  La réécriture de quelques lignes ajoutées par #498, #2025, #927, #1666, #365
  et #2021 (témoins CI recalculés par le raccord et le correctif K7) est le seul
  écart de contenu signalé par `scripts/paquet-inclusion.py` — 6 avertissements
  « contenu partiel à la tête », aucun défaut, aucune unité manquante.

Tête de composition : `b3f1e28bc` (b9aaf6f2a + les deux correctifs) ; la tête
poussée du paquet est le commit de ce manifeste. Aucune CI intermédiaire (§3) :
une seule CI groupée sur cette tête, puis auto-fusion armée sur le SHA exact.
Les admissions du paquet suivant (#2061, #1898, #2008, #2022, #1683, #1777…)
restent hors de cette correction (pilote 14:05).

Tests ciblés des trois correctifs (huit modules, deux pilotes, logs
`tests-final-1445-psql.log` / `tests-final-1445-psycopg.log`) : **299 OK par
pilote**, 2 sautés. Seule erreur, `tests.test_l56_role_executor.RoleExecutorTest`
en `setUpClass` (« fe_sendauth: no password supplied » sur la base neuve du banc
— `.pgpass` local sans entrée générique pour `agent_mesh`) : **reproduite à
l'identique sur `c8871afea` nu**, artefact du banc local et non du delta ; la
classe rejouée avec `PGPASSWORD` rend **23 OK psql et 23 OK psycopg**.
