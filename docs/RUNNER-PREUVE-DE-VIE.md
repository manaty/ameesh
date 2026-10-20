# Preuve de vie du runner

Chaque runner écrit un signal indépendant du bail pour les personas `execute`
placées sur son hôte, limitées à `--agents` si cette option est fournie.
Les personas arrêtées sont incluses ; les externes sont exclues. Une porte fermée, un manque
de capacité ou une persona inéligible n’empêchent pas le signal.

`ameesh show <persona> --json` et `ameesh list --json` exposent :

- `runner_seen_ts` : instant PostgreSQL (`clock_timestamp()`), en secondes epoch ;
- `runner_seen_host` : hôte du runner ayant écrit ;
- `runner_seen_owner` : identité du runner ayant écrit.

Ces valeurs prouvent une passe du runner, pas une autorisation de tourner.
Elles ne modifient ni le bail, ni le placement, ni le statut, ni `last_seen`.
Un hook ou une inscription ne les avance pas. Le signal cesse à l’arrêt
du runner ; `--dry-run` ne l’écrit pas.

Une seule instruction UPDATE écrit le groupe servi, au premier passage puis
au plus une fois toutes les 60 secondes, même si le courrier réveille souvent
la boucle. `AMEESH_RUNNER_LIVENESS_INTERVAL` peut augmenter cet intervalle
(minimum 60 s). Un poll plus long reporte aussi le signal. Les erreurs de base
passent par la reprise habituelle de la boucle.

Pour vérifier un redémarrage sans bail, un contrôle doit comparer le signal
à l’heure **de la base** relevée avant le redémarrage et vérifier l’hôte et
l’identité attendus. Avec l’intervalle par défaut, une attente de 90 s est
prévue pour le futur contrôle de déploiement. Les scripts de déploiement
basculeront sur ce signal dans une modification séparée.

En mode médié, `agents.runner_seen` utilise le jeton d’exécuteur sans bail :
le serveur force l’hôte, limite les noms aux agents admis, et exige le préfixe
`exec:<executor_id>:` du propriétaire. Le jeton de session ne peut pas l’appeler.
La disponibilité de l’hôte ne bloque pas cette preuve de vie.

`ameesh resume` avertit aussi pour une persona arrêtée sans bail, sauf si
elle porte une preuve du même hôte, avec une identité de runner, datant
d’au plus `2 × max(60 s, poll, runner_liveness_interval)`.
Ce seuil utilise la configuration locale de `resume` et suppose une cadence
homogène dans le mesh, y compris pour le runner de l’hôte distant.
L’âge est mesuré avec l’heure de la base. Cette preuve évite un avertissement
quand le runner a rendu normalement le bail ; elle ne garantit pas un tour
immédiat et ne change pas la valeur `runner_live`, réservée au bail.
