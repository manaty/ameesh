# SPDX-License-Identifier: AGPL-3.0-only
"""#8043 — le courrier de relecture dit de lire la tête gelée depuis un
instantané immuable.

L'auditeur a raté trois relectures (#8033 `0ce50df7`, #7986, #8031
`fdbeb15e`) : il lisait un arbre de travail partagé ou une branche qui bougeait,
pas la tête gelée. La désignation porte désormais une consigne permanente, à
côté de la question N-1 mais sans dépendre du SQL : lire le diff et les
fichiers de la tête gelée depuis un instantané immuable (`git diff
<base>...<sha>`, `git show <sha>:<path>`, ou une worktree détachée à `<sha>`),
jamais depuis un arbre de travail partagé ni un nom de branche qui peut bouger ;
le verdict cite le SHA lu.

Témoin : `reviewer_message` contient la consigne avec et sans migration SQL,
dans la salle de correction comme dans le chemin normal.
"""
from __future__ import annotations

import unittest

from ameesh import review_designation as rd

SHA = "a" * 40
BASE = "c" * 40
LOT = {"id": 8043, "title": "Désignation des relecteurs : consigne instantané",
       "app": "ameesh", "assignee": "mty-amh-impl-02"}
REVIEWER = {"name": "revue-claude", "family": "claude"}
AUTHORS = [{"name": "mty-amh-impl-02", "family": "deepseek"}]
FREEZE = {"id": 7, "sha": SHA}
SHORT = {"state": "short", "required": 2, "remaining": 1}

RULE = "read the diff and the files of the frozen head from an immutable snapshot"
SHARED = "never from a shared working tree or a branch name that can move"
CITES = "the verdict cites the SHA read"


class SnapshotInstructionTest(unittest.TestCase):
    """La consigne est permanente, dans les deux chemins de `reviewer_message`."""

    def mail(self, *, sql=False, correction=None):
        return rd.reviewer_message(
            LOT, freeze=FREEZE, required=2, rank=1, reviewer=REVIEWER,
            authors=AUTHORS, base=BASE, risk="sensitive",
            correction=correction, sql_migration=sql)

    def assert_rule(self, body):
        self.assertIn(rd.SNAPSHOT_INSTRUCTION, body)
        self.assertIn(RULE, body)
        self.assertIn("`git diff <base>...<sha>`", body)
        self.assertIn("`git show <sha>:<path>`", body)
        self.assertIn("detached worktree at `<sha>`", body)
        self.assertIn(SHARED, body)
        self.assertIn(CITES, body)
        self.assertEqual(body.count(rd.SNAPSHOT_INSTRUCTION), 1)

    def test_normal_path_without_sql(self):
        body = self.mail()
        self.assert_rule(body)
        self.assertNotIn(rd.N1_QUESTION, body)
        self.assertNotIn("do not push this correction", body)

    def test_normal_path_with_sql(self):
        body = self.mail(sql=True)
        self.assert_rule(body)
        self.assertIn(rd.N1_QUESTION, body)
        self.assertNotIn("do not push this correction", body)

    def test_correction_room_without_sql(self):
        body = self.mail(correction=SHORT)
        self.assert_rule(body)
        self.assertIn("do not push this correction", body)
        self.assertNotIn(rd.N1_QUESTION, body)

    def test_correction_room_with_sql(self):
        body = self.mail(correction=SHORT, sql=True)
        self.assert_rule(body)
        self.assertIn("do not push this correction", body)
        self.assertIn(rd.N1_QUESTION, body)

    def test_the_rule_is_not_conditioned_on_sql(self):
        # Même ligne, même place, avec et sans migration : seule la question
        # N-1 s'ajoute quand le diff porte du SQL.
        plain = self.mail().splitlines()
        sql = self.mail(sql=True).splitlines()
        self.assertEqual(plain.index(rd.SNAPSHOT_INSTRUCTION),
                         sql.index(rd.SNAPSHOT_INSTRUCTION))
        self.assertEqual(plain.count(rd.SNAPSHOT_INSTRUCTION),
                         sql.count(rd.SNAPSHOT_INSTRUCTION))


if __name__ == "__main__":
    unittest.main()
