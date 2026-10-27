# SPDX-License-Identifier: AGPL-3.0-only
"""#3837: prior provenance follows delivered content, including rewrites."""
import os
import shutil
import tempfile
import types
import unittest
from unittest import mock

from ameesh import review_authors as a, work
from .test_2447_review_designation_activation import _Repo


class DeliveredFreezeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-3837-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = _Repo(os.path.join(self.tmp, "repo"))
        self.repo.write("base", "base\n")
        self.base = self.repo.commit("base")
        self.repo.write("545.sql", "shared_implementation();\nold_number_545();\n}\n\n")
        self.old = self.repo.commit("old migration")
        self.repo.git("checkout", "-qb", "target", self.base)
        self.repo.write("553.sql", "shared_implementation();\nnew_number_553();\n}\n\n")
        self.repo.write("own.py", "}\n")
        self.target = self.repo.commit("reviewed renamed migration")
        self.repo.write("own.py", "own_contribution();\n")
        self.head = self.repo.commit("own lot")

    def landing(self):
        tree = a._merged_tree(self.repo.root, self.target, self.head)
        return a._delivered_paths(self.repo.root, self.target, tree) if tree else None

    def outside(self, commit=None):
        return a._outside_delivered(self.repo.root, self.target,
                                    commit or self.old, self.landing(), {})

    def carried(self, *, trace=None, fail_note=False):
        rows = [{"id": 1, "kind": "frozen", "sha": self.old, "at_ts": 1},
                {"id": 2, "kind": "frozen", "sha": self.head, "at_ts": 2}]
        st = types.SimpleNamespace(work=types.SimpleNamespace(milestones=lambda *_: rows))
        with mock.patch.object(a.storage, "of", return_value=st), \
                mock.patch.object(work, "note", side_effect=OSError if fail_note else None) as note, \
                mock.patch.object(a, "_prior_reading", return_value=(
                    {"historic": {"codex"}}, {"historic": [self.old]}, {}, [])) as read:
            result = a._carried_from_previous(
                object(), {"id": 3837}, rows[-1], [self.head], self.repo.root,
                ("codex", "claude", "deepseek"), base=self.target,
                head=self.head, target=self.target, exclusions_out=trace)
        return result, note, read

    def test_changed_patch_with_target_content_is_excluded_and_journaled(self):
        self.assertEqual(a._not_in_target(self.repo.root, [self.old], self.target,
                                          head=self.head), [self.old])
        self.assertTrue(self.outside())
        trace = []
        result, note, read = self.carried(trace=trace)
        self.assertEqual(result, ([], [], []))
        read.assert_not_called()
        self.assertIn("contenu déjà dans la cible", note.call_args.args[2])
        self.assertTrue(note.call_args.kwargs["once"])
        self.assertIn("0 lignes communes", note.call_args.args[2])
        self.assertEqual(trace[0]["freeze_id"], 1)
        self.assertEqual(trace[0]["commit"], self.old)

    def test_novel_content_copied_to_another_path_keeps_author(self):
        self.repo.write("elsewhere.sql", "old_number_545();\n")
        self.head = self.repo.commit("carry historical content")
        self.assertFalse(self.outside())
        result, note, read = self.carried()
        self.assertEqual(result[0][0]["name"], "historic")
        read.assert_called_once()
        note.assert_not_called()

    def test_fast_forward_prior_freeze_is_still_read(self):
        self.repo.git("checkout", "-qb", "ff", self.old)
        self.repo.write("own.py", "own_contribution();\n")
        self.head = self.repo.commit("fast forward")
        result, note, read = self.carried()
        self.assertEqual(result[0][0]["name"], "historic")
        read.assert_called_once()
        note.assert_not_called()

    def test_failed_journal_does_not_grant_exception(self):
        _, note, read = self.carried(fail_note=True)
        note.assert_called_once()
        read.assert_called_once()

    def test_failed_or_conflicting_merge_keeps_provenance(self):
        with mock.patch.object(a, "_merged_tree", return_value=None):
            self.assertFalse(self.outside())

    def test_failed_target_read_keeps_provenance(self):
        with mock.patch.object(a.plan_git, "_git", side_effect=OSError):
            self.assertFalse(a._outside_delivered(
                self.repo.root, self.target, self.old, {"own.py": ({"}"}, set())}, {}))

    def test_exact_whitespace_and_regex_characters(self):
        self.repo.write("literal", "a.b[0]();\n \n")
        original = self.repo.commit("literal contribution")
        self.repo.git("checkout", "-qb", "different", self.target)
        self.repo.write("literal", "axb0();\n\t\n")
        self.target = self.repo.commit("different literals")
        self.repo.write("copied", "a.b[0]();\n \n")
        self.head = self.repo.commit("deliver literal contribution")
        self.assertFalse(self.outside(original))

    def test_same_lines_in_new_file_keep_author(self):
        self.repo.write("literal", "a.b[0]();\n \n")
        original = self.repo.commit("old literal contribution")
        self.repo.git("checkout", "-qb", "literal-target", self.target)
        self.repo.write("literal-target", "a.b[0]();\n \n")
        self.target = self.repo.commit("reviewed literal content")
        self.repo.write("copy", "a.b[0]();\n \n")
        self.head = self.repo.commit("copy content already on target")
        self.assertFalse(self.outside(original))

    def test_generic_line_in_new_file_keeps_author(self):
        self.repo.write("brand-new.py", "}\n")
        self.head = self.repo.commit("generic contribution in a new file")
        self.assertFalse(self.outside())

    def test_literal_line_repeated_in_same_path_keeps_author(self):
        self.repo.write("literal [x]", "a.b[0]();\n \n")
        original = self.repo.commit("historical literal")
        self.repo.git("checkout", "-qb", "same-path-target", self.target)
        self.repo.write("literal [x]", "a.b[0]();\n \n")
        self.target = self.repo.commit("target literal")
        self.repo.write("literal [x]", "a.b[0]();\na.b[0]();\n \n \n")
        self.head = self.repo.commit("repeat target literal")
        cache = {}
        self.assertFalse(a._outside_delivered(self.repo.root, self.target,
                                              original, self.landing(), cache))

    def test_same_path_existing_line_repeated_keeps_author(self):
        self.repo.write("own.py", "own_contribution();\n}\n\n")
        self.target = self.repo.commit("existing target lines")
        self.repo.write("own.py", "own_contribution();\n}\n}\n\n")
        self.head = self.repo.commit("repeat existing line")
        self.assertFalse(self.outside())

    def test_rewritten_bypass_with_existing_line_keeps_author(self):
        self.repo.write("check.py", "def check(user):\n    if not user.admin:\n"
                        "        return False\n    return True\n")
        self.target = self.repo.commit("target check")
        bypass = "def check(user):\n    return True\n    if not user.admin:\n" \
                 "        return False\n    return True\n"
        self.repo.write("check.py", bypass)
        original = self.repo.commit("historical bypass")
        self.repo.git("checkout", "-qb", "rewritten-bypass", self.target)
        self.repo.write("check.py", bypass)
        self.head = self.repo.commit("rewritten bypass")
        self.assertFalse(a._is_ancestor(self.repo.root, original, self.head))
        self.assertFalse(self.outside(original))

    def test_deletion_still_delivered_keeps_author(self):
        self.repo.git("checkout", "-qb", "deletion", self.base)
        self.repo.write("victim", "unique_victim();\n")
        self.target = self.repo.commit("victim exists on target")
        self.repo.git("rm", "victim")
        deleted = self.repo.commit("historical deletion")
        self.head = deleted
        self.assertFalse(self.outside(deleted))

    def test_binary_patch_keeps_author(self):
        self.repo.write("binary", "\x00binary")
        binary = self.repo.commit("binary")
        self.head = binary
        self.assertFalse(self.outside(binary))

    def test_reverted_removal_outside_landing_keeps_author(self):
        self.repo.write("gate.py", "require_identity();\nbypass();\n")
        self.target = self.repo.commit("target gate")
        self.repo.write("gate.py", "require_identity();\n")
        removal = self.repo.commit("historical removal")
        self.repo.git("checkout", "-qb", "reverted-removal", self.target)
        self.repo.write("unrelated.py", "unrelated();\n")
        self.head = self.repo.commit("rewrite without the removal")
        self.assertNotIn("gate.py", self.landing())
        self.assertFalse(self.outside(removal))


if __name__ == "__main__":
    unittest.main()
