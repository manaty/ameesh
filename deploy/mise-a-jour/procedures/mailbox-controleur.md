# Réduction de lecture du courrier par le contrôleur

Bascule par le bureau uniquement, après intégration du correctif #6372
(manaty/ameesh#4920) dans un paquet. Aucune migration automatique : le REVOKE
est non additif. Ne pas poser pendant les coupures de 10:00Z et 22:00Z,
ni pendant une opération du contrôleur. Le bureau choisit un créneau libre
et garde les exécuteurs du contrôleur arrêtés pendant la bascule.

## Étape 0 : additive avant atterrissage

Le bureau pose `deploy/sql/release-decision-additive.sql` depuis la tête gelée,
avant l'atterrissage et avant tout lancement du code #6372 (backend installé,
stage bootstrap et bibliothèque compris). Utiliser le compte PostgreSQL
administrateur SUPERUSER de l'enrôlement (`source_owner_dsn`, sans exposer son
DSN), le search_path du mesh et le paramètre psql `role` du contrôleur déjà enrôlé.
Le simple propriétaire LOGIN du schéma n'a pas les droits CREATE ROLE et
ALTER FUNCTION OWNER nécessaires ; il ne convient pas pour cette pose.
La transaction idempotente crée le propriétaire NOLOGIN, les deux fonctions et leurs
EXECUTE bornés ; le SELECT existant du contrôleur reste actif. Les REVOKE
sur EXECUTE ferment seulement les endpoints ; aucun SELECT existant n'est retiré.

Le reçu cite le SHA gelé et les SHA-256 réellement posés du script et de son
inclusion `_release-decision.sql`, ainsi que le chemin/SHA réellement lancé.
Il consigne aussi le compte PostgreSQL réellement utilisé (`current_user`) et
la vérification de son attribut SUPERUSER, sans DSN ni secret.
Sur une même décision signée, comparer `decisions.get` et
`decisions.get_for_controller` : mêmes `view_of` et preuves valides.
Comparer aussi l'ensemble des identifiants renvoyés par
`release_controller_decision_ids(question)` à l'ancienne requête filtrée sur
la même question, les demandes humaines signées et l'état answered.
Consigner seulement les identifiants et les résultats, aucun contenu de décision.
Le reçu du bureau est une condition d'unhold, en plus du C3 et de la coupure
du 09/11 à 10:00Z ; unhold par le pilote. Aucune pose centrale par l'auteur.

## Préparation et essai avant

Identifier la version effectivement exécutée, le schéma et le rôle contrôleur,
ainsi qu'une vraie décision signée du contrôleur dont l'id est connu du bureau.
Utiliser les fichiers de connexion privés déjà enrôlés ; ne jamais mettre de
DSN ni de contenu de courrier dans un journal ou dans les arguments publics.
Avec l'ancien contrôleur et sa connexion, exécuter `decisions.get(db, id)` et
`decisions.verify_proof(db, view)`. Consigner seulement l'id, la présence de la
demande, le booléen de preuve et le SHA exécuté ; ne pas exécuter d'action de
release ni signer une décision pour cet essai. Conserver l'ancienne version
pour le retour arrière.

## Pose et essai après

1. Arrêter les services contrôleur et vérifier qu'aucune opération n'est active.
2. Avec le même compte administrateur SUPERUSER de l'enrôlement, le même
   search_path et le paramètre `role`, exécuter
   `deploy/sql/release-decision-cutover.sql` depuis le paquet intégré, ses
   inclusions à côté. Cette transaction distincte retire les SELECT de table
   et de colonnes, puis vérifie les contrats endpoint et lecteurs LOGIN fermés.
   L'étape 0 doit déjà avoir réussi.
3. Installer la version intégrée du contrôleur et de sa bibliothèque ameesh.
   Depuis sa connexion réelle, exécuter `decisions.get_for_controller(db,id)`
   sur la même décision, puis `decisions.verify_proof(db,view)`. Comparer les
   mêmes booléens à l'essai avant ; garder l'id et le SHA uniquement.
4. Sur cette connexion, vérifier que `SELECT body,meta,payload FROM
   agent_mailbox LIMIT 0` échoue avec permission refusée. Vérifier aussi qu'un
   id de courrier ordinaire connu ne renvoie aucune ligne par la fonction.
5. Avec ce compte administrateur, exécuter `_contrat-mailbox-readers.sql` :
   seuls ameesh, ameesh_host_agents1 et auditeur_lecture sont admis comme
   lecteurs LOGIN non superuser, y compris via SET ROLE.
6. Redémarrer le contrôleur, relever la version réellement lancée et son état.
   Aucune recette owner ni publication de contenu de décision n'est requise.

Un échec du contrat annule la transaction : diagnostiquer le droit hérité ou
PUBLIC signalé, sans élargir la liste fermée pour faire passer la pose.

## Retour arrière

Si l'essai après ou le démarrage échoue, arrêter le contrôleur. Le bureau
rétablit explicitement le droit antérieur avec ce compte administrateur :
`GRANT SELECT ON <schema>.agent_mailbox TO <role_controleur>` (identifiants
quotés par psql selon les noms réellement enrôlés). Remettre l'ancienne version
du contrôleur et de sa bibliothèque, puis répéter `decisions.get` et la preuve
sur le même id avant de redémarrer. La fonction peut rester installée : elle
ne change pas les données. Tracer le retour au droit large ; le contrat fermé
doit alors échouer tant que ce repli est actif.
