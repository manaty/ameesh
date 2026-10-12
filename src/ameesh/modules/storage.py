# SPDX-License-Identifier: AGPL-3.0-only
"""Named Postgres operations for generic project modules and milestones.

This repository accepts a connection or an existing transaction, like the
core storage domains. Module semantics belong to installed descriptors.
"""
from __future__ import annotations

import json


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
        existing = self.db.query("SELECT project, canon FROM project_modules")
        for entry in declarations:
            if any(row["project"] == entry["project"] and row["canon"] != canon
                   for row in existing):
                raise ValueError("project %s is owned by another canon" % entry["project"])
        # Upsert first, then remove absent declarations; every write takes
        # the same project lock as lifecycle checks via the policy trigger.
        wanted = set()
        for entry in sorted(declarations, key=lambda row: (row["project"], row["module"])):
            wanted.add((entry["project"], entry["module"]))
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
