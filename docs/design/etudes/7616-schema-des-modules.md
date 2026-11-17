---
type: Study
status: decided
work_item: 7616
---

# Schéma des modules : sortir les tables de développement du schéma commun

Lot #7616 (manaty/ameesh#5802), demande de Sébastien du 16 novembre 2026.
Cette étude est la **spécification de référence** des lots issus de la
décision #222989 (owner, 17/11/2026) : elle ne modifie aucun profil, aucune
base, aucun code, ne fusionne rien, ne déploie rien et ne supprime aucune
donnée. La consigne #222966 (`ameesh`, 16/11/2026 23:01) interdit, dans ce
lot, toute action irréversible ou de production (déploiement, fusion,
suppression de données, serveur de production, dépense engagée).

Compagnon exhaustif : [`7616-schema-des-modules/inventaire.tsv`](7616-schema-des-modules/inventaire.tsv),
174 lignes, une par table du schéma, avec propriétaire proposé, origine,
lecteurs et écrivains dans le code, fonctions et triggers SQL, notes.

## 1. Résumé

L'inventaire du code à `1ac9cb6a1` (16/11/2026 22:25) trouve **174 tables** :
**138 du noyau** (dont les deux registres de migrations du runner) et **36
proposées au module `dev`** ; le module `it` n'en possède aucune. Trois vues
noyau complètent le schéma ; elles ne lisent que des tables noyau.

Les 36 tables `dev` ne sont pas isolées : le noyau les lit ou les écrit
directement (au moins douze modules Python hors du stockage), des triggers du
noyau les mettent à jour, `work_items` porte encore douze colonnes de
développement, et la machine d'états des lots ne laisse atteindre `delivered`
que par le connecteur `git-merge` du module `dev`.

La proposition tient en trois mécanismes :

1. **Un schéma PostgreSQL par module**, nommé `<schéma_noyau>__<module>`
   (`dev`, `it`, modules externes), avec un registre de schémas et de
   migrations propre au module. Les migrations de module sont découvertes,
   ordonnées, vérifiées par empreinte et appliquées dans la même transaction
   et le même verrou que les migrations du noyau.
2. **Le noyau ne nomme plus les tables d'un module** : il passe par des ports
   optionnels lus dans le registre des modules et dégrade proprement quand le
   module est absent (profil nu). La garde SQL existante (`project_modules`)
   continue de garder les types et les règles par projet ; elle ne devient pas
   une garde de schéma.
3. **Une bascule sans perte par métadonnées** : `ALTER TABLE … SET SCHEMA`
   (aucune copie de ligne), puis des **vues de compatibilité** dans le schéma
   noyau pour que le code de la version précédente continue de fonctionner.
   Les vues sont retirées après une release, quand le code qualifié est
   déployé. Aucun `DROP TABLE`, `DELETE` ni `TRUNCATE`.

Point dur constaté : `dev` est **toujours actif** (`ALWAYS_ACTIVE = True`,
L101 T4 #4357) et le profil nu `AMEESH_MODULES=bare` est réservé à la CI et
aux tests, refusé par `doctor` sur un hôte. Le mécanisme de schéma ne suffit
donc pas, à lui seul, à débarrasser un mesh non informatique des tables de
développement : il faut aussi une décision de politique sur l'activation de
`dev` (question Q1). La décision #222989 tranche Q1–Q8 : cette étude livre
l'indépendance fonctionnelle, et l'activation d'un hôte réel sans `dev`
reste un lot de politique distinct (§6, §7).

## 2. Cadrage

### 2.1 Sources

- Lot #7616, corps de la demande de Sébastien (16/11/2026) : inventaire
  exhaustif, déclaration et migration des tables par module, plan de bascule
  sans perte pour Manaty et IMA/TPK ; « évolution de fond : spécification à
  écrire avec Sébastien avant tout code. Pas d'urgence. »
- Consigne #222966 (`ameesh`, 16/11/2026 23:01) : prise du lot, interdictions
  du lot, renvoi à `mesh-design` si le lot n'est pas pour l'agent.
- Canon `home/processus/lots.md` : SQL et migrations sensibles (§1, §8),
  revue par une autre famille, migrations additives seulement en staging,
  aucune migration destructive sans go owner (§10).
- Code : `src/ameesh/modules/__init__.py` (profils, module toujours actif),
  `src/ameesh/modules/registry.py` (interface `Module`), `src/ameesh/modules/storage.py`
  (garde SQL par projet), `src/ameesh/migrations.py` (découverte, empreintes,
  non-additivité), `src/ameesh/work_lifecycle.py` (états).
- Études antérieures : `docs/design/etudes/project-modules.md` (L317a),
  `docs/design/etudes/l243-projects-and-names.md` (sélection par projet),
  `docs/design/etudes/1467-trains-integration.md` (paquets et releases).

### 2.2 Périmètre

Dans le périmètre : les **tables** du schéma commun et leur propriétaire,
le mécanisme de schéma et de migrations par module, la bascule des tables.

Hors périmètre de cette étude, mais signalé comme couplage restant :

- les **colonnes** de développement de `work_items` (`pr_ref`, `branch*`,
  `migration*`, `change_size`, `partial_*`, `reviewer(s)`) : elles restent
  dans la table noyau après la bascule des tables ;
- la **machine d'états** `merged`/`promoted`/`delivered` et le connecteur
  `git-merge` ;
- le devenir de `dev` comme module toujours actif (décision de politique) ;
- les modules externes : le mécanisme les accepte, mais leur admission et
  leur revue sont une décision séparée.

### 2.3 Méthode d'inventaire

L'inventaire a été produit par analyse statique du dépôt à `1ac9cb6a1` :

- les 243 migrations forward de `src/ameesh/migrations/*.sql` (plus trois
  fichiers de retour arrière manuels dans `rollback/`) ;
- les 173 `CREATE TABLE` distincts trouvés dans ces migrations (recherche
  insensible à la casse, `IF NOT EXISTS` et nom qualifié acceptés), plus
  `schema_migrations`, créée par le `BOOTSTRAP` de `migrations.py`, soit
  174 tables ;
- le contrôle de complétude compare ces noms à la colonne 1 du TSV
  (`comm`) : aucun écart. Il a rattrapé quatre tables créées après la
  première rédaction, `work_packet_snapshots` (0580), `credit_cycles` et
  `credit_requests` (0581), `credit_reconciliations` (0582) ;
- les chaînes SQL du code Python (`ast`), classées en lecture, écriture ou
  simple mention selon le verbe qui précède le nom de table (`FROM`, `JOIN`,
  `INSERT`, `UPDATE`, `DELETE`, `ALTER`, `TRUNCATE`…) ; les docstrings sont
  exclues ;
- les corps de fonctions SQL et les triggers des migrations, pour repérer
  les objets qui nomment une table sans passer par le code Python ;
- une vérification des vues : `agent_mesh_overview`, `mesh_approvals_status`
  et `mesh_keys` ne dépendent que de tables noyau.

Limites assumées : l'inventaire est **au fichier**, pas à la fonction ; il ne
suit pas le SQL construit par `%`/`.format` au-delà des chaînes littérales ;
il ne inventorie pas les colonnes ; une table peut être lue par un fichier
pour plusieurs fonctions. La colonne `mentions` du TSV rattrape les noms de
tables cités sans verbe SQL (listes, messages, `agent_rename.py`). Le TSV est
la source de vérité de l'inventaire ; le corps de cette étude en donne la
lecture et les cas transverses. Il est daté du commit `1ac9cb6a1` et doit être
régénéré si le code bouge avant la revue.

## 3. Inventaire

### 3.1 Chiffres

| Catégorie | Nombre | Remarque |
|---|---:|---|
| Tables du schéma | 174 | 173 créées par les migrations + `schema_migrations` (bootstrap) |
| Tables noyau | 138 | dont `schema_migrations` et `schema_non_additive_migrations` |
| Tables `dev` (proposées) | 36 | voir §3.2 |
| Tables `it` | 0 | `it` utilise `work_module_milestones` (noyau) |
| Vues | 3 | `agent_mesh_overview`, `mesh_approvals_status`, `mesh_keys` ; aucune table `dev` |
| Migrations forward | 243 | `0001` à `0584` |
| Retours arrière manuels | 3 | `rollback/0518`, `rollback/0583_restore`, `rollback/0584_restore` |

Le TSV contient 174 lignes : 138 `core`, 36 `dev`, 0 `it`.

### 3.2 Les 36 tables `dev`

Le classement suit le **producteur de la donnée** (le workflow de
développement), pas le lecteur d'aujourd'hui : c'est précisément le lecteur
noyau qui est le couplage à retirer.

| Famille | Tables | Stockage principal |
|---|---|---|
| CI | `ci_runs`, `ci_test_failures`, `ci_runner_state`, `ci_queue_state`, `ci_guard_scan`, `ci_guard_run`, `ci_map_state`, `ci_disk_state`, `ci_disk_refusals`, `ci_disk_dispatch` | `storage/postgres/ci.py` |
| Fusion, trains, paquets | `merge_queue`, `merge_locks`, `integration_trains`, `integration_train_fires`, `integration_train_queue_receipts`, `work_packet_members`, `work_packet_snapshots`, `work_landings`, `work_resolutions` | `storage/postgres/work.py`, `train_*.py`, `merge_queue.py` |
| Réservation de migration | `work_migration_reservations` | `storage/postgres/migration_reservations.py` |
| Espaces de travail | `managed_worktrees`, `work_item_branch_heads` | `storage/postgres/work.py` |
| Prévalidation | `prevalidation_reports` | `storage/postgres/prevalidation.py` |
| Veille GitHub | `github_watch_cursors`, `github_watch_events`, `github_watch_scans`, `github_watch_pending` | `storage/postgres/github_watch.py` |
| Revue | `review_catch_up`, `review_finding_resolutions`, `work_review_withdrawals` | `storage/postgres/work.py`, `review_finding.py` |
| Provenance | `provenance_receipts`, `provenance_revocations` | `storage/postgres/provenance.py` |
| Empreintes et impact | `work_item_footprints`, `work_item_impact_analyses`, `repository_footprint_policies` | `storage/postgres/work_footprints.py` |
| Mesures | `work_item_measures` | `storage/postgres/work.py` |

Deux cas sont explicitement **frontière** et doivent être confirmés avant la
bascule :

- `work_migration_reservations` : le trigger noyau `work_items_migration_slot_sync`
  sur `work_items` l'écrit (0419, 0432, 0467, 0518), et `process_registry.py`
  la lit et l'écrit. Tant que `work_items` porte les colonnes `migration*`,
  la réservation est un invariant du noyau. La déplacer demande de déplacer
  aussi le trigger et de rendre `process_registry` tolérant à l'absence
  (il teste déjà `to_regclass`, mais sur le nom non qualifié).
- `work_review_withdrawals` : les triggers noyau `ameesh_review_seats_guard`
  (sur `work_items`) et `ameesh_review_verdict_guard` (sur
  `work_item_milestones`) la lisent et l'écrivent (0529) ; `agent_review_hold.py`
  la lit. C'est un invariant de revue du noyau. La déplacer demande de
  réinstaller ces triggers depuis le module et de rendre la garde de revue
  optionnelle en profil nu.

La recommandation est de **les inclure dans la première bascule**, avec
leurs triggers traités par la migration de module (fonctions déplacées ou
triggers recréés), plutôt que de laisser deux tables `dev` dans le schéma
noyau : sinon la frontière reste floue. Si la revue préfère
minimiser la première bascule, elles peuvent être reportées à un lot suivant,
sans perte : elles restent noyau en attendant.

### 3.3 Le noyau qui touche encore les tables `dev`

Lecteurs ou écrivains hors `src/ameesh/storage/postgres/` et
`src/ameesh/modules/dev/` (le TSV donne la liste complète par table) :

| Module noyau | Tables `dev` | Usage | Traitement proposé |
|---|---|---|---|
| `auditor_pass.py` | `ci_runs` | état de la porte CI | port optionnel `dev` ; vide en profil nu |
| `debit.py` | `ci_runs`, `work_packet_members` | imputation de coût | port optionnel ; le coût CI disparaît sans module |
| `model_evaluation_report.py` | `ci_runs`, `ci_test_failures`, `provenance_receipts`, `review_finding_resolutions` | rapport d'évaluation | port optionnel ; rapport partiel sans module |
| `exploitation.py` | `integration_trains` | tableau d'exploitation | port optionnel ou déplacement dans `dev` |
| `packet_reviews.py` | `work_landings`, `work_packet_members` | revue de paquet | déplacer dans `dev` (logique de paquet) |
| `packet_link.py`, `packet_close.py`, `packet_snapshot.py` | `work_packet_snapshots` | reçus de composition | déplacer dans `dev` (logique de paquet) |
| `repair_observation.py` | `github_watch_pending` | observation de réparation | déplacer dans `dev` |
| `reprise.py`, `review_designation_activation.py` | `managed_worktrees` | reprise et désignation | passer par la façade `workspaces` de `dev` |
| `alerts_delegation_pg.py` | `merge_locks` | alerte de bail | port optionnel |
| `agent_review_hold.py` | `work_review_withdrawals` | garde de revue | port optionnel ou garde noyau |
| `review_finding.py`, `verdict_carry.py` | `provenance_receipts` | provenance de verdict | déplacer dans `dev` |
| `process_registry.py` | `work_migration_reservations` | slots de migration | port optionnel ; `to_regclass` qualifié |
| `train_admissions.py`, `train_candidates.py`, `train_initial.py` | `integration_train_queue_receipts` | admissions de train | déplacer dans `dev` (code `train_*`) |
| `agent_rename.py` | liste codée en dur `REWRITTEN` | renommage d'agent | lire la liste des tables du module, pas une constante |

Le stockage `storage/postgres/work.py` lit ou écrit 14 des 36 tables `dev` ;
`storage/postgres/ci.py` en lit ou écrit 11. Ce sont les deux gros
déplacements de code. La bascule des tables ne les exige pas (les vues
protègent l'ancien code), mais le retrait des vues, si.

### 3.4 Objets SQL transverses

| Objet | Type | Tables | Origine | Effet |
|---|---|---|---|---|
| `work_items_migration_slot_sync` | trigger sur `work_items` | écrit `work_migration_reservations` | 0419, 0432, 0467, 0518 | réserve/libère un slot de migration à chaque changement de `migration`, `state` ou cible |
| `work_migration_reservation_guard` | trigger sur `work_migration_reservations` | la table elle-même | 0419 | empêche deux réservations actives sur la même cible |
| `ameesh_review_seats_guard` | trigger sur `work_items` | lit/écrit `work_review_withdrawals` | 0529 | enregistre un retrait de relecteur |
| `ameesh_review_verdict_guard` | trigger sur `work_item_milestones` | lit `work_review_withdrawals` | 0529 | refuse un verdict d'un relecteur retiré |
| `zz_ameesh_limited_login` | trigger sur `provenance_receipts` | `limited_logins` (noyau) | 0567, recréé 0583/0584 | limite les écritures sous login limité ; `tg_table_schema` devient le schéma du module après déplacement |
| `ameesh_agent_spelling` | triggers sur de nombreuses tables (dont 13 `dev`) | colonnes d'agent | 0516 (boucle), 0519, 0534 | normalise l'orthographe des agents |
| `review_finding_resolution_guard` | trigger sur `review_finding_resolutions` | la table elle-même | 0564 | garde les résolutions de constat |
| `merge_queue_generation_seq` | séquence explicite | `merge_queue.generation` | 0412 | non `OWNED BY` : `ALTER TABLE … SET SCHEMA` ne la déplace pas |
| `REWRITTEN` | constante Python | 36 tables `dev` | `agent_rename.py` | liste de tables à réécrire lors d'un renommage |

Les fonctions SQL qui **lisent ou écrivent** une table `dev` sont peu
nombreuses : `work_items_migration_slot_sync`,
`work_migration_reservation_guard`, `ameesh_review_seats_guard` et
`ameesh_review_verdict_guard`. `review_finding_resolution_guard` ne fait que
citer `review_finding_resolutions` dans un message d'erreur ; elle lit
`work_item_milestones` (noyau). `ameesh_limited_login_guard` ne fait que
comparer `tg_table_name` à `'provenance_receipts'` ; `ameesh_agent_spelling`
lit les colonnes de la ligne, pas la table. Aucune n'est `SECURITY DEFINER`. Les deux gardes de revue fixent
`search_path FROM CURRENT` (le schéma de l'appelant, donc le schéma noyau) ;
les deux fonctions de slot n'ont pas de `SET search_path` et suivent aussi
l'appelant. Toutes les quatre résolvent donc les vues de compatibilité comme
les tables. `ameesh_limited_login_guard` fixe `pg_catalog, pg_temp` mais
qualifie le schéma par `tg_table_schema`, donc elle suit la table.

### 3.5 `work_items` : colonnes et machine d'états

La table `work_items` reste noyau, mais elle porte encore douze colonnes de
développement : `pr_ref`, `branch`, `branch_target`, `branch_head`,
`branch_head_at`, `migration`, `migration_repo`, `change_size`,
`partial_merge`, `partial_seen_at`, `reviewer`, `reviewers`. Elles ne sont pas
déplacées par cette bascule ; leur sort est un lot suivant (Q3).

La normalisation L99 T5 (0518) a bien ramené les états persistés à
`intake`, `build`, `qa`, `validated`, `delivered`, `blocked`,
`waiting_human`, `closed`. `merged` et `promoted` restent acceptés en entrée
et sont réécrits en `delivered` + `legacy_state` (`merged` ou `promoted`) ;
`work_item_events.state` journalise l'orthographe historique. Cependant :

- sans `workflow_ref`, `delivered` exige encore
  `workflow_stage IN ('merged','promoted')` ; c'est une règle de
  développement dans une contrainte noyau ;
- dans `work.deliver`, les connecteurs noyau `document` et `done` ferment en
  `closed` ; seul le connecteur `git-merge` du module `dev` atteint
  `delivered` ; un lot `delivered` sous `workflow_ref` reçoit
  `legacy_state = 'merged'` par défaut, donc la phase par défaut reste un
  mot de développement ;
- `work_lifecycle.phase` retombe sur `legacy_state`, `workflow_stage` ou
  `merged` par défaut ; le jalon `merged` est écrit par un trigger noyau
  quand `delivered` + `legacy_state = 'merged'`.

Autrement dit, la séparation des états est faite dans le stockage, mais pas
jusqu'au bout dans les chemins d'écriture : hors workflow Process, un projet
non `dev` ne peut pas atteindre `delivered` par les connecteurs noyau sans
une étape `dev`. C'est un couplage de machine
d'états, distinct de la table, à trancher (Q4). La présente étude ne le
corrige pas.

### 3.6 `it` n'a aucune table

`it` déclare les types `release` et `publication`, des schémas de preuve et
des règles de cycle de vie ; ses preuves sont stockées dans
`work_module_milestones`, table noyau, et ses rôles `ameesh_it_proof` /
`ameesh_it_qa` sont des rôles de base. Aucune migration de module n'est donc
nécessitée par `it` aujourd'hui. Le mécanisme proposé doit néanmoins
l'accepter sans schéma : `MIGRATIONS = None` est un cas valide.

## 4. Proposition : schéma et migrations par module

### 4.1 Nommage et registres

Le schéma d'un module est dérivé du schéma noyau pour éviter les collisions
entre plusieurs schémas de test dans une même base (les tests utilisent déjà
`t_*`) :

```
<schéma_noyau>__<slug_du_module>
```

Exemple : noyau `public` → `public__dev` ; noyau `t_4616` → `t_4616__dev`.
Le slug est le nom du module en minuscules, `[a-z0-9_]`, tronqué avec un
suffixe SHA-256 si le nom dépasse 63 octets. Le nom logique (`dev`) reste
celui du descripteur ; le nom physique est stocké dans le registre et jamais
deviné par le code appelant.

Trois tables additives, dans le schéma noyau :

```sql
CREATE TABLE IF NOT EXISTS module_schemas (
    module            text PRIMARY KEY,
    schema_name       text NOT NULL UNIQUE,
    schema_sha256     text NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now(),
    CHECK (module ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'),
    CHECK (schema_name ~ '^[a-z_][a-z0-9_]{0,62}$')
);

CREATE TABLE IF NOT EXISTS module_schema_migrations (
    module     text NOT NULL REFERENCES module_schemas(module) ON DELETE RESTRICT,
    version    integer NOT NULL,
    name       text NOT NULL,
    checksum   text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (module, version)
);

CREATE TABLE IF NOT EXISTS module_schema_non_additive_migrations (
    module   text NOT NULL,
    version  integer NOT NULL,
    name     text NOT NULL,
    checksum text NOT NULL,
    PRIMARY KEY (module, version)
);
```

`module_schemas` est écrit par le runner dans la même transaction que la
première migration du module. `schema_sha256` est l'empreinte de l'**identité
de schéma** (module, nom logique, nom physique), pas des migrations ni des
types ou règles du module : ajouter une migration ne change pas cette
identité, et un changement de politique de projet ne change pas le schéma.
Une ligne existante dont le `schema_name` ou le `schema_sha256` ne correspond
plus est une erreur, pas une réécriture silencieuse. Les migrations ont
leurs propres empreintes dans `module_schema_migrations`. Le registre des migrations de module est **séparé** de
`schema_migrations` : une version de module est locale au module (`0001` de
`dev` n'entre pas en collision avec `0001` du noyau), et l'empreinte rend les
migrations de module immuables comme les autres.

### 4.2 Interface d'un module

Le descripteur gagne des faits, pas du comportement :

```python
class Dev:
    NAME = "dev"
    SCHEMA = "dev"              # nom logique ; le nom physique est dérivé
    MIGRATIONS = "migrations"   # dossier relatif au paquet, ou None
    REQUIRES = ()               # noms de modules dont le schéma est requis
```

- `SCHEMA` est optionnel ; par défaut, le nom logique est le nom du module.
  Un module sans table (aujourd'hui `it`) peut l'omettre.
- `MIGRATIONS` est un dossier de fichiers `NNNN_nom.sql`, local au module.
  Un module sans migration retourne `None` ; le runner ne crée alors pas de
  schéma.
- `REQUIRES` ordonne les modules entre eux (un module peut créer une clé
  étrangère vers une table d'un autre module) ; un cycle, un module absent
  ou un nom en double est refusé avant application.
- `schema_sha256` est calculé sur l'identité de schéma (module et nom
  logique) ; les migrations ont leurs propres empreintes dans
  `module_schema_migrations`. `ameesh modules --json` et `doctor` montrent
  la liste des migrations et leurs empreintes, sans confondre le schéma avec
  les types et règles du module.

`ameesh modules --json` gagne des champs **additifs** : `schema` (nom logique),
`schema_actual` (nom physique, quand la base est lisible), `migrations`
(version, nom, empreinte, appliquée ou non), `requires`. Aucun champ existant
n'est retiré ; les consommateurs actuels ignorent les nouveaux champs.
`ameesh doctor` rapporte, par module : schéma présent ou manquant, migrations
en attente, empreintes modifiées, migrations non additives, dépendances
manquantes, et tables de module orphelines (schéma présent sans module
découvert).

Les cartes `Pipeline` du canon restent des modules **de données** : elles
n'ont ni `SCHEMA` ni `MIGRATIONS`, et le runner ne lit jamais un chemin
d'exécution fourni par le canon. Un module externe installé par point
d'entrée peut déclarer un dossier de migrations ; c'est du code installé,
pas du canon.

### 4.3 Runner de migrations

Le runner actuel (`migrations.py`) est étendu, pas remplacé :

1. Découvrir les migrations noyau (`src/ameesh/migrations`), vérifier les
   empreintes des migrations déjà appliquées, refuser une modification.
2. Découvrir les modules et leurs migrations ; vérifier les empreintes ;
   refuser une migration de module modifiée après application. Tout module
   découvert hors profil nu est migré, qu'il soit sélectionné par un projet
   ou non : l'installation est un acte privilégié, et un schéma de module ne
   vaut pas admission de type (la garde `project_modules` reste distincte).
   Un module externe dont on ne veut pas le schéma ne doit pas être installé.
3. Prendre le verrou consultatif existant `ameesh_migrations` et ouvrir la
   transaction ; c'est le même verrou pour le noyau et les modules.
4. Appliquer les migrations noyau en ordre croissant.
5. Appliquer les modules en ordre topologique (`REQUIRES`), puis leurs
   migrations en ordre croissant. Pour chaque module : créer le schéma si
   besoin, insérer `module_schemas`, appliquer les migrations, écrire
   `module_schema_migrations` dans la même transaction.
6. Pour chaque migration de module, poser
   `SET LOCAL search_path = <schéma_module>, <schéma_noyau>, pg_temp`, puis
   restaurer le `search_path` du noyau après la migration. Les objets du
   noyau touchés par une migration de module sont qualifiés explicitement.
7. `--additive-only` s'applique aux deux flux : si une migration noyau ou de
   module est non additive, rien n'est appliqué.
8. Une migration non additive (noyau ou module) exige les exécuteurs arrêtés,
   comme aujourd'hui ; `--allow-live-executors` garde sa raison obligatoire.

Le mode `check`/`doctor` reste en lecture seule : il lit les deux registres
et ne crée rien. Un `schema_migrations` plus récent que l'image (retour
arrière) reste signalé ; le registre de module suit la même règle.

La découverte des migrations de module doit être **locale au module** : un
module ne peut pas lire un dossier hors de son paquet ou de sa distribution.
Les migrations d'un module sont des fichiers SQL, pas du Python exécuté au
moment de la migration.

### 4.4 Numérotation et collisions de branches

Les migrations noyau gardent leur numérotation globale et leur réservation
(`work_migration_reservations`) : deux branches qui ajoutent `0585` se rencontrent et
sont renumérotées au paquet. Les migrations de module sont numérotées
localement (`dev/0001`, `it/0001`) et ne peuvent pas entrer en collision
avec le noyau. Reste la collision entre deux branches qui ajoutent
`dev/0001` : la réservation et le contrôle de collision doivent être étendus
à une clé `(module, version)`, ou les migrations de module doivent adopter
un préfixe temporel (`20261117T1200_...`) qui rend la collision improbable.
La recommandation est la **clé `(module, version)`** : elle reste lisible,
garde l'ordre intra-module et s'applique aux modules externes. C'est un lot
d'outillage de la phase 1, pas un prérequis de la bascule.

### 4.5 Garde SQL des modules

`project_modules` et ses triggers (`ameesh_module_work_guard`,
`ameesh_module_requirements`, `ameesh_module_writer`) restent la garde **par
projet** : types admis, règles, schémas de preuve, écritures. Le schéma d'un
module est **par hôte**, pas par projet : un module découvert et migré peut
avoir des tables même si aucun projet ne le sélectionne, et un projet peut
sélectionner un module dont les tables sont vides. Les deux gardes sont
orthogonales :

- module découvert, schéma migré, aucun projet ne le sélectionne : tables
  présentes, aucune admission de type ; `doctor` le signale comme inactif ;
- projet sélectionne un module, schéma absent ou migration en attente :
  l'admission est refusée (ou `doctor` refuse le démarrage), jamais un
  demi-schéma ;
- désélection d'un module : aucune suppression de schéma ni de données ;
  une purge serait une action destructive séparée, avec go owner.

Un module externe ne reçoit pas de droits supplémentaires sur le schéma
noyau : le runner crée le schéma avec le rôle de la base, et les migrations
de module sont relues comme du SQL sensible. Une fonction `SECURITY DEFINER`
de module doit fixer son `search_path` ; le runner devra refuser
`SECURITY DEFINER` sans `SET search_path`.

### 4.6 Ce que le noyau ne fait plus

Le noyau ne nomme plus une table de module. Il lit une **capacité** dans le
registre (`registry.get("dev")`, déjà utilisé pour `workspaces` et
`git-merge`) ou un port optionnel déclaré par le module. En profil nu, la
capacité est absente : le noyau dégrade (rapport partiel, coût CI nul,
observation vide) au lieu d'échouer sur une table manquante.

Les ports proposés, à préciser dans les lots de code, sont peu nombreux :
`audit_state`, `cost_metrics`, `report_metrics`, `train_state`,
`migration_slots`, `review_withdrawals`, `provenance`, `workspaces`. Un port
est une méthode nommée, sans SQL dans le noyau ; le module peut la
implémenter avec ses tables qualifiées. Le noyau ne connaît ni `ci_runs` ni
`work_landings`.

Toute connexion garde le schéma noyau dans son `search_path` ; le code de
module qualifie ses tables ou ajoute son schéma **après** le schéma noyau.
Un `search_path` réduit au seul schéma du module casserait les triggers de
tables noyau qui nomment encore une table `dev` par son nom court.

Le code déplacé n'écrit pas le nom physique en dur : un résolveur
`relation(name)` lit `module_schemas` et rend le nom courant (`<noyau>.<name>`
avant P2, `<noyau>__dev.<name>` après). C'est ce qui permet à P1b d'être
déployée avant ou après P2 sans changer le code une seconde fois.

Deux tests de frontière rendent la règle mécanique :

- `tests/test_generic_core.py` (ou un test voisin) échoue si un fichier de
  `src/ameesh/` hors `modules/<module>/` nomme une table d'un module, avec
  une liste d'exceptions datées et décroissante (les ports en cours de
  migration) ;
- un test des migrations échoue si une migration noyau postérieure à la
  bascule nomme une table `dev`, ou si une migration de module crée ou
  modifie un objet hors de son schéma sans déclaration explicite.

La liste `REWRITTEN` d'`agent_rename.py` est remplacée par la lecture des
tables déclarées par les modules (ou par une constante générée), pas par une
liste de 36 noms dans le noyau.

### 4.7 Profil nu

Aujourd'hui, `AMEESH_MODULES=bare` ne découvre aucun module intégré :
`registry.get("dev")` retourne `None`, `workspaces` documente un no-op, et
les ports optionnels rendraient des résultats vides. C'est le comportement
attendu du noyau générique.

Mais `bare` est un profil de CI et de test, refusé par `doctor` sur un hôte
(decision 2 de L98 T2b #4408). Et `dev` est `ALWAYS_ACTIVE` : un mesh qui ne
fait pas d'informatique utilise quand même `dev`, donc son schéma et ses
migrations. Le mécanisme de schéma **ne change pas** cette politique : il
range les tables dans un schéma de module, il ne les fait pas disparaître
d'un hôte qui garde `dev`.

Deux niveaux doivent être distingués :

- **Indépendance fonctionnelle** (cette étude) : le noyau ne lit ni n'écrit
  les tables `dev` ; en profil nu, il fonctionne sans elles. C'est ce que
  les ports et les tests de frontière garantissent.
- **Absence physique** (question Q2) : une base neuve en profil nu ne
  contient pas les 36 tables. Les migrations historiques les créent encore ;
  il faudrait une **base de départ par profil** (squash des migrations) ou
  un mécanisme de migration conditionnelle. C'est un chantier distinct, plus
  risqué, qui touche l'immutabilité et les empreintes. La présente étude ne
  le propose pas.

Tant que `dev` reste toujours actif, même l'absence physique ne serait pas
atteinte sur un hôte non informatique : il faut d'abord décider si `dev`
devient sélectionnable (Q1).

### 4.8 Options écartées

| Option | Pourquoi non |
|---|---|
| Garder les tables dans le schéma noyau et déplacer seulement le code | ne répond pas au constat ; le schéma reste commun, les migrations noyau continuent de créer les tables |
| Préfixer les tables (`dev_ci_runs`) | pollue toujours le schéma noyau, casse tous les noms, ne donne pas de frontière de droits ni de `search_path` |
| Un `search_path` par module sans schéma | ne déplace rien ; les tables restent dans le schéma noyau |
| Une base par module | casse les clés étrangères et les transactions communes (lots, revues, provenance) |
| Renommer les tables | casse l'ancien code et les triggers ; une vue ne sauve pas les séquences |
| Copier les données dans un nouveau schéma (`INSERT … SELECT`) | copie et double l'écriture, downtime, risque de perte ; la métadonnée suffit |
| Schéma global `dev` | collision entre plusieurs schémas de test dans une même base ; le nom dérivé du schéma noyau évite cela |

## 5. Bascule sans perte

### 5.1 Vue d'ensemble

| Phase | Nature | Contenu | Ancien code |
|---|---|---|---|
| P0 | étude | cette étude, décisions Q1–Q8 adoptées le 17/11/2026 | inchangé |
| P1a | additive | registres, interface module, runner, tests de frontière ; **aucune table déplacée** | inchangé |
| P1b | code | ports et SQL qualifié dans les modules ; après P1a, avant ou après P2, sans bascule | inchangé |
| P2 | non additive | `ALTER TABLE … SET SCHEMA` des 36 tables, vues de compatibilité, migration de module `dev/0001` | fonctionne via les vues |
| P3 | non additive | retrait des vues par la migration de module `dev/0002`, après une release de P2 et le déploiement de P1b | fonctionne via les ports |
| P4 | optionnelle | base de départ par profil, colonnes `dev` de `work_items`, machine d'états | lot distinct |

Condition de la décision #222989 : seule la phase **additive** P1 (P1a, P1b)
démarre. P2 et P3 ne sont pas additifs et restent **retenus jusqu'au go de
l'owner** (note du bureau du 17/11/2026 sur #7616 ; consigne de l'owner du
17/11 : migrations additives seulement sur les meshs en service).

La règle de compatibilité est **N-1** : la release qui retire les vues ne
supporte que la release précédente, qui doit déjà utiliser les ports ou au
moins les noms qualifiés. Si P1b n'est pas prête, P3 attend ; les vues
peuvent rester plus longtemps, elles ne coûtent qu'un `SELECT *`.

### 5.2 Phase 1a — mécanisme additif

- Migration noyau additive : les trois registres (`module_schemas`,
  `module_schema_migrations`, `module_schema_non_additive_migrations`).
- `Migration` gagne un champ `module` (ou un `ModuleMigration` séparé) ;
  `discover` accepte un dossier de module ; `pending`, `migrate`, `check`
  lisent les deux registres.
- `non_additive()` est refactoré pour lire un texte SQL et non seulement un
  `Migration` noyau ; `_NON_ADDITIVE` gagne
  `ALTER TABLE|SEQUENCE|… SET SCHEMA` (aujourd'hui non détecté) et
  `ALTER TABLE … DROP CONSTRAINT` s'il n'est pas déjà couvert.
- `Module` gagne `schema`, `migrations`, `requires` ; `as_dict()` et
  `ameesh modules --json` les exposent de façon additive.
- `doctor` rapporte l'état des schémas et des migrations de module.
- Tests : découverte, ordre topologique, cycle refusé, empreinte modifiée,
  `--additive-only`, `SET SCHEMA` classé non additif, profil nu sans module.
- Aucun déplacement de table ; les tests existants passent.

### 5.3 Phase 2 — bascule des tables

La bascule est une **migration de module `dev/0001`**, marquée non additive,
appliquée avec les exécuteurs arrêtés, dans une seule transaction. C'est une
**exception de transition assumée** : cette migration de module déplace des
tables créées par l'histoire du noyau ; elle est revue comme telle et ne crée
pas de précédent pour les migrations de module suivantes. Le runner pose
`<schéma_module>, <schéma_noyau>, pg_temp` en tête du `search_path` ; la
migration qualifie donc le schéma noyau explicitement.

```sql
-- ameesh: non additive
-- Bascule de métadonnées : aucune ligne n'est copiée ni supprimée.
CREATE SCHEMA IF NOT EXISTS <schéma_noyau>__dev;

DO $$
DECLARE
  core text := '<schéma_noyau>';
  mod  text := '<schéma_noyau>__dev';
  t    text;
BEGIN
  FOREACH t IN ARRAY ARRAY['ci_runs', 'ci_test_failures', ...] LOOP
    EXECUTE format('ALTER TABLE %I.%I SET SCHEMA %I', core, t, mod);
    EXECUTE format('CREATE VIEW %I.%I AS SELECT * FROM %I.%I', core, t, mod, t);
  END LOOP;
END $$;
```

**Droits.** `ALTER TABLE … SET SCHEMA` conserve les ACL de la table, mais le
schéma est un objet nouveau et la vue est un objet nouveau. La migration doit
donc, dans la même transaction :

- `GRANT USAGE ON SCHEMA <schéma_noyau>__dev` aux rôles qui écrivent les
  tables (rôle de la base, rôles de login limité, rôles de module) ;
- recopier sur chaque vue les privilèges de la table déplacée (lire
  `aclexplode(relacl)` et accorder les mêmes `SELECT/INSERT/UPDATE/DELETE`
  au même `grantee`), sinon un rôle qui écrivait la table directement
  perdrait l'accès par la vue.

Le code noyau qui passe aux tables qualifiées reçoit ces droits dans la même
migration ; l'ancien code, lui, passe par la vue et n'a pas besoin d'`USAGE`
sur le schéma du module.

Points vérifiés sur PostgreSQL 17.11 (schémas de test, transaction annulée) :

| Opération | Résultat |
|---|---|
| `ALTER TABLE … SET SCHEMA` | déplace la table, ses index, ses contraintes, ses triggers et les séquences `OWNED BY` |
| `CREATE VIEW` de compatibilité | la vue est auto-modifiable (`INSERT`, `UPDATE`, `DELETE`) |
| `INSERT … ON CONFLICT (colonne)` via la vue | fonctionne (inférence de colonne) |
| `UPDATE … FROM`, `DELETE … USING` via la vue | fonctionnent |
| `SELECT … FOR UPDATE`, `RETURNING` via la vue | fonctionnent |
| `TRUNCATE` via la vue | échoue (`"…" is not a table`) ; aucun usage runtime, quelques tests à adapter |
| Clé étrangère inter-schémas | conservée par OID après le déplacement |
| Séquence explicite non `OWNED BY` | **ne bouge pas** ; `merge_queue_generation_seq` reste dans le schéma noyau |

La migration de module doit aussi traiter les triggers et fonctions
transverses :

- `work_items_migration_slot_sync` (sur `work_items`), `ameesh_review_seats_guard`
  (sur `work_items`) et `ameesh_review_verdict_guard` (sur
  `work_item_milestones`) sont des triggers de tables noyau qui nomment des
  tables `dev`. Pendant P2, les vues les font fonctionner sans changement ;
  la migration peut aussi déplacer leurs fonctions dans le schéma `dev`
  (`ALTER FUNCTION … SET SCHEMA`), les triggers suivant par OID. Le choix
  est à trancher dans la revue : garder les fonctions noyau (plus simple,
  mais elles dépendent du schéma `dev`) ou les déplacer (plus propre, mais
  le profil nu sans schéma `dev` échouerait sur ces triggers).
- `ameesh_agent_spelling` est attaché aux tables `dev` (ou à leurs
  colonnes) : il lit `NEW`, suit le déplacement de la table et n'a pas
  besoin d'être réécrit.
- `zz_ameesh_limited_login` suit aussi la table, mais **pas sa fonction** :
  `ameesh_limited_login_guard` lit `limited_logins` dans `tg_table_schema`.
  Après déplacement de `provenance_receipts`, `tg_table_schema` vaut le
  schéma du module et `limited_logins` (restée noyau) n'y est plus. La
  bascule doit soit recréer le trigger avec le schéma noyau en argument
  (`tg_argv[0]`, avec `coalesce(tg_argv[0], tg_table_schema)` dans la
  fonction), soit faire lire `limited_logins` dans le schéma noyau à la
  fonction. Un test de login limité écrivant `provenance_receipts` via la
  vue est requis.
- `merge_queue_generation_seq` reste dans le schéma noyau. Le code continue
  de la référencer sans qualification : le `search_path` noyau la trouve.
  Si elle doit un jour rejoindre `dev`, il faudra d'abord qualifier le nom
  dans une release, puis la déplacer dans la suivante (détail §5.6).

Les vues sont créées avec `SELECT *` : elles figent la liste des colonnes à
la bascule. Une migration de module ultérieure qui ajoute une colonne ne
l'expose pas à l'ancien code, ce qui est correct (l'ancien code ne la
connaît pas). Si une migration de module change un type ou retire une
colonne, elle est non additive et relue comme telle ; l'ancien code peut
alors échouer, même via la vue : c'est la règle N-1 qui s'applique, pas un
effet de la vue.

### 5.4 Phase 3 — retrait des vues

Après une release de P2 et le déploiement de P1b :

- les vues de compatibilité sont supprimées par une **migration de module
  `dev/0002`** non additive (`DROP VIEW`, pas `DROP TABLE`). Le retrait doit
  être une migration de module et non une migration noyau : le runner
  applique toutes les migrations noyau avant celles des modules, donc une
  migration noyau de P3 s'exécuterait, sur une base neuve, avant `dev/0001`
  qui crée les vues, et ne les retirerait pas ;
- les tests qui visaient un nom de vue pour `TRUNCATE` visent la table de
  base (`<schéma>__dev.<table>`) ; les `TRUNCATE` de tables noyau avec
  `CASCADE` continuent de fonctionner ;
- les exceptions du test de frontière sont retirées ;
- les fonctions noyau qui nommaient encore un nom court sont qualifiées ou
  déplacées. Sont concernées au minimum `work_items_migration_slot_sync`,
  `work_migration_reservation_guard`, `ameesh_review_seats_guard` et
  `ameesh_review_verdict_guard` ; un test écrit sur chacune des tables
  concernées après P3 ;
- `merge_queue_generation_seq` reste noyau (décision Q7), sauf lot ultérieur.

Aucune donnée n'est supprimée : on ne retire que les vues.

### 5.5 Retour arrière

Un fichier `rollback/<version>_module_schema_cutover.sql` est écrit, testé
sur une copie avec des données, et **jamais appliqué automatiquement** (même
convention que `rollback/0518`). Il :

1. `DROP VIEW` des 36 vues (seulement si elles existent) ;
2. `ALTER TABLE <schéma>__dev.<table> SET SCHEMA <schéma_noyau>` pour les 36
   tables ;
3. restaure les triggers/fonctions déplacés dans leur schéma d'origine ;
4. `DROP SCHEMA <schéma>__dev` seulement s'il est vide (aucun objet restant) ;
5. ne touche à aucune ligne.

Le registre `module_schema_migrations` doit suivre le retour arrière : le
script retire la ligne de la migration qu'il annule (`dev/0002` pour un
retour de P3, `dev/0001` pour un retour de P2), sinon une nouvelle montée
considérerait la migration comme déjà appliquée. Pour un retour de P2, il
retire aussi la ligne `module_schemas` si le schéma est supprimé. Ces
suppressions portent sur des lignes de registre, pas sur des données
métier ; elles sont journalisées dans la preuve du geste.

Le retour arrière de P3 vers P2 demande de **recréer les vues** (le script
de P2 peut être rejoué en mode « vues seulement ») : le code P2 utilise les
vues, que P3 a supprimées. Le retour arrière de P2 vers P1 utilise le script
complet. Le retour arrière de P3 vers P1 n'est pas supporté sans recréer les
vues : c'est la limite N-1.

### 5.6 Séquences et objets non couverts par une vue

Une vue ne couvre pas une séquence. `merge_queue_generation_seq` est créée
par 0412 sans `OWNED BY` et référencée sans qualification dans
`storage/postgres/work.py` (`nextval('merge_queue_generation_seq')`). Si elle
migrait avec la table par un `ALTER TABLE`, elle resterait dans le schéma
noyau ; c'est le comportement actuel et il est sûr pour l'ancien code.

Pour la déplacer un jour :

1. release A : qualifier le nom dans le code (`<schéma>__dev.merge_queue_generation_seq`)
   et vérifier que l'ancien code (release A-1) utilise encore le nom court ;
2. release B : `ALTER SEQUENCE … SET SCHEMA <schéma>__dev` ; l'ancien code
   A utilise le nom qualifié, donc fonctionne ; A-1 n'est plus supporté ;
3. ou garder la séquence dans le schéma noyau : ce n'est pas une table, le
   lot ne l'exige pas.

La recommandation est de **garder la séquence dans le schéma noyau** pour
cette bascule (Q7). Elle est petite, sans données propres, et le code la
nomme déjà sans qualification.

### 5.7 Manaty, IMA/TPK

La migration est la même pour toutes les bases ; le déroulé est par base :

1. **Préflight** : version de l'image, tête de `main`, état des migrations
   (`ameesh doctor --probe`), nombre de lignes par table `dev`, taille du
   schéma, transactions longues, exécuteurs vivants.
2. **Sauvegarde** : `pg_dump` complet (ou sauvegarde physique / PITR) et
   vérification de la restauration sur une copie ; le script de rollback est
   rejoué sur cette copie avec des données.
3. **Arrêt** : exécuteurs arrêtés ; aucun autre écrivain ; `lock_timeout`
   court pour échouer sans bloquer.
4. **Bascule** : `ameesh migrate` (non additif, exécuteurs arrêtés) ; une
   transaction ; si une table est verrouillée, rien n'est appliqué.
5. **Contrôles** : nombre de lignes avant/après par table, `SELECT` via les
   vues, `INSERT/UPDATE/DELETE` via une vue sur une base de test, triggers
   de slot et de revue, clés étrangères, séquences.
6. **Redémarrage** et smoke réel : un lot, une revue, une CI, un paquet.
7. **Observation** 24 h ; puis P3 à la release suivante.

Ordre proposé : **Manaty d'abord** (pilote), puis **IMA/TPK** après une
période d'observation d'au moins une release. Aucune de ces actions n'est
faite dans ce lot ; le plan est une proposition à valider par l'owner et le
bureau.

### 5.8 Preuves de non-perte

- `ALTER TABLE … SET SCHEMA` est une opération de catalogue : aucune ligne
  n'est lue, copiée ou réécrite. Les 36 tables gardent leurs OID, leurs
  données, leurs index, leurs contraintes et leurs triggers.
- Les vues sont des objets vides ; les créer ne duplique aucune donnée.
- Aucun `DROP TABLE`, `DROP COLUMN`, `DELETE`, `TRUNCATE` dans la bascule.
- Les séquences `OWNED BY` suivent la table ; la séquence explicite reste.
- Les clés étrangères inter-schémas restent valides (vérifié).
- Le rollback ne fait que déplacer et supprimer des vues ; il ne touche pas
  aux lignes.
- Les compteurs avant/après et un `pg_dump` de contrôle sont conservés dans
  la preuve du lot de bascule.

### 5.9 Risques et parades

| Risque | Parade |
|---|---|
| `ALTER TABLE` bloque sur une table active | exécuteurs arrêtés, `lock_timeout`, une transaction, rien n'est appliqué si échec |
| Ancien code cassé par un nom déplacé | vues de compatibilité, testées sur les 7 opérations DML de §5.3 |
| Trigger noyau cassé par le déplacement | vues pendant P2 ; fonctions déplacées ou qualifiées ; test des triggers slot/revue |
| `TRUNCATE` de test sur une vue | recenser les ~25 fichiers de test concernés et viser la table de base |
| `SELECT *` figé | les colonnes ajoutées après P2 ne sont pas exposées à l'ancien code ; pas de besoin avant P3 |
| Séquence explicite oubliée | elle reste dans le schéma noyau ; documentée et testée |
| Rollback impossible | script testé sur copie avec données ; `DROP` uniquement de vues ; schéma non supprimé s'il n'est pas vide |
| Profil nu sans tables | P1b rend le noyau indépendant ; l'absence physique est P4, pas P2 |
| Module externe non revu | migration de module = SQL sensible, deux familles ; `SECURITY DEFINER` avec `search_path` obligatoire |
| ACL perdues sur une vue | recopie des privilèges de la table sur la vue, `GRANT USAGE` sur le schéma, contrôle par rôle |
| RLS sur une table déplacée | vérifier l'absence de politique RLS sur les 36 tables ; sinon `security_invoker` ou politique équivalente |
| `zz_ameesh_limited_login` après déplacement | passer le schéma noyau en `tg_argv[0]` ou faire lire `limited_logins` au noyau ; test de login limité |

## 6. Décisions demandées

| # | Question | Recommandation |
|---|---|---|
| Q1 | Le but est-il un **hôte réel sans `dev`** (mesh non informatique), ou seulement le profil nu de CI/test ? | Séparer : cette étude livre l'indépendance fonctionnelle ; un lot de politique distinct rend `dev` sélectionnable si un hôte réel sans `dev` est voulu. Ne pas changer `ALWAYS_ACTIVE` ici. |
| Q2 | Le profil nu doit-il être **physiquement** sans les 36 tables sur une base neuve ? | Non dans cette bascule : les migrations historiques les créent. Une base de départ par profil est un lot distinct (P4), à cause de l'immutabilité des migrations. |
| Q3 | Les douze colonnes `dev` de `work_items` sont-elles dans ce lot ? | Non : lot suivant, après la bascule des tables ; sinon la frontière reste dans la table noyau. |
| Q4 | `delivered` doit-il rester inatteignable sans `dev` ? | Non : un connecteur de livraison noyau (`document`, `done`) devrait pouvoir produire un `delivered` générique, ou `delivered` devient un état `dev`. Décision de machine d'états, lot séparé. |
| Q5 | Les modules externes peuvent-ils déclarer des migrations ? | Oui, code installé et revu comme SQL sensible ; le canon ne fournit jamais de chemin d'exécution. Un module sans migration reste valide (`it`). |
| Q6 | Durée des vues de compatibilité ? | Une release (N-1) après P2, retrait en P3 quand P1b est déployée. Si P1b glisse, les vues restent. |
| Q7 | `merge_queue_generation_seq` : noyau ou `dev` ? | Noyau pour cette bascule ; déplacement ultérieur avec la méthode qualifier-puis-déplacer, ou abandon au profit d'une identité. |
| Q8 | Nommage `<schéma_noyau>__<module>` ? | Oui : évite les collisions entre schémas de test, reste lisible, et le nom physique est dans le registre. |

### Décision #222989 (owner, 17/11/2026)

La demande a été confirmée par l'owner : décision #222989, option **a**,
signée par `human:smichea` le 2026-11-17 à 03:34Z (approbation #377, clé
owner, source `pc-smichea:710c45bf`, réponse relayée par le bureau). Texte
enregistré de la réponse :

> a — réponse de l'owner relayée par le bureau, source pc-smichea:710c45bf

L'owner adopte les recommandations **Q1–Q8 telles qu'écrites** et découpe la
suite en **P1a, P1b, P2 et P3** (lots #7649 à #7652, §7).

Portée du go, consignée par le bureau le 17/11/2026 à 03:34 sur #7616 :
**seule la phase additive P1 (P1a, P1b) démarre**. P2 et P3 sont découpés
mais **retenus jusqu'au go de l'owner**, car ils ne sont pas additifs
(consigne de l'owner du 17/11 sur ce lot : migrations additives seulement
sur les meshs en service). La présente étude ne déplace donc aucune table :
elle est la spécification de P1a et P1b, et le plan de P2/P3 reste prêt,
non exécuté.

## 7. Découpage adopté

Décision #222989 (option a, 17/11/2026) : P1a, P1b, P2 et P3 sont découpés
en lots.

| Phase | Lot | Contenu | Condition |
|---|---|---|---|
| P1a | #7649 | Mécanisme de schéma et de migrations de module : registres, interface, runner, `doctor`, `non_additive` `SET SCHEMA`, tests. Aucune table déplacée. SQL sensible. | additif ; après #7616 |
| P1b | #7650 | Code générique : ports et SQL qualifié. Stockage `dev` dans `modules/dev`, ports, consommateurs noyau, test de frontière, `REWRITTEN`. | additif ; après #7616 et #7649 |
| P2 | #7651 | Bascule des tables : migration `dev/0001`, vues, triggers, rollback, Manaty puis IMA/TPK. SQL sensible. | non additif, **retenu jusqu'au go owner** ; après #7616 et #7649 |
| P3 | #7652 | Retrait des vues : `DROP VIEW`, tests, exceptions, migration `dev/0002`. SQL sensible. | non additif, **retenu jusqu'au go owner** ; après #7650 et #7651 |

Restent de simples propositions, non ouvertes :

5. **Colonnes `dev` de `work_items` et machine d'états** (P4, SQL sensible) :
   lot suivant, séparé (Q3, Q4).
6. **Base de départ par profil** (P4, optionnel) : squash des migrations ou
   baseline ; seulement si Q2 devient « oui ».
7. **Activation de `dev`** (politique) : seulement si Q1 devient « hôte réel
   sans `dev` ».

## 8. Sources et preuves

- Lot #7616, corps de la demande (Sébastien, 16/11/2026), issue
  manaty/ameesh#5802.
- Consigne #222966 (`ameesh`, 16/11/2026 23:01).
- Décision #222989 (option a), signée par `human:smichea` le 2026-11-17 à
  03:34Z, approbation #377, source `pc-smichea:710c45bf` ; note du bureau du
  17/11/2026 sur #7616 (P2 et P3 retenus jusqu'au go owner).
- Lots #7649 (P1a), #7650 (P1b), #7651 (P2), #7652 (P3), ouverts par
  `mesh-design` le 17/11/2026 à 03:37.
- `src/ameesh/modules/__init__.py`, `registry.py`, `storage.py` ;
  `src/ameesh/migrations.py` ; `src/ameesh/work_lifecycle.py`.
- Migrations citées : 0013, 0412, 0419, 0432, 0456, 0462, 0467, 0516,
  0518, 0519, 0524, 0529, 0534, 0564, 0567, 0580, 0581, 0582, 0583,
  0584.
- `docs/design/etudes/project-modules.md`, `l243-projects-and-names.md`,
  `1467-trains-integration.md`.
- Inventaire : `docs/design/etudes/7616-schema-des-modules/inventaire.tsv`
  (174 lignes, code à `1ac9cb6a1`).
- Vérification PostgreSQL 17.11 sur la base de test locale
  `agent_mesh` (schémas `t_7616_*`, transaction annulée) : DML via vue,
  `ON CONFLICT`, `FOR UPDATE`, `RETURNING`, clé étrangère inter-schémas,
  séquence `OWNED BY` déplacée, séquence explicite non déplacée,
  `TRUNCATE` refusé sur une vue.
