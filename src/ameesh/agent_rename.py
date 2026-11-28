# SPDX-License-Identifier: AGPL-3.0-only
"""L243b step 3 (#4299, decision #157436 a): rename one agent identity in place.

  ameesh agent rename OLD NEW --reason R [--actor A] [--dry-run] [--json]

One transaction moves the registry row's primary from OLD to NEW, keeps OLD as
an alias (with every other alias), and rewrites the columns that designate the
identity (`REWRITTEN`), so per-agent reads under the new primary see the same
lots, mail, sessions, costs and standings. Foreign keys follow by ON UPDATE
CASCADE (0516). The key, lease epoch, session and worktree stay on the same
row: no second row is created and nothing is copied.

Evidence is never rewritten (`KEPT`): approvals, nonces, keys and key events,
receipts, executor audit, tokens and allowlists, alias resolutions and renames.
Signed bytes stay as signed; their spellings resolve through the alias.

Refused while the agent holds a live lease or is running (stop its runner
first, between two turns), when NEW already designates another identity or
already appears in a rewritten column, for an owner key holder, and while a
receipt of any of its spellings belongs to a lot not yet merged or closed (the
review gate pairs kept receipts with rewritten names). Reverse is
the same gesture in the other direction: `rename NEW OLD` restores OLD as
primary and keeps NEW as alias. `--dry-run` runs the whole transaction and
rolls it back, so a refusal by any trigger shows before the real pass.

The host's local state directory (`cfg.agent_dir`) moves with
`move_local_state`, leaving OLD as a symlink; canon cards, placements and
service units are a separate application change (docs/AGENT-ALIASES.md).
"""
from __future__ import annotations

import json
import os
import re

from . import agent_names, storage, work
from .modules.dev import tables as dev_tables
from .config import Config

RENAMED_EVENT = "agent.renamed"

# Columns holding an agent name, rewritten where equal to OLD. Array columns
# are marked with a trailing "[]".
REWRITTEN = (
    # #7631 (0586): the claim (agent, seq) follows the journal entry — the
    # conversation log cascades on rename; a go already cited must not become
    # reusable under the new spelling.
    "accord_claims.agent",
    "account_budgets.set_by", "account_rate_events.agent", "account_reservations.agent",
    "account_reservations.closed_by", "account_switches.agent", "account_turn_slots.agent",
    "account_turn_starts.agent", "agent_lot_sessions.agent", "agent_mailbox.recipient",
    "agent_mailbox.sender", "agent_registry.created_by", "agent_registry.operator_pause_by",
    "agent_registry.retired_by", "agent_watches.agent", "agent_watches.enabled_by",
    "alert_acknowledgements.actor", "budget_events.actor", "budget_limits.set_by",
    "commitments.created_by", "commitments.owner", "conversation_heads.agent",
    # #7050 (0572): the move attempt follows the agent; its requester is an
    # ordinary actor (not an approval), renamed like `work_items.planned_by`
    "conversation_moves.agent", "conversation_moves.requested_by",
    "estimate_references.published_by", "external_executors.declared_by",
    "external_executors.retired_by", "host_lifecycle.actor",
    "host_lifecycle.return_requested_by", "host_lifecycle_transitions.actor",
    "housekeeping_log.actor", "housekeeping_log.agent",
    "notify_briefs.recipient", "notify_state.owner",
    "operator_pause_events.actor", "persona_sessions.persona", "provider_account_credits.agent",
    "provider_account_credits.prepaid_by",
    "sandbox_turns.agent", "session_bindings.agent",
    "tranche_reservations.agent", "tranche_reservations.closed_by", "tranche_events.actor",
    "tranche_limits.admitted_by",
    "session_bindings.created_by", "spend_ledger.decided_by", "spend_marker_events.agent",
    "spend_pending.agent",
    "standing_reservations.reserved_by",
    "turn_costs.agent", "turn_resources.agent", "visibility_checks.persona",
    "work_item_delegations.delegate", "work_item_delegations.delegated_by",
    "work_item_delegations.resolved_by", "work_item_dependencies.created_by",
    "work_item_estimates.estimated_by", "work_item_events.actor",
    "work_item_links.created_by",
    "work_item_links.decided_by", "work_item_milestones.actor", "work_items.assignee",
    "work_items.delegated_by", "work_items.estimate_by",
    "work_items.external_executor", "work_items.planned_by", "work_items.reviewer",
    "work_items.reviewers[]",
    "work_packages.planned_by",
    # P1b #7650 : les colonnes des tables du module `dev` sont déclarées par
    # leur propriétaire (une seule liste, jamais recopiée ici).
) + dev_tables.REWRITTEN

# Children of agent_registry(name) and conversation_heads(agent): moved by the
# cascade (0516), counted only.
CASCADED = (
    "agent_candidates.name", "agent_peek.agent", "alerts_delegations.role",
    "conversation_log.agent", "host_handoffs.agent",
    "host_lifecycle_agents.agent", "operator_pause_events.agent", "role_handover.name",
    "role_standings.name",
    # #7034 (0571): the restricted child's rows follow the identity; the FKs
    # are `on update cascade`, deleted with the child.
    "spawn_contexts.child", "spawn_profiles.child", "spawn_reports.child",
)

# Evidence, authority and unrelated names: never rewritten. Executor
# allowlists (jsonb) are granted authority, read through the alias.
KEPT = (
    # #7518 (0581): historical statement author and request attribution.
    # Admission/settlement use account + attempt_id, never this spelling.
    "credit_cycles.actor", "credit_requests.agent",
    "credit_reconciliations.actor",  # Operator reconciliation audit evidence.
    # #8347 (0611): the lineage reservation's `agent` is a dated attribution
    # of the admitted turn (the row lives 2 h and is deleted at settlement).
    # The cap is summed by lineage `root`, and admission/settlement/replay use
    # `spend_key`, never this spelling as a lookup key. A rename happens
    # between turns (refused under a live lease); an orphan reservation left
    # by a crashed turn expires and stops counting. Rewriting it would also
    # need `root` (outside the rename contract) plus a write-time trigger, for
    # no per-agent read.
    "budget_lineage_reservations.agent",
    # #8344 (0612): an application alert's `recipient` is a `human:<id>`,
    # never an agent; `raised_by`/`resolved_by` are the dated attribution of
    # who raised or resolved it. Nothing looks a row up by these spellings
    # (the identity is app, type, key), so a rename leaves them as written.
    "app_alerts.recipient", "app_alerts.raised_by", "app_alerts.resolved_by",
    # #7066: the spelling is bound by the owner's signature. A renamed agent
    # needs a fresh decision; rewriting this index must not widen authority.
    "account_turn_exceptions.agent",
    # #6881 (0567): the agent a limited login may act for, granted by the
    # administrator (role-bureau.sql) — a rename does not move it; the guard
    # refuses until the role is reinstalled under the new name (fails closed).
    "limited_logins.agent",
    # #7106 (0575): who registered a tenant of a shared host, dated evidence
    # like `executors.enrolled_by`; a rename does not rewrite it.
    "host_tenants.registered_by",
    # #7911 (0610): the seat-connection window keeps dated attributions:
    # `opened_by` and `closed_by` are the human or avatar's gesture (or the
    # closure cause, `pod-end`/`expiry`), and `pod_ended_by` is the tenant
    # runner's id (`host/tenant`, never an agent spelling) that observed the
    # connection pod's end. Like `host_tenants.registered_by`, evidence of a
    # dated gesture, not an identity link; a rename does not rewrite it.
    "tenant_logins.closed_by", "tenant_logins.opened_by",
    "tenant_logins.pod_ended_by",
    # #8193 (0608): who created or closed a lot subject, dated attribution
    # like `host_tenants.registered_by`, never an authority; not rewritten.
    "work_subjects.author", "work_subjects.closed_by",
    # #7697 (0601): the pod request's actor columns are operational
    # attribution, never an identity link. `requested_by` is hashed into the
    # request fingerprint (`pod_children.fingerprint`) together with its
    # `parent` pair, which a rename does not rewrite; `claimed_by` is the
    # provisioner actor (`system:ameesh-pod-provisioner`), not an agent
    # spelling. The template's `name` is a Kubernetes identifier and
    # `registered_by` is the provisioner heartbeat actor. Old spellings
    # resolve through the registry alias.
    "pod_requests.claimed_by", "pod_requests.requested_by",
    "pod_templates.name", "pod_templates.registered_by",
    "action_attempts.approver", "action_attempts.launched_by", "action_attempts.settled_by",
    "action_events.actor", "actions.auth_by", "actions.proposed_by", "actions.replaced_by",
    "agent_name_resolutions.primary_name", "agent_name_resolutions.supplied_name",
    "agent_registry.name", "agent_registry.roles", "agent_renames.actor",
    "agent_renames.new_name", "agent_renames.old_name", "authenticator_syncs.applied_by",
    "authenticators.approver",
    "exec_audit.agent", "executor_events.actor", "executor_invitations.agents_allowlist",
    "executor_invitations.created_by", "executors.agents_allowlist",
    "executor_tokens.agent", "executors.enrolled_by", "executors.revoked_by",
    "key_events.holder", "key_events.name", "key_holders.name", "mesh_approvals.approver",
    "mesh_approvals.consumed_by", "mesh_consumed_nonces.approver",
    "mesh_consumed_nonces.consumed_by",
    # #6873 (0566) : la révocation est une preuve datée (acteur, raison) ;
    # sa colonne vit avec le module `dev` (P1b #7650).
    # #7119 (0574) : le journal des grants d'attach est une preuve datée (grant
    # accepté ou refusé pour CET agent) ; un renommage ne le réécrit pas.
    "attach_grant_events.agent", "attach_grant_events.checked_by",
    # #6669 (0564) : le journal additif des résolutions de constat est une
    # preuve historique ; sa colonne vit avec le module `dev` (P1b #7650).
    "schema_migrations.name",
    "schema_non_additive_migrations.name",
    # #7865 (0596) : le ledger de dépense garde les acteurs d'approbation, de
    # confirmation et d'arrêt comme preuves datées, comme les actions et
    # `standing_approvals` ; seul `decided_by`, lu par le relevé de l'agent
    # (`spend.recap`), suit le renommage.
    "spend_ledger.approver", "spend_ledger.confirmed_by",
    "spend_ledger.stop_requested_by", "spend_ledger.stopped_by",
    "spend_review_confirmations.confirmed_by",
    "spend_marker_events.actor",  # Human resolution / runner audit evidence.
    "sandbox_turns.owner",  # Runner lease owner, never an agent identity.
    "standing_approvals.approver", "standing_approvals.registered_by",
    "standing_approvals.revoked_by", "turn_cost_corrections.actor",
    "work_module_milestones.actor",
) + dev_tables.KEPT

#: Noms de colonnes qui peuvent désigner une identité d'agent quelque part
#: dans le schéma. Toute colonne de ce genre doit être classée ci-dessus
#: (`REWRITTEN`, `CASCADED`, `KEPT`) ou ci-dessous (`IGNORED`, `NOT_AGENTS`),
#: pour qu'une nouvelle table ou un `ADD COLUMN` ne reste jamais silencieux.
#: Le test `test_every_identity_column_is_classified` le lit sur le schéma
#: Postgres ; `scripts/garde-colonnes-identite.py` en donne la variante
#: statique au gel, sur les migrations du diff (#8387).
IDENTITY_COLUMNS = re.compile(
    r"^(agent|persona|assignee|reviewers?|recipient|sender|delegate|owner|approver|actor|"
    r"holder|external_executor|name|primary_name|supplied_name|old_name|new_name|"
    r"agents_allowlist|[a-z_]+_by)$")

#: Noms qui ressemblent à une identité mais n'en portent jamais.
NOT_AGENTS = {
    # Identity-like names that never hold an agent spelling.
    "agent_registry.name",
}

#: Colonnes génériques retenues par `IDENTITY_COLUMNS` qui ne portent jamais
#: de nom d'agent, avec leur raison : un renommage ne les déplace pas.
IGNORED = {
    # #4148 (0507): a scanner lock token "<actor>:<host>", not an agent name;
    # it expires within the scan, a rename never needs to move it.
    "host_release_locks.owner",
    # #6091 (0554): the declared non-agent executor is `system:<name>`, a
    # system identity that can never collide with an agent (`NAME_RE` forbids
    # the colon); a rename never moves it.
    "external_executors.name",
    # #7649 (0591): the per-module migration registry labels a migration
    # (`0001_init`), never an agent; a rename never moves it.
    "module_schema_migrations.name",
    "module_schema_non_additive_migrations.name",
}

#: Colonnes dont la valeur est préfixée par le type d'acteur ; seul `agent:`
#: suit le renommage, `human:` reste une preuve intacte (#7865).
PREFIXED = {"spend_ledger.decided_by": "agent:"}


class RenameError(ValueError):
    """Renaming refused; the text says why."""


def _split(column: str) -> tuple[str, str, bool]:
    table, _, name = column.partition(".")
    is_array = name.endswith("[]")
    return table, name[:-2] if is_array else name, is_array


def _uses(tx, column: str, name: str) -> int:
    table, col, is_array = _split(column)
    if table in dev_tables.TABLES:
        # P1b #7650 : le nom physique d'une table `dev` est lu au registre.
        return storage.of(tx).agent_columns.uses(table, col, is_array, name)
    predicate = "%s = ANY(%s)" if is_array else "%s = %s"
    value = PREFIXED.get(column, "") + name
    rows = tx.query("SELECT count(*) AS n FROM %s WHERE %s" % (
        table, predicate % ("%s", col)), (value,))
    return int(rows[0]["n"])


def _rewrite(tx, column: str, old: str, new: str) -> int:
    table, col, is_array = _split(column)
    if table in dev_tables.TABLES:
        # P1b #7650 : la table n'est pas dans le schéma noyau après P2.
        return storage.of(tx).agent_columns.rewrite(table, col, is_array, old, new)
    if column in PREFIXED:
        rows = tx.query(
            "SELECT ameesh_rename_spend_agent_column(%s, %s, %s, %s) AS n",
            (table, col, old, new))
        return int(rows[0]["n"])
    # Counted in SQL (0516): the psql driver's execute() reports no row count.
    rows = tx.query("SELECT ameesh_rename_agent_column(%s, %s, %s, %s, %s) AS n",
                    (table, col, is_array, old, new))
    return int(rows[0]["n"])


def rename(cfg: Config, db, old: str, new: str, *, reason: str,
           actor: str | None = None, dry_run: bool = False) -> dict:
    """Rename `old` to `new` atomically; RenameError when refused.

    Returns `{old, new, actor, aliases, rewritten, cascaded, event, dry_run}`;
    `rewritten` and `cascaded` count rows per column (non-zero only).
    """
    reason = " ".join((reason or "").split())
    if not reason:
        raise RenameError("agent rename: --reason is required (the decision or the lot)")
    who = work.corrector(cfg, db, actor, what="agent rename")
    result: dict = {}

    class _DryRun(Exception):
        pass

    if new == old:
        raise RenameError("agent rename: %s is already the primary" % old)
    primary = agent_names.resolve(old, db=db, operation="agents.rename")
    if primary == new and not dry_run:
        # Already renamed: only the stray rows written under the old spelling.
        return {"old": old, "new": new, "actor": who, "dry_run": False,
                "already": True, "rewritten": {}, "cascaded": {}, "event": None,
                "aliases": list((_row(db, new) or {}).get("aliases") or []),
                "swept": sweep(db, old, new)}
    try:
        with db.transaction() as tx:
            result = _rename_tx(tx, old, new, who, reason)
            if dry_run:
                raise _DryRun()
    except _DryRun:
        result["event"] = None
    result["dry_run"] = dry_run
    result["already"] = False
    # A writer that resolved OLD just before the commit may still insert it.
    result["swept"] = {} if dry_run else sweep(db, old, new)
    return result


def _row(db, name: str) -> dict | None:
    rows = db.query("SELECT name, aliases FROM agent_registry WHERE name = %s", (name,))
    return rows[0] if rows else None


def sweep(db, old: str, new: str) -> dict:
    """Rewrite rows still spelled `old` once `old` is an alias of `new`.

    Safe to repeat: `old` then designates only `new`. Returns counts per column.
    """
    with db.transaction() as tx:
        tx.execute("SET LOCAL lock_timeout = '10s'")
        tx.execute("SELECT set_config('ameesh.reviewer_audit', 'python', true)")
        rows = tx.query("SELECT 1 FROM agent_registry WHERE name = %s AND %s = ANY(aliases)"
                        " FOR SHARE", (new, old))
        if not rows:
            raise RenameError("agent rename: %s is not an alias of %s; nothing swept"
                              % (old, new))
        return {c: n for c in REWRITTEN for n in [_rewrite(tx, c, old, new)] if n}


def _rename_tx(tx, old: str, new: str, who: str, reason: str) -> dict:
    tx.execute("SET LOCAL lock_timeout = '10s'")
    tx.execute("SET LOCAL statement_timeout = '300s'")
    # The 0462 audit row would read a moved reviewer as a withdrawal.
    tx.execute("SELECT set_config('ameesh.reviewer_audit', 'python', true)")
    rows = tx.query(
        "SELECT name, aliases, status, key_role, public_key IS NOT NULL AS has_key,"
        "       lease_owner, lease_epoch,"
        "       (lease_owner IS NOT NULL AND lease_expires_at > clock_timestamp()) AS lease_live"
        "  FROM agent_registry WHERE name = %s FOR UPDATE", (old,))
    if not rows:
        primary = agent_names.resolve(old, db=tx, operation="agents.rename")
        if primary != old:
            raise RenameError("agent rename: %s is an alias of %s; rename the primary"
                              % (old, primary))
        raise RenameError("agent rename: unknown agent: %s" % old)
    row = rows[0]
    aliases = list(row["aliases"] or [])
    if not valid_name(new):
        raise RenameError("agent rename: invalid agent name: %s" % new)
    if row["lease_live"]:
        raise RenameError("agent rename refused: %s holds a live lease (%s): stop its "
                          "runner between two turns, or wait for the lease to expire"
                          % (old, row["lease_owner"]))
    if row["status"] == "running":
        raise RenameError("agent rename refused: %s is running" % old)
    if row["has_key"] and row["key_role"] == "owner":
        raise RenameError("agent rename refused: %s holds an owner key" % old)
    # The review gate and the train read signed receipts (kept spelling)
    # against rewritten milestones, assignees and reviewers; on a lot still
    # under review that pairing is not proven to hold everywhere. Merged and
    # closed lots are past the gate.
    spellings = [old] + aliases
    # P1b #7650 : `provenance_receipts` est une table du module `dev`.
    pending = storage.of(tx).provenance.pending_lots(spellings, work.OPEN_STATES)
    if pending:
        raise RenameError("agent rename refused: %s has signed proofs on unmerged lots "
                          "(%s): rename once they are merged or closed"
                          % (old, ", ".join("#%d" % lot for lot in pending)))
    other = tx.query("SELECT name FROM agent_registry WHERE name <> %s"
                     " AND (name = %s OR %s = ANY(aliases))", (old, new, new))
    if other:
        raise RenameError("agent rename refused: %s already designates %s"
                          % (new, other[0]["name"]))
    if tx.query("SELECT 1 FROM key_holders WHERE name = %s", (new,)):
        raise RenameError("agent rename refused: %s is a human key holder name" % new)
    if new not in aliases:
        # A spelling of nobody may still be on rows: merging them would graft
        # a foreign history onto this identity.
        found = {c: n for c in REWRITTEN + CASCADED for n in [_uses(tx, c, new)] if n}
        if found:
            raise RenameError("agent rename refused: %s already appears in %s"
                              % (new, ", ".join("%s (%d)" % kv for kv in sorted(found.items()))))
    cascaded = {c: n for c in CASCADED for n in [_uses(tx, c, old)] if n}
    final_aliases = [a for a in aliases if a != new] + [old]
    tx.execute("UPDATE agent_registry SET name = %s,"
               "       aliases = ARRAY(SELECT jsonb_array_elements_text(%s::jsonb)),"
               "       updated_at = now() WHERE name = %s",
               (new, json.dumps(final_aliases), old))
    rewritten = {c: n for c in REWRITTEN for n in [_rewrite(tx, c, old, new)] if n}
    summary = {"old": old, "new": new, "actor": who, "aliases": final_aliases,
               "lease_epoch": row["lease_epoch"], "rewritten": rewritten,
               "cascaded": cascaded}
    journal = tx.query(
        "INSERT INTO agent_renames (old_name, new_name, actor, reason, lease_epoch, rewritten)"
        " VALUES (%s, %s, %s, %s, %s, %s::jsonb) RETURNING id",
        (old, new, who, reason, row["lease_epoch"],
         json.dumps({"rewritten": rewritten, "cascaded": cascaded}, sort_keys=True)))
    # The reason is free text, readable by the contents role only: the event
    # (base supervisor role) carries identifiers and the journal row.
    summary["event"] = storage.of(tx).events.record(
        RENAMED_EVENT, "agent:%s" % new, None,
        {"agent": new, "old": old, "actor": who, "rename": journal[0]["id"]})
    return summary


def valid_name(name: str) -> bool:
    """Same spelling rule as the 0418 namespace guard."""
    return (bool(name) and len(name) <= 64 and name != "all"
            and not name.startswith("human:")
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name) is not None)


def move_local_state(cfg: Config, old: str, new: str) -> str:
    """Move this host's state directory of `old` to `new`, leaving `old` as a
    symlink; idempotent, and the reverse call undoes it. Returns what was done."""
    source, target = cfg.agent_dir(old), cfg.agent_dir(new)
    if os.path.islink(target) and os.path.realpath(target) == os.path.realpath(source):
        # Reverse of a previous move: `new` is the link to `old`'s directory.
        if not os.path.isdir(source) or os.path.islink(source):
            raise RenameError("local state: %s is a link but %s is not a directory"
                              % (target, source))
        os.unlink(target)
    if os.path.islink(source) and os.path.realpath(source) == os.path.realpath(target):
        return "already moved"
    if not os.path.lexists(source):
        return "no local state"
    if os.path.lexists(target):
        raise RenameError("local state: %s already exists; nothing moved" % target)
    if os.path.islink(source):
        raise RenameError("local state: %s is a link elsewhere; nothing moved" % source)
    os.rename(source, target)
    os.symlink(os.path.basename(target), source)
    return "moved"
