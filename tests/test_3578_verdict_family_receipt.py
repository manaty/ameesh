# SPDX-License-Identifier: AGPL-3.0-only
"""#3578 — la famille d'un verdict vient de son reçu, pas de la session courante.

La porte d'avis lisait la famille d'un relecteur au moment de la porte
(`actor_family`, harnais de la SESSION COURANTE) : un `ok` rendu sous codex
par ameesh-cx1, aujourd'hui en repli deepseek, était relu deepseek, et deux
avis de familles distinctes n'en faisaient plus qu'une (#3501, #3532, h-21).

La famille d'un verdict est le harnais de son reçu exact (#1465/#2928) ; sans
reçu, la famille déclarée de l'acteur, jamais le harnais d'une session
ultérieure.
"""
from __future__ import annotations

from ameesh import merge_queue, registry, review_lot, storage, work

from .support import PgTestCase

HEAD = "b" * 40
FAMILIES = ("claude", "codex", "deepseek")


class VerdictFamilyFromReceiptTest(PgTestCase):

    def setUp(self):
        super().setUp()
        self.db.execute("DELETE FROM provenance_receipts")
        self.db.execute("DELETE FROM turn_costs")
        # (nom, harnais déclaré, harnais de la session courante)
        for name, harness, live in (("author", "claude", None),
                                    ("cx1", "codex", "deepseek"),
                                    ("impl", "deepseek", None)):
            registry.upsert(self.db, name, harness=harness, host=self.cfg.host,
                            cwd=self.tmp, status="idle")
            self.db.execute("UPDATE agent_registry SET team = 'ameesh'"
                            " WHERE name = %s", (name,))
            if live:
                # la session vivante bascule (repli), le harnais déclaré reste
                self.db.execute(
                    "UPDATE agent_registry SET session_id = %s,"
                    " session_origin = %s, session_harness = %s WHERE name = %s",
                    ("s-" + live, "s-" + live, live, name))
        self.item = work.add(self.db, title="famille du verdict", app="ameesh",
                             assignee="author", branch="lot/3578")
        self.lot = int(self.item["id"])
        self.ops = storage.of(self.db).work
        self.ops.add_milestone(self.lot, "frozen", sha=HEAD, actor="author",
                               verdict=None, note="")

    def verdict(self, actor, *, harness=None, verdict="ok"):
        mark = self.ops.add_milestone(self.lot, "verdict", sha=HEAD, actor=actor,
                                      verdict=verdict, note="")
        if harness:
            self.db.execute(
                "INSERT INTO provenance_receipts"
                " (subject, sha, actor, session, turn, harness, work_item_id)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                ("milestone:%d" % int(mark["id"]), HEAD, actor,
                 "s-" + harness, "t", harness, self.lot))
        return mark

    def gate(self, *, cls="sensitive", required=2):
        return merge_queue.review_gate(
            self.db, work.milestones(self.db, self.lot), HEAD,
            klass={"class": cls, "files": []},
            item=work.get(self.db, self.lot), families=FAMILIES,
            required=required)

    def families(self, out):
        return {row["actor"]: row["family"] for row in out["accepted"]}

    def test_the_live_session_is_the_fallback_trap(self):
        # le défaut constaté : la session courante de cx1 est deepseek
        self.assertEqual(merge_queue.actor_family(self.db, "cx1", families=FAMILIES),
                         "deepseek")
        self.assertEqual(merge_queue.actor_family(self.db, "cx1", families=FAMILIES,
                                                  declared=True), "codex")

    def test_ok_rendered_under_codex_counts_codex_despite_a_deepseek_session(self):
        # témoin (1) : #3501/#3532 — deux avis, deux familles sur leurs reçus
        self.verdict("cx1", harness="codex")
        self.verdict("impl", harness="deepseek")
        out = self.gate()
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.families(out), {"cx1": "codex", "impl": "deepseek"})

    def test_ok_rendered_under_deepseek_by_a_codex_agent_counts_deepseek(self):
        # témoin (2) : la famille déclarée ne remplace pas le reçu
        self.db.execute("UPDATE agent_registry SET session_harness = 'codex',"
                        " session_origin = 's-codex', session_id = 's-codex'"
                        " WHERE name = 'cx1'")
        self.verdict("cx1", harness="deepseek")
        self.verdict("impl", harness="deepseek")
        out = self.gate()
        self.assertFalse(out["ok"], out)
        self.assertIn("deepseek", out["why"])
        self.assertNotIn("codex", out["why"])

    def test_sensitive_verdict_without_receipt_is_not_counted(self):
        # témoin (3) : inchangé (#2928)
        self.verdict("cx1")
        self.verdict("impl", harness="deepseek")
        out = self.gate()
        self.assertFalse(out["ok"], out)
        self.assertEqual([row["actor"] for row in out["unproven"]], ["cx1"])

    def test_light_class_without_receipt_uses_the_declared_family(self):
        # sans reçu, la famille déclarée de l'acteur, jamais la session courante
        self.verdict("cx1")
        self.verdict("impl")
        out = self.gate(cls="normal")
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.families(out), {"cx1": "codex", "impl": "deepseek"})

    def test_a_receipt_is_read_outside_the_sensitive_class(self):
        self.verdict("cx1", harness="deepseek")
        self.verdict("impl", harness="deepseek")
        out = self.gate(cls="normal")
        self.assertFalse(out["ok"], out)

    def test_verdict_family_reader(self):
        mark = self.verdict("cx1", harness="codex")
        self.assertEqual(merge_queue.verdict_family(self.db, mark,
                                                    families=FAMILIES), "codex")
        self.assertEqual(merge_queue.verdict_family(
            self.db, {"actor": "human:smichea"}, families=FAMILIES), "")
        self.assertEqual(review_lot.family_of("codex", FAMILIES), "codex")
