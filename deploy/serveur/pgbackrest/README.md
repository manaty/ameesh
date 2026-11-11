# Archivage WAL hors hôte et restauration à un instant (EP8.1, #6858)

Étude : [replique-base-mesh.md §6](../../../docs/design/etudes/replique-base-mesh.md).
Décision owner #207987 a. Classe sensible ; staging seulement (§14), avec un
retour arrière à chaque étape.

| Pièce | Rôle |
|---|---|
| `pgbackrest-ameesh.sh` → `/usr/local/sbin/ameesh-pgbackrest` | configurer, activer, désactiver, sauvegarder, exercices |
| `ameesh-pgbackrest@.service` | une sauvegarde (`full` ou `diff`) sous `postgres` ; rien avant l'activation |
| `ameesh-pgbackrest-full.timer` | complète le dimanche à 02:00 |
| `ameesh-pgbackrest-diff.timer` | différentielle du lundi au samedi à 02:00 |

Le dump quotidien (`ameesh-sauvegarde.timer`, 03:30) ne change pas : c'est le
second format, indépendant, et le chemin de restauration lisible.

## Budget

WAL mesuré : 1,5 à 2 Go/j, donc 21 à 28 Go bruts sur 14 jours, compressés en
zstd (en général 3 à 5 fois moins), plus deux ou trois sauvegardes complètes
de la base (1,5 Go). Il faut compter environ 10 Go dans le bucket. Sur
l'hôte, la file asynchrone (`/var/spool/pgbackrest`) reste petite. Si S3 est
injoignable, `pg_wal` grossit au plus de 4 Gio (`archive-push-queue-max`)
avant que les WAL soient abandonnés : la restauration à un instant a alors un
trou, mais la base vive ne remplit pas son disque. Il faut relever le disque
libre de `manaty-mesh-1` avant l'activation (étape 2).

La rétention de 14 jours est tenue par pgBackRest. **Aucune règle de cycle de
vie sur le bucket** : elle effacerait une sauvegarde complète dont dépendent
les WAL plus récents.

## Procédure (pilote ou bureau, sur l'hôte de la base, en root)

1. **Bucket et accès** : le bureau confirme le bucket. C'est l'existant si
   `/etc/ameesh/sauvegarde.env` est déjà configuré, sinon un nouveau bucket
   hors de `fr-par`. Il fournit une clé dédiée et une phrase de chiffrement,
   dont une copie reste chez lui. Il écrit `/etc/ameesh/pgbackrest.env`
   (root, 0600) : `PGBR_S3_BUCKET`, `PGBR_S3_REGION`, `PGBR_S3_KEY`,
   `PGBR_S3_KEY_SECRET`, `PGBR_CIPHER_PASS`. Aucun secret dans le dépôt. La
   stanza est toujours `ameesh` (`PGBR_STANZA` est refusé : les minuteries ne
   lisent pas ce fichier).
2. **Avant tout changement** : relever `df -h /var/lib/postgresql`, puis
   restaurer le dump S3 actuel sur un autre hôte :
   `ameesh-pgbackrest exercice-dump --fichier <dump> --dossier <vide>`.
   Cet exercice doit finir sur `doctor` vert. En root, les outils de Postgres
   (qui refusent root) passent sous `postgres`, propriétaire du dossier ; le
   dump est lu par root et donné à `pg_restore` sur son entrée.
3. `ameesh-pgbackrest configurer`, puis `ameesh-pgbackrest activer`, sans
   redémarrage : la stanza est créée et le réglage posé, mais rien n'est
   encore appliqué. Retour arrière : `ameesh-pgbackrest desactiver`.
4. **Fenêtre** de 30 à 60 s, planifiée par le pilote ou le bureau, l'owner
   informé : `ameesh-pgbackrest activer --redemarrer`. Cette commande fait le
   redémarrage (sa durée est mesurée et affichée), `pgbackrest check`, la
   première sauvegarde complète, puis met les minuteries en marche. Retour
   arrière : `ameesh-pgbackrest desactiver --redemarrer`.
5. **Exercice de restauration** : sur un hôte qui n'est pas celui de la base,
   en root ou sous `postgres`, avec le même `pgbackrest.conf` :
   `ameesh-pgbackrest restaurer --cible '2026-11-12 08:00:00+00' --dossier <vide>`.
   Il restaure à l'instant choisi dans une instance jetable : socket local,
   aucune écoute réseau, `archive-mode=off`, donc rien ne repart vers le
   dépôt. Il passe ensuite `ameesh doctor --probe` et affiche le RTO mesuré.
   La VM `ameesh-tests` n'existe pas
   ([PILOT-ameesh-agents-3](../../guest/PILOT-ameesh-agents-3.md)) : le
   bureau désigne l'hôte de l'exercice.
6. Le lendemain, vérifier que le dump de 03:30 a bien été écrit et que
   `ameesh-pgbackrest etat` montre la différentielle.
