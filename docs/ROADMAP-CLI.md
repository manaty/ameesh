# Préparer et éditer une roadmap avec la CLI

`ameesh plan` tient les propositions et engagements : périmètre, échéance,
responsable, source, note et dépendances. `plan show` conserve sa vue Gantt
(`ameesh-roadmap/1`). Le format éditable d'import/export est distinct :
`ameesh-plan/1`.

## Créer une proposition et la relire

```sh
ameesh plan add "Qualifier les métriques du module" --proposed \
  --project acme --key acme/metrics --note-file specification.md
ameesh plan list --project acme --json
ameesh plan get e42 --json
ameesh plan update e42 --due 2026-12-03 --owner coordination \
  --note-file specification-revisee.md --if-match JETON_RENDU_PAR_GET
ameesh plan accept e42
```

Sans `--proposed`, `add` crée un engagement ouvert qui exige une date ;
`--decision` autorise un jalon de décision sans date. Une proposition peut
rester sans date et ne produit pas d'alerte d'échéance avant son acceptation.
`propose --record` conserve son extraction de propositions depuis les décisions
et les corps des tâches.

`get` rend la note complète et un jeton `etag`. `update --if-match JETON` refuse
l'édition si le contenu a changé depuis sa lecture. Sans jeton, l'édition porte
sur la dernière version lue par la commande ; une modification concurrente
pendant son exécution est également refusée.

`update` accepte `--what`, `--due`, `--kind commitment|decision`, `--owner`,
`--project`, `--lot`, `--card`, `--source-type`, `--source`, `--note` ou
`--note-file`. Une option absente conserve sa valeur. `-` efface une date ou
un champ nullable ; un engagement ouvert ordinaire doit garder sa date.
Le rattachement à un lot reprend son projet par défaut et doit rester cohérent
avec celui-ci. `--note-file -` lit l'entrée standard.

`--depends-on REF` répété remplace la liste entière ; `--clear-dependencies`
l'efface. La clé reste immuable. Les transitions de statut passent par
`accept`, `done`, `cancel`, ou par un import explicite. Les métadonnées d'un
engagement terminé ou annulé ne sont plus éditables.

Avec `add --key CLE`, répéter une création identique rend le même engagement.
Un contenu différent sous la même clé est refusé et doit être édité explicitement.

## Échanger un plan JSON

```sh
ameesh plan export --project acme --output plan.json
ameesh plan validate plan.json --json
ameesh plan import plan.json --json           # aperçu, aucune écriture
ameesh plan import plan.json --apply --json
ameesh plan import plan-revise.json --update --json
ameesh plan import plan-revise.json --apply --update --json
```

L'import lit un fichier UTF-8 ou `-` pour l'entrée standard. Il refuse les champs
inconnus, les clés répétées, les références inconnues et les cycles. Il est
limité à 10 Mio et 1 000 entrées. L'aperçu présente pour chaque clé l'action
`create`, `update` ou `unchanged`, les valeurs proposées et les avertissements.
`validate FICHIER` vérifie les mises à jour sans exiger `--update`.

`--apply` revalide puis applique toutes les entrées dans une transaction :
une erreur annule l'ensemble. Les éditions du graphe sont sérialisées et les
écritures vérifient aussi que les lignes lues n'ont pas changé. Un second import
identique ne réécrit rien et ne crée aucun doublon. Les entrées absentes du
fichier sont conservées.

Pour une clé existante, les champs omis conservent leurs valeurs. Une différence
exige `--update`, même dans l'aperçu d'import. Cet import peut accepter ou clore
un engagement actif ; il ne peut ni rouvrir un engagement terminé/annulé, ni
transformer un engagement ouvert en proposition. L'identité, la clé, la date
et l'auteur de création des entrées existantes sont conservés.

Exemple avec une dépendance locale, indépendante des numéros `eN` attribués :

```json
{
  "schema": "ameesh-plan/1",
  "commitments": [
    {
      "key": "acme/contract",
      "what": "Spécifier le contrat des métriques",
      "status": "proposed",
      "due": "2026-12-01",
      "project": "acme",
      "note": "Périmètre, critères de validation et méthode de mesure."
    },
    {
      "key": "acme/qualification",
      "what": "Qualifier les métriques",
      "status": "proposed",
      "due": "2026-12-03",
      "project": "acme",
      "depends_on": ["@acme/contract"]
    }
  ]
}
```

| Champ | Valeur et défaut pour une nouvelle entrée |
| --- | --- |
| `key` | Texte stable obligatoire, 1 à 256 caractères : lettres ASCII, chiffres, `._:/-` ; commence par une lettre ou un chiffre |
| `what` | Phrase obligatoire |
| `kind` | `commitment` (défaut) ou `decision` |
| `status` | `proposed` (défaut), `open`, `done`, `cancelled` |
| `due` | Date ISO `AAAA-MM-JJ` ou `null` ; aucun jour relatif dans le JSON |
| `owner`, `project` | Texte ou `null` ; peuvent être déduits du lot rattaché |
| `lot` | Numéro d'un lot d'exécution existant ou `null` |
| `card` | Identifiant d'une WorkPackage existante ou `null` |
| `source_type` | `conversation` (défaut), `decision`, `thread`, `task`, `other` |
| `source` | Identifiant ou lien, ou `null` |
| `depends_on` | Liste de références, vide par défaut |
| `note` | Texte, vide par défaut ; conserve les sauts de ligne |

L'export inclut les propositions et engagements ouverts ; `--all` ajoute les
statuts terminaux. Il remplace les références `eN` incluses dans l'export par
`@CLE`, et conserve les références externes : celles-ci doivent exister sur la
destination. Pour les anciennes lignes sans clé, l'export calcule une clé stable
sans modifier le stockage. Le format échange le contenu du plan ; il ne
constitue pas une sauvegarde de ses horodatages ni de son historique.

## Valider les dépendances et filtrer un projet

```sh
ameesh plan validate --project acme --json
ameesh plan show --project acme --json
```

Les références acceptées sont `eN` (engagement), `@CLE` (entrée du même import),
`task #N`, `lot #N`, `#N` ou `N` (lot d'exécution), un identifiant de WorkPackage
existant, et `gate:NOM` ou `external:NOM`. Les dépendances locales peuvent viser
une entrée placée plus loin dans le fichier. Les cycles d'engagements et les
dépendances d'un lot vers lui-même sont refusés. La validation et l'aperçu
d'import signalent aussi les échéances antérieures à celles des prérequis et
les prérequis annulés.

`validate` sans fichier examine les engagements actifs et les engagements dont
ils dépendent ; les erreurs donnent un code de sortie non nul. Le filtre projet
des engagements inclut les engagements globaux (`project: null`). Celui des
tâches du Gantt est appliqué avant la limite et le décompte : les autres projets
ne consomment pas les places affichées.

Les liens entre engagements `eN` décrivent le plan et sont validés ; ils ne
constituent pas une condition de démarrage du runner. Les dépendances
d'exécution restent gérées par `ameesh work dependencies` et les attentes
planifiées existantes. Ni un import, ni une proposition ne crée un lot ou un
agent. Les estimations et l'ordonnancement des lots restent accessibles par
`ameesh work plan` et `ameesh work schedule`.
