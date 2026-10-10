# Plan de travail — jalons, epics, lots (L29)

Le plan est **déclaré au canon** (OKF, [décision 0005](design/decisions/0005-canon-okf.md)) ;
l'**état d'exécution** des lots reste dans ameesh (`work_items`) ; GitHub n'en
est qu'une **vue**. Une modification faite dans GitHub n'est jamais reprise
comme état : au plus, elle est signalée comme **proposition** à porter au
canon par une PR.

## Le modèle

```
jalon (milestone) ──► epic ──► lot          fiches WorkPackage du canon
                                 │
                                 └──► work_items (#12, #13…)   état, jalons L10, actions
```

Une fiche `WorkPackage` (frontmatter seulement, comme toute fiche du profil) :

```yaml
type: WorkPackage
title: "Recherche plein texte dans le catalogue"
kind: lot                    # milestone | epic | lot
parent: catalogue            # identifiant d'une autre fiche WorkPackage
responsible: human:alice     # REQUIS, résolu vers une fiche Member (comme pour Agent)
team: acme-web               # facultatif
scope: ["src/catalogue/recherche/**"]   # globs de fichiers, facultatif
status: draft                # déclaratif, facultatif (cycle de la fiche, pas l'avancement)
id: cat-recherche            # facultatif : par défaut le nom du fichier sans .md
start: 2026-10-12            # L96, facultatifs : début, fin, livraison prévus (AAAA-MM-JJ)
end: 2026-10-16
delivery: 2026-10-19         # un jalon peut écrire `date:` (alias de delivery)
```

Règles vérifiées par `ameesh canon check` :

| Code | Gravité | Règle |
|---|---|---|
| `package-kind-invalid` | erreur | `kind` ∉ {milestone, epic, lot} |
| `package-parent-unknown` | erreur | `parent` ne désigne aucune fiche |
| `package-kind-incoherent` | erreur | un lot se place sous un epic ou un jalon ; un epic sous un jalon (ou rien) ; un jalon n'a pas de parent |
| `package-cycle` | erreur | les parents bouclent |
| `package-responsible-missing` / `-unresolved` | erreur | responsable absent, ou qui ne résout pas vers un Member humain unique |
| `package-duplicate`, `package-id-invalid` | erreur | identifiant en double ou hors grammaire (`[A-Za-z0-9][A-Za-z0-9._-]*`, 64 car.) |
| `package-lot-orphan` | avertissement | lot sans parent |
| `package-date-invalid` | avertissement | (L96) `start`, `end`, `delivery` ou `date` illisible (attendu `AAAA-MM-JJ`) : la date est ignorée, la fiche reste lue |

Une erreur du plan **ne bloque aucun agent** (elle n'entre pas dans la
condition de réclamation) ; elle figure au diagnostic du canon.

`ameesh canon sync` recopie les fiches dans la table `work_packages`
(migration 0026) avec leur `canon_ref` (`membre:chemin@commit`). Une fiche en
erreur n'est pas écrite (sa copie précédente reste) ; une fiche retirée du
canon est marquée absente, jamais effacée ; les lots rattachés suivent le
parent courant de leur fiche.

Exemple complet : [`examples/canon/plan/`](../examples/canon/plan/) (jalon
`v1`, epics `catalogue` et `paiement`, trois lots).

## Les lots (`ameesh work`)

```
ameesh work add --title T --package <fiche>       rattacher dès la création (lot ou epic)
ameesh work link <id> <fiche> | --none             rattacher / détacher après coup
ameesh work list [--stale-after 6h] [--json]       colonne EPIC, attente, stagnation
ameesh work show <id> [--json]                     fiche, epic, PR, attente
ameesh work close <id> --abandoned                 lot qui ne sera pas fait
ameesh work close <id> --superseded-by <id2>       lot remplacé ou absorbé par un autre
```

`close` passe le lot dans l'état terminal `closed` et pose un jalon `closed` :
rien ne reste ouvert par oubli. Un lot fermé n'est **jamais rouvert** par une
fusion constatée plus tard (le constat est signalé).

Pour chaque lot non fusionné, ameesh dit **ce qu'il attend et de qui**
(`waiting_for` : `{what, who, label}`), dérivé de l'état, des jalons (L10) et
des actions : démarrage, travail de l'auteur, verdict du relecteur, correction
de l'auteur, approbation de la fusion, fusion, issue de fusion inconnue,
décision humaine (du responsable de la fiche), déblocage. Une action du lot
qui attend un humain (approbation, réconciliation) passe avant tout ; un
verdict bloquant ne compte que s'il suit le dernier gel. Quand ameesh ne sait
pas qui, il le dit (« relecteur non désigné ») au lieu de deviner.

Un lot ni fusionné ni fermé sans activité (modification, transition, note,
jalon, action, message lié) depuis le seuil (`--stale-after`,
`AMEESH_STALE_AFTER`, défaut 6 h) est **stagnant** : il est signalé avec la
durée, et la frise ne le dessine plus comme actif.

Ces règles sont dans un seul module, `ameesh.stagnation`, appelé par
`ameesh progress`, `ameesh work list/show` et l'alerte `stale_lot` de
`ameesh alerts` (qui garde son champ `waiting_for` en un mot et ajoute
`waiting`, la forme détaillée).

## Fusion constatée : fermeture automatique

Une fusion se prouve **par le contenu**, jamais par le nom (une branche se
renomme, une fusion se fait par rebase, squash ou cherry-pick). Quatre voies,
toutes idempotentes, qui posent le lot en `merged` avec son jalon `merged`
(commit et auteur de la fusion) :

1. **la porte** : quand l'action `git-merge` liée au lot (`--work-item`) est
   confirmée (exécution, issue tardive ou réconciliation) ;
2. **le contenu**, dans un clone local (lecture seule, aucun `fetch`) :

   ```
   ameesh work sync-merges --git-dir D [--target origin/main] [--repo owner/repo] [--dry-run] [--json]
   ```

   seul le **dernier gel** du lot fait autorité (jalon `frozen` le plus
   récent, avec son commit) : un gel antérieur déjà fusionné ne prouve rien
   sur le travail courant. Ce commit est **ancêtre** de la cible ; sinon
   chaque commit du gel a son **`git patch-id --verbatim`** parmi les commits
   de la cible depuis leur base commune (rebase, cherry-pick) ; sinon le diff
   entier du gel est un commit de la cible (**squash**). `--verbatim` ne
   normalise aucun espace (`--stable` confondrait `"a b"` et `"ab"`) : faux
   négatif plutôt que faux positif ; sans lui (git < 2.40), seul l'ancêtre
   prouve. La fermeture est conditionnée en base au gel examiné : un nouveau
   gel déclaré entre-temps l'empêche (`refrozen`) ;
3. **la PR liée** (à défaut, ou seule) :

   ```
   ameesh work sync-github --repo owner/repo [--limit 200] [--dry-run] [--json]
   ```

   lit les PR fusionnées (`gh pr list`, lecture seule) ; une PR se relie à
   une fiche par sa branche `lot/<id>` ou `lot/<id>-…` (la plus longue
   correspondance, sans casse) ou par une ligne `ameesh-lot: <id>` du corps ;
   une ligne `ameesh-work: <n>` désigne un lot précis (et seulement lui).
   Seuls les lots créés **avant** la fusion sont fermés.

4. **la branche du lot** (L118), avec ou sans PR ni gel :

   ```
   ameesh work sync-branches [--host H | --all-hosts] [--dry-run] [--json]
   ```

   un lot qui porte sa branche (`work add|assign --branch`, ou branche
   `agent/…` citée par un `mail send --lot`) est fermé quand elle entre dans
   sa cible : pointe entrée par un commit de fusion, dernier commit vu en
   avance retrouvé dans la cible (ancêtre, patch-id, squash), ou commit de
   fusion qui cite la branche. L'exécuteur fait ce relevé toutes les 5 min
   dans le dossier de travail de l'assigné ; voir EXPLOITATION.md,
   « Courrier lié aux lots et fusion des branches ».

Un lot absorbé par un autre se clôt par `--superseded-by`.

## Projection GitHub

```
ameesh work project-github --repo owner/repo [--dry-run] [--canon-url URL] [--json]
```

* une issue par fiche `lot` : titre `[<id>] <titre>`, lien vers la fiche
  (`--canon-url https://…/blob/{commit}` ; sinon le `canon_ref`), labels
  `ameesh` et `ameesh:<état>` (`intake`, `build`, `qa`, `merged`, `blocked`,
  `waiting_human`) ; fermée (terminée) quand le lot est fusionné, fermée
  (non planifiée) quand il est abandonné ;
* une issue par `epic` (label `ameesh:epic`), progression dans le corps, et
  ses lots en **sous-issues** (API sub-issues de GitHub) ; si l'API est
  indisponible, liste de tâches `- [x] #12` dans le corps de l'epic ;
* chaque issue porte un marqueur caché `<!-- ameesh:package=<id> d=… -->` :
  il la retrouve parmi **toutes** les issues du dépôt, quels que soient ses
  labels — jamais de doublon, même si le label `ameesh` a été retiré (mise à
  jour idempotente : rien n'est écrit si rien n'a changé) et garde l'empreinte de ce qu'ameesh a écrit. Un titre, un corps,
  un label `ameesh…` ou un état changé dans GitHub est signalé
  `PROPOSITION` (à porter au canon), puis la vue est réécrite. Les autres
  labels ne sont jamais touchés ;
* `--dry-run` lit GitHub et montre ce qui serait créé ou mis à jour, sans
  rien écrire.

`gh` est résolu comme pour le connecteur `git-merge` (`AMEESH_GH_BIN`,
`AMEESH_BIN_DIR/gh`, PATH) ; les tests utilisent un faux `gh`
(`tests/fake_github.py`), jamais GitHub.

## `ameesh progress`

Champs **ajoutés** au schéma `ameesh-progress/1` (voir [PROGRESS.md](PROGRESS.md)) :
`epics` (lots regroupés par epic, progression), `milestones` (les fiches
`milestone`), `stale_after_s`, et par lot `package`, `epic`, `pr_ref`,
`closed`, `waiting_for`, `last_activity_ts`, `stale`. Le texte et la page
montrent l'attente, la stagnation et la section EPICS.

## Feuille de route : dates prévues et engagements (L96)

Le plan dit **quoi** ; la feuille de route dit **pour quand**, et compare au
réel. Migration 0044.

* **Dates prévues** d'une tâche (`ameesh work plan <id> --debut J --fin J
  --livraison J --source S`) et d'une fiche (`ameesh work plan <fiche> …`,
  qui prime sur les dates du canon ci-dessus). La replanification n'est pas
  une activité de la tâche.
* **Engagements datés** (`ameesh plan add "…" --pour J --projet P --source
  S [--lot N] [--depend-de REF]`) et **jalons de décision** (`--decision`) ;
  `plan accept | done | cancel`. Un engagement rattaché à une tâche est tenu
  à sa livraison.
* **Propositions** (`ameesh plan propose [--from docs/design/decisions]
  [--record]`) : tirées des fiches `Decision` (question ouverte → jalon de
  décision ; « pour le 2026-10-20 », « on fera ça lundi » → engagement ;
  « lots L97–L104 », « même vague que L95 » → élément et dépendances) et du
  corps des tâches ouvertes. À valider : rien n'est créé sans `--record`,
  rien n'est daté sans `plan accept`.
* **Gantt** : `ameesh plan show [--json]` et la section `roadmap` de `ameesh
  progress` (affichée par défaut, `--no-gantt` pour la retirer) — epics et
  jalons (prévu de la fiche, réel de leurs tâches), tâches (prévu face aux
  jalons réels génériques : demandée, en cours, soumise, verdict, livrée),
  engagements, décisions attendues, ligne du jour, retards, source de chaque
  élément ; plus « qui avance sur quoi ».
* **Alerte** `engagement_overdue` (poussée par `ameesh notify`) : un
  engagement ou une date prévue passé sans que l'élément soit atteint.

Le suivi de projet est au **cœur générique** : le code de la feuille de
route ne nomme aucun état ni jalon de métier ; la correspondance avec le
cycle actuel des lots tient dans `roadmap_dev.CORRESPONDANCES`, que le futur
module « développement » reprendra. Détail, schéma `ameesh-roadmap/1` et
consigne des orchestrateurs : [EXPLOITATION.md](EXPLOITATION.md), « Feuille
de route ».

## File d'amélioration continue (L119, décision 0037)

```
ameesh work backlog add --title T --value "valeur attendue" --score 1-100
                        [--priority 1|2|3] [--source S] [--team E] [--requires CAP …]
ameesh work backlog list [--all] [--json]
```

Un élément de la file est un lot de type `improvement`, en `intake`, sans
assigné ; sa valeur attendue (une phrase) et son score sont obligatoires : pas
de travail pour occuper. La file se lit dans l'ordre de prise : priorité, puis
score, puis ancienneté. Un élément en file n'est ni stagnant ni un « lot sans
assigné » d'`idle_capacity` : il attend son preneur.

À chaque passage, `ameesh notify` confie l'élément le mieux classé qui
correspond à l'équipe et aux capacités d'un agent réveillable au repos depuis
`--take-idle` (30 min), par l'attribution gardée et un courrier lié au lot.
Garde-fous :

* **seulement si aucun lot ne l'attend** : ni lot ouvert, ni courrier non lu,
  ni consigne en attente, ni lot de session encore ouvert ; et tant qu'un lot
  du projet attend un preneur, aucune amélioration n'est prise ;
* **jamais un geste irréversible ou de production** : le courrier de prise
  l'interdit (déploiement, fusion, suppression de données, serveur de
  production, dépense engagée) ; l'agent le propose à un humain ;
* forfaits d'abord ; le token seulement avec `--take-paid`, jamais pendant
  `balance_low` ; rien sous un plafond atteint ni sur un hôte sous pression,
  sur batterie faible ou non prêt ; au plus `--take-max-per-hour` (2) prises
  par heure glissante pour tout le mesh.

Quand des agents sont au repos et que la file est vide (et qu'aucun lot du
projet n'attend), l'alerte `backlog_empty` invite l'humain responsable à la
remplir.
