# SPDX-License-Identifier: AGPL-3.0-only
"""L160d: prepared conflict resolutions — `ameesh work resolve` (throwaway
worktree, resolution commit under `refs/ameesh/resolutions/`, record), the
radar's replay in its private clone primed by `rerere-train.sh` (states
`prepared` and `stale`), the resolver, and the radar mail.

Tests without a database first (git repositories, storage mocked), then on
real Postgres (table `work_resolutions`, `work show`, `work resolve`, radar
pass)."""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from ameesh import radar, resolutions, storage, work

from .support import PgTestCase
from .test_l160b_radar import LOTS, MIG, _git, _write, make_repo

#: the line both lots 1 and 2 changed, resolved
RESOLVED = "UN-One\ndeux\ntrois\nquatre\ncinq\nsix\nsept\n"


def _commit_on(repo, branch, path, text, message):
    _git(repo, "checkout", "-q", branch)
    _write(repo, path, text)
    _git(repo, "commit", "-qam", message)
    _git(repo, "checkout", "-q", "main")


def _identity(repo):
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "user.email", "t@example.invalid")


def _manual_resolution(repo, ours="lot/1-a", theirs="lot/2-b", text=RESOLVED):
    """R(ours, theirs) made by hand in a scratch worktree; returns its sha."""
    wt = os.path.join(os.path.dirname(repo), "wt-%s" % os.urandom(3).hex())
    _git(repo, "worktree", "add", "-q", "--detach", wt, ours)
    try:
        try:
            _git(wt, "merge", "-q", "--no-ff", "--no-commit", theirs)
            raise RuntimeError("the pair should conflict")
        except AssertionError:
            pass
        _write(wt, "shared.py", text)
        _git(wt, "add", "shared.py")
        _git(wt, "commit", "-qm", "R")
        return _git(wt, "rev-parse", "HEAD")
    finally:
        _git(repo, "worktree", "remove", "--force", wt)


class FakeWork:
    """The `storage.of(db).work` operations L160d uses, in memory."""

    def __init__(self, lots):
        self.lots = {r["id"]: dict(r, state=r.get("state", "build")) for r in lots}
        self.rows: dict = {}
        self.notes: list = []

    def resolutions(self, ids):
        ids = set(ids)
        return [r for r in sorted(self.rows.values(), key=lambda r: -r["created_ts"])
                if r["work_item_id"] in ids or r["other_item_id"] in ids]

    def record_resolution(self, item_id, other_id, *, commit, lot_tip, other_tip, ref,
                          files, note, actor):
        if item_id not in self.lots or other_id not in self.lots:
            return None
        row = {"work_item_id": item_id, "other_item_id": other_id, "commit_sha": commit,
               "lot_tip": lot_tip, "other_tip": other_tip, "ref": ref, "files": list(files),
               "created_by": actor, "created_ts": float(len(self.notes) + 1)}
        self.rows[(item_id, other_id)] = row
        self.notes.append((item_id, note, actor))
        return row


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-l160d-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = make_repo(self.tmp)
        _identity(self.repo)
        self.fake = FakeWork(LOTS[:4])
        store = mock.Mock(work=self.fake)
        for patch in (mock.patch.object(storage, "of", return_value=store),
                      mock.patch.object(work, "get",
                                        side_effect=lambda db, n: self.fake.lots.get(n))):
            patch.start()
            self.addCleanup(patch.stop)

    def origin(self, *, heads_only=False):
        bare = os.path.join(self.tmp, "origin.git")
        _git(self.tmp, "init", "-q", "--bare", bare)
        if heads_only:
            hook = os.path.join(bare, "hooks", "update")
            with open(hook, "w") as fh:
                fh.write("#!/bin/sh\ncase \"$1\" in refs/heads/*) exit 0;; esac\n"
                         "echo \"refused: $1\" >&2\nexit 1\n")
            os.chmod(hook, 0o755)
        _git(self.repo, "remote", "add", "origin", bare)
        return bare


class ResolverTest(unittest.TestCase):
    def lot(self, n, **kw):
        return dict({"work_item": n, "base": "b%d" % n, "tip": "t%d" % n}, **kw)

    def test_order(self):
        choose = resolutions.choose_resolver
        # the more urgent lot merges first: the other one resolves
        self.assertEqual(choose(self.lot(1, priority=1), self.lot(2), []),
                         {"lot": 2, "reason": "priority"})
        self.assertEqual(choose(self.lot(1, priority=3), self.lot(2, priority=2), []),
                         {"lot": 1, "reason": "priority"})
        # same priority: the lot further along merges first
        self.assertEqual(choose(self.lot(1, state="build"), self.lot(2, state="qa"), []),
                         {"lot": 1, "reason": "state"})
        # then the smaller change in the conflicting files resolves
        sizes = {("b1", "t1", ("f",)): 40, ("b2", "t2", ("f",)): 3}
        self.assertEqual(choose(self.lot(1), self.lot(2), ["f"], repo="/r", sizes=sizes),
                         {"lot": 2, "reason": "smaller change"})
        # then the newer lot
        self.assertEqual(choose(self.lot(7), self.lot(3), []),
                         {"lot": 7, "reason": "newer lot"})

    def test_change_size(self):
        tmp = tempfile.mkdtemp(prefix="ameesh-l160d-test-")
        self.addCleanup(shutil.rmtree, tmp, True)
        repo = make_repo(tmp)
        base = _git(repo, "rev-parse", "main")
        tip = _git(repo, "rev-parse", "lot/1-a")
        self.assertEqual(resolutions.change_size(repo, base, tip, ["shared.py"]), 2)
        self.assertEqual(resolutions.change_size(repo, base, tip, ["other.py"]), 0)


class ReplayTest(_Base):
    """The radar's clone: primed from R, replay of the current tips."""

    def replayer(self, script="auto"):
        return resolutions.Replayer(self.repo, os.path.join(self.tmp, "state", "p.clone"),
                                    script=script)

    def check(self, script):
        sha = _manual_resolution(self.repo)
        rp = self.replayer(script)
        rp.ensure()
        self.assertEqual(rp.prime([sha]), [])
        a, b = (_git(self.repo, "rev-parse", x) for x in ("lot/1-a", "lot/2-b"))
        self.assertEqual(rp.replay(a, b)["state"], "prepared")
        # order does not matter: rerere sorts the two sides
        self.assertEqual(rp.replay(b, a)["state"], "prepared")
        # a push outside the conflicting hunk keeps the resolution valid
        _commit_on(self.repo, "lot/1-a", "shared.py",
                   "UN\ndeux\ntrois\nquatre\ncinq\nsix\nSept\n", "a elsewhere")
        a2 = _git(self.repo, "rev-parse", "lot/1-a")
        self.assertEqual(rp.replay(a2, b)["state"], "prepared")
        # a push that changes the hunk makes it stale
        _commit_on(self.repo, "lot/1-a", "shared.py",
                   "UNO\ndeux\ntrois\nquatre\ncinq\nsix\nSept\n", "a changes the hunk")
        a3 = _git(self.repo, "rev-parse", "lot/1-a")
        got = rp.replay(a3, b)
        self.assertEqual((got["state"], got["remaining"]), ("stale", ["shared.py"]))
        # nothing left behind in the agents' repository
        self.assertEqual(_git(self.repo, "status", "--porcelain"), "")
        # trained once
        self.assertEqual(rp.trained(), [sha])
        with mock.patch.object(resolutions, "train",
                               side_effect=AssertionError("trained twice")):
            rp.prime([sha])

    @unittest.skipUnless(resolutions.rerere_train_script(), "rerere-train.sh not installed")
    def test_primed_by_rerere_train_sh(self):
        self.check("auto")

    def test_primed_by_python_port(self):
        self.check(None)

    def test_resolution_from_another_host_is_fetched(self):
        bare = self.origin()
        sha = _manual_resolution(self.repo)
        _git(self.repo, "push", "-q", "origin", "%s:refs/ameesh/resolutions/2/1" % sha)
        # the agents' repository of this host never had it
        _git(self.repo, "reflog", "expire", "--expire=now", "--all")
        _git(self.repo, "gc", "-q", "--prune=now")
        self.assertNotEqual(bare, "")
        rp = self.replayer()
        rp.ensure()
        self.assertEqual(rp.prime([sha]), [])
        self.assertTrue(rp.has(sha))
        self.assertEqual(rp.prime(["0" * 40]), ["0" * 40])


class AgentFlowTest(_Base):
    """`ameesh work resolve` on the agent's side."""

    def test_open_resolve_done(self):
        bare = self.origin()
        out = resolutions.open_resolution(None, 2, 1, repo=self.repo)
        self.assertEqual((out["state"], out["files"], out["remaining"]),
                         ("open", ["shared.py"], ["shared.py"]))
        path = out["path"]
        self.assertTrue(path.startswith(os.path.join(self.repo, ".git", "ameesh-resolve")))
        self.assertIn("locked", _git(self.repo, "worktree", "list", "--porcelain"))
        # opening again shows the same resolution
        again = resolutions.open_resolution(None, 2, 1, repo=self.repo)
        self.assertTrue(again["already"])
        with self.assertRaises(resolutions.ResolveError) as ctx:
            resolutions.finish_resolution(None, 2, 1, repo=self.repo, actor="a2")
        self.assertIn("conflict markers", str(ctx.exception))
        _write(path, "shared.py", RESOLVED)
        done = resolutions.finish_resolution(None, 2, 1, repo=self.repo, actor="a2",
                                             note="kept both spellings")
        sha = done["commit"]
        self.assertEqual(done["ref"], "refs/ameesh/resolutions/2/1")
        self.assertEqual(_git(bare, "rev-parse", "refs/ameesh/resolutions/2/1"), sha)
        self.assertEqual(_git(self.repo, "rev-parse", "refs/ameesh/resolutions/2/1"), sha)
        self.assertEqual(_git(self.repo, "rev-parse", sha + "^@").split(),
                         [_git(self.repo, "rev-parse", "lot/2-b"),
                          _git(self.repo, "rev-parse", "lot/1-a")])
        message = _git(self.repo, "log", "-1", "--format=%B", sha)
        self.assertIn("ameesh-resolution: 2 1", message)
        self.assertIn("kept both spellings", message)
        self.assertEqual(_git(self.repo, "show", "%s:shared.py" % sha) + "\n", RESOLVED)
        # the lot branches did not move, the worktree is gone
        self.assertEqual(_git(self.repo, "log", "-1", "--format=%s", "lot/2-b"), "lot/2-b")
        self.assertFalse(os.path.exists(path))
        self.assertNotIn("ameesh-resolve", _git(self.repo, "worktree", "list"))
        row = self.fake.rows[(2, 1)]
        self.assertEqual((row["commit_sha"], row["files"], row["created_by"]),
                         (sha, ["shared.py"], "a2"))
        self.assertIn("resolution prepared with #1", self.fake.notes[0][1])
        self.assertIn("resolution with #1", resolutions.show_lines(
            self.fake.resolutions([2]), 2)[0])
        self.assertIn("resolved by #2", resolutions.show_lines(
            self.fake.resolutions([1]), 1)[0])

    def test_reopen_replays_unchanged_hunks(self):
        resolutions.open_resolution(None, 2, 1, repo=self.repo)
        _write(resolutions.worktree_path(self.repo, 2, 1), "shared.py", RESOLVED)
        resolutions.finish_resolution(None, 2, 1, repo=self.repo, actor="a2")
        # lot 2 moves elsewhere: the resolution is re-prepared without work
        _commit_on(self.repo, "lot/2-b", "other.py", "z\n", "b elsewhere")
        out = resolutions.open_resolution(None, 2, 1, repo=self.repo)
        self.assertEqual((out["replayed"], out["markers"]), (["shared.py"], []))
        with open(os.path.join(out["path"], "shared.py")) as fh:
            self.assertEqual(fh.read(), RESOLVED)
        done = resolutions.finish_resolution(None, 2, 1, repo=self.repo, actor="a2")
        self.assertIsNone(done["ref"])            # no origin: local only
        self.assertEqual(done["push_error"], "no origin remote")
        self.assertEqual(len(self.fake.rows), 1)  # replaced, not added

    def test_push_falls_back_to_a_branch(self):
        bare = self.origin(heads_only=True)
        path = resolutions.open_resolution(None, 2, 1, repo=self.repo)["path"]
        _write(path, "shared.py", RESOLVED)
        done = resolutions.finish_resolution(None, 2, 1, repo=self.repo, actor="a2")
        self.assertEqual(done["ref"], "refs/heads/ameesh/resolve/2-1")
        self.assertEqual(_git(bare, "rev-parse", "ameesh/resolve/2-1"), done["commit"])

    def test_clean_pair_and_abort(self):
        out = resolutions.open_resolution(None, 1, 3, repo=self.repo)
        self.assertEqual((out["state"], out["path"]), ("clean", None))
        self.assertNotIn("ameesh-resolve", _git(self.repo, "worktree", "list"))
        path = resolutions.open_resolution(None, 1, 2, repo=self.repo)["path"]
        self.assertTrue(resolutions.abort_resolution(1, 2, repo=self.repo))
        self.assertFalse(os.path.exists(path))
        self.assertFalse(resolutions.abort_resolution(1, 2, repo=self.repo))
        with self.assertRaises(resolutions.ResolveError):
            resolutions.finish_resolution(None, 1, 2, repo=self.repo)

    def test_refusals(self):
        with self.assertRaises(resolutions.ResolveError):
            resolutions.open_resolution(None, 1, 1, repo=self.repo)
        with self.assertRaises(resolutions.ResolveError):
            resolutions.open_resolution(None, 1, 99, repo=self.repo)
        self.fake.lots[3]["branch_target"] = "lot/4-d"
        with self.assertRaises(resolutions.ResolveError) as ctx:
            resolutions.open_resolution(None, 1, 3, repo=self.repo)
        self.assertIn("do not share a target", str(ctx.exception))
        self.fake.lots[4]["branch"] = None
        with self.assertRaises(resolutions.ResolveError):
            resolutions.open_resolution(None, 1, 4, repo=self.repo)

    def test_finish_after_a_failed_record_is_resumed(self):
        path = resolutions.open_resolution(None, 2, 1, repo=self.repo)["path"]
        _write(path, "shared.py", RESOLVED)
        with mock.patch.object(self.fake, "record_resolution", return_value=None):
            with self.assertRaises(resolutions.ResolveError):
                resolutions.finish_resolution(None, 2, 1, repo=self.repo, actor="a2")
        self.assertTrue(os.path.isdir(path))      # commit kept in the worktree
        done = resolutions.finish_resolution(None, 2, 1, repo=self.repo, actor="a2")
        self.assertEqual(self.fake.rows[(2, 1)]["commit_sha"], done["commit"])


class RadarTest(_Base):
    """The radar pass: resolver, `prepared`/`stale`, mail, rendering."""

    def setUp(self):
        super().setUp()
        self.cfg = dataclasses.make_dataclass("Cfg", ["state_dir"])(self.tmp + "/state")
        self.project = radar.Project(name="p", repo=self.repo, migrations=MIG)
        self.lots = [dict(r, assignee={1: "a1", 2: "a2", 3: "a1"}.get(r["id"]),
                          state="build") for r in LOTS]

    def scan(self, cache=None):
        report = radar.scan(self.repo, self.lots, migrations=MIG, cache=cache)
        radar.annotate_resolutions(self.cfg, None, self.project, report, self.lots, cache)
        return report

    def pair(self, report):
        return [p for p in report["pairs"] if p["lots"] == [1, 2]][0]

    def test_resolver_named_in_the_mail(self):
        report = self.scan()
        self.assertEqual(self.pair(report)["resolver"], {"lot": 2, "reason": "newer lot"})
        conflict = radar.conflicts_of(report)["pair:1-2"]
        branches = {b["work_item"]: b for b in report["branches"]}
        mine = radar.message_text(conflict, 2, branches)
        self.assertIn("`ameesh work resolve 2 --with 1`", mine)
        self.assertIn("ton lot est le plus récent", mine)
        self.assertIn("Le lot #2 prépare la résolution", radar.message_text(conflict, 1,
                                                                           branches))
        self.assertIn("[résolveur #2 : newer lot]", "\n".join(radar.render(report)))

    def test_prepared_then_stale(self):
        cache = radar.Cache()
        sha = _manual_resolution(self.repo, "lot/2-b", "lot/1-a")
        self.fake.record_resolution(2, 1, commit=sha, lot_tip="x", other_tip="y",
                                    ref=None, files=["shared.py"], note="", actor="a2")
        report = self.scan(cache)
        pair = self.pair(report)
        self.assertEqual(pair["state"], "prepared")
        self.assertEqual((pair["resolution"]["by"], pair["resolution"]["commit"]), (2, sha))
        self.assertEqual(radar.summary(report)["prepared"], 1)
        self.assertEqual(radar.summary(report)["conflict"], 0)
        # a prepared pair sends no mail
        self.assertNotIn("pair:1-2", radar.conflicts_of(report))
        self.assertIn("prepared #1 #2", " ".join("\n".join(radar.render(report)).split()))
        # cached: no replay when nothing moved
        with mock.patch.object(resolutions.Replayer, "replay",
                               side_effect=AssertionError("replayed again")):
            self.assertEqual(self.pair(self.scan(cache))["state"], "prepared")
        # lot 1 changes the conflicting line: stale, only lot 2 is told
        _commit_on(self.repo, "lot/1-a", "shared.py",
                   "UNO\ndeux\ntrois\nquatre\ncinq\nsix\nsept\n", "a changes the hunk")
        report = self.scan(cache)
        self.assertEqual(self.pair(report)["state"], "stale")
        stale = radar.conflicts_of(report)["stale:1-2"]
        self.assertEqual((stale["lots"], stale["files"]), ([2], ["shared.py"]))
        branches = {b["work_item"]: b for b in report["branches"]}
        text = radar.message_text(stale, 2, branches)
        self.assertIn("ne couvre plus le conflit", text)
        self.assertIn("`ameesh work resolve 2 --with 1`", text)
        self.assertIn(sha[:12], text)
        self.assertIn({"key": "pair:1-2", "from": "prepared", "to": "stale"},
                      radar.changes({"pairs": [dict(pair, state="prepared")]}, report))

    def test_stale_mail_goes_to_the_resolver_once(self):
        sha = _manual_resolution(self.repo, "lot/2-b", "lot/1-a",
                                 text="autre\ndeux\ntrois\nquatre\ncinq\nsix\nsept\n")
        self.fake.record_resolution(2, 1, commit=sha, lot_tip="x", other_tip="y",
                                    ref=None, files=["shared.py"], note="", actor="a2")
        _commit_on(self.repo, "lot/1-a", "shared.py",
                   "UNO\ndeux\ntrois\nquatre\ncinq\nsix\nsept\n", "a changes the hunk")
        sent = []
        from ameesh import mail
        with mock.patch.object(mail, "send", side_effect=lambda db, s, to, body, **kw:
                               sent.append((to, kw["payload"])) or len(sent)):
            report = self.scan()
            radar.send_mail(self.cfg, None, self.project, report, ci=None)
            radar.send_mail(self.cfg, None, self.project, report, ci=None)
        stale = [(to, p["radar"]["key"]) for to, p in sent if p["radar"]["kind"] == "stale"]
        self.assertEqual(stale, [("a2", "stale:1-2")])

    def test_database_without_the_table(self):
        store = mock.Mock()
        store.work.resolutions.side_effect = RuntimeError('relation "work_resolutions"')
        with mock.patch.object(storage, "of", return_value=store):
            report = self.scan()
        self.assertEqual(self.pair(report)["state"], "conflict")
        self.assertIn("work_resolutions", report["resolution_error"])

    def test_no_clone_without_resolutions(self):
        self.scan()
        self.assertFalse(os.path.exists(radar.clone_dir(self.cfg, "p")))


class ResolutionsDbTest(PgTestCase):
    """Table `work_resolutions` (0057), `work show`, the CLI, a radar pass."""

    def lots(self):
        a = work.add(self.db, title="a", branch="lot/1-a")
        b = work.add(self.db, title="b", branch="lot/2-b")
        return a["id"], b["id"]

    def test_record_and_list(self):
        a, b = self.lots()
        ops = storage.of(self.db).work
        row = ops.record_resolution(b, a, commit="1" * 40, lot_tip="2" * 40,
                                    other_tip="3" * 40, ref=None,
                                    files=["shared.py", "it's.py"],
                                    note="resolution prepared with #%d" % a, actor="a2")
        self.assertEqual((row["work_item_id"], row["other_item_id"], row["files"]),
                         (b, a, ["shared.py", "it's.py"]))
        self.assertIsNone(row["ref"])
        again = ops.record_resolution(b, a, commit="4" * 40, lot_tip="2" * 40,
                                      other_tip="3" * 40, ref="refs/ameesh/resolutions/x",
                                      files=[], note="again", actor="a2")
        self.assertEqual((again["commit_sha"], again["files"]), ("4" * 40, []))
        listed = ops.resolutions([a])
        self.assertEqual([(r["work_item_id"], r["commit_sha"]) for r in listed],
                         [(b, "4" * 40)])
        self.assertEqual(ops.resolutions([999999]), [])
        self.assertIsNone(ops.record_resolution(b, 999999, commit="1" * 40,
                                                lot_tip="2" * 40, other_tip="3" * 40,
                                                ref=None, files=[], note="", actor=""))
        notes = [e["note"] for e in work.events(self.db, b)]
        self.assertIn("resolution prepared with #%d" % a, notes)
        shown = self.mesh("work", "show", str(a))
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn("résolut. : resolved by #%d: 444444444444" % b, shown.stdout)
        data = json.loads(self.mesh("work", "show", str(b), "--json").stdout)
        self.assertEqual(data["resolutions"][0]["commit_sha"], "4" * 40)
        listed = self.mesh("work", "resolve", str(b))
        self.assertIn("resolution with #%d" % a, listed.stdout)

    def test_cli_and_radar_pass(self):
        repo = make_repo(self.tmp)
        _identity(repo)
        a, b = self.lots()
        env = self.env(AGENT_MAIL_NAME="a2")
        opened = self.mesh("work", "resolve", str(b), "--with", str(a), "--repo", repo,
                          env=env)
        self.assertEqual(opened.returncode, 0, opened.stderr)
        self.assertIn("conflicts: shared.py", opened.stdout)
        _write(resolutions.worktree_path(repo, b, a), "shared.py", RESOLVED)
        done = self.mesh("work", "resolve", str(b), "--with", str(a), "--repo", repo,
                        "--done", "--json", env=env)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)["ref"], None)
        cfg = dataclasses.replace(self.cfg, radar={"projects": {"p": {"repo": repo}}})
        lines = radar.run_pass(cfg, self.db, ci=None)
        self.assertEqual(lines[0]["summary"]["prepared"], 1, lines)
        snapshot = radar.read_snapshot(cfg, "p")
        pair = [p for p in snapshot["pairs"] if sorted(p["lots"]) == [a, b]][0]
        self.assertEqual((pair["state"], pair["resolution"]["by"]), ("prepared", b))


if __name__ == "__main__":
    unittest.main()
