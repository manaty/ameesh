# SPDX-License-Identifier: AGPL-3.0-only
"""L148 (stage 3 C1) : les lecteurs libres lisent `workstream` d'abord.

Stage 3 C1 of the L148a plan (`docs/design/l148-plan.md` §4) starts the last
database step of #147: migration 0410 keeps `agent_registry.chantier` and
`.workstream` in sync, so the free files read the English name first and keep
the French one as a fallback for the rows the hot files still produce
(`storage/postgres/registry.py`, `backend.py` and their listings). The hot
files themselves are C2, once the PRs that hold them merge.

`identity.py` also drops its last French local names (the third element of
`aliases()` and `write_alias()`'s parameter); the module alias
`chantier_of = workstream_of` stays until stage 7.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from types import SimpleNamespace

from ameesh import alert_briefs, identity


def listing(**rows) -> list:
    """`listing(orch={"team": "t"})` -> `[{"name": "orch", "team": "t"}]`."""
    return [dict(name=name, **fields) for name, fields in rows.items()]


class ModuleDeviationBriefTest(unittest.TestCase):
    """`alert_briefs.briefs` reads workstream, with the old key as fallback."""

    ALERT = {"type": "module_deviation", "orchestrators": ["orch"],
             "project": "app", "action": "Module procedure deviation"}

    def briefs(self, rows: list) -> dict:
        return alert_briefs.briefs(self.ALERT, rows, orchestrator_names={"orch"},
                                   auditor_names=("auditeur",))

    def test_workstream_selects_the_orchestrator(self):
        out = self.briefs(listing(orch={"team": "t", "workstream": "app"}))
        self.assertIn("orch", out)

    def test_chantier_still_selects_it(self):
        out = self.briefs(listing(orch={"team": "t", "chantier": "app"}))
        self.assertIn("orch", out)

    def test_workstream_wins_over_chantier(self):
        out = self.briefs(listing(orch={"workstream": "other", "chantier": "app"}))
        self.assertNotIn("orch", out)
        out = self.briefs(listing(orch={"workstream": "app", "chantier": "other"}))
        self.assertIn("orch", out)

    def test_another_workstream_is_not_briefed(self):
        self.assertNotIn("orch", self.briefs(listing(orch={"workstream": "other"})))

    def test_auditor_also_reads_workstream(self):
        out = self.briefs(listing(orch={"workstream": "app"},
                                  auditeur={"workstream": "other"}))
        self.assertNotIn("auditeur", out)
        out = self.briefs(listing(orch={"workstream": "app"},
                                  auditeur={"workstream": "app"}))
        self.assertIn("auditeur", out)

    def test_an_auditor_without_workstream_stays_briefed(self):
        out = self.briefs(listing(orch={"workstream": "app"}, auditeur={"team": "t"}))
        self.assertIn("auditeur", out)


class AliasWorkstreamTest(unittest.TestCase):
    """`identity.aliases()` names its third field workstream, no French left."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = SimpleNamespace(config_dir=self.tmp.name,
                                   aliases_path=os.path.join(self.tmp.name, "aliases"))

    def test_write_alias_takes_workstream_by_keyword(self):
        directory = os.path.join(self.tmp.name, "work", "proj")
        identity.write_alias(self.cfg, "me", directory, workstream="app")
        self.assertEqual(identity.aliases(self.cfg),
                         [(os.path.realpath(directory), "me", "app")])
        self.assertEqual(identity.workstream_of("me", self.cfg), "app")
        self.assertIs(identity.chantier_of, identity.workstream_of)

    def test_without_workstream_the_third_field_is_empty(self):
        identity.write_alias(self.cfg, "me", self.tmp.name)
        self.assertEqual(identity.aliases(self.cfg)[0][2], "")

    def test_a_two_field_line_reads_as_no_workstream(self):
        with open(self.cfg.aliases_path, "w", encoding="utf-8") as fh:
            fh.write("other\t%s\n" % self.tmp.name)
        self.assertEqual(identity.workstream_of("other", self.cfg), "")


if __name__ == "__main__":
    unittest.main()
