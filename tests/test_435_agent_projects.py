# SPDX-License-Identifier: AGPL-3.0-only
"""L243-1 (#435): an Agent card lists the projects its agent works on
(`projects:`, the first being the primary one). `team` then reads as that
first project: kept beside `projects`, any other value is an error
(`agent-team-mismatch`), and so is a project listed twice. A project neither
a work package's team, in `github.projects` nor a lot's project is a warning
(`agent-project-unknown`, checked with the database only). `canon sync`
writes the list to `agent_registry.projects` (0388) and `team` as its first,
an ephemeral agent copies both from its creator."""
from __future__ import annotations

import json
import unittest

from ameesh import canon, canon_sync

from .test_canon import _CanonDbCase, _TmpMixin, codes, fiche, write


def orchestre(**fields) -> str:
    card = dict(type="Agent", title="orchestre", responsible="human:alice",
                capabilities=["read", "report-drift", "propose"], harness="claude",
                model="claude-opus", provider="anthropic",
                credential_mode="subscription")
    card.update(fields)
    return fiche(**{k: v for k, v in card.items() if v is not None})


class ProjectsCardTest(_TmpMixin, unittest.TestCase):

    def check(self, root: str):
        loaded = canon.load(root, untrusted=True)
        return loaded, canon.validate(loaded)

    def assertFinding(self, findings, code, severity=canon.ERROR, **subject):
        matches = [f for f in findings if f.code == code and f.severity == severity
                   and all(getattr(f, k) == v for k, v in subject.items())]
        self.assertTrue(matches, "%s absent from %s" % (code, findings))
        return matches

    def agent(self, loaded, name="orchestre"):
        return next(a for a in loaded.agents if a.title == name)

    def test_projects_first_is_the_team(self):
        root = self.example_copy()
        write(root, "agents/orchestre.md", orchestre(projects=["acme-web", "acme-api"]))
        loaded, findings = self.check(root)
        self.assertEqual(codes(findings), {"canon-untrusted"}, findings)
        agent = self.agent(loaded)
        self.assertEqual((agent.team, agent.projects, agent.effective_projects),
                         ("acme-web", ["acme-web", "acme-api"], ["acme-web", "acme-api"]))
        shown = {a["title"]: a for a in loaded.to_dict()["agents"]}
        self.assertEqual(shown["orchestre"]["projects"], ["acme-web", "acme-api"])
        # a card without `projects` keeps its team as its only project
        relecteur = self.agent(loaded, "relecteur")
        self.assertEqual((relecteur.projects, relecteur.effective_projects),
                         (None, ["acme-web"]))
        # the same team beside them: one truth, no finding
        write(root, "agents/orchestre.md",
              orchestre(team="acme-web", projects=["acme-web", "acme-api"]))
        _loaded, findings = self.check(root)
        self.assertEqual(codes(findings), {"canon-untrusted"}, findings)

    def test_projects_one_truth_per_card(self):
        root = self.example_copy()
        write(root, "agents/orchestre.md",
              orchestre(team="acme-api", projects=["acme-web", "acme-api"]))
        _loaded, findings = self.check(root)
        found = self.assertFinding(findings, "agent-team-mismatch", agent="orchestre")
        self.assertIn("acme-web", found[0].message)
        write(root, "agents/orchestre.md", orchestre(team="acme-web", projects=[]))
        loaded, findings = self.check(root)
        self.assertFinding(findings, "agent-team-mismatch", agent="orchestre")
        self.assertEqual(self.agent(loaded).effective_projects, [])
        write(root, "agents/orchestre.md",
              orchestre(projects=["acme-web", "acme-api", "acme-web"]))
        _loaded, findings = self.check(root)
        found = self.assertFinding(findings, "agent-project-duplicate", agent="orchestre")
        self.assertIn("acme-web", found[0].message)
        write(root, "agents/orchestre.md",
              orchestre().replace("harness:", "projects: {a: b}\nharness:"))
        _loaded, findings = self.check(root)
        self.assertFinding(findings, "agent-projects-invalid", agent="orchestre")

    def test_projects_unknown_is_a_warning_with_known_projects_only(self):
        root = self.example_copy()
        # acme-web: the team of the example's work packages
        write(root, "agents/orchestre.md",
              orchestre(projects=["acme-web", "nexl", "zorg"]))
        loaded = canon.load(root, untrusted=True)
        self.assertNotIn("agent-project-unknown", codes(canon.validate(loaded)))
        findings = canon.validate(loaded, known_projects={"nexl"})
        found = self.assertFinding(findings, "agent-project-unknown", canon.WARNING,
                                   agent="orchestre")
        self.assertIn("for orchestre: zorg (", found[0].message)
        self.assertNotIn("agent-project-unknown",
                         codes(canon.validate(loaded, known_projects={"nexl", "zorg"})))


class ProjectsSyncTest(_CanonDbCase):

    def test_sync_writes_the_projects_and_the_primary_as_team(self):
        write(self.root, "agents/orchestre.md", orchestre(projects=["acme-web", "acme-api"]))
        self.sync()
        row = self.row("orchestre")
        self.assertEqual((row["team"], row["projects"]), ("acme-web", ["acme-web", "acme-api"]))
        self.assertEqual(self.actions(self.sync())["orchestre"], "inchangé")
        # the order is the card's: a new primary project is a new team
        write(self.root, "agents/orchestre.md", orchestre(projects=["acme-api", "acme-web"]))
        self.assertNotEqual(self.actions(self.sync())["orchestre"], "inchangé")
        row = self.row("orchestre")
        self.assertEqual((row["team"], row["projects"]), ("acme-api", ["acme-api", "acme-web"]))
        # an ephemeral agent works on its creator's projects
        canon_sync.spawn(self.db, "aide1", "orchestre", 600)
        child = self.row("aide1")
        self.assertEqual((child["team"], child["projects"]),
                         ("acme-api", ["acme-api", "acme-web"]))
        # `team` alone: its only project; neither: none
        write(self.root, "agents/orchestre.md", orchestre(team="acme-web"))
        self.sync()
        row = self.row("orchestre")
        self.assertEqual((row["team"], row["projects"]), ("acme-web", ["acme-web"]))
        write(self.root, "agents/orchestre.md", orchestre())
        self.sync()
        row = self.row("orchestre")
        self.assertEqual((row["team"], row["projects"]), (None, []))

    def test_check_knows_the_lots_projects(self):
        write(self.root, "agents/orchestre.md",
              orchestre(projects=["acme-web", "nexl", "zorg"]))
        env = self.env(AMEESH_CANON=self.root, AMEESH_CANON_UNTRUSTED="1")

        def unknown():
            proc = self.mesh("canon", "check", "--json", env=env)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            return [f["message"] for f in json.loads(proc.stdout)["findings"]
                    if f["code"] == "agent-project-unknown"]

        self.db.execute("DELETE FROM work_items WHERE app IN ('nexl', 'zorg')")
        self.assertEqual(len(unknown()), 1)
        self.assertIn("for orchestre: nexl, zorg (", unknown()[0])
        self.db.execute("INSERT INTO work_items (type, title, app) "
                        "VALUES ('bug', 'x', 'nexl')")
        self.assertIn("for orchestre: zorg (", unknown()[0])
        self.db.execute("INSERT INTO work_items (type, title, app) "
                        "VALUES ('bug', 'y', 'zorg')")
        self.assertEqual(unknown(), [])
        self.assertLessEqual({"nexl", "zorg"}, canon_sync.known_projects(self.db))
