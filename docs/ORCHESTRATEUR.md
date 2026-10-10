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
