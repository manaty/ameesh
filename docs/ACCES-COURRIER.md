# Accès au courrier des agents (`agent_mailbox`) par rôle PostgreSQL

**Lot** : #6337 — inventaire des rôles LOGIN qui lisent `agent_mailbox.body`,
`.meta` et `.payload` au-delà d'`ameesh`.
**Relevé** : 2026-11-08T18:03Z, base centrale `ameesh`, PostgreSQL 17.11,
schéma `public`.
**Mode** : lecture seule — catalogues (`pg_roles`, `pg_auth_members`,
`pg_class`, `pg_attribute`), `aclexplode` et fonctions `has_*_privilege` ;
aucun contenu de message lu, aucune écriture.

## Pourquoi

`agent_mailbox` ne porte pas de RLS (`relrowsecurity = false`) : un `SELECT`
sur la table, ou sur une colonne, lit **toutes** les lignes, messages humains
compris. Le lot #6329 a borné le rôle `vm_probe` ; il restait à savoir qui
d'autre lit le courrier et si ce droit correspond à son besoin réel.

## Résultat

| Rôle LOGIN | Attributs | Voie d'accès | `SELECT` table | `body` | `meta` | `payload` | Besoin |
|---|---|---|---|---|---|---|---|
| `ameesh` | LOGIN INHERIT ; propriétaire de la table et de la base | propriétaire (ACL complète) | oui | oui | oui | oui | **hors périmètre** : rôle applicatif propriétaire |
| `ameesh-test` | LOGIN INHERIT | aucune | non | non | non | non | rôle de test ; aucun besoin de courrier |
| `ameesh_host_agents1` | LOGIN INHERIT, `search_path=public` | membre de `ameesh_executor` (ACL de table `arwd`) | oui | oui | oui | oui | rôle de connexion prévu pour l'exécuteur de l'hôte `ameesh-agents-1` ; le code de livraison lit/écrit le courrier des personas de l'hôte |
| `ameesh_release_controller_agents_1_3392` | LOGIN INHERIT | membre de `ameesh_release_controller` (ACL de table `r`) | oui | oui | oui | oui | contrôleur de release ; besoin borné à la demande de décision revalidée |
| `ameesh_release_controller_agents_3_3392` | LOGIN INHERIT | membre de `ameesh_release_controller` (ACL de table `r`) | oui | oui | oui | oui | idem |
| `auditeur_lecture` | LOGIN INHERIT, `default_transaction_read_only=on`, `jit=off` | membre de `ameesh_superviseur` (colonnes d'état) et `ameesh_superviseur_contenus` (ACL de colonnes `body`, `meta`, `payload`) | non | oui | oui | oui | audit interne ; lecture des contenus par contrat |
| `vm_probe` | LOGIN NOINHERIT | aucune ACL table/colonne ; `EXECUTE` sur `vm_probe_send` et `vm_probe_receipt` (SECURITY DEFINER) | non | non | non | non | sonde VM ; seuls les faits rendus par les fonctions bornées |

`SELECT` table mesure le droit de **table** (toutes les colonnes) ; `body`,
`meta` et `payload` mesurent le droit **effectif de colonne**, y compris quand
il vient d'un droit de table. C'est pourquoi `auditeur_lecture` a « non » en
table mais « oui » aux trois colonnes.

`ameesh` n'est pas superuser : il possède la table et la base, ce qui lui
donne tous les droits sur le courrier. `postgres` est superuser (hors
périmètre). Les rôles `ameesh_executor`,
`ameesh_release_controller`, `ameesh_superviseur`,
`ameesh_superviseur_contenus` et `vm_probe_writer` sont NOLOGIN : ils ne se
connectent pas, mais portent les droits que leurs LOGIN membres héritent.

## Justification par besoin

### `ameesh_host_agents1` — exécuteur de l'hôte, lecture large par construction

`deploy/sql/role-executor.sql` accorde au groupe `ameesh_executor`
`SELECT, INSERT, UPDATE, DELETE` sur toutes les tables du schéma, donc sur
`agent_mailbox` (ACL de table `arwd`). Le rôle de connexion est provisionné
pour l'exécuteur de l'hôte ; aucune session n'était ouverte sous ce nom au
relevé (le mesh se connecte encore sous `ameesh`). Le code de livraison
(`src/ameesh/storage/postgres/mailbox.py`) lit `body`, `payload`, `meta`
(consigne, renvoi, etc.) et écrit les colonnes correspondantes.

Un seul LOGIN sert plusieurs personas sur l'hôte. Un `GRANT` de colonne ou une
vue ne peut pas restreindre les **lignes** ; sans RLS ni un LOGIN par persona,
le droit de table reste large. C'est un choix d'architecture (frontière de
confiance de l'hôte), pas une réduction de droits possible en l'état.

### `ameesh_release_controller_*` — lecture plus large que le besoin

`deploy/sql/role-release-controller.sql` accorde `SELECT` sur la table entière
à `ameesh_release_controller`, donc `body`, `meta`, `payload` et le matériel de
signature. Le besoin du contrôleur est plus étroit : `deploy/mise-a-jour/autorite-release.py`
(`SignedDecisionAuthority`) revalide **une** demande de décision par son id.
`decisions.get` ne lit que `id, sender, recipient, body, payload, work_item_id,
status, host, created_at, delivered_at` ; `verify_proof` lit les approbations,
les clés et les nonces, jamais `agent_mailbox.meta` ni `signature`,
`signed_payload`, `nonce` ou `slack_ts`. Le contrôleur n'a pas besoin de lire
les autres lignes du courrier.

Les deux LOGIN sont provisionnés pour l'enrôlement contrôleur (#3392/#5281) ;
aucune session n'était ouverte sous ces rôles au relevé. Le lot de réduction
**#6372** (backlog, valeur 45) porte la cible : une fonction SECURITY DEFINER
bornée, comme `vm_probe_receipt` de #6329, et le retrait de tout `SELECT` de
`ameesh_release_controller` sur `agent_mailbox`.

### `auditeur_lecture` — lecture des contenus par contrat

Le rôle de connexion de l'auditeur interne est membre des deux contrats de
supervision : `ameesh_superviseur` (état) et `ameesh_superviseur_contenus`
(`body`, `meta`, `payload`). C'est la permission explicite de l'audit
(`deploy/sql/role-auditeur.sql`, `deploy/sql/role-superviseur-contenus.sql`) :
les requêtes d'audit lisent le courrier pour mesurer les délais, les copies,
les interventions humaines et les demandes de décision. Le rôle est en lecture
seule (`default_transaction_read_only=on`), sans droit direct sur le schéma.

### `vm_probe` — aucune lecture directe

Le rôle de connexion n'a ni `SELECT` de table ni `SELECT` de colonne sur
`agent_mailbox`. Il n'a que l'`EXECUTE` sur `vm_probe_send` et
`vm_probe_receipt`, deux fonctions SECURITY DEFINER dont le propriétaire
`vm_probe_writer` est NOLOGIN. `vm_probe_writer` porte des `SELECT` de colonnes
(`body`, `meta`, `payload`) et des `INSERT` bornés, mais aucun LOGIN n'en est
membre : ces droits ne s'exercent que dans le corps des fonctions, contrôlé par
le contrat `deploy/sql/_contrat-vm-probe.sql`.

### `ameesh-test` — aucun droit

Le rôle de test n'a aucun droit sur `agent_mailbox` (ni table, ni colonne). Il
peut se connecter et utiliser le schéma, sans accès au courrier.

## Constats et suite

- Quatre rôles LOGIN hors `ameesh` lisent `body`, `meta` et `payload` :
  `ameesh_host_agents1` (par construction), les deux
  `ameesh_release_controller_*` (plus large que le besoin) et
  `auditeur_lecture` (par contrat d'audit). Deux n'y ont aucun accès :
  `ameesh-test` et `vm_probe`.
- Les rôles LOGIN d'exécuteur et de contrôleur sont provisionnés mais aucune
  session n'était ouverte à leur nom au relevé : l'inventaire porte sur les
  droits **accordés**, pas sur l'usage courant.
- Aucun `GRANT` à `PUBLIC` sur `agent_mailbox` ; aucun rôle LOGIN hors
  `ameesh` ne possède la table ni n'a de privilège par défaut sur elle. Le
  seul privilège par défaut du schéma qui touche le courrier est celui
  d'`ameesh_executor` sur les futures tables créées par `ameesh` (conséquence
  de `role-executor.sql`).
- Aucun rôle LOGIN hors `ameesh` n'est membre d'un rôle prédéfini de lecture
  des données (`pg_read_all_data`) ni de `pg_database_owner` ; `ameesh` possède
  la base (donc `pg_database_owner`) et n'est membre que de
  `pg_read_all_stats` pour les statistiques.
- La réduction au besoin réel est le lot **#6372** pour le contrôleur de
  release. Pour l'exécuteur, la réduction par `GRANT` n'existe pas : elle
  demanderait un LOGIN par persona et une RLS, donc une conception séparée.
- Rejouer les requêtes de l'annexe après toute modification de rôle ou de
  `GRANT`, et mettre à jour ce document après #6372.

## Annexe — requêtes du relevé

```sql
-- 1. Rôles LOGIN non superuser et attributs
SELECT rolname, rolinherit, rolconfig
  FROM pg_roles
 WHERE rolcanlogin AND NOT rolsuper
 ORDER BY rolname;

-- 2. ACL de la table et RLS
SELECT c.relowner::regrole AS proprietaire,
       c.relrowsecurity, c.relforcerowsecurity,
       c.relacl
  FROM pg_class c
 WHERE c.oid = 'agent_mailbox'::regclass;

-- 3. ACL des colonnes de body/meta/payload
SELECT a.attname, a.attacl
  FROM pg_attribute a
 WHERE a.attrelid = 'agent_mailbox'::regclass
   AND a.attname IN ('body', 'meta', 'payload')
 ORDER BY a.attnum;

-- 4. Appartenances (INHERIT et SET ROLE)
SELECT r.rolname AS role_login, g.rolname AS role_reachable,
       pg_has_role(r.oid, g.oid, 'USAGE') AS herite,
       pg_has_role(r.oid, g.oid, 'MEMBER') AS set_role
  FROM pg_roles r
  JOIN pg_roles g ON g.oid <> r.oid
 WHERE r.rolcanlogin AND NOT r.rolsuper
   AND pg_has_role(r.oid, g.oid, 'MEMBER')
   AND g.rolname NOT LIKE 'pg\_%'
 ORDER BY 1, 2;

-- 5. Droits effectifs sur le courrier
WITH logins AS (
  SELECT oid, rolname FROM pg_roles WHERE rolcanlogin AND NOT rolsuper
)
SELECT l.rolname,
       has_table_privilege(l.oid, 'agent_mailbox', 'SELECT') AS select_table,
       has_column_privilege(l.oid, 'agent_mailbox', 'body', 'SELECT') AS body,
       has_column_privilege(l.oid, 'agent_mailbox', 'meta', 'SELECT') AS meta,
       has_column_privilege(l.oid, 'agent_mailbox', 'payload', 'SELECT') AS payload
  FROM logins l
 ORDER BY l.rolname;

-- 6. PUBLIC et privilèges par défaut du schéma
SELECT x.grantee::regrole AS grantee, x.privilege_type
  FROM aclexplode((SELECT relacl FROM pg_class
                    WHERE oid = 'agent_mailbox'::regclass)) x
 WHERE x.grantee = 0;
SELECT d.defaclrole::regrole AS proprietaire,
       d.defaclnamespace::regnamespace AS schema,
       d.defaclobjtype, x.grantee::regrole AS grantee, x.privilege_type
  FROM pg_default_acl d
  CROSS JOIN LATERAL aclexplode(d.defaclacl) x
 ORDER BY 1, 2, 3, 4, 5;

-- 7. Rôles prédéfinis et propriétaire de la base
SELECT r.rolname AS role_login, g.rolname AS role_predefini
  FROM pg_roles r
  JOIN pg_roles g ON g.oid <> r.oid
 WHERE r.rolcanlogin AND NOT r.rolsuper
   AND g.rolname LIKE 'pg\_%'
   AND pg_has_role(r.oid, g.oid, 'MEMBER')
 ORDER BY 1, 2;
SELECT datname, datdba::regrole AS proprietaire
  FROM pg_database WHERE datname = current_database();
```
