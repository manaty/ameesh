# SPDX-License-Identifier: AGPL-3.0-only
"""#3111 — l'ancien domaine `<agent>@ameesh.local` d'avant la convention #834.

Des harnais ont commité sous `<agent>@ameesh.local` jusqu'au 14/10/2026
14:32Z ; ces commits survivent dans des plages gelées reportées (#2924) et la
porte les tenait pour d'auteur inconnu (#1288 nexlink, #276, #3088). Ils se
lisent désormais, mais seulement datés d'avant `LEGACY_GIT_EMAIL_UNTIL` et vers
un agent du registre ; la famille se prouve toujours par le journal des tours
ou par un reçu, jamais par l'adresse.
"""
from __future__ import annotations

import os
from unittest import TestCase

from ameesh import identity, merge_queue, registry, review_authors, storage, work

from .support import PgTestCase
from .test_2447_review_designation_activation import _Repo

FAMILIES = ("claude", "codex", "deepseek")
BEFORE = "2026-10-14T10:40:26Z"          # c6778aea1 de deepseek6, #1288 nexlink
AFTER = "2026-10-15T10:00:00Z"


def _ts(iso):
    from datetime import datetime
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


class AgentOfGitEmailTest(TestCase):

    def test_the_convention_address_is_read_at_any_date(self):
        self.assertEqual(identity.agent_of_git_email("Dsk@ameesh.invalid", _ts(AFTER)), "dsk")

    def test_the_legacy_address_is_read_only_before_the_convention(self):
        self.assertEqual(identity.agent_of_git_email("dsk@ameesh.local", _ts(BEFORE)), "dsk")
        self.assertEqual(identity.agent_of_git_email("dsk@ameesh.local", _ts(AFTER)), "")
        self.assertEqual(identity.agent_of_git_email(
            "dsk@ameesh.local", identity.LEGACY_GIT_EMAIL_UNTIL), "")
        self.assertEqual(identity.agent_of_git_email(
            "dsk@ameesh.local", _ts(BEFORE), legacy=False), "")

    def test_other_addresses_name_nobody(self):
        for email, stamp in (("dsk@agents.ameesh.local", _ts(BEFORE)),
                             ("dsk@example.local", _ts(BEFORE)),
                             ("a@dsk@ameesh.local", _ts(BEFORE)),
                             ("@ameesh.local", _ts(BEFORE)),
                             ("dsk@ameesh.local", 0), ("dsk@ameesh.local", "x"),
                             ("", _ts(BEFORE))):
            with self.subTest(email=email, stamp=stamp):
                self.assertEqual(identity.agent_of_git_email(email, stamp), "")


class LegacyDomainGateTest(PgTestCase):
    """La porte relit une plage portant des commits `.local` d'avant la convention."""

    def setUp(self):
        super().setUp()
        for table in ("turn_costs", "provenance_receipts", "canon_state",
                      "quota_gauge_readings"):
            self.db.execute("DELETE FROM %s" % table)
        self.ops = storage.of(self.db).work
        self.repo = _Repo(os.path.join(self.tmp, "depot"))
        self.repo.write("src/ameesh/chat.py", "base\n")
        self.base = self.repo.commit("base")
        self.harnesses = {"dsk": "deepseek", "r_claude": "claude",
                          "r_deepseek": "deepseek"}
        for name, harness in self.harnesses.items():
            registry.upsert(self.db, name, harness=harness, host=self.cfg.host,
                            cwd=self.repo.root, status="idle")
        # le tour de dsk qui a écrit les anciens commits
        self.db.execute(
            "INSERT INTO turn_costs (agent, harness, session, recorded_at)"
            " VALUES ('dsk', 'deepseek', 's-dsk', %s)", ("2026-10-14T10:00:00Z",))
        self.item = work.add(self.db, title="plage ancienne", app="ameesh",
                             branch="lot/1-ancien", branch_target=self.base,
                             assignee="dsk")
        self.lot = int(self.item["id"])

    def legacy_commit(self, agent, date, text):
        self.repo.write("src/ameesh/chat.py", text)
        address = "%s@ameesh.local" % agent
        env = dict(self.repo.dry, GIT_AUTHOR_NAME=agent, GIT_AUTHOR_EMAIL=address,
                   GIT_COMMITTER_NAME=agent, GIT_COMMITTER_EMAIL=address,
                   GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
        self.repo.git("add", "-A")
        self.repo.git("commit", "-qm", "%s: ancien" % agent, env=env)
        return self.repo.git("rev-parse", "HEAD")

    def gate(self, head, reviewer):
        self.ops.add_milestone(self.lot, "frozen", sha=head, actor="dsk",
                               verdict=None, note="")
        row = self.ops.add_milestone(self.lot, "verdict", sha=head, actor=reviewer,
                                     verdict="ok", note="")
        harness = self.harnesses[reviewer]
        self.db.execute(
            "INSERT INTO provenance_receipts"
            " (subject, sha, actor, session, turn, harness, work_item_id)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)",
            ("milestone:%d" % int(row["id"]), head, reviewer, "s-" + harness,
             "t", harness, self.lot))
        marks = [dict(mark, at_ts=float(mark["at_ts"]))
                 for mark in self.ops.milestones(self.lot, None)
                 if mark["kind"] in ("frozen", "verdict")]
        klass = {"class": "sensitive", "project": "ameesh", "base_sha": self.base,
                 "files": [{"path": "src/ameesh/chat.py"}]}
        return merge_queue.review_gate(
            self.db, marks, head, repo=self.repo.root, klass=klass,
            item=work.get(self.db, self.lot), families=FAMILIES, required=1)

    def test_an_old_local_commit_of_a_registered_agent_is_read(self):
        old = self.legacy_commit("dsk", BEFORE, "lot\n")
        out = self.gate(old, "r_claude")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["authors"], [{"name": "dsk", "family": "deepseek"}])

    def test_the_family_of_an_old_local_author_stays_excluded(self):
        # l'adresse ne rend pas l'auteur indépendant : sa famille (journal des
        # tours) reste interdite
        old = self.legacy_commit("dsk", BEFORE, "lot\n")
        out = self.gate(old, "r_deepseek")
        self.assertFalse(out["ok"], out)
        self.assertEqual(out["accepted"], [])

    def test_a_local_commit_after_the_convention_is_refused(self):
        late = self.legacy_commit("dsk", AFTER, "lot\n")
        out = self.gate(late, "r_claude")
        self.assertFalse(out["ok"], out)
        self.assertIn("cannot be read", out["why"])

    def test_a_local_commit_of_an_unknown_name_is_refused(self):
        self.legacy_commit("dsk", BEFORE, "lot\n")
        ghost = self.legacy_commit("ghost6", BEFORE, "lot\nghost\n")
        out = self.gate(ghost, "r_claude")
        self.assertFalse(out["ok"], out)
        self.assertIn("cannot be read", out["why"])

    def test_the_legacy_address_needs_the_registry_even_for_the_reference(self):
        old = self.legacy_commit("dsk", BEFORE, "lot\n")
        ghost = self.legacy_commit("ghost6", BEFORE, "lot\nghost\n")
        self.assertEqual(review_authors.commit_actor(self.repo.root, old, self.db),
                         ("dsk", _ts(BEFORE)))
        self.assertEqual(review_authors.commit_actor(self.repo.root, old), ("", 0.0))
        self.assertEqual(review_authors.commit_actor(self.repo.root, ghost, self.db),
                         ("", 0.0))
        self.assertTrue(review_authors.commits_written_by(
            self.repo.root, [old], "dsk", self.db))
        self.assertFalse(review_authors.commits_written_by(
            self.repo.root, [ghost], "ghost6", self.db))
        self.assertFalse(review_authors.commits_written_by(
            self.repo.root, [old], "dsk"))
