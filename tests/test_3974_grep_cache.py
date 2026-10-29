# SPDX-License-Identifier: AGPL-3.0-only
"""#3974 — balayages d'auctorialité gardés par l'hôte et bornés.

Témoin du lot : la même lecture deux fois, la seconde sans processus
`git grep` (le cache en mémoire de #3753 est vidé entre les deux, comme entre
deux exécutions). Un autre arbre relance le balayage, mais pas d'autres motifs ; un
échec n'est jamais gardé. Une échéance épuisée refuse en nommant le lot et
l'étape, même quand une couche intermédiaire l'a lue comme un doute.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from ameesh import canon, grep_cache, merge_queue, review_authors


def _git_env() -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
               GIT_AUTHOR_NAME="banc", GIT_AUTHOR_EMAIL="banc@example.invalid",
               GIT_COMMITTER_NAME="banc", GIT_COMMITTER_EMAIL="banc@example.invalid")
    return env


def _git(cwd: str, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, env=_git_env(),
                          capture_output=True, check=True)
    return proc.stdout.decode("utf-8").strip()


def _greps(run) -> int:
    return sum(1 for call in run.call_args_list
               if "grep" in list(call.args[0] if call.args else call.kwargs["args"]))


class GrepCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t3974-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        _git(self.repo, "init", "-q")
        self.write("a.py", "un = 1\ndeux = 2\n")
        self.write("b.py", "trois = 3\n")
        self.first = self.commit()
        env = mock.patch.dict(os.environ, {
            "AMEESH_GREP_CACHE": "on",
            "AMEESH_GREP_CACHE_DIR": os.path.join(self.tmp, "cache")})
        env.start()
        self.addCleanup(env.stop)
        switch = mock.patch.object(grep_cache, "ENABLED", True)
        switch.start()
        self.addCleanup(switch.stop)
        canon._content_cache_clear()
        self.addCleanup(canon._content_cache_clear)

    def write(self, path: str, text: str) -> None:
        with open(os.path.join(self.repo, path), "w", encoding="utf-8") as handle:
            handle.write(text)

    def commit(self) -> str:
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "c")
        return _git(self.repo, "rev-parse", "HEAD")

    def counted(self, fn, *args, **kwargs):
        canon._content_cache_clear()        # un autre processus (#3753 vide)
        with mock.patch.object(canon.subprocess, "run",
                               wraps=subprocess.run) as run:
            value = fn(*args, **kwargs)
        return value, _greps(run)

    def test_the_same_reading_twice_runs_no_second_grep(self):
        first, launched = self.counted(review_authors._tree_lines, self.repo,
                                       self.first, ["deux = 2", "absente"])
        self.assertEqual(first, {"deux = 2"})
        self.assertEqual(launched, 1)
        again, launched = self.counted(review_authors._tree_lines, self.repo,
                                       self.first, ["absente", "deux = 2"])
        self.assertEqual(again, first)
        self.assertEqual(launched, 0)

    def test_source_paths_are_kept_by_tree(self):
        first, launched = self.counted(review_authors._source_paths, self.repo,
                                       self.first, ["trois = 3"], {})
        self.assertEqual(first, {"b.py"})
        self.assertEqual(launched, 1)
        again, launched = self.counted(review_authors._source_paths, self.repo,
                                       self.first, ["trois = 3"], {})
        self.assertEqual(again, {"b.py"})
        self.assertEqual(launched, 0)

    def test_an_empty_result_is_kept(self):
        self.counted(review_authors._tree_lines, self.repo, self.first, ["nulle"])
        value, launched = self.counted(review_authors._tree_lines, self.repo,
                                       self.first, ["nulle"])
        self.assertEqual(value, set())
        self.assertEqual(launched, 0)

    def test_another_tree_greps_again_but_other_patterns_share_the_tree(self):
        self.counted(review_authors._tree_lines, self.repo, self.first, ["un = 1"])
        self.write("b.py", "un = 1\n")
        second = self.commit()
        value, launched = self.counted(review_authors._tree_lines, self.repo,
                                       second, ["un = 1"])
        self.assertEqual((value, launched), ({"un = 1"}, 1))
        value, launched = self.counted(review_authors._tree_lines, self.repo,
                                       self.first, ["trois = 3", "absente"])
        self.assertEqual((value, launched), ({"trois = 3"}, 0))
        # le mode binaire (-a) n'est pas le mode texte (-I)
        _value, launched = self.counted(review_authors._tree_lines, self.repo,
                                        self.first, ["un = 1"], binary=True)
        self.assertEqual(launched, 1)

    def test_old_pattern_limited_entries_do_not_hide_other_lines(self):
        grep_cache.cached(self.repo, self.first, "lines-I", ["un = 1"],
                          lambda: ["un = 1"])
        value, launched = self.counted(review_authors._tree_lines, self.repo,
                                       self.first, ["trois = 3"])
        self.assertEqual((value, launched), ({"trois = 3"}, 1))

    def test_the_same_tree_in_another_commit_shares_the_entry(self):
        self.counted(review_authors._tree_lines, self.repo, self.first, ["un = 1"])
        _git(self.repo, "commit", "-q", "--allow-empty", "-m", "vide")
        empty = _git(self.repo, "rev-parse", "HEAD")
        value, launched = self.counted(review_authors._tree_lines, self.repo,
                                       empty, ["un = 1"])
        self.assertEqual((value, launched), ({"un = 1"}, 0))

    def test_a_failed_read_is_not_kept(self):
        failing = subprocess.CompletedProcess(["git"], 2, b"", b"boom")
        real = review_authors.plan_git._git

        def flaky(repo, *args, **kwargs):
            return failing if args[:1] == ("grep",) else real(repo, *args, **kwargs)

        with mock.patch.object(review_authors.plan_git, "_git", side_effect=flaky):
            self.assertIsNone(review_authors._tree_lines(self.repo, self.first,
                                                         ["un = 1"]))
        value, launched = self.counted(review_authors._tree_lines, self.repo,
                                       self.first, ["un = 1"])
        self.assertEqual((value, launched), ({"un = 1"}, 1))

    def test_off_switch_and_unreadable_tree_compute(self):
        calls = []
        with mock.patch.dict(os.environ, {"AMEESH_GREP_CACHE": "off"}):
            for _ in range(2):
                grep_cache.cached(self.repo, self.first, "m", ["x"],
                                  lambda: calls.append(1) or [])
        for _ in range(2):
            grep_cache.cached(self.repo, "0" * 40, "m", ["x"],
                              lambda: calls.append(1) or [])
        self.assertEqual(len(calls), 4)

    def _info_attributes(self, text: str) -> None:
        path = os.path.join(self.repo, ".git", "info", "attributes")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_changed_info_attributes_change_the_key(self):
        # B/P2 cx2 : même arbre, `a.py binary` dans info/attributes → -I l'écarte
        first, _launched = self.counted(review_authors._tree_lines, self.repo,
                                        self.first, ["un = 1"])
        self.assertEqual(first, {"un = 1"})
        self._info_attributes("a.py binary\n")
        value, launched = self.counted(review_authors._tree_lines, self.repo,
                                       self.first, ["un = 1"])
        self.assertEqual((value, launched), (set(), 1))
        sources, launched = self.counted(review_authors._source_paths, self.repo,
                                         self.first, ["un = 1"], {})
        self.assertEqual((sources, launched), (set(), 1))
        self._info_attributes("")
        value, launched = self.counted(review_authors._tree_lines, self.repo,
                                       self.first, ["un = 1"])
        self.assertEqual((value, launched), ({"un = 1"}, 0))

    def test_another_clone_with_other_attributes_does_not_share(self):
        self.counted(review_authors._tree_lines, self.repo, self.first, ["un = 1"])
        other = os.path.join(self.tmp, "autre")
        _git(self.tmp, "clone", "-q", self.repo, other)
        path = os.path.join(other, ".git", "info", "attributes")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("a.py binary\n")
        value, launched = self.counted(review_authors._tree_lines, other,
                                       self.first, ["un = 1"])
        self.assertEqual((value, launched), (set(), 1))

    def test_worktree_attributes_do_not_change_the_reading(self):
        # le balayage lit les attributs de l'arbre balayé, pas du dépôt de travail
        self.write(".gitattributes", "a.py binary\n")
        value, launched = self.counted(review_authors._tree_lines, self.repo,
                                       self.first, ["un = 1"])
        self.assertEqual((value, launched), ({"un = 1"}, 1))
        os.unlink(os.path.join(self.repo, ".gitattributes"))
        value, launched = self.counted(review_authors._tree_lines, self.repo,
                                       self.first, ["un = 1"])
        self.assertEqual((value, launched), ({"un = 1"}, 0))

    def test_attributes_committed_in_the_tree_apply(self):
        self.write(".gitattributes", "a.py binary\n")
        second = self.commit()
        value, _launched = self.counted(review_authors._tree_lines, self.repo,
                                        second, ["un = 1"])
        self.assertEqual(value, set())

    def test_the_process_cache_follows_info_attributes(self):
        # relevé cx2 sur fbf586ac : sans vider le cache mémoire de #3753
        canon._content_cache_clear()
        with mock.patch.object(grep_cache, "ENABLED", False):
            first = review_authors._tree_lines(self.repo, self.first, ["un = 1"])
            self._info_attributes("a.py binary\n")
            after = review_authors._tree_lines(self.repo, self.first, ["un = 1"])
            sources = review_authors._source_paths(self.repo, self.first,
                                                   ["un = 1"], {})
        self.assertEqual((first, after, sources), ({"un = 1"}, set(), set()))
        # puis le cache disque, rallumé, ne garde pas une lecture périmée
        value, launched = self.counted(review_authors._tree_lines, self.repo,
                                       self.first, ["un = 1"])
        self.assertEqual((value, launched), (set(), 1))

    def test_git_2_40_is_not_trusted_with_attr_source(self):
        self.assertEqual(grep_cache.ATTR_SOURCE_GIT, (2, 41))
        with mock.patch.object(grep_cache, "_git_version_cache", [(2, 40)]):
            self.assertFalse(grep_cache._attr_source_supported())
            one = grep_cache.neutral_attributes(self.first, self.repo)
            two = grep_cache.neutral_attributes(self.first, self.repo)
        self.assertNotEqual(one[grep_cache.ATTR_TOKEN_ENV],
                            two[grep_cache.ATTR_TOKEN_ENV])
        with mock.patch.object(grep_cache, "_git_version_cache", [(2, 41)]):
            self.assertTrue(grep_cache._attr_source_supported())

    def test_an_old_git_caches_nothing(self):
        calls = []
        with mock.patch.object(grep_cache, "_attr_source_supported",
                               return_value=False):
            for _ in range(2):
                grep_cache.cached(self.repo, self.first, "m", ["x"],
                                  lambda: calls.append(1) or [])
        self.assertEqual(len(calls), 2)

    def test_a_key_lock_stays_one_lock_when_nothing_is_published(self):
        # B/P2 cx3 sur bdd1baf9 : A calcule sans rien publier (lecture
        # impossible) ; B attendait ; C arrive pendant que B tient le verrou.
        import threading
        name = grep_cache.key("t", "m", ["x"])
        b_holds, b_release = threading.Event(), threading.Event()
        outcome = {}

        def waiter():
            with grep_cache._KeyLock(name, wait=10) as lock:
                outcome["b"] = lock.held
                b_holds.set()
                b_release.wait(10)

        with grep_cache._KeyLock(name, wait=0) as first:
            self.assertTrue(first.held)
            thread = threading.Thread(target=waiter)
            thread.start()
            import time
            time.sleep(0.3)                # B a ouvert le fichier et attend
            self.assertIsNone(grep_cache._read(name)[1])   # rien de publié
        self.assertTrue(b_holds.wait(10))
        try:
            with grep_cache._KeyLock(name, wait=0) as third:
                self.assertFalse(third.held)   # jamais deux verrous exclusifs
        finally:
            b_release.set()
            thread.join(10)
        self.assertTrue(outcome["b"])
        with grep_cache._KeyLock(name, wait=0) as again:
            self.assertTrue(again.held)        # le même verrou se reprend

    def test_lock_files_are_stable_and_bounded(self):
        name = grep_cache.key("t", "m", ["x"])
        with grep_cache._KeyLock(name, wait=0) as lock:
            path = lock.path
        self.assertTrue(os.path.exists(path))     # jamais supprimé
        self.assertEqual(os.path.basename(path),
                         name[:grep_cache.LOCK_STRIPE_CHARS] + ".lock")
        grep_cache._write(name, [1])
        self.assertEqual(grep_cache.prune(limit=0), 1)
        self.assertTrue(os.path.exists(path))     # l'élagage n'y touche pas

    def test_prune_keeps_the_most_recently_read(self):
        for index in range(5):
            grep_cache._write(grep_cache.key("t%d" % index, "m", ["x"]), [index])
        self.assertEqual(grep_cache.prune(limit=2), 3)
        kept = [grep_cache._read(grep_cache.key("t%d" % index, "m", ["x"]))[0]
                for index in range(5)]
        self.assertEqual(sum(kept), 2)

    def test_each_write_enforces_the_byte_budget_and_keeps_recent_reads(self):
        names = [grep_cache.key("bounded%d" % i, "m", []) for i in range(3)]
        with mock.patch.object(grep_cache, "MAX_BYTES", 1000):
            for name in names[:2]:
                grep_cache._write(name, ["x" * 300])
            # La première entrée relue devient la plus récente.
            os.utime(grep_cache._path(names[0]), (2000000000, 2000000000))
            grep_cache._write(names[2], ["x" * 600])
            paths = [grep_cache._path(name) for name in names]
            self.assertLessEqual(sum(os.path.getsize(p) for p in paths
                                     if os.path.exists(p)), 1000)
            self.assertTrue(grep_cache._read(names[0])[0])
            self.assertFalse(grep_cache._read(names[1])[0])
            self.assertTrue(grep_cache._read(names[2])[0])
            # Une seule entrée surdimensionnée ne peut contourner la borne.
            huge = grep_cache.key("huge", "m", [])
            grep_cache._write(huge, ["x" * 2000])
            self.assertFalse(grep_cache._read(huge)[0])
            self.assertLessEqual(sum(os.path.getsize(p) for p in paths
                                     if os.path.exists(p)), 1000)


class SweepDeadlineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t3974d-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        _git(self.tmp, "init", "-q")
        with open(os.path.join(self.tmp, "a.py"), "w", encoding="utf-8") as handle:
            handle.write("un = 1\n")
        _git(self.tmp, "add", "-A")
        _git(self.tmp, "commit", "-qm", "c")
        self.head = _git(self.tmp, "rev-parse", "HEAD")
        canon._content_cache_clear()
        self.addCleanup(canon._content_cache_clear)

    def test_an_exhausted_sweep_names_the_step_and_runs_no_grep(self):
        with mock.patch.object(canon.subprocess, "run",
                               wraps=subprocess.run) as run:
            with review_authors.sweep_deadline(0, "lot #42") as sweep:
                with self.assertRaises(review_authors.SweepDeadline) as caught:
                    review_authors._tree_lines(self.tmp, self.head, ["un = 1"])
        self.assertIn("lot #42", str(caught.exception))
        self.assertIn("git grep -h -I -G -f - %s" % self.head, str(caught.exception))
        self.assertTrue(sweep.tripped.startswith("git grep"))
        self.assertEqual(_greps(run), 0)

    def test_a_live_sweep_reads_normally(self):
        with review_authors.sweep_deadline(60, "lot #42") as sweep:
            found = review_authors._tree_lines(self.tmp, self.head, ["un = 1"])
        self.assertEqual(found, {"un = 1"})
        self.assertEqual(sweep.tripped, "")

    def test_without_a_sweep_nothing_changes(self):
        self.assertEqual(review_authors._source_paths(
            self.tmp, self.head, ["un = 1"], {}), {"a.py"})

    def test_queue_readiness_refuses_with_the_lot_and_the_step(self):
        def slow(*_args, **_kwargs):
            review_authors._tree_lines(self.tmp, self.head, ["un = 1"])
            return {}

        with mock.patch.object(merge_queue, "AUTHORS_SWEEP_BUDGET_S", 0), \
                mock.patch.object(merge_queue, "_readiness", side_effect=slow):
            with self.assertRaises(merge_queue.QueueError) as caught:
                merge_queue.readiness(None, 3974, self.tmp)
        text = str(caught.exception)
        self.assertIn("lot #3974", text)
        self.assertIn("git grep -h -I -G -f -", text)

    def test_a_deadline_read_as_doubt_still_refuses(self):
        def swallowed(*_args, **_kwargs):
            try:
                review_authors._tree_lines(self.tmp, self.head, ["un = 1"])
            except Exception:  # noqa: BLE001 - une couche qui lit un doute
                pass
            return {"target": "main"}

        with mock.patch.object(merge_queue, "AUTHORS_SWEEP_BUDGET_S", 0), \
                mock.patch.object(merge_queue, "_readiness", side_effect=swallowed):
            with self.assertRaises(merge_queue.QueueError) as caught:
                merge_queue.readiness(None, 7, self.tmp)
        self.assertIn("lot #7: the authorship sweep exceeded 0 s",
                      str(caught.exception))


if __name__ == "__main__":
    unittest.main()
