# Bascule du stockage de développement (P2)

`dev/0001_module_schema_cutover` est non additive. Le go signé #228379 b
n'autorise que le code et les preuves sur copie. Le fichier est livré dans
le paquet, mais `dev.MIGRATIONS` reste `None` : le tic ne la découvre pas,
ne l'applique pas et son préflight additif reste ouvert. L'activer dès cette
release ferait refuser tout staging, même sans application de la migration.
Les tests activent le dossier explicitement sur leurs copies isolées.

L'application à chaque base demande une nouvelle autorisation owner et un
déroulé validé par le bureau (étude #7616, §5.7). La déclaration du dossier
`MIGRATIONS = "migrations"` appartient à ce geste ultérieur et doit figurer
dans la version validée pour la bascule. Une fois activée, la migration est
visible dans `doctor` et refusée par `--additive-only`, sans déplacement ni
inscription. Ne pas activer la déclaration dans une release de staging tant
que ses bases n'ont pas reçu leur go et leur bascule contrôlée.

La migration couvre les 36 tables déclarées par P1b et
`integration_train_requalifications` (0592), classée dans le module `dev`.
Les tables noyau, notamment `work_items`, restent en place. Les fonctions de
trigger restent dans le noyau ; leurs références courtes passent par les
vues durant P2. La séquence explicite `merge_queue_generation_seq` reste
noyau (Q7). Les séquences `OWNED BY`, index, contraintes et triggers suivent
les tables par OID.

## Préparation par base

1. Consigner la version exacte et le mode de la base cible, les empreintes
   des migrations, `doctor --probe`, les transactions longues et l'inventaire
   des exécuteurs et autres écrivains. P1a doit être appliquée. Vérifier les
   37 tables de `modules/dev/tables.py`, leurs compteurs, ACL, politiques RLS,
   séquences et clés étrangères. La migration refuse une RLS inattendue.
2. Faire une sauvegarde complète, restreinte à son opérateur, et restaurer
   cette sauvegarde sur une base isolée. Une sauvegarde non restaurée ne
   constitue pas la preuve requise. Sur cette copie avec données, appliquer
   P2, comparer les compteurs et contenus, puis exécuter le retour arrière
   et comparer à nouveau. Conserver sauvegarde, empreinte et résultats dans
   le dossier de preuve du geste, hors dépôt produit.
3. Faire valider le déroulé par le bureau, puis obtenir le go owner propre à
   cette base, en lui présentant la preuve de restauration et de retour
   arrière. Prévoir la fenêtre et l'opérateur du retour arrière.

Le test `tests.test_7651_dev_schema_cutover` restaure un vrai `pg_dump` P1
avec données dans un autre schéma isolé, applique P2 et rejoue le rollback.
Il contrôle aussi les lignes, OID, contraintes et ACL des 37 tables, les
écritures N-1 via les vues, le résolveur P1b, les triggers de réservation et
les reçus d'une connexion limitée. Ces preuves de code ne remplacent pas la
restauration de la sauvegarde de la base cible.

## Geste autorisé

1. Arrêter tous les exécuteurs et autres écrivains ; fermer leurs connexions.
   Le résolveur de P1b conserve le schéma dans le cache de la connexion : les
   connexions ouvertes avant la bascule doivent être remplacées.
2. Vérifier encore l'identité de la base et son schéma noyau. Lancer le
   `ameesh migrate` de la release validée, avec les modules découverts, sans
   `--additive-only` et sans dérogation pour exécuteurs vivants. Examiner
   d'abord toutes les migrations en attente : la commande applique la passe
   entière. Le runner utilise une transaction, un verrou consultatif et
   `lock_timeout = 5s`. Une erreur annule toute la passe.

   Après activation explicite de `MIGRATIONS` dans la release approuvée et
   configuration de la base cible, la commande est :

   ```sh
   ameesh migrate
   ```
3. Comparer chaque compteur et empreinte de données avant/après ; vérifier
   les clés étrangères inter-schémas, les ACL de table et de colonne, les
   séquences possédées et la séquence explicite restée noyau. Conserver un
   `pg_dump` de contrôle. Vérifier les registres `module_schemas`,
   `module_schema_migrations` et `module_schema_non_additive_migrations`.
4. Faire un smoke réel du courrier, de la prise d'un lot, des réservations
   de migration, de la revue et des reçus de provenance sous le rôle limité.
   Reprendre les exécuteurs avec des connexions neuves ; observer 24 h.
5. La première base précède les autres d'au moins une release. Aucun autre
   déploiement ne déclenche implicitement cette migration.

Les vues conservent le propriétaire et les droits DML de table et de
colonne, y compris les options de délégation. Les ACL héritées des droits
par défaut sont retirées avant cette copie. Les vues ordinaires sont
nécessaires aux lectures limitées à certaines colonnes : une vue
`security_invoker` portant un `SELECT *` exige les droits sur toutes les
colonnes sous-jacentes. Toute introduction future de RLS exige donc une
nouvelle revue de cette compatibilité. `TRUNCATE` doit viser les tables de
base, jamais les vues. Le retrait des vues appartient à P3, après une
release de P2 et le déploiement de P1b.

## Retour P2 vers P1

Le fichier manuel est
`src/ameesh/modules/dev/migrations/rollback/0001_module_schema_cutover.sql`.
Il ne fait pas partie de la découverte des migrations. Avec le go requis,
les écrivains arrêtés et le `search_path` fixé au schéma noyau, l'opérateur
l'exécute en entier avec arrêt à la première erreur. Le fichier contient sa
transaction, son verrou et son délai de verrouillage : ne pas l'englober
avec une commande qui pourrait continuer après une erreur.

Depuis la racine de la release approuvée, avec `DATABASE_URL` pointant sur la
base cible et `PGOPTIONS` configuré pour placer son schéma noyau en premier
dans le `search_path`, la commande est :

```sh
psql "$DATABASE_URL" -X -v ON_ERROR_STOP=1 -f src/ameesh/modules/dev/migrations/rollback/0001_module_schema_cutover.sql
```

Il refuse les migrations de module postérieures à P2, exige les tables
attendues, supprime uniquement les vues de compatibilité, remet les tables
et séquences possédées dans le noyau et restaure la fonction et le trigger
de login limité. Le schéma est supprimé avec `RESTRICT` : un objet restant
fait annuler tout le retour arrière. Il retire seulement les inscriptions
`dev/0001` des deux registres de migrations et l'identité de schéma `dev` ;
aucune ligne métier n'est supprimée. Une nouvelle montée peut alors rejouer
P2. Refaire les contrôles et le smoke avant de reprendre les écrivains.
