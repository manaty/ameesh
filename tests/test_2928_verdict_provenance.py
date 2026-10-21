# SPDX-License-Identifier: AGPL-3.0-only
"""#2928 — un verdict posé au nom d'un autre agent ne compte pas.

Deux défauts, deux gardes :

* l'écriture : `ameesh work milestone … verdict --actor <autre agent>` était
  accepté depuis une session liée à une identité d'agent ; le jalon était
  écrit sous le nom revendiqué, sans reçu de provenance (#1465). La garde est
  celle de `work.forcer` (#540) : une session liée pose le jalon en son nom ;
  une session sans identité ne nomme qu'un humain (`--actor human:<id>`).
* la porte : en classe sensible, un verdict ne compte que s'il porte son reçu
  exact (acteur, session, tour, harnais, tête). Sans lui, le journal des tours
  suffisait à faire compter un `ok` fabriqué au nom d'un relecteur pendant son
  tour, et ce faux dernier avis masquait le vrai (un `blocked` réel).

Le cas des têtes du dépôt canon est couvert : la preuve se résout là où le
commit existe (`--repo`, dépôt canon configuré), pas seulement dans le
répertoire courant du relecteur.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from ameesh import freeze_gate, identity, merge_queue, registry, storage, work

from .support import PgTestCase

HEAD = "a" * 40
FAMILIES = ("claude", "codex", "deepseek")


# --------------------------------------------------------------------------
# l'écriture : la garde de `work milestone` (même règle que #540)
# --------------------------------------------------------------------------

def _binding(name="", *, ok=True, source="runner", reason=""):
    return types.SimpleNamespace(name=name, ok=ok, source=source, reason=reason)


class MilestoneActorTest(unittest.TestCase):
    """Une session liée ne signe un gel/verdict que pour elle-même."""

    def actor(self, binding, explicit, *, kind="verdict"):
        with mock.patch.object(identity, "resolve_binding", return_value=binding), \
                mock.patch.dict(os.environ, {"AGENT_MAIL_NAME": ""}, clear=False):
            return work.milestone_actor(None, None, explicit, kind=kind)

    def test_a_bound_session_posts_in_its_own_name(self):
        self.assertEqual(
            self.actor(_binding("mty-amh-impl-07"), "mty-amh-impl-07"),
            "mty-amh-impl-07")
        # sans --actor, l'identité liée est écrite : jamais vide
        self.assertEqual(self.actor(_binding("mty-amh-impl-07"), ""),
                         "mty-amh-impl-07")

    def test_a_bound_session_cannot_post_on_behalf_of_another_agent(self):
        with self.assertRaises(work.WorkError) as caught:
            self.actor(_binding("mty-amh-impl-07"), "ameesh-cx2")
        text = str(caught.exception)
        self.assertIn("milestone refused", text)
        self.assertIn("mty-amh-impl-07", text)
        self.assertIn("ameesh-cx2", text)
        self.assertIn("verdict milestone", text)
        # le gel suit la même règle
        with self.assertRaises(work.WorkError):
            self.actor(_binding("mty-amh-impl-07"), "ameesh-cx2", kind="frozen")

    def test_a_rejected_binding_does_not_fall_back_to_the_human_terminal(self):
        with self.assertRaises(work.WorkError) as caught:
            self.actor(_binding("mty-amh-impl-07", ok=False,
                                reason="lease does not match"), "mty-amh-impl-07")
        self.assertIn("invalid session identity", str(caught.exception))

    def test_an_unbound_session_cannot_name_an_agent(self):
        with self.assertRaises(work.WorkError) as caught:
            self.actor(_binding("", ok=False, source="none"),
                       "mty-amh-impl-07")
        self.assertIn("no bound agent identity", str(caught.exception))

    def test_an_unbound_session_may_name_a_human(self):
        self.assertEqual(self.actor(_binding("", ok=False, source="none"),
                                    "human:smichea"), "human:smichea")

    def test_an_unbound_session_without_a_name_stays_unknown(self):
        # même comportement qu'avant : le jalon est anonyme, pas signé d'un
        # agent au hasard.
        self.assertEqual(self.actor(_binding("", ok=False, source="none"), ""),
                         work.UNKNOWN_ACTOR)


# --------------------------------------------------------------------------
# la porte : reçu exact en classe sensible
# --------------------------------------------------------------------------

class SensitiveVerdictGateTest(PgTestCase):
    """Un verdict sans reçu exact ne compte pas dans la classe sensible."""

    def setUp(self):
        super().setUp()
        self.db.execute("DELETE FROM provenance_receipts")
        self.db.execute("DELETE FROM turn_costs")
        for name, harness in (("author", "deepseek"), ("r_claude", "claude"),
                              ("r_codex", "codex")):
            registry.upsert(self.db, name, harness=harness, host=self.cfg.host,
                            cwd=self.tmp, status="idle")
            self.db.execute("UPDATE agent_registry SET team = 'ameesh'"
                            " WHERE name = %s", (name,))
        self.item = work.add(self.db, title="porte sensible", app="ameesh",
                             assignee="author", branch="lot/2928")
        self.lot = int(self.item["id"])
        self.ops = storage.of(self.db).work
        self.frozen = self.ops.add_milestone(
            self.lot, "frozen", sha=HEAD, actor="author", verdict=None, note="")

    def verdict(self, actor, sha=HEAD, verdict="ok", *, receipt=False,
                harness=None, receipt_actor=None):
        mark = self.ops.add_milestone(self.lot, "verdict", sha=sha, actor=actor,
                                      verdict=verdict, note="")
        if receipt:
            harness = harness or {"r_claude": "claude", "r_codex": "codex",
                                  "author": "deepseek"}[receipt_actor or actor]
            self.db.execute(
                "INSERT INTO provenance_receipts"
                " (subject, sha, actor, session, turn, harness, work_item_id)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (subject) DO NOTHING",
                ("milestone:%d" % int(mark["id"]), sha, receipt_actor or actor,
                 "s-" + harness, "t", harness, self.lot))
        return mark

    def gate(self, *, cls="sensitive", required=1):
        marks = work.milestones(self.db, self.lot)
        return merge_queue.review_gate(
            self.db, marks, HEAD,
            klass={"class": cls, "files": []},
            item=work.get(self.db, self.lot), families=FAMILIES,
            required=required)

    def test_a_receiptless_verdict_does_not_count(self):
        self.verdict("r_claude")
        out = self.gate()
        self.assertFalse(out["ok"], out)
        self.assertEqual(out["accepted"], [])
        self.assertEqual([row["actor"] for row in out["unproven"]], ["r_claude"])
        self.assertIn("without an exact provenance receipt", out["why"])

    def test_a_real_verdict_with_its_receipt_still_counts(self):
        self.verdict("r_claude", receipt=True)
        out = self.gate()
        self.assertTrue(out["ok"], out)
        self.assertEqual([row["actor"] for row in out["accepted"]], ["r_claude"])
        self.assertEqual(out["unproven"], [])

    def test_a_receiptless_ok_does_not_mask_a_real_blocked(self):
        # le vrai dernier avis est un block, avec son reçu ; le faux ok arrive
        # après, au même nom, sans reçu : il ne le masque plus.
        self.verdict("r_claude", verdict="blocked", receipt=True)
        self.verdict("r_claude", verdict="ok")
        out = self.gate()
        self.assertFalse(out["ok"], out)
        self.assertIn("blocked verdict", out["why"])
        self.assertEqual([row["actor"] for row in out["blocked"]], ["r_claude"])

    def test_a_receiptless_blocked_does_not_block(self):
        # symétrique : un faux block sans reçu ne ferme pas la porte
        self.verdict("r_claude", verdict="blocked")
        self.verdict("r_codex", receipt=True)
        out = self.gate()
        self.assertTrue(out["ok"], out)
        self.assertEqual([row["actor"] for row in out["accepted"]], ["r_codex"])

    def test_a_contradictory_receipt_is_not_a_proof(self):
        # un reçu écrit sous un autre acteur n'est pas le reçu exact du jalon
        self.verdict("r_claude", receipt=True, receipt_actor="r_codex")
        out = self.gate()
        self.assertFalse(out["ok"], out)
        self.assertEqual([row["actor"] for row in out["unproven"]], ["r_claude"])

    def test_a_normal_lot_keeps_the_historical_fallback(self):
        # hors classe sensible, la règle ne change pas : le verdict reste lu
        # par le registre/le journal, comme avant #2928.
        self.verdict("r_claude")
        out = self.gate(cls="normal")
        self.assertTrue(out["ok"], out)
        self.assertEqual([row["actor"] for row in out["accepted"]], ["r_claude"])

    def test_the_sensitive_family_reader_ignores_a_receiptless_verdict(self):
        # même règle pour le lecteur historique du train : sans reçu exact, pas
        # de famille — un faux dernier avis ne peut pas en fournir une.
        from ameesh import review_authors
        self.verdict("r_claude")
        self.assertEqual(
            review_authors.verdict_families(
                self.db, work.milestones(self.db, self.lot), FAMILIES,
                strict=True, require_receipt=True),
            {})

    def test_the_sensitive_family_reader_reads_a_receipted_verdict(self):
        from ameesh import review_authors
        self.verdict("r_claude", receipt=True)
        self.assertEqual(
            review_authors.verdict_families(
                self.db, work.milestones(self.db, self.lot), FAMILIES,
                strict=True, require_receipt=True),
            {"r_claude": "claude"})


# --------------------------------------------------------------------------
# la preuve se résout là où la tête existe (dépôt canon)
# --------------------------------------------------------------------------

class CanonHeadProofTest(PgTestCase):
    """Une tête du dépôt canon s'écrit avec sa preuve exacte, pas sans reçu."""

    def setUp(self):
        super().setUp()
        self.db.execute("DELETE FROM provenance_receipts")
        self.reviewer = "mty-amh-impl-07"
        storage.of(self.db).agents.upsert_unleased(
            self.reviewer, chantier=None, harness="deepseek", host=self.cfg.host,
            cwd=self.tmp, session_id="s-canon")
        registry.upsert(self.db, "author", harness="deepseek", host=self.cfg.host,
                        cwd=self.tmp, status="idle")
        self.item = work.add(self.db, title="tête canon", app="ameesh",
                             assignee="author", branch="canon/2928")
        self.lot = int(self.item["id"])
        self.repo = tempfile.mkdtemp(prefix="ameesh-2928-canon-", dir=self.tmp)
        subprocess.run(["git", "init", "-q", self.repo], check=True,
                       capture_output=True)
        env = dict(os.environ, GIT_AUTHOR_NAME="author",
                   GIT_AUTHOR_EMAIL="author@ameesh.invalid",
                   GIT_COMMITTER_NAME="author",
                   GIT_COMMITTER_EMAIL="author@ameesh.invalid")
        with open(os.path.join(self.repo, "head.txt"), "w", encoding="utf-8") as fh:
            fh.write("canon head\n")
        subprocess.run(["git", "add", "head.txt"], cwd=self.repo, check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-qm", "canon head"], cwd=self.repo,
                       check=True, capture_output=True, env=env)
        self.head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, text=True).strip()
        self.turn_env = {"AGENT_MAIL_NAME": self.reviewer,
                    "AMEESH_TURN_ID": "t" * 32,
                    "AMEESH_HARNESS": "deepseek"}

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)
        super().tearDown()

    def test_a_verdict_on_a_canon_head_gets_its_exact_receipt(self):
        # le dépôt courant ne contient pas cette tête : seule la résolution
        # dans le dépôt nommé (--repo) permet d'écrire la preuve.
        with mock.patch.dict(os.environ, self.turn_env):
            mark = work.milestone(self.db, self.lot, "verdict", sha=self.head,
                                  actor=self.reviewer, verdict="ok",
                                  repo=self.repo)
        receipt = storage.of(self.db).provenance.milestone(
            "milestone:%d" % int(mark["id"]))
        self.assertIsNotNone(receipt, "la tête canon doit porter son reçu")
        self.assertEqual(receipt["sha"], self.head)
        self.assertEqual(receipt["actor"], self.reviewer)
        self.assertEqual(receipt["session"], "s-canon")

    def test_an_abbreviated_canon_head_resolves_for_the_receipt(self):
        with mock.patch.dict(os.environ, self.turn_env):
            mark = work.milestone(self.db, self.lot, "verdict",
                                  sha=self.head[:12], actor=self.reviewer,
                                  verdict="ok", repo=self.repo)
        receipt = storage.of(self.db).provenance.milestone(
            "milestone:%d" % int(mark["id"]))
        self.assertIsNotNone(receipt, "l'abréviation doit se résoudre dans --repo")
        self.assertEqual(receipt["sha"], self.head)

    def test_the_configured_canon_repositories_are_resolution_candidates(self):
        cfg = types.SimpleNamespace(
            canon=self.repo,
            canon_entries=(types.SimpleNamespace(path=self.repo),))
        db = types.SimpleNamespace(cfg=cfg)
        self.assertIn(self.repo, freeze_gate._head_repos(db, {"id": self.lot}))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
