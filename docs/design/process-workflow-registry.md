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
  | contraintes | reçu complet (étape, version, `evidence` `process-receipt/1` avec `intent.sha256` 64 hex, `action` connue), version non négative, étape sans référence interdite |

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
```

* `Verifier(context) -> Verification` : appelé **dans la transaction**, après
  le verrou du lot et le contrôle d'autorité. `context` porte `db` (la
  transaction), la ligne du lot verrouillée, la `Command`, l'artefact compilé,
  `from_stage`/`to_stage`. Un vérificateur non installé (`verifier-uninstalled`),
  un retour qui n'est pas un `Verification` (`verifier-invalid`, un booléen est
  refusé), une exception (`verifier-error`) ou un refus (`verifier-refused`)
  annulent la transaction.
* `Command` : `action` ∈ `advance` (arête `from`→`to` gardée), `hold`
  (`blocked`/`waiting_human`, l'étape est conservée ; `waiting_human` exige une
  demande de décision native ouverte, L233), `release` (reprise de la retenue).
* `transfer_revision(..., spec=…)` : première adoption (l'étape devient
  l'initiale de l'artefact, une retenue est conservée) ou transfert (l'étape
  est conservée, elle doit exister dans le nouvel artefact). Exige le
  vérificateur réservé `revision_transfer` : rien n'est possible par défaut.
* `Receipt` : `accepted`, `replayed`, `event_id`/`version`, `state`,
  `workflow_ref`, `workflow_stage`, `workflow_version`, `intent_sha256`.

## Invariants de la transaction

1. `SELECT … FOR UPDATE` sur le lot avant tout contrôle : deux commandes du
   même lot ne se doublent jamais.
2. Autorité vérifiée dans la transaction, ligne `agent_registry` en
   `FOR SHARE` : acteur connu, non `stopped` ; source `runner` avec bail vivant,
   détenu par ce runner à cet epoch (`authority-lease`, `authority-epoch`,
   `authority-expired`). `explicit`/`session` : epoch 0. La vérification passe
   **avant** la lecture du reçu : un acteur révoqué ne récupère pas un résultat
   autorisé par le passé.
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
   (`terminal-reopened`) ; l'entrée en `waiting_human` exige une demande de
   décision native ouverte et verrouille sa ligne.

## Preuves

`tests/test_process_registry.py` (41 cas, deux pilotes) : rejeu après
connexion fraîche, même clé concurrente, deux commandes à la même version,
conflit de charge, acteur inconnu/arrêté, bail d'un autre runner, époque
périmée, bail échu, course avec révocation (verrou `FOR SHARE` tenu),
retenues et reprise, terminal, réparation bornée, transfert/rejeu/régression,
annulation par trigger, historique intact, contraintes de la migration. Le
module est déclaré dans `tests/parts/pilote-psql.txt` (transaction, verrous,
concurrence : comportement des deux pilotes).
