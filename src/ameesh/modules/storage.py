# SPDX-License-Identifier: AGPL-3.0-only
"""Named Postgres operations for generic project modules and milestones.

This repository accepts a connection or an existing transaction, like the
core storage domains. Module semantics belong to installed descriptors.
"""
from __future__ import annotations

import json


def _phase_state(alias: str) -> str:
    """#4621 : la phase de livraison, lue par les états de policy.

    Depuis 0518 le core `delivered` porte `merged` et `promoted` ; un constat
    doit nommer la phase (`merged`/`promoted`), comme les règles `require_qa`.
    `to_jsonb` garde la lecture compatible avec une base d'avant 0518.
    """
    return "coalesce(to_jsonb(%s)->>'legacy_state', %s.state) AS state" % (
        alias, alias)


class ModuleStore:
    def __init__(self, db):
        self.db = db

    def replace(self, canon, declarations):
        with self.db.transaction() as tx:
            ModuleStore(tx)._replace_locked(canon, declarations)

    def _replace_locked(self, canon, declarations):
        # Serialized across all canons; ownership conflicts must never steal
        # another canon's project, including projects removed in this sync.
        self.db.execute("SELECT pg_advisory_xact_lock(hashtext('ameesh-project-modules'))")
        existing = self.db.query("SELECT project, module, canon, types, schemas, rules "
                                 "FROM project_modules")
        for entry in declarations:
            if any(row["project"] == entry["project"] and row["canon"] != canon
                   for row in existing):
                raise ValueError("project %s is owned by another canon" % entry["project"])
        # Upsert first, then remove absent declarations; every write takes
        # the same project lock as lifecycle checks via the policy trigger.
        wanted = set()
        for entry in sorted(declarations, key=lambda row: (row["project"], row["module"])):
            wanted.add((entry["project"], entry["module"]))
            # Skip the INSERT entirely: a BEFORE INSERT policy trigger also
            # acquires project locks, even if ON CONFLICT would change nothing.
            if any(row["project"] == entry["project"]
                   and row["module"] == entry["module"]
                   and row["canon"] == canon
                   and all(row[name] == entry[name] for name in ("types", "schemas", "rules"))
                   for row in existing):
                continue
            self.db.execute(
                "INSERT INTO project_modules (project, module, canon, types, schemas, rules) "
                "VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb) "
                "ON CONFLICT (project, module) DO UPDATE SET "
                "types = EXCLUDED.types, schemas = EXCLUDED.schemas, rules = EXCLUDED.rules",
                (entry["project"], entry["module"], canon, json.dumps(entry["types"]),
                 json.dumps(entry["schemas"]), json.dumps(entry["rules"])))
        for row in self.db.query("SELECT project, module FROM project_modules WHERE canon = %s",
                                 (canon,)):
            if (row["project"], row["module"]) not in wanted:
                self.db.execute("DELETE FROM project_modules WHERE project = %s AND module = %s",
                                (row["project"], row["module"]))

    def enabled(self, project, module):
        return bool(self.db.query("SELECT module FROM project_modules "
                                  "WHERE project = %s AND module = %s", (project, module)))

    def admits(self, project, kind):
        return bool(self.db.query("SELECT module FROM project_modules "
                                  "WHERE project = %s AND types ? %s", (project, kind)))

    def latest(self, item_id, module, kind):
        rows = self.db.query("SELECT id, work_item_id, module, kind, subject, binding_id, payload, actor "
                             "FROM work_module_milestones WHERE work_item_id = %s "
                             "AND module = %s AND kind = %s ORDER BY id DESC LIMIT 1",
                             (int(item_id), module, kind))
        return rows[0] if rows else None

    def milestones(self, item_id, limit=100):
        return self.db.query("SELECT id, work_item_id, module, kind, subject, binding_id, payload, "
                             "actor, at, extract(epoch from at)::float8 AS at_ts "
                             "FROM work_module_milestones WHERE work_item_id = %s "
                             "ORDER BY id DESC LIMIT %s", (int(item_id), int(limit)))

    def record(self, item_id, module, kind, subject, payload, *, actor, binding_id=None):
        rows = self.db.query(
            "INSERT INTO work_module_milestones "
            "(work_item_id, module, kind, subject, payload, actor, binding_id) "
            "VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s) "
            "RETURNING id, work_item_id, module, kind, subject, payload, actor, binding_id",
            (int(item_id), module, kind, subject, json.dumps(payload), actor, binding_id))
        return rows[0]

    def active(self):
        """Active installed names; tolerate a database predating modules."""
        rows = self.db.query("SELECT to_regclass('project_modules') AS relation")
        if not rows or not rows[0].get("relation"):
            return []
        return self.db.query("SELECT project, module FROM project_modules ORDER BY project, module")

    def stored(self):
        """Applied project policies, full rows; tolerate a predating database.

        L98 T2a: `ameesh modules --active` and `doctor` read the applied truth
        here, then compare its descriptor digest with the readable canon."""
        rows = self.db.query("SELECT to_regclass('project_modules') AS relation")
        if not rows or not rows[0].get("relation"):
            return []
        return self.db.query("SELECT canon, project, module, types, schemas, rules "
                             "FROM project_modules ORDER BY project, module")

    def audit_evidence(self, project, module, types, subject_kind, evidence_kind):
        """Read current declarations and matching evidence in one snapshot."""
        return self.db.query(
            "SELECT w.id, w.title, w.type, " + _phase_state("w") + ", w.assignee, "
            "extract(epoch from w.created_at)::float8 AS created_ts, "
            "d.id AS declaration_id, d.subject, "
            "extract(epoch from d.at)::float8 AS declaration_ts, "
            "EXISTS (SELECT 1 FROM work_module_milestones e "
            "WHERE e.work_item_id = w.id AND e.module = p.module "
            "AND e.kind = %s AND e.binding_id = d.id AND e.subject = d.subject) AS proved "
            "FROM work_items w JOIN project_modules p "
            "ON p.project = coalesce(nullif(w.app, ''), w.workstream, '') "
            "LEFT JOIN LATERAL (SELECT id, subject, at FROM work_module_milestones "
            "WHERE work_item_id = w.id AND module = p.module AND kind = %s "
            "ORDER BY id DESC LIMIT 1) d ON true "
            "WHERE p.project = %s AND p.module = %s "
            "AND w.type IN (SELECT jsonb_array_elements_text(%s::jsonb)) "
            "AND w.state <> 'closed' ORDER BY w.id",
            (evidence_kind, subject_kind, project, module, json.dumps(list(types))))

    def policy(self, project, module):
        """Installed policy of a project module, or None when it is not active."""
        rows = self.db.query("SELECT project, module, canon, types, schemas, rules "
                             "FROM project_modules WHERE project = %s AND module = %s",
                             (project, module))
        return rows[0] if rows else None

    def audit_qa(self, project, module, types, subject_kind, evidence_kind, states):
        """Current declaration and latest evidence of lots in the given states (L244).

        One snapshot: a lot whose current declaration has no matching `ok`
        evidence, or whose evidence is bound to an earlier declaration, is a
        deviation the module decides how to report."""
        return self.db.query(
            "SELECT w.id, w.title, w.type, " + _phase_state("w") + ", w.assignee, "
            "extract(epoch from w.created_at)::float8 AS created_ts, "
            "d.id AS declaration_id, d.subject, "
            "extract(epoch from d.at)::float8 AS declaration_ts, "
            "e.id AS evidence_id, e.binding_id AS evidence_binding, "
            "e.payload->>'verdict' AS evidence_verdict "
            "FROM work_items w JOIN project_modules p "
            "ON p.project = coalesce(nullif(w.app, ''), w.workstream, '') "
            "LEFT JOIN LATERAL (SELECT id, subject, at FROM work_module_milestones "
            "WHERE work_item_id = w.id AND module = p.module AND kind = %s "
            "ORDER BY id DESC LIMIT 1) d ON true "
            "LEFT JOIN LATERAL (SELECT id, binding_id, payload FROM work_module_milestones "
            "WHERE work_item_id = w.id AND module = p.module AND kind = %s "
            "ORDER BY id DESC LIMIT 1) e ON true "
            "WHERE p.project = %s AND p.module = %s "
            "AND w.type IN (SELECT jsonb_array_elements_text(%s::jsonb)) "
            "AND EXISTS (SELECT 1 FROM jsonb_array_elements_text(%s::jsonb) s(value) "
            "WHERE s.value IN (w.state, to_jsonb(w)->>'legacy_state')) "
            "ORDER BY w.id",
            (subject_kind, evidence_kind, project, module,
             json.dumps(list(types)), json.dumps(list(states))))
