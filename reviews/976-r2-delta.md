# R2 delta #976 / PR #777 — codex3 (Codex), 2026-10-13

**NO-GO — 1 B/P1 résiduel (B1) / 0 N / 0 n** sur `2c26bed311d16231f32b0a703b7b84b5ee57f9e2`.

Delta relu : `2309123ff67e6f9cdc783b1d1eedaf954d4c3c6f..2c26bed311d16231f32b0a703b7b84b5ee57f9e2`, six fichiers. Lecture du stockage, de l'appelant, du chemin d'inscription exact de cette tête et des témoins ajoutés. Aucun rejeu des suites, aucune mutation de boîte ou de prod. `git diff --check` propre. Les 34/19/46 tests verts sont déclarés par l'auteur, pas exécutés par le relecteur.

## Partie de B1 corrigée

Pour les lignes existantes, la CTE d'expiration lit le statut sous FOR SHARE ; le changement de statut ne peut plus se terminer entre cette décision et la mutation du courrier. Une mise à jour concurrente déjà en cours est attendue et la version courante est relue. Le renvoi verrouille old/new FOR UPDATE par ordre de nom avant de verrouiller les messages ; old vivant et new absent/arrêté sont refusés. MailboxRefused est traduit en Refused par l'appelant.

Les trois témoins concurrentiels ajoutés couvrent effectivement les changements d'état de lignes existantes, avec connexions distinctes, verrou détenu par le holder, observation d'une attente SQL et vérification du courrier conservé.

## B1 résiduel — l'absence du nom ne sérialise pas une inscription concurrente

`src/ameesh/storage/postgres/mailbox.py:478-486` accepte encore `NOT EXISTS (SELECT 1 FROM v)`. Sans ligne, FOR SHARE ne verrouille rien. `forward`, lignes 407-427, accepte pareillement old absent. L'inscription (`storage/postgres/registry.py:168-203`, upsert) peut INSERT la ligne sans verrou commun avec ces chemins. Écrire dans un commentaire que cette inscription est « ordonnée après » l'expiration ne crée aucune coordination entre les transactions.

Entrelacement à barrières pour l'expiration, déduit du code (pas exécuté ici) :

1. A est absent du registre ; M, son ancien courrier, est non remis/non réservé et assez vieux. T1 prend FOR UPDATE sur M, sans le modifier.
2. T2 lance expire_dead_letters(A). Son snapshot voit A absent ; la CTE ne verrouille aucune ligne et l'UPDATE attend sur M.
3. T3 inscrit A vivant par registry.upsert puis commit. Il ne rencontre aucun verrou incompatible de T2. L'inscription est terminée et A peut lire sa boîte.
4. T1 libère M sans créer de nouvelle version. T2 continue avec son snapshot et marque M remis/expiré, bien qu'A soit maintenant présent et vivant.

Pour forward : verrouiller M avant l'appel, laisser le SELECT registre conclure old absent et le SELECT messages attendre ; inscrire old vivant et commit ; libérer M. La copie et la remise de l'original ont ensuite lieu sans relecture du registre. Cela reste le cas « courrier du destinataire devenu vivant entre lecture et écriture » signalé dans la R2 initiale.

Le test `test_expire_un_nom_absent_du_registre` (ligne 692) ne lance aucune inscription concurrente : il valide l'expiration normale d'un fantôme, pas sa protection contre cette course.

Fondement : [PostgreSQL Read Committed](https://www.postgresql.org/docs/current/transaction-iso.html#XACT-READ-COMMITTED) conserve le snapshot de l'instruction pour les autres lignes ; les [verrous de ligne](https://www.postgresql.org/docs/current/explicit-locking.html#LOCKING-ROWS) portent sur les lignes récupérées, sans empêcher l'insertion d'une ligne absente. Les verrous de table implicites ROW SHARE et ROW EXCLUSIVE sont compatibles.

Attendu, déjà demandé dans la R2 initiale : coordonner aussi l'absence avec l'inscription (verrou commun par nom respecté par tous les chemins de création, ou verrou de table adapté avant une lecture fraîche dans la transaction) ; à défaut, refuser prudemment la mutation d'une boîte absente tant que cette coordination manque, en rendant cette limite explicite. Ajouter les témoins inscription/expiration et inscription/renvoi, à barrières et connexions distinctes. Une simple seconde lecture sans verrou laisse la même couture.

Ce verdict porte sur le delta de code. La CI exacte et la R1 Claude restent des portes distinctes ; aucune attente ni sondage de CI. Deux tours ont maintenant été rendus sur cette PR : si une troisième relecture devient nécessaire, appliquer le canon §2 (correction par le relecteur ou travail en binôme) pour éviter un nouveau cycle de courriers.
