# SPDX-License-Identifier: AGPL-3.0-only
"""P1b #7650 (étude #7616 §3.2, §4.6) : les tables que le module `dev` possède.

Ce fichier est la **déclaration** du propriétaire, pas une liste d'exceptions :
le test de frontière (`tests/test_7650_dev_boundary.py`) refuse qu'un fichier
du noyau, hors ``modules/dev/``, nomme une de ces tables ; `agent_rename` y
lit les colonnes d'agent à réécrire au lieu de les recopier dans le noyau.

Les noms restent les noms **logiques** : le nom physique du schéma est lu dans
``module_schemas`` (résolveur ``relation`` du stockage du module). Avant P2
(#7651), les tables sont encore dans le schéma noyau ; après P2, le même code
les trouve dans ``<schéma_noyau>__dev`` sans second changement.
"""
from __future__ import annotations

#: Les 36 tables de l'inventaire #7616 (§3.2), par famille.
TABLES = (
    # CI
    "ci_runs", "ci_test_failures", "ci_runner_state", "ci_queue_state",
    "ci_guard_scan", "ci_guard_run", "ci_map_state", "ci_disk_state",
    "ci_disk_refusals", "ci_disk_dispatch",
    # Fusion, trains, paquets
    "merge_queue", "merge_locks", "integration_trains",
    "integration_train_fires", "integration_train_queue_receipts",
    "work_packet_members", "work_packet_snapshots", "work_landings",
    "work_resolutions",
    # Réservation de migration
    "work_migration_reservations",
    # Espaces de travail
    "managed_worktrees", "work_item_branch_heads",
    # Prévalidation
    "prevalidation_reports",
    # Veille GitHub
    "github_watch_cursors", "github_watch_events", "github_watch_scans",
    "github_watch_pending",
    # Revue
    "review_catch_up", "review_finding_resolutions", "work_review_withdrawals",
    # Provenance
    "provenance_receipts", "provenance_revocations",
    # Empreintes et impact
    "work_item_footprints", "work_item_impact_analyses",
    "repository_footprint_policies",
    # Mesures
    "work_item_measures",
)

#: Colonnes d'agent réécrites par `ameesh agent rename` (contrat de #4299).
#: Le noyau garde ses propres colonnes ; le module déclare les siennes pour que
#: `REWRITTEN` ne soit plus une liste de tables `dev` recopiée dans le noyau.
REWRITTEN = (
    "github_watch_pending.agent",
    "integration_train_fires.changed_by",
    "integration_train_fires.owner",
    "managed_worktrees.agent",
    "merge_locks.owner",
    "merge_queue.queued_by",
    "repository_footprint_policies.updated_by",
    "review_catch_up.actor",
    "work_item_impact_analyses.analyzed_by",
    "work_item_footprints.created_by",
    "work_packet_members.recorded_by",
    "work_resolutions.created_by",
    "work_review_withdrawals.reviewer",
    "work_landings.created_by",
)

#: Colonnes d'agent d'une table `dev` que le renommage ne réécrit **jamais**
#: (preuve ou autorité : la signature reste sur son orthographe d'origine).
KEPT = (
    "ci_runner_state.name",
    "ci_runs.superseded_by",
    "work_packet_snapshots.recorded_by",
    "work_packet_snapshots.lease_owner",
    # preuve datée : le renommage ne réécrit pas la signature d'origine
    "provenance_receipts.actor",
    "provenance_revocations.actor",
    "review_finding_resolutions.withdrawn_by",
)

#: Séquence explicite de `merge_queue` : elle reste dans le schéma noyau
#: (décision Q7, #222989). Elle n'est pas dans `TABLES` et aucun résolveur ne
#: la déplace.
CORE_SEQUENCES = ("merge_queue_generation_seq",)
