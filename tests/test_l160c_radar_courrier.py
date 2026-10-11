# SPDX-License-Identifier: AGPL-3.0-only
"""L160c : courrier du radar — passif (clé `radar`, L125) et lié au lot à
chaque conflit nouveau, réveil seulement quand le conflit bloque une PR verte."""
from __future__ import annotations

import dataclasses
import shutil
import tempfile
import unittest
from unittest import mock

from ameesh import mail, radar, registry, work

from .support import PgTestCase
from .test_l160b_radar import LOTS, MIG, _git, make_repo

PROJECT = radar.Project(name="ameesh", repo="", migrations=MIG)
#: a1 tient les lots 1 et 3, a2 le lot 2 ; le lot 4 n'a pas d'assigné
ASSIGNED = [dict(row, assignee={1: "a1", 2: "a2", 3: "a1"}.get(row["id"]))
            for row in LOTS]


class SendMailTest(unittest.TestCase):
    """`radar.send_mail` sans base : `mail.send` et `work.get` simulés."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-radar-test-")
        self.repo = make_repo(self.tmp)
        self.cfg = dataclasses.make_dataclass("Cfg", ["state_dir"])(self.tmp + "/state")
        self.sent = []
        self.ci = {}
        patches = [mock.patch.object(mail, "send", side_effect=self._send),
                   mock.patch.object(work, "get", side_effect=lambda db, n: {
                       "id": n, "state": "merged" if n == 2 else "build"})]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _send(self, db, sender, to, body, **kw):
        self.sent.append(dict(kw, sender=sender, to=to, body=body))
        return len(self.sent)

    def run_once(self, lots):
        report = radar.scan(self.repo, lots, migrations=MIG)
        self.sent = []
        out = radar.send_mail(self.cfg, None, PROJECT, report,
                              ci=lambda branch: self.ci.get(branch))
        return out, self.sent

    def merge_b(self):
        _git(self.repo, "merge", "-q", "--no-edit", "lot/2-b")

    def test_passive_once_per_new_conflict(self):
        out, sent = self.run_once(ASSIGNED)
        self.assertEqual(sorted((s["key"], s["to"], s["level"]) for s in out),
                         [("migration:main:0002", "a1", "passive"),
                          ("migration:main:0002", "a1", "passive"),
                          ("pair:1-2", "a1", "passive"), ("pair:1-2", "a2", "passive")])
        for one in sent:
            self.assertEqual(one["kind"], "event")
            self.assertEqual(one["sender"], work.SYSTEM_SENDER)
            self.assertIn("radar", one["payload"])
            self.assertEqual(mail.passive_reason({"payload": one["payload"]}), "radar")
        pair = [s for s in sent if s["payload"]["radar"]["key"] == "pair:1-2"]
        self.assertEqual(sorted(s["work_item_id"] for s in pair), ["1", "2"])
        self.assertIn("#2 « b » (lot/2-b, a2)", [s for s in pair if s["to"] == "a1"][0]["body"])
        # passage suivant, rien de nouveau : aucun courrier
        self.assertEqual(self.run_once(ASSIGNED)[0], [])

    def test_wake_only_when_blocking(self):
        self.run_once(ASSIGNED)
        self.merge_b()                       # le lot 2 entre dans main
        lots = [r for r in ASSIGNED if r["id"] != 2]
        self.ci["lot/1-a"] = {"number": 77, "green": False}
        out, sent = self.run_once(lots)
        self.assertEqual([(s["key"], s["level"]) for s in out], [("target:1", "passive")])
        self.assertIn("radar", sent[0]["payload"])
        self.assertEqual(self.run_once(lots)[0], [])
        # la CI passe au vert : un réveil, une seule fois
        self.ci["lot/1-a"] = {"number": 77, "green": True}
        out, sent = self.run_once(lots)
        self.assertEqual([(s["key"], s["to"], s["level"]) for s in out],
                         [("target:1", "a1", "blocking")])
        payload = sent[0]["payload"]
        self.assertNotIn("radar", payload)
        self.assertEqual(payload["radar_blocking"]["merged"], [2])
        self.assertEqual(payload["radar_blocking"]["pr"], 77)
        self.assertEqual(mail.passive_reason({"payload": payload}), "")
        self.assertFalse(payload.get("urgent"))
        self.assertIn("PR #77 est verte", sent[0]["body"])
        self.assertIn("#2 y est entré", sent[0]["body"])
        self.assertEqual(self.run_once(lots)[0], [])

    def test_green_at_first_sight_wakes_directly(self):
        self.merge_b()
        self.ci["lot/1-a"] = {"number": 5, "green": True}
        out, _ = self.run_once([r for r in ASSIGNED if r["id"] != 2])
        self.assertEqual([(s["key"], s["level"]) for s in out if s["key"] == "target:1"],
                         [("target:1", "blocking")])

    def test_resolved_then_back_is_new(self):
        self.run_once(ASSIGNED)
        without_b = [r for r in ASSIGNED if r["id"] != 2]
        self.run_once(without_b)             # le lot 2 sort du radar : conflit résolu
        self.assertNotIn("pair:1-2", radar.read_notified(self.cfg, "ameesh"))
        out, _ = self.run_once(ASSIGNED)
        self.assertEqual(sorted(s["to"] for s in out if s["key"] == "pair:1-2"),
                         ["a1", "a2"])

    def test_failed_send_is_retried(self):
        report = radar.scan(self.repo, ASSIGNED, migrations=MIG)
        with mock.patch.object(mail, "send", side_effect=RuntimeError("base")):
            self.assertEqual(radar.send_mail(self.cfg, None, PROJECT, report, ci=None), [])
        self.assertEqual(radar.read_notified(self.cfg, "ameesh"), {})
        self.assertEqual(len(radar.send_mail(self.cfg, None, PROJECT, report, ci=None)), 4)

    def test_humans_and_unassigned_get_nothing(self):
        lots = [dict(r, assignee="human:bob") if r["id"] == 2 else r for r in ASSIGNED]
        out, _ = self.run_once(lots)
        self.assertNotIn("human:bob", {s["to"] for s in out})
        self.assertNotIn(4, {s["lot"] for s in out})


class HelpersTest(unittest.TestCase):
    def test_passive_key(self):
        self.assertEqual(mail.passive_reason({"payload": {"radar": {"key": "k"}}}), "radar")
        self.assertEqual(mail.passive_label({"payload": {"radar": {"key": "k"}}}),
                         "radar de conflits")
        self.assertEqual(mail.passive_reason({"payload": {"radar_blocking": {"k": 1}}}), "")
        self.assertEqual(mail.passive_reason(
            {"payload": {"radar": {"k": 1}, "urgent": True}}), "")

    def test_ci_green(self):
        ok = {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"}
        self.assertTrue(radar.ci_green([ok, dict(ok, conclusion="SKIPPED"),
                                        {"__typename": "StatusContext", "state": "SUCCESS"}]))
        self.assertFalse(radar.ci_green([]))
        self.assertFalse(radar.ci_green([ok, dict(ok, status="IN_PROGRESS", conclusion="")]))
        self.assertFalse(radar.ci_green([ok, dict(ok, conclusion="FAILURE")]))
        self.assertFalse(radar.ci_green([{"__typename": "StatusContext", "state": "PENDING"}]))

    def test_github_settings(self):
        got = radar.parse_settings({"projects": {"p": {"repo": "/r", "github": "o/r"}}})
        self.assertEqual(got.projects["p"].github, "o/r")
        self.assertEqual(radar.github_slug(got.projects["p"]), "o/r")
        with self.assertRaises(radar.SettingsError):
            radar.parse_settings({"projects": {"p": {"repo": "/r", "github": "pas un dépôt"}}})

    def test_slug_from_origin(self):
        tmp = tempfile.mkdtemp(prefix="ameesh-radar-test-")
        self.addCleanup(shutil.rmtree, tmp, True)
        repo = make_repo(tmp)
        project = radar.Project(name="p", repo=repo)
        self.assertIsNone(radar.github_slug(project))
        self.assertIsNone(radar.github_ci(project))
        _git(repo, "remote", "add", "origin", "git@github.com:manaty/ameesh.git")
        self.assertEqual(radar.github_slug(project), "manaty/ameesh")


class RunPassMailTest(PgTestCase):
    """Le passage de l'exécuteur dépose le courrier passif dans la base : il
    n'ouvre pas de tour (`unread_active` vide)."""

    def test_run_pass_mail(self):
        repo = make_repo(self.tmp)
        for name in ("a1", "a2"):
            registry.upsert(self.db, name, harness="claude", host=self.cfg.host)
        for row in ASSIGNED[:2]:
            work.add(self.db, title=row["title"], branch=row["branch"],
                     assignee=row["assignee"])
        cfg = dataclasses.replace(self.cfg, radar={"projects": {"ameesh": {"repo": repo}}})
        lines = radar.run_pass(cfg, self.db, ci=None)
        self.assertEqual(sorted(s["to"] for s in lines[0]["mail"]), ["a1", "a2"])
        for name in ("a1", "a2"):
            self.assertEqual(len(mail.unread(self.db, name)), 1)
            self.assertEqual(mail.unread_active(self.db, name), [])
        self.assertEqual(radar.run_pass(cfg, self.db, ci=None)[0]["mail"], [])


if __name__ == "__main__":
    unittest.main()
