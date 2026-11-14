# SPDX-License-Identifier: AGPL-3.0-only
"""#7350: a SLO sweep reuses remote-free readers without stale attributes."""
import os
import tempfile
import unittest
from unittest import mock

from ameesh import review_authors as authors
from tests.test_2447_review_designation_activation import _Repo


class SweepReadersTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(temporary.cleanup)
        self.repo = _Repo(os.path.join(temporary.name, "repo"))
        self.repo.write("content", "original content\n")
        self.head = self.repo.commit("base")

    def test_many_slo_reads_share_one_reader_and_keep_the_same_content(self):
        expected = authors._read_git(self.repo.root, "show", self.head + ":content")
        original = authors._new_local_object_repo
        with mock.patch.object(authors, "_new_local_object_repo", wraps=original) as create:
            with authors.sweep_deadline(30):
                for _ in range(50):
                    actual = authors._read_git(self.repo.root, "show", self.head + ":content")
                    self.assertEqual((actual.returncode, actual.stdout, actual.stderr),
                                     (expected.returncode, expected.stdout, expected.stderr))
                self.assertEqual(create.call_count, 1)

    def test_reader_is_removed_at_sweep_exit_even_on_failure(self):
        with self.assertRaisesRegex(ValueError, "fixture"):
            with authors.sweep_deadline(30):
                with authors._local_object_repo(self.repo.root) as (path, env):
                    self.assertTrue(os.path.isdir(path))
                    self.assertNotEqual(path, self.repo.root)
                    self.assertIn("GIT_OBJECT_DIRECTORY", env)
                self.assertTrue(os.path.isdir(path))
                raise ValueError("fixture")
        self.assertFalse(os.path.exists(path))
        self.assertIsNone(authors.active_sweep())

    def test_changed_attributes_create_a_new_reader(self):
        attributes = os.path.join(self.repo.root, ".git", "info", "attributes")
        with authors.sweep_deadline(30):
            with authors._local_object_repo(self.repo.root) as (first, _env):
                pass
            for value in (b"content binary\n", b"content -binary\n", None):
                if value is None:
                    os.unlink(attributes)
                else:
                    with open(attributes, "wb") as stream:
                        stream.write(value)
                with authors._local_object_repo(self.repo.root) as (second, _env):
                    self.assertNotEqual(first, second)
                    copy = os.path.join(second, "info", "attributes")
                    if value is None:
                        self.assertFalse(os.path.exists(copy))
                    else:
                        with open(copy, "rb") as stream:
                            self.assertEqual(stream.read(), value)
                first = second

    def test_replaced_source_repository_gets_a_new_object_store(self):
        with authors.sweep_deadline(30):
            with authors._local_object_repo(self.repo.root) as (first, _env):
                pass
            os.rename(self.repo.root, self.repo.root + "-old")
            replacement = _Repo(self.repo.root)
            replacement.write("content", "replacement content\n")
            head = replacement.commit("replacement")
            with authors._local_object_repo(self.repo.root) as (second, _env):
                self.assertNotEqual(first, second)
            actual = authors._read_git(self.repo.root, "show", head + ":content")
            self.assertEqual(actual.stdout, b"replacement content\n")
            missing = authors._read_git(self.repo.root, "show", self.head + ":content")
            self.assertNotEqual(missing.returncode, 0)

    def test_a_new_sweep_does_not_keep_the_previous_reader(self):
        paths = []
        for _ in range(2):
            with authors.sweep_deadline(30):
                with authors._local_object_repo(self.repo.root) as (path, _env):
                    paths.append(path)
        self.assertNotEqual(*paths)
        self.assertTrue(all(not os.path.exists(path) for path in paths))

    def test_failed_initialization_is_retried(self):
        original = authors._new_local_object_repo
        with authors.sweep_deadline(30), mock.patch.object(
                authors, "_new_local_object_repo",
                side_effect=[ValueError("fixture"), original(self.repo.root, metadata=True)]) as create:
            with self.assertRaises(ValueError):
                with authors._local_object_repo(self.repo.root):
                    pass
            with authors._local_object_repo(self.repo.root):
                pass
            self.assertEqual(create.call_count, 2)


class AlertsCacheTest(unittest.TestCase):
    def run_cli(self, state=None, error=None):
        from ameesh import alert_ack, exploitation, notify
        db = mock.Mock()
        args = exploitation.build_parser().parse_args([
            'alerts', '--follow', '--iterations', '2', '--json'])
        seen = []

        def alerts(_cfg, _db, **kwargs):
            seen.append(kwargs['gate_cache'])
            # Follow iterations retain newly computed entries, too.
            kwargs['gate_cache']['fixture'] = {'ok': True}
            return []

        with mock.patch.object(exploitation.db_mod, 'connect', return_value=db), \
                mock.patch.object(exploitation.db_mod, 'require_schema'), \
                mock.patch.object(exploitation.db_mod, 'batched',
                                  side_effect=lambda db, fn, **kw: fn(db)), \
                mock.patch.object(notify, 'load_shared_state', return_value=state,
                                  side_effect=error), \
                mock.patch.object(notify, 'save_shared_state') as save, \
                mock.patch.object(exploitation, 'alerts', side_effect=alerts), \
                mock.patch.object(alert_ack, 'annotate',
                                  side_effect=lambda cfg, db, rows: rows), \
                mock.patch.object(exploitation.time, 'sleep'):
            self.assertEqual(exploitation.cmd_alerts(mock.Mock(), args), 0)
            save.assert_not_called()
        self.assertEqual(len(seen), 2)
        self.assertIs(seen[0], seen[1])
        db.close.assert_called_once()
        return seen[0]

    def test_cli_reuses_shared_gates_without_writing_delivery_state(self):
        cache = {'123': {'ok': False, 'key': 'signature', 'at': 1000}}
        self.assertIs(self.run_cli({'gates': cache}), cache)

    def test_missing_or_unreadable_cache_does_not_hide_alerts(self):
        for state in (None, {}, {'gates': []}):
            with self.subTest(state=state):
                self.assertEqual(self.run_cli(state), {'fixture': {'ok': True}})
        self.assertEqual(self.run_cli(error=OSError('fixture')),
                         {'fixture': {'ok': True}})
