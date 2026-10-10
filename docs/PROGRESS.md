# `ameesh progress` — schéma `ameesh-progress/1`

Vue temps réel de l'avancement (lot L24, décision
[0024](design/decisions/0024-vue-temps-reel.md)) : lots sur une frise, agents,
jalons et budget, **alimentés par ce qu'ameesh enregistre lui-même** —
transitions des lots (`work_items`, `work_item_events`), actions sous porte
(`actions`), tours et baux du registre (`agent_registry`, `spend_pending`),
grand livre des coûts (`turn_costs`). Ni git ni le board ne sont lus.

```
ameesh progress [--project P] [--since 24h] [--stale-after 6h]   # texte, terminal étroit
ameesh progress --json [--project P] [--since 24h]   # ce schéma
ameesh progress --html FICHIER [--project P] [--since 24h]
```

* `--since` : début de la fenêtre, durée (`90m`, `16h`, `2d`/`2j`) ou date
  ISO 8601 (`2026-10-05`, `2026-10-05T08:00Z` ; sans fuseau = heure locale).
  Défaut `24h`.
* `--stale-after D` (L29) : un lot non fusionné sans activité depuis `D` est
  stagnant (défaut `AMEESH_STALE_AFTER`, sinon `6h`).
* `--project P` : lots dont `app` ou `workstream` vaut `P` ; actions du projet
  `P` (et celles liées à ces lots) ; agents dont `chantier` ou `team` vaut
  `P` ; coûts de ces agents.
* `--html FICHIER` : page statique **autonome** (CSS et script en ligne,
  données embarquées, politique `default-src 'none'` : aucune requête
  réseau, aucune police ni CDN), lisible sur téléphone. Aucun serveur :
  ouvrez le fichier, ou copiez-le où vous voulez. Écriture atomique (fichier
  temporaire puis renommage), régénérer = relancer la commande.

Codes de sortie : 0, 1 (base injoignable, schéma absent, erreur SQL),
2 (option illisible).

## Conventions

* Instants : secondes epoch UTC (nombres), clés suffixées `_ts`, ou `null`
  quand l'instant n'est pas connu. Durées en secondes (`_s`).
* Montants : dollars US (`_usd`), estimations comprises (voir `budget`).
* Une donnée qu'ameesh n'a pas est `null` ou une liste vide, jamais devinée ;
  `missing` dit en clair ce qui manque.
* Compatibilité : un champ peut être **ajouté** sans changer de version ;
  retirer ou changer le sens d'un champ fait passer à `ameesh-progress/2`.

## Racine

| Clé | Type | Sens |
|---|---|---|
| `schema` | `"ameesh-progress/1"` | version du schéma |
| `generated_ts`, `generated_at` | nombre, texte ISO (UTC, `Z`) | instant de l'instantané |
| `host` | texte | hôte qui a produit l'instantané |
| `project` | texte \| null | filtre `--project` |
| `window` | `{from_ts, to_ts}` | fenêtre de la frise |
| `projects` | liste | (L62) la vue par projet de `ameesh projects` (schéma des éléments : `ameesh-projects/1`, [EXPLOITATION.md](EXPLOITATION.md)) ; filtrée par `--project` sur le nom du projet ; en tête du texte et de la page |
| `lots` | liste | voir ci-dessous, du plus ancien au plus récent |
| `agents` | liste | voir ci-dessous, par nom |
| `milestones` | liste | jalons du projet : fiches `WorkPackage` `milestone` du canon (L29) |
| `epics` | liste | (L29) lots regroupés par epic du plan, avec la progression |
| `stale_after_s` | entier | (L29) seuil de stagnation appliqué, en secondes |
| `actions` | liste | actions de la fenêtre |
| `budget` | objet | dépense et jauges |
| `truncated` | objet | bornes de rendu atteintes : `{lots?, actions?}`, chacun `{shown, total, limit}` ; `{}` si rien n'est coupé |
| `missing` | liste de textes | données que la vue ne sait pas (encore) lire |

## `lots[]`

Un lot figure s'il est ouvert (ni `merged` ni `promoted`) ou modifié dans la
fenêtre. Borne de rendu : 500 lots, les ouverts d'abord puis les plus
récemment modifiés (coupure signalée dans `truncated.lots`) ; la liste est
rendue dans l'ordre de création. Les jalons et l'état d'un lot affiché sont
lus sans borne (journal complet, toutes ses actions).

| Clé | Sens |
|---|---|
| `id`, `title`, `type`, `project` (`app`), `workstream`, `issue_ref` | identité du lot |
| `state` | `active` \| `review` \| `blocked` \| `approved` \| `merged` \| `closed` (L29 : abandonné ou remplacé) |
| `work_state` | état brut de `work_items` (`intake`, `build`, `qa`, `merged`, `promoted`, `blocked`, `waiting_human`, `closed`) |
| `milestones` | `{requested, frozen, verdict, merged}` : instants, `null` si inconnus |
| `last_frozen_ts` | dernier gel (différent de `milestones.frozen`, le premier, si le lot a été regelé) |
| `verdict` | verdict retenu (le plus récent) : `ok` \| `blocked` \| null |
| `milestones_source` | `milestones` (table des jalons de lot, L10) \| `events` (déduction) |
| `blocked_verdicts` | nombre de verdicts bloquants (entier ; null si la source ne le sait pas) |
| `blocks` | nombre de mises en attente (`blocked`, `waiting_human`) |
| `assignee`, `reviewer` | auteur ; auteur du verdict retenu (approbateur pour une action de fusion) |
| `qa_loops` | allers-retours `qa → build` comptés par `work_items` |
| `updated_ts`, `budget_usd`, `spent_usd` | dernière modification, budget du lot |
| `actions` | ids des actions liées au lot |
| `package`, `epic` | (L29) fiche WorkPackage du lot et son epic, ou null |
| `pr_ref` | (L29) PR dont la fusion a fermé le lot, ou null |
| `closed` | (L29) `{reason: abandoned\|superseded, superseded_by, at_ts}` pour un lot fermé, sinon null |
| `waiting_for` | (L29) ce que le lot attend et de qui : `{what, who, label}` (`what` : start, build, verdict, fix, merge-approval, merge, merge-outcome, decision, unblock ; `who` null quand ameesh ne le sait pas) ; null pour un lot fusionné ou fermé |
| `last_activity_ts` | (L29) dernière transition, note, jalon ou action |
| `stale` | (L29) `{since_ts, idle_s, threshold_s}` si le lot non fermé n'a aucune activité depuis le seuil, sinon null ; la page ne le dessine plus actif |
| `delegation` | (L40, 0030) délégation à échéance (`ameesh work delegate`) : `{delegated_by, delegate, delegated_ts, due_ts, due_in_s, overdue, settled, label}` — `due_in_s` négatif en retard, `due_ts` null et `settled` vrai quand la délégation est soldée (le délégué a travaillé), `label` : « délégué par X, échéance dans 12 min » / « délégué par X, en retard de 5 min » ; null hors délégation et pour un lot fusionné ou fermé (voir [EXPLOITATION.md](EXPLOITATION.md)) |

Jalons (fonction unique `progress._jalons_de_lot`). Source : la table des
jalons de lot (L10, `work_item_milestones`) quand le lot y a un gel ou un verdict
**déclaré** (tous les instants viennent alors de la table) ; sinon
déduction depuis le journal des transitions :

* `requested` : création du lot ;
* `frozen` (gel) : premier gel déclaré, sinon première entrée en `qa` ;
* `verdict` : verdict retenu, le plus récent — verdict déclaré, sinon sortie de
  `qa` vers `merged` (`ok`) ou vers `build` (`blocked`) ; l'approbation de
  l'action `git-merge` du lot compte comme un verdict `ok` ;
* `merged` : jalon de fusion, sinon première entrée en `merged`, sinon fin
  confirmée de l'action `git-merge`.

État : `merged` (`merged`/`promoted`) ; `blocked` (`blocked`/`waiting_human`) ;
en `qa`, `approved` si le verdict retenu est `ok` et postérieur au **dernier**
gel, sinon `review` (un lot regelé repasse en revue) ; `active` sinon
(`intake`, `build`).

## `agents[]`

| Clé | Sens |
|---|---|
| `name`, `harness`, `model`, `host`, `project` (`team`, sinon `chantier`) | identité |
| `effort` | réglage `ameesh set … effort=` ; **null** = défaut du harnais ou inconnu (réglage local d'un autre hôte) |
| `state` | `working` \| `idle` \| `paused` \| `stopped` |
| `status`, `status_text` | statut brut du registre et son texte (raison d'une pause) |
| `since_ts` | début du tour en cours (`working`), fin du dernier tour (`idle`), dernière mise à jour sinon |
| `turn` | tour en cours ou null : `{started_ts, duration_s, label, task}` (`task` = consigne en cours, tronquée à 140 caractères) |
| `turns`, `unread`, `pending_prompt`, `lease_live` | tours comptés, non-lus, consigne en attente, bail vivant |
| `mode`, `stop_reason` | `execute` \| `externe` ; raison d'arrêt structurée quand l'état est `stopped`, sinon `null` (L37, décision 0030) |

État : `running` sous bail vivant → `working` ; `running` sans bail vivant,
`stopped`, `dead` → `stopped` ; `blocked` (pause budget ou comptable de L13)
→ `paused` ; `idle`, `queued` → `idle`. Le début du tour vient du marqueur
comptable posé avant chaque tour (`spend_pending`).

## `milestones[]`

Jalons du **projet** déclarés au canon (fiches `WorkPackage` de sorte
`milestone`, L29, [plan de travail](PLAN-DE-TRAVAIL.md)) : `{id, title,
at_ts, status, responsible, canon_ref, epics, lots_total, lots_merged,
lots_abandoned, lots_open, lots_pending, progress}`. `at_ts` est null (le
profil ne déclare pas de date) ; vide sans plan synchronisé.

## `epics[]` (L29)

`{id, title, milestone, responsible, status, canon_ref, lots, work_items,
lots_total, lots_merged, lots_abandoned, lots_open, lots_pending,
progress}`. Les unités d'un epic sont ses fiches `lot` (`lots[]` :
`{id, title, status, work_items}`, statut `pending` sans lot créé, `open`,
`merged`, `abandoned`) et les lots rattachés directement à l'epic
(`work_items`). `progress` = fusionnées / (total − abandonnées), null si
aucune ; calculé sur tous les lots, hors fenêtre.

## `actions[]`

`{id, project, work_item, connector, operation, class, state, proposed_by,
attempts, created_ts, approved_ts, launched_ts, finished_ts}` — actions
modifiées dans la fenêtre ou non terminales (`proposed`, `approved`,
`launched`, `unknown`). Borne de rendu : 500, les non terminales d'abord
(coupure signalée dans `truncated.actions`). Les actions d'un lot affiché
sont toujours dans son champ `actions`, même au-delà de la borne.

## `budget`

| Clé | Sens |
|---|---|
| `currency` | `"USD"` |
| `paid_harnesses` | harnais payés au token (dépense réelle) ; les autres sont des forfaits dont le coût est une estimation |
| `hourly_cap_usd` | plafond horaire de l'usage payé au token (décision 0019) |
| `limits` | L70 : plafonds en vigueur et leur source (`base`, `config`, `défaut`) — même forme que `limits` de `ameesh budget --json` ; null sans configuration |
| `spend` | `{window, 1h, 24h}`, chacun `{total_usd, paid_usd}` |
| `by_agent` | `{agent, harness, model, paid, usd, turns, input_tokens, cached_input_tokens, output_tokens}` sur la fenêtre |
| `plans` | jauges de forfait : `{harness, key, used, pace_cap, elapsed, resets_ts, window_s, exceeded}` (fractions 0..1) |

Les jauges sont celles de `ameesh cost` : lues dans les journaux locaux des
harnais **de l'hôte qui produit l'instantané** ; leur historique est en base depuis L26
(`ameesh cost gauges --json`, voir `docs/EXPLOITATION.md`).

## Exemple (abrégé)

```json
{
  "schema": "ameesh-progress/1",
  "generated_ts": 1791200000.0,
  "generated_at": "2026-10-05T11:33:20Z",
  "host": "poste-1",
  "project": "demo",
  "window": {"from_ts": 1791113600.0, "to_ts": 1791200000.0},
  "lots": [{
    "id": 12, "title": "Export des factures", "state": "review", "work_state": "qa",
    "milestones": {"requested": 1791150000.0, "frozen": 1791180000.0,
                   "verdict": null, "merged": null},
    "verdict": null, "blocked_verdicts": 0, "blocks": 0,
    "assignee": "agent-a", "reviewer": null, "actions": []
  }],
  "agents": [{
    "name": "agent-a", "harness": "codex", "model": null, "effort": "high",
    "state": "working", "since_ts": 1791199000.0,
    "turn": {"started_ts": 1791199000.0, "duration_s": 1000,
             "label": "tour consigne (codex)", "task": "Corriger le test d'export"}
  }],
  "milestones": [],
  "epics": [],
  "budget": {"spend": {"1h": {"total_usd": 0.4, "paid_usd": 0.1}}, "plans": []},
  "missing": ["…"]
}
```
