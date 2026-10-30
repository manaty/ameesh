# SPDX-License-Identifier: AGPL-3.0-only
"""#4621 : les lecteurs nomment la phase de livraison, pas le core.

Depuis 0518, `merged` et `promoted` partagent le core `delivered` ;
`legacy_state` porte la phase. Les constats du module IT, les causes
dependency-blocking et la sortie humaine de `work show`/`work list` lisent
cette phase — sans changer les prédicats de porte (`not_blocking` reste sur
`lifecycle.DELIVERED`, et les états de policy gardent leur orthographe).
"""
from __future__ import annotations

import json
from pathlib import Path
import unittest

from tests import support  # noqa: F401  (isole la config de l'hôte avant les modules)
from tests.support import PgTestCase
from ameesh import storage, work
from ameesh.modules import it
from ameesh.modules.storage import ModuleStore


class DeliveryPhaseReadersTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE work_items CASCADE")
        self.db.execute("DELETE FROM project_modules")
        self.install_it()
        self.lot = work.add(self.db, title="livré", type="release", app="acme")["id"]
        self.artifact = Path(self.tmp, "release.py")
        self.artifact.write_text("print('ok')\n")

    def install_it(self, rules=()):
        # Politique IT permissive : ce témoin vérifie la projection de
        # `lot_state`, pas le moteur de règles (couvert par test_420).
        descriptor = it.descriptor()
        self.db.execute(
            "INSERT INTO project_modules(project, module, canon, types, schemas, rules)"
            " VALUES ('acme', 'it', 'test', %s::jsonb, %s::jsonb, %s::jsonb)",
            (json.dumps(descriptor["types"]), json.dumps(descriptor["schemas"]),
             json.dumps(list(rules))))

    @staticmethod
    def qa_rule(states=("promoted",)):
        return [{"types": list(it.TYPES), "states": list(states),
                 "subject_kind": "delivery", "evidence_kind": "qa",
                 "evidence_verdict": "ok"}]

    def declare(self):
        if ModuleStore(self.db).latest(self.lot, "it", "delivery") is None:
            it.declare(self.db, self.lot, self.artifact, actor="dev")

    def deliver(self, stage):
        self.declare()
        self.db.execute("UPDATE work_items SET state = %s WHERE id = %s",
                        (stage, self.lot))
        row = work.get(self.db, self.lot)
        self.assertEqual((row["state"], row["legacy_state"]), ("delivered", stage))

    def test_dependency_cause_names_the_delivery_phase(self):
        dependent = work.add(self.db, title="dépendant", type="release",
                             app="acme")["id"]
        store = storage.of(self.db).work_dependencies
        self.assertEqual(store.add(dependent, self.lot, actor="test"), {"added": True})
        for stage in ("merged", "promoted"):
            with self.subTest(stage=stage):
                self.deliver(stage)
                [row] = store.for_items([dependent])
                self.assertEqual(row["blocker_state"], stage)
                # le prédicat de porte ne change pas : un livré ne bloque plus
                self.assertFalse(row["blocking"])

    def test_module_audit_evidence_names_the_delivery_phase(self):
        self.deliver("promoted")
        [row] = ModuleStore(self.db).audit_evidence(
            "acme", "it", it.TYPES, "delivery", "proof")
        self.assertEqual((row["state"], row["id"]), ("promoted", self.lot))

    def test_module_audit_qa_names_the_delivery_phase(self):
        self.deliver("promoted")
        self.db.execute("UPDATE project_modules SET rules = %s::jsonb"
                        " WHERE project = 'acme' AND module = 'it'",
                        (json.dumps(self.qa_rule()),))
        [row] = ModuleStore(self.db).audit_qa(
            "acme", "it", it.TYPES, "delivery", "qa", ["promoted"])
        self.assertEqual((row["state"], row["id"]), ("promoted", self.lot))

    def test_it_findings_carry_the_delivery_phase(self):
        self.deliver("promoted")
        self.db.execute("UPDATE project_modules SET rules = %s::jsonb"
                        " WHERE project = 'acme' AND module = 'it'",
                        (json.dumps(self.qa_rule()),))
        findings = {finding["rule"]: finding
                    for finding in it.audit(ModuleStore(self.db), "acme")}
        self.assertEqual(sorted(findings), ["execution-proof", "missing_qa"])
        for rule, finding in findings.items():
            with self.subTest(rule=rule):
                self.assertEqual(finding["lot_state"], "promoted")


class HumanOutputTest(PgTestCase):
    """`work show`/`work list` : le core reste visible, la phase le distingue."""

    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE work_items CASCADE")
        self.lot = work.add(self.db, title="lot livré", app="demo")["id"]

    def line(self, proc):
        return [row for row in proc.stdout.splitlines()
                if row.split() and row.split()[0] == str(self.lot)][0]

    def test_work_show_and_list_print_the_delivery_phase(self):
        self.db.execute("UPDATE work_items SET state = 'merged' WHERE id = %s",
                        (self.lot,))
        shown = self.mesh("work", "show", str(self.lot))
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn("state: delivered", shown.stdout)
        self.assertIn("phase    : merged (delivery stage)", shown.stdout)

        listed = self.mesh("work", "list")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn("PHASE", listed.stdout)
        # ID TYPE STATE PHASE : le core reste `delivered`, la phase le distingue
        self.assertEqual(self.line(listed).split()[:4],
                         [str(self.lot), "evolution", "delivered", "merged"])

    def test_work_list_leaves_the_phase_empty_outside_delivery(self):
        listed = self.mesh("work", "list")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(self.line(listed).split()[:4],
                         [str(self.lot), "evolution", "intake", "—"])

    def test_work_show_omits_the_phase_line_outside_delivery(self):
        shown = self.mesh("work", "show", str(self.lot))
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertNotIn("phase    :", shown.stdout)


if __name__ == "__main__":
    unittest.main()
