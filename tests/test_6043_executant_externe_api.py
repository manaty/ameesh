# SPDX-License-Identifier: AGPL-3.0-only
"""#6043 (manaty/ameesh#4642) : l'API d'un exécutant externe (adaptateur IMA).

Écart 1 : `work plan --project P --executor X` rend prêts pour X ses lots
réservés, sans que L165 les prenne. Écart 2 (volet sortie) : `work add --json`
rend le lot créé. Écart 3 : `work links --project P` lit en une fois les liens
typés des lots ouverts du projet.
"""
from __future__ import annotations

import json

from ameesh import project_plan, storage, work
from ameesh import work_dependencies as dependencies

from . import test_3447_prise_externe as prise_externe
from .test_l119_backlog import _Base


class ExecutantExterneApiTest(_Base):
    external_agent = prise_externe.PriseExterneTest.external_agent
    reserve = prise_externe.PriseExterneTest.reserve

    def test_le_plan_porte_sur_un_executant_rend_ses_lots_prets(self):
        self.external_agent()
        self.agent("claude1", team="nexlink")
        lot = self.reserve()
        general = project_plan.report(self.db, "nexlink")
        [row] = [r for r in general["lots"] if r["id"] == lot["id"]]
        self.assertIn("reserved for external executor", row["waiting"])
        self.assertNotIn(lot["id"], general["ready"])
        mine = project_plan.report(self.db, "nexlink", executor="bureau")
        self.assertIn(lot["id"], mine["ready"])
        other = project_plan.report(self.db, "nexlink", executor="human:autre")
        self.assertNotIn(lot["id"], other["ready"])
        # la vue de l'exécutant ne change rien à la prise L165
        self.assertEqual(self.take(), [])
        self.assertIsNone(work.get(self.db, lot["id"])["assignee"])

    def test_cli_plan_executor(self):
        self.external_agent()
        lot = self.reserve()
        proc = self.mesh("work", "plan", "--project", "nexlink", "--executor", "bureau",
                         "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(lot["id"], json.loads(proc.stdout)["ready"])
        proc = self.mesh("work", "plan", str(lot["id"]), "--executor", "bureau")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--executor goes with --project", proc.stderr)

    def test_cli_add_json_rend_le_lot_cree(self):
        self.external_agent()
        proc = self.mesh("work", "add", "--title", "geste ima", "--app", "agence-x",
                         "--new-project", "--external-executor", "bureau",
                         "--actor", "human:proprio", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(out["schema"], "ameesh.work.add/1")
        self.assertEqual(out["lot"]["title"], "geste ima")
        self.assertEqual(out["lot"]["external_executor"], "bureau")
        self.assertEqual(work.get(self.db, out["lot"]["id"])["title"], "geste ima")
        # le texte habituel reste lisible, sur stderr
        self.assertIn("#%d created" % out["lot"]["id"], proc.stderr)

    def test_liens_types_du_projet_en_une_lecture(self):
        a, b, c = [work.add(self.db, title=t, app="nexlink")["id"] for t in "abc"]
        other = work.add(self.db, title="ailleurs", app="autre")["id"]
        store = storage.of(self.db).work_dependencies
        store.add_link(a, b, "part_of", actor="test")
        store.add_link(other, c, "duplicates", actor="test")
        out = dependencies.project_links(self.db, "nexlink")
        lots = {row["id"]: row["links"] for row in out["lots"]}
        self.assertNotIn(other, lots)
        self.assertEqual([(r["label"], r["lot"]) for r in lots[a]], [("part of", b)])
        self.assertEqual([(r["label"], r["lot"]) for r in lots[b]], [("has part", a)])
        self.assertEqual([(r["label"], r["lot"]) for r in lots[c]], [("duplicated by", other)])
        proc = self.mesh("work", "links", "--project", "nexlink", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["lots"], json.loads(json.dumps(out["lots"])))
        proc = self.mesh("work", "links", str(a), "--project", "nexlink")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("no lot, no edit", proc.stderr)
        proc = self.mesh("work", "links")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("needs a lot, or --project", proc.stderr)
