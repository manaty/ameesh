# Registre natif des workflows Process (#1915 tranche B, lot #1922)

Contrat consommateur : `manaty/ameesh#1379` (corps et commentaire
6207021753). Tranche A (#1918) : compilateur strict `process_workflow`.
Tranche C : vérificateurs finis et types installés. Ce document publie le
**seam** que C branche et les invariants que B garantit.

## Ce que B ajoute au dépôt

* migration additive `0452_process_workflow_registry` :

  | objet | ajout |
  |---|---|
  | `work_items` | `workflow_ref` text NULL, `workflow_stage` text NULL, `workflow_version` bigint NOT NULL default 0 |
  | `work_item_events` | `command_id` uuid NULL, `workflow_stage` text, `workflow_version` bigint, `evidence` jsonb |
  | index | `work_item_events_command_uidx` unique partiel `(work_item_id, command_id) WHERE command_id IS NOT NULL` |
  | contraintes | reçu complet exigé `IS TRUE` (étape, version, `evidence` `process-receipt/1` avec `intent.sha256` 64 hex, `action` connue) : toute clé absente ou `null` imbriqué est refusé ; version non négative ; étape sans référence interdite |

  L'historique existant est conservé : les lignes d'avant la migration ont
  `command_id IS NULL` et ne sont pas concernées par la contrainte de reçu.

* `ameesh.process_registry` : la seule écriture native d'une transition de
  workflow. Aucune route CLI ou HTTP nouvelle ; les lots sans workflow ne
  passent pas par ce module (`workflow-absent`) et gardent `work.move`.

## Deux versions distinctes

* **révision Process** — `workflow_ref` + `workflow_version` sur le lot et sur
  le reçu. Elle ne change que par `transfer_revision`, explicite, journalisé et
  sans régression ; aucune adoption automatique d'une nouvelle révision.
* **version optimiste de transition** — `expected_version` d'une commande et
  `Receipt.version` : l'id de la dernière ligne `work_item_events` du lot
  (0 si le journal est vide), toutes écritures confondues (commandes du
  registre comme `work.move` historique). Toute écriture concurrente du journal
  fait échouer une commande restée sur l'ancienne version (`version-conflict`).

## API publiée (tranche C)

```python
from ameesh import process_registry as pr

pr.install_verifier("qa_started", verifier)      # garde → appelable serveur
pr.install_verifier(pr.HOLD_VERIFIER, verifier)  # retenues : entrée ET sortie

receipt = pr.apply_command(db, pr.Command(
    work_item_id=1915,
    command_id="…-uuid…",
    expected_version=12,                          # journal du lot
    authority=pr.Authority(actor="…", source="runner", epoch=7, runner_id="…"),
    workflow_ref="canon/…@sha", workflow_version=3,
    action="advance",                             # advance | hold | release
    from_stage="review", to_stage="tests", guard="qa_started",
    evidence={"pr": "…", "merge_sha": "…"},       # faits structurés, jamais un booléen
), spec=compiled_workflow)                        # artefact de la tranche A

state = pr.registry_state(db, 1915)
stored = pr.receipt_of(db, 1915, command_id)
sources = pr.read_sources(context)                # collections réelles, dans la tx
```

* `Verifier(context) -> Verification` : appelé **dans la transaction**, après
  le verrou du lot et le contrôle d'autorité. `context` porte `db` (la
  transaction), la ligne du lot verrouillée (le snapshot de C : `id`, `title`,
  `type`, `assignee`, `reviewer` par `to_jsonb`, `closed_at`, `state`,
  `loops`, `workflow_*`), la `Command`, l'artefact compilé,
  `from_stage`/`to_stage`. Un vérificateur non installé (`verifier-uninstalled`),
  un retour qui n'est pas un `Verification` (`verifier-invalid`, un booléen est
  refusé), une exception (`verifier-error`) ou un refus (`verifier-refused`)
  annulent la transaction. Les vérificateurs viennent du registre **installé** :
  `apply_command` et `transfer_revision` n'acceptent aucun `verifiers=` de
  l'appelant (le banc de tests passe par le point privé `_apply`/`_transfer`).
  Deux noms réservés : `revision_transfer`, et `HOLD_VERIFIER` (`hold`, tranche
  C : l'appelable `verify_hold` dispatche sur l'`action`) pour l'entrée comme
  pour la sortie de retenue (`from_stage = to_stage` = étape conservée ;
  `work_item.state` porte la retenue quittée).
* `Command` : `action` ∈ `advance` (arête `from`→`to` gardée ; refusée depuis
  une retenue, `held`), `hold` (`blocked`/`waiting_human`, l'étape est
  conservée ; `waiting_human` exige une demande de décision native ouverte,
  L233), `release` (reprise de la retenue, vérifiée comme l'entrée ; une
  décision encore ouverte la refuse, `decision-pending`).
* `transfer_revision(..., spec=…)` : première adoption (l'étape devient
  l'initiale de l'artefact, une retenue est conservée ; l'étape initiale doit
  projeter le cœur courant, sinon `stage-mismatch`, et un lot terminal n'est
  jamais rouvert, `terminal-reopened`) ou transfert (l'étape est conservée,
  elle doit exister dans le nouvel artefact et projeter le cœur courant hors
  retenue). Exige le vérificateur réservé `revision_transfer` : rien n'est
  possible par défaut.
* `Receipt` : `accepted`, `replayed`, `event_id`/`version`, `state`,
  `workflow_ref`, `workflow_stage`, `workflow_version`, `intent_sha256`.
* `read_sources(context) -> Mapping` : la lecture des sources pour C (qui est
  pur et sans SQL), **dans la transaction et sous verrou**. Le mapping ne
  déclare que ce qui est réellement collecté (`SOURCE_KEYS` : `milestones`,
  `receipts`, `decisions`, `deliveries`) ; `observations` n'est pas installée et
  n'est **pas** déclarée : un vérificateur qui l'exige refuse
  `source-unavailable` (fail-closed). `receipts` porte `{accepted, guard,
  event_id, command_id, workflow_stage, workflow_version, sha256, evidence,
  facts}` — `guard` est la garde Process ou le nom réservé du vérificateur
  (`hold`, `revision_transfer`).

## Invariants de la transaction

1. `SELECT … FOR UPDATE` sur le lot avant tout contrôle : deux commandes du
   même lot ne se doublent jamais.
2. Autorité vérifiée dans la transaction, ligne `agent_registry` en
   `FOR SHARE` : acteur connu, ni `stopped` ni `dead` ; source `runner` avec
   bail vivant, détenu par ce runner à cet epoch (`authority-lease`,
   `authority-epoch`, `authority-expired`). `explicit`/`session` : epoch 0. La
   vérification passe **avant** la lecture du reçu : un acteur révoqué ne
   récupère pas un résultat autorisé par le passé.
3. Révision du lot = révision de la commande = révision de l'artefact
   (`ref-mismatch`, `revision-mismatch`, `spec-mismatch`).
4. Intent canonique : JCS + SHA-256 (`intent_sha256`) sur les entrées
   explicites (action, clé, version attendue, révision, autorité, faits ;
   `from`/`to`/`guard` ou `hold`). Rejeu de la même intention → reçu accepté
   (`replayed=True`) ; même clé, autre intention → `intent-conflict`.
5. Cœur, étape, `loops` (boucle qa → build bornée à `work.MAX_QA_LOOPS`) et
   reçu dans la même transaction : toute erreur annule l'ensemble
   (`rollback sans demi-événement`).
6. Cœurs terminaux (`promoted`, `closed`) jamais rouverts
   (`terminal-reopened`), y compris par adoption de workflow ; l'entrée en
   `waiting_human` exige une demande de décision native ouverte et verrouille sa
   ligne ; sa reprise attend la réponse (`decision-pending`). Une retenue ne se
   traverse pas (`held`) : seule une `release` vérifiée en sort.
7. Un lot pinné (`workflow_ref` non NULL) n'est écrit que par ce module :
   `work.move`, `work.close` et `work.close_merged` le refusent — le constat de
   fusion seul ne pose pas `merged`. Le filtre est aussi **dans le SQL** de ces
   écritures historiques (et de la réouverture `release_unassigned` de #1668) :
   une adoption concurrente, qui garde l'état, ne laisse plus passer un move
   legacy entre la lecture et l'UPDATE, et une retenue `blocked` du registre ne
   tombe pas à la fermeture d'un prérequis (elle n'est levée que par une
   commande `release` vérifiée). La lecture de `workflow_ref`, elle, passe par
   l'interface de stockage (`work.get` le porte comme `reviewer`, '' sur une
   base d'avant 0452, et donc par le contrat médié) : les commandes tournent
   aussi sur une connexion d'exécuteur médié (session de VM), où un SELECT
   brut est refusé (B5).
8. Ordre des verrous : `agent_mailbox` (demandes de décision ouvertes du lot)
   **avant** `work_items`, comme la réponse du propriétaire
   (`decisions.answer` ferme la demande, puis déplace le lot) : pas
   d'interblocage, et une réponse ne se glisse pas entre le contrôle et
   l'écriture de la retenue.

## Preuves

`tests/test_process_registry.py` (61 cas, deux pilotes) : rejeu après
connexion fraîche, même clé concurrente, deux commandes à la même version,
conflit de charge, acteur inconnu/arrêté/`dead`, bail d'un autre runner, époque
périmée, bail échu, course avec révocation (verrou `FOR SHARE` tenu), retenues
et reprise par le vérificateur réservé (`verifier-uninstalled` sans lui, refus
d'un booléen, avance depuis une retenue refusée, réponse avant reprise,
réponse concurrente sans interblocage), terminal jamais rouvert (y compris par
adoption), adoption sans régression de cœur, transfert/rejeu/régression,
API publique sans injection de vérificateurs, `work.move`/`close`/`close_merged`
refusés sur un lot pinné (y compris la couche de stockage appelée avec
l'instantané d'avant l'adoption : le filtre `workflow_ref` est dans le SQL),
`read_sources` fail-closed, annulation par trigger,
historique intact, contraintes de la migration (`IS TRUE`, y compris la
réapplication du fichier qui réinstalle la définition durcie). Un cas de **seam
réel avec la tranche C** (`process_verifiers.install_verifiers` sur le registre
public : avance gardée `planning`, retenue, reprise, `waiting_human` par
`read_sources`) tourne dès que C est présent, sinon il est sauté — C n'est pas
encore dans `main`. `tests/test_l385_work_dependencies.py` garde la réouverture
#1668 : un dépendant pinné, retenu `blocked`, non assigné, n'est pas rouvert à
la fermeture de son dernier prérequis (B4) — sa retenue ne tombe que par une
`release` vérifiée. La lecture médiée de la garde est gardée par
`tests/test_l109_mediated_mode.py` (B5) : `work note`/`move` sur un lot non
pinné, et le refus d'un lot pinné, ne font aucun SELECT brut — la session de
VM n'enverrait que `work.get`, jamais `work.move`. Le module est
déclaré dans `tests/parts/pilote-psql.txt` (transaction, verrous, concurrence :
comportement des deux pilotes).
