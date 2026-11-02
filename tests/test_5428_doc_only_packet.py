"""#5428 (canon §3) : un paquet doc seule, prouvé sur son diff réel, n'attend
aucune CI à l'admission ni à l'atterrissage ; tout le reste y retombe."""
import os
from unittest import mock

from ameesh import ci_parts, land, merge_queue

from .test_1651_ci_parts import _Contract, contexts
from .test_l160b_radar import _git, _write
from .test_l160e2_land import FakeGh, _Land

PACKET = "integration/paquet-5428-t"
MANIFEST = "reviews/integration-paquet-5428-t.md"


def _klass(*rules, **extra):
    """Une classe calculée : `sensitive` par défaut, sans règle nommée."""
    return dict({"class": "sensitive", "doubt": False, "why": "policy",
                 "files": [{"path": "f", "class": "sensitive", "rule": rule}
                           for rule in rules or (None,)]}, **extra)


class DocOnlyPacketTest(_Contract):
    def packet(self, files=None, chmod=None, link=None):
        self.fake.lots[4]["branch"] = PACKET
        _git(self.repo, "checkout", "-q", "-b", PACKET, "main")
        for name in files if files is not None else ("docs/roadmap.md", MANIFEST):
            _write(self.repo, name, "x\n")
        if chmod:
            _write(self.repo, chmod, "x\n")
            os.chmod(os.path.join(self.repo, chmod), 0o755)
        if link:
            os.makedirs(os.path.join(self.repo, "docs"), exist_ok=True)
            os.symlink("../shared.py", os.path.join(self.repo, link))
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "paquet")
        _git(self.repo, "push", "-q", "origin", PACKET)
        _git(self.repo, "checkout", "-q", "main")
        self.ready(4)
        return _git(self.repo, "rev-parse", PACKET)

    def enter(self, tip, *, green=False, runs=None, klass=None):
        status = {"number": 7, "url": "u", "head": tip, "green": green, "draft": False}
        with mock.patch.object(merge_queue, "_class_of", return_value=klass or _klass()):
            return merge_queue.enter(None, 4, repo=self.repo, actor="orch",
                                     ci=lambda branch: status, clone=self.clone(),
                                     project=self.project,
                                     commit_ci=lambda sha: runs or contexts())

    def test_a_docs_packet_enters_without_ci_and_says_so(self):
        row = self.enter(self.packet())
        self.assertEqual(row["state"], "queued")
        note = [text for lot, text, actor in self.fake.notes if "queued at" in text][-1]
        self.assertIn("PR #7 doc-only, no CI awaited", note)
        self.assertNotIn("green", note)
        self.assertIn("(0 due)", note)

    def test_anything_else_falls_back_on_the_ci(self):
        for why, kwargs in (
                ("workflow", {"files": ["docs/a.md", ".github/workflows/x.yml"]}),
                ("site config", {"files": ["docs/a.md", "site/mkdocs.yml"]}),
                ("scripts", {"files": ["docs/a.md", "scripts/x.md"]}),
                ("non-markdown under docs", {"files": ["docs/conf.py"]}),
                ("hidden under docs", {"files": ["docs/.config/a.md"]}),
                ("markdown elsewhere", {"files": ["README.md"]}),
                ("another packet's manifest", {"files": ["reviews/integration-paquet-x.md"]}),
                ("executable", {"files": [], "chmod": "docs/run.md"}),
                ("symlink", {"files": [], "link": "docs/link.md"})):
            with self.subTest(why=why):
                tip = self.packet(**kwargs)
                with self.assertRaisesRegex(merge_queue.QueueError, "not green"):
                    self.enter(tip)
                _git(self.repo, "branch", "-qD", PACKET)
                _git(self.repo, "push", "-q", "origin", "--delete", PACKET)

    def test_a_part_due_on_the_selector_falls_back_on_the_ci(self):
        _write(self.repo, ".ci/parts.json",
               '[{"context": "lot/x-suite", "paths": ["docs/**"]}]')
        self.publish_main()
        with self.assertRaisesRegex(merge_queue.QueueError, "not green"):
            self.enter(self.packet())

    def test_only_this_packet_manifest_is_hidden_from_the_selector(self):
        _write(self.repo, ".ci/parts.json",
               '[{"context": "lot/x-suite", "paths": ["reviews/**"]}]')
        self.publish_main()
        self.assertEqual(self.enter(self.packet())["state"], "queued")
        tip = _git(self.repo, "rev-parse", PACKET)
        self.assertEqual(ci_parts.due(self.project, tip, self.base(), self.repo)["due"],
                         ("lot/x-suite",))

    def test_another_review_file_stays_visible_to_the_selector(self):
        _write(self.repo, ".ci/parts.json",
               '[{"context": "lot/x-suite", "paths": ["reviews/**"]}]')
        self.publish_main()
        with self.assertRaisesRegex(merge_queue.QueueError, "not green"):
            self.enter(self.packet(files=["docs/a.md", MANIFEST, "reviews/other.md"]))

    def test_a_named_rule_a_doubt_or_a_release_keeps_the_ci(self):
        tip = self.packet()
        for why, klass in (("rule", _klass("docs/**")),
                           ("critical", _klass(None, None) | {"files": [
                               {"path": "f", "class": "light", "rule": None,
                                "critical": True}]}),
                           ("doubt", _klass(doubt=True)),
                           ("release", _klass(**{"class": "release"})),
                           ("no file", _klass() | {"files": []})):
            with self.subTest(why=why), self.assertRaisesRegex(merge_queue.QueueError,
                                                               "not green|full suite"):
                self.enter(tip, klass=klass)

    def test_a_red_check_refuses_the_docs_packet(self):
        with self.assertRaisesRegex(merge_queue.QueueError, "is red .*docs-gate"):
            self.enter(self.packet(), runs=dict(contexts(**{"lot/docs-gate": "failed"}),
                                                negative=["lot/docs-gate"]))

    def test_a_work_branch_keeps_the_ci(self):
        tip_branch = self.lot_touching("docs")
        self.ready(4)
        tip = _git(self.repo, "rev-parse", tip_branch)
        with self.assertRaisesRegex(merge_queue.QueueError, "not green"):
            self.enter(tip)

    def test_landing_proves_it_again_on_the_read_head(self):
        tip = self.packet()
        row = {"branch": PACKET, "target": "main", "frozen_sha": tip}
        landing = land._Landing.__new__(land._Landing)
        landing.row, landing.project, landing.replayer = row, self.project, self.clone()
        landing.db, landing.lot, landing.cfg, landing.parts = None, 4, None, None
        for klass, expected in ((_klass(), True), (_klass("docs/**"), False)):
            with self.subTest(klass=klass), mock.patch.object(
                    merge_queue, "_class_of", return_value=klass), mock.patch(
                    "ameesh.radar.commit_ci", return_value=contexts()):
                self.assertEqual(landing.doc_only(tip) is not None, expected)
                if expected:
                    self.assertIsNone(landing.required_ci())


class FollowPrTest(_Land):
    """L'attente de la CI sur la PR : un paquet doc seule prouvé ne l'attend pas."""
    LAND = "pr"

    def pending(self, doc_only):
        self.queued(4)
        gh = FakeGh()
        gh.head = self.tip(4)
        out = self.land(4, gh=gh)
        self.assertEqual(out["result"], "landing", out)
        gh.head, gh.ci = out["sha"], "pending"
        with mock.patch.object(land._Landing, "doc_only", **doc_only):
            return self.land(4, gh=gh), gh

    def merge_calls(self, gh):
        return [call for call in gh.calls if call[:2] == ["pr", "merge"]]

    def test_a_proven_docs_packet_merges_on_a_pending_ci(self):
        out, gh = self.pending({"return_value": {"due": ()}})
        self.assertTrue(self.merge_calls(gh), out)

    def test_anything_else_keeps_waiting(self):
        out, gh = self.pending({"return_value": None})
        self.assertEqual(out["result"], "landing", out)
        self.assertFalse(self.merge_calls(gh))

    def test_a_red_check_ejects(self):
        out, gh = self.pending({"side_effect": merge_queue.QueueError("is red")})
        self.assertEqual(out["result"], "ejected", out)
        self.assertFalse(self.merge_calls(gh))
