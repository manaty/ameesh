# SPDX-License-Identifier: AGPL-3.0-only
"""#3611 : le feu sur un porteur `integration/**` dit pourquoi rien ne s'armera.

h-23 v4 (#3599, 25/10) : le porteur poussé, `paquet.yml` lance la suite
complète sur push. `work fire 3599 --sha ab6d7b59` répond « the CI on the
frozen commit is still running » : la file attend ce run. Mais ni PR ni
étiquette `ci-full` n'existaient, et le check requis de `main` (`tests`)
n'existe que sur le run `pull_request` de `ci-full` : merge-pr refusé, fusion
admin. Le témoin rejoue ce porteur : le passage du feu nomme les causes et le
geste (PR, puis `ameesh ci run <lot> --full`), et n'entre pas en file ; un
porteur publié, une cible sans garde de la forge (Nexlink) ou une tête qui
porte déjà le check gardent la voie d'avant. `ameesh ci status` le dit aussi.
"""
from __future__ import annotations

import types
import unittest
from unittest import mock

from ameesh import ci, fire, land, packet_automerge

A = "ab6d7b590" + "0" * 31
CFG = types.SimpleNamespace(radar={"projects": {"ameesh": {
    "repo": "/srv/ameesh", "github": "manaty/ameesh", "target": "main",
    "land": "pr", "gates": ["true"]}}})
PUSH_RUN = [{"name": "suite complète du paquet / tests", "status": "in_progress",
             "conclusion": ""}]


def _marks():
    return [{"kind": "frozen", "sha": A, "actor": "impl-01", "at_ts": 1.0, "id": 1},
            {"kind": "fire", "sha": A, "actor": "mesh-design", "at_ts": 2.0, "id": 2}]


def _status(**extra):
    row = {"number": 2794, "state": "OPEN", "head": A, "base": "main", "draft": False,
           "ci": "pending", "merged": False, "merge_commit": "", "armed": False,
           "url": "https://forge/pr/2794", "branch": ROW["branch"], "rollup": []}
    row.update(extra)
    return row


ROW = {"id": 3599, "title": "Paquet horaire h-20261025-23",
       "branch": "integration/paquet-h-20261025-23-v4", "assignee": "impl-01",
       "app": "ameesh", "branch_target": "main", "state": "qa"}


class FireOnCarrierTest(unittest.TestCase):

    def _act(self, *, status=None, published=None, required=(("tests", 15368),),
             head=PUSH_RUN, branch=None):
        row = dict(ROW, branch=branch or ROW["branch"])
        marks = _marks()
        names = ("_repo_of", "_slug", "_pr_status", "_repo_info", "_target",
                 "ready_verdicts", "_queue", "_open_pr", "_merge_direct", "_fresh",
                 "_renew", "_passive", "_disarm", "_invalidate", "_queue_declared",
                 "required_checks", "_head_check_runs", "_head_statuses")
        with mock.patch.multiple(fire, **{name: mock.DEFAULT for name in names}) as patched, \
                mock.patch.object(packet_automerge, "publication", create=True) as publication:
            publication.return_value = published or {"requested": False, "armed": False,
                                                     "pr": None, "base": ""}
            patched["_repo_of"].return_value = None
            patched["_slug"].return_value = "manaty/ameesh"
            patched["_pr_status"].return_value = status
            patched["_repo_info"].return_value = {"default_branch": "main"}
            patched["_target"].return_value = "main"
            patched["ready_verdicts"].return_value = (True, "", {"class": "sensitive"})
            patched["_queue"].return_value = {"lot": 3599, "result": "queued"}
            patched["_merge_direct"].return_value = {"lot": 3599, "result": "not-ready"}
            patched["_fresh"].return_value = (row, marks)
            patched["_renew"].return_value = True
            if isinstance(required, Exception):
                patched["required_checks"].side_effect = required
            else:
                patched["required_checks"].return_value = tuple(required)
            patched["_head_check_runs"].return_value = list(head)
            patched["_head_statuses"].return_value = []
            out = fire._act(CFG, None, row, marks, gh=lambda *a, **k: None, dry_run=False,
                            repo=None, cache={})
        return out, patched, publication

    def test_unpublished_carrier_names_the_causes_and_the_gesture(self):
        """Le témoin rouge de h-23 v4 : avant #3611, le feu entrait en file et
        répondait « the CI on the frozen commit is still running »."""
        out, calls, _ = self._act()
        self.assertEqual(out["result"], "not-ready", out)
        detail = out["detail"]
        self.assertIn("no PR for integration/paquet-h-20261025-23-v4", detail)
        self.assertIn("no ci-full-%s label requested" % A[:12], detail)
        self.assertIn("required check(s) tests of main are absent", detail)
        self.assertIn("gh pr create --repo manaty/ameesh --base main "
                      "--head integration/paquet-h-20261025-23-v4", detail)
        self.assertIn("ameesh ci run 3599 --full", detail)
        calls["_queue"].assert_not_called()
        calls["_open_pr"].assert_not_called()
        # le passage lit les checks imposés par la forge seule (projet None),
        # comme l'armement du porteur (#2813)
        self.assertIsNone(calls["required_checks"].call_args.args[2])

    def test_pr_without_label_names_the_ci_run_only(self):
        out, calls, _ = self._act(status=_status())
        self.assertEqual(out["result"], "not-ready", out)
        self.assertNotIn("no PR", out["detail"])
        self.assertNotIn("gh pr create", out["detail"])
        self.assertIn("ameesh ci run 3599 --full", out["detail"])
        calls["_merge_direct"].assert_not_called()
        calls["_queue"].assert_not_called()

    def test_armed_carrier_keeps_its_path(self):
        out, calls, _ = self._act(status=_status(), published={
            "requested": True, "armed": True, "pr": 2794, "base": "main"})
        self.assertEqual(out["result"], "queued", out)
        calls["required_checks"].assert_not_called()

    def test_target_without_forge_guard_keeps_the_fire_path(self):
        """Nexlink : sans check imposé (403 de plan), le feu est la voie du porteur."""
        out, calls, _ = self._act(required=land.ForbiddenLandError("403 plan"))
        self.assertEqual(out["result"], "queued", out)
        out, calls, _ = self._act(required=())
        self.assertEqual(out["result"], "queued", out)

    def test_head_carrying_the_required_check_keeps_its_path(self):
        out, calls, _ = self._act(status=_status(), head=PUSH_RUN + [
            {"name": "tests", "status": "in_progress", "conclusion": ""}])
        self.assertEqual(out["result"], "queued", out)

    def test_unreadable_head_still_reports_the_local_causes(self):
        names = ("_head_check_runs",)
        with mock.patch.multiple(fire, **{n: mock.DEFAULT for n in names}) as patched:
            patched["_head_check_runs"].side_effect = land.LandError("gh down")
            out, calls, _ = self._act()
        self.assertEqual(out["result"], "not-ready", out)

    def test_work_branch_is_not_read(self):
        out, calls, publication = self._act(branch="lot/3611-feu-porteur")
        publication.assert_not_called()
        calls["required_checks"].assert_not_called()


class PublicationJournalTest(unittest.TestCase):

    def _publication(self, notes):
        events = [{"id": i, "note": note} for i, note in enumerate(notes, start=1)]
        with mock.patch.object(packet_automerge.work, "events_noted",
                               side_effect=lambda db, lot, prefix, since: [
                                   e for e in events if e["note"].startswith(prefix)]):
            return packet_automerge.publication(None, 3599, A)

    def test_nothing_journaled(self):
        self.assertEqual(self._publication([]),
                         {"requested": False, "armed": False, "pr": None, "base": ""})

    def test_applied_full_request_and_armed_merge(self):
        label = "ci-full-%s" % A
        notes = ["ci-label applied pr=2794 label=%s sha=%s base=%s mode=full token=%s"
                 % (label, A, "c" * 40, "d" * 16),
                 "auto-merge armed repo=manaty/ameesh pr=2794 sha=%s base=main method=merge "
                 "required=tests — ameesh merges" % A]
        self.assertEqual(self._publication(notes),
                         {"requested": True, "armed": True, "pr": 2794, "base": "main"})

    def test_lot_request_or_other_head_does_not_count(self):
        other = "e" * 40
        notes = ["ci-label applied pr=2794 label=ci-lot-%s sha=%s base=%s mode=lot token=%s"
                 % (A, A, "c" * 40, "d" * 16),
                 "ci-label applied pr=2794 label=ci-full-%s sha=%s base=%s mode=full token=%s"
                 % (other, other, "c" * 40, "d" * 16),
                 "auto-merge armed repo=manaty/ameesh pr=2794 sha=%s base=main method=merge "
                 "required=tests" % A,
                 "auto-merge disarmed repo=manaty/ameesh pr=2794 sha=%s — pushed" % A]
        self.assertEqual(self._publication(notes),
                         {"requested": False, "armed": False, "pr": None, "base": ""})


class CiStatusCarrierTest(unittest.TestCase):

    def _report(self, **carrier):
        return {"work_item": 3599, "title": ROW["title"], "branch": ROW["branch"], "runs": [],
                "carrier": dict({"sha": A, "requested": False, "armed": False, "pr": None,
                                 "base": ""}, **carrier)}

    def test_unrequested_carrier_says_nothing_merges_it(self):
        text = "\n".join(ci.render_status(self._report()))
        self.assertIn("no CI run seen", text)
        self.assertIn("packet carrier: no ci-full request on the frozen head %s" % A[:12], text)
        self.assertIn("ameesh ci run 3599 --full", text)

    def test_requested_but_not_armed(self):
        text = "\n".join(ci.render_status(self._report(requested=True)))
        self.assertIn("auto-merge is not armed", text)

    def test_armed(self):
        text = "\n".join(ci.render_status(self._report(requested=True, armed=True, pr=2794,
                                                       base="main")))
        self.assertIn("auto-merge armed on %s → main (PR #2794)" % A[:12], text)

    def test_status_of_reads_the_carrier_only_for_a_frozen_carrier(self):
        with mock.patch.object(ci, "frozen_of", return_value={"sha": A}), \
                mock.patch.object(packet_automerge, "publication",
                                  return_value={"requested": False, "armed": False,
                                                "pr": None, "base": ""}) as publication:
            self.assertEqual(ci._carrier_of(None, ROW)["sha"], A)
            self.assertIsNone(ci._carrier_of(None, dict(ROW, branch="lot/3611-x")))
        publication.assert_called_once_with(None, 3599, A)
        with mock.patch.object(ci, "frozen_of", return_value=None):
            self.assertIsNone(ci._carrier_of(None, ROW))


if __name__ == "__main__":
    unittest.main()
