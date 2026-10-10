# Orchestrateur : la consigne

Un orchestrateur répartit le travail de son équipe entre ses agents. Il ne
décide jamais à la place d'un humain (pas de capacité `approve`, décision
[0010](design/decisions/0010-responsabilite-et-orchestrateurs.md)). Ce
document est sa consigne pour qu'ameesh voie ce qu'il confie : qui travaille
sur quoi, ce qui attend, ce qui est livré.

## Être reconnu comme orchestrateur

ameesh reconnaît un orchestrateur de trois façons : sa fiche Agent du canon
porte `roles: [orchestrateur]`, il est déclaré dans
`AMEESH_ALERT_ORCHESTRATORS`, ou il a confié des lots (`work assign`,
`work delegate`) dans les 30 derniers jours. La fiche est la voie durable.

## Tout travail confié passe par un lot

Un travail confié par courrier sans lot est invisible : `ameesh projects`
montre l'agent « sans lot », les alertes de sous-utilisation se trompent et
la frise reste vide. Règle : **chaque message qui confie du travail porte son
lot**.

| Situation | Commande |
|---|---|
| Nouveau travail | `ameesh mail send <agent> "…" --new-lot "RÉF : titre"` |
| Lot existant, sans agent | `ameesh mail send <agent> "…" --lot <id\|RÉF>` |
| Reprendre un lot à un autre agent | `ameesh work assign <id> <agent>`, puis le message `--lot <id>` |
| Message sur un lot (relance, question) | `ameesh mail send <agent> "…" --lot <id>` |

* `--lot` accepte le numéro, ou une référence unique : `issue_ref`, fiche du
  plan, branche, premier mot du titre. Commencer les titres par une
  référence courte (« RT-9c : cercle… ») la rend utilisable.
* Un lot qui appartient à un autre agent n'est jamais réassigné par un
  message : ameesh avertit et donne la commande `work assign`.
* Un message à un agent sans aucun lot ouvert déclenche un avertissement
  (sortie d'erreur et fil). Il signale un oubli : rattacher le travail.
* L'attribution passe par la garde habituelle : un agent non réveillable est
  refusé et rien n'est déposé.

## Nommer la branche

Citer la branche dans le message (`agent/<agent>-<sujet>`, une seule par
message) la pose sur le lot ; sinon `ameesh work assign <id> <agent> --branch
agent/…`. L'exécuteur ferme alors le lot quand la branche entre dans sa cible,
avec ou sans PR : fusion directe, avance rapide, squash. La cible par défaut
est celle du dépôt (`git config ameesh.target develop` dans le clone si
l'équipe n'intègre pas sur la branche par défaut), ou `--target`.

Sans branche, fermer à la main : `ameesh work close <id> --superseded-by`
ou `ameesh work move <id> …`.

## Le courrier « Capacité au repos »

Quand des agents réveillables restent au repos sans lot pendant que du
travail attend, `ameesh notify` envoie à l'orchestrateur un courrier `event`
d'`ameesh` : la liste des agents au repos de son équipe et des lots ouverts
sans agent. Réponse attendue : leur confier ces lots (`--lot`), ou en créer
(`--new-lot`). Un même épisode n'est envoyé qu'une fois.

## Livraison et déploiements

Règles du propriétaire (2026-10-10), après qu'une version mineure (1.6.2) est
restée 4 h fusionnée sans être déployée :

1. **Fusionner d'abord.** Une PR à CI verte qui débloque une demande du
   propriétaire se fusionne avant tout nouveau chantier.
2. **Un déploiement en attente se prépare entièrement** : étiquette posée,
   `deploy/mise-a-jour/poste.sh verifier <REF>` et `deploy/mise-a-jour/vm.sh
   verifier <REF>` passés, plan de retour connu (étape `retour`). Il est
   proposé au propriétaire dès son retour, en premier point, avec les choix
   possibles : version seule ou `main` entier, migrations, risques. Seul le
   propriétaire décide du déploiement ; une consigne de veille ne se contente
   jamais d'interdire « tout déploiement » sans le préparer.
3. **Redémarrer détaché.** `poste.sh redemarrer` ne redémarre un exécuteur
   qu'hors tour : l'agent qui le lance alors qu'il a lui-même un exécuteur
   sur le poste le lance détaché (`setsid`/`nohup`), sinon il s'attend
   lui-même.
4. **Réserver les migrations.** Les numéros de migration se réservent dès le
   début d'un lot, annoncés dans le lot et au journal de conception, pour
   éviter les renumérotations en cascade quand plusieurs lots fusionnent à la
   suite.
