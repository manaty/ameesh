# Événements du train

Le raccord est borné à la qualification de mesh-design du 15 octobre 2026
à 18:53, fil `ameesh/1467`, en réponse au contrat consommateur de 14:14.
Il ne sélectionne ni profil actif, ni canon, ni contrat CI.

`train_events.emit(db, kind=..., project=..., target=...)` émet via
`storage.of(db).wakeups.notify`. Le producteur lui passe sa transaction :
PostgreSQL ne livre le signal qu'après commit et l'abandonne au rollback.
L'enveloppe `ameesh-train-wakeup/1`, sur le canal `ameesh_train`, porte
seulement `schema`, `namespace` SQL, `kind`, `project` et `target` canonique.
#7806 : le type `requalification-request` est le seul dont le `target` est
vide — la demande ne nomme qu'un projet, et le service sélectionne ses
passages par ce projet. Le namespace isole les schémas partageant une même
base ; ce filtrage n'est pas une preuve d'autorité.

Les producteurs installés sont :

- `storage.postgres.work.WorkItems.add_milestone` : le lot verrouillé donne
  `app` et `branch_target`, ce dernier canonicalisé par la fonction commune.
  Un jalon `verdict` émet après son insertion, dans la même transaction.
  Un lot sans sujet routable conserve son jalon sans émettre de signal.
- `storage.postgres.ci.CiResults.conclude` : l'UPDATE conditionnel et le
  signal sont dans une transaction commune. Le routage ne dépend ni du
  destinataire, ni de la base par défaut de GitHub. Il exige le manifeste
  persistant de même source GitHub normalisée, tête et branche ; la cible
  du plan doit correspondre à la clé de réservation. `success`, `failure`
  et `cancelled` réveillent tous le train. Un second appel sans transition
  n'émet rien ; une erreur SQL/transport annule aussi la transition.
- `storage.postgres.work.WorkItems.train_requalify_request` (#7806) :
  l'INSERT de la demande et le réveil `requalification-request` sont dans
  la même transaction. Une demande déposée est appliquée par le détenteur
  vivant au passage suivant, sans attendre le passage horaire ni fournir
  d'autorité dans le message. Le conflit d'unicité d'un dépôt idempotent
  n'émet pas de second signal ; un rollback abandonne la demande et le
  signal ensemble.

`Runner._listen_loop` souscrit ce canal seulement lorsqu'un service explicite
existe. Après établissement de LISTEN, il réveille une fois le service pour
rattraper un éventuel trou de démarrage/reconnexion. Un timeout d'écoute
ne réveille jamais le train. `Runner.dispatch` filtre l'enveloppe et appelle
seulement `Service.event`, sans SQL, Git, CI ou réveil des workers. Le service
regroupe les signaux et relit ses preuves dans un passage `ScopedDb` frais.
Une notification malformée, d'un autre namespace/projet/cible, ou contenant
des champs supplémentaires est ignorée. Aucun verdict ne voyage dans ce
signal et aucun signal ne suffit à autoriser une fusion.

Le transport accepte aussi le type `fire` déjà défini par `Service.event` ;
aucun producteur de feu n'est installé par ce raccord.

## Limites du producteur CI existant

`ci.Watcher.watch` ne suit que `WORKFLOWS = ("tests.yml",)` et découvre les
exécutions par leurs états `in_progress` ou `queued`. Son appel `_conclude`
ne couvre donc ni les vingt workflows candidats Nexlink ni un run terminé
avant d'être découvert, ni les autres transitions que ce veilleur ne suit
pas. Ce raccord ne crée pas de second veilleur et n'étend pas sa sélection.
La couverture événementielle Nexlink reste à qualifier et à fournir avant
activation ; le contrat CI complet reste également externe.

## Témoins ciblés

```sh
ameesh heavy -- env PYTHONPATH=src:. AMEESH_DRIVER=psycopg .venv/bin/python -m unittest tests.test_1467_train_events tests.test_1467_train_service -q
ameesh heavy -- env PYTHONPATH=src:. AMEESH_DRIVER=psql .venv/bin/python -m unittest tests.test_1467_train_events.TrainEventTransactionTest -q
```

SQL et LISTEN/NOTIFY sont réels dans des schémas jetables. Les témoins
couvrent commit/rollback, doublons, filtrage de namespace, état committé lu
par passage frais, verdict transactionnel, les trois statuts CI sans
destinataire, refus d'une autre source/tête/branche et rollback de l'UPDATE
si l'émission échoue. #7806 ajoute le dépôt de requalification : signal après
commit, aucun signal au rollback ni au dépôt idempotent, et sélection du
passage par le projet. La CI distante et l'exécution du moteur sont des
fixtures ; aucun run distant ou profil de train n'est lancé.
