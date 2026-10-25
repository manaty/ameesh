# SPDX-License-Identifier: AGPL-3.0-only
"""#3288 — an untended blocking verdict has a deadline and a relay.

Constat (mesure du 24/10) : après un avis bloquant, la correction est gelée en
6 min en médiane, mais #3033 (dev7) est resté 22 h en qa sans correction
pendant ~60 tours sur d'autres lots, et #3195 (mesh-design) 9 h pendant 59
tours ailleurs. La notification du verdict est un courrier lu une fois ; le §9
n'avait aucune étape « avis bloquant → nouveau gel », donc un lot en qa avec un
avis bloquant n'était surveillé par personne.

Attendu : le même calcul que les avis dus (#3033) rend les **corrections
dues** (lot ouvert, dernier gel, verdict `blocked` non annulé et sans gel plus
récent ; débiteur = l'assigné) ; le brief de reprise et la relance
d'inactivité les portent en tête ; le §9 mesure l'étape (2 h, 1 h en P1) et
escalade ×1 assigné, ×2 pilote qui réassigne, ×3 bureau/auditeur.

Témoins : #3033 (blocked dev3, puis ~60 tours ailleurs) et #3195 (blocked
cx2, 9 h) → section en tête du brief, dépassement SLO, escalade.
"""
from __future__ import annotations

import time
import types
import unittest

from ameesh import (adapters, alert_briefs, exploitation, progress, registry,
                    reprise, runner, slo, storage, work)
from ameesh import review_designation_activation as activation

from .support import PgTestCase

H = 3600.0
#: une tête gelée de test : le lecteur ne vérifie pas son existence ici
SHA = "a" * 40


class _Base(PgTestCase):
    """Des agents joignables, un lot gelé bloqué, et ses lecteurs SLO."""

    def setUp(self) -> None:
        super().setUp()
        for name, harness in (("author", "deepseek"), ("reviewer", "claude"),
                              ("other", "codex"), ("pilot", "claude"),
                              ("bureau", "codex"), ("auditeur", "codex")):
            registry.upsert(self.db, name, harness=harness, host=self.cfg.host)
        self.db.execute("UPDATE agent_registry SET lease_owner = 'runner-' || name,"
                        " lease_expires_at = now() + interval '1 hour'")

    # -- fixture ----------------------------------------------------------
    def blocked(self, *, age: float = 3 * H, priority: int | None = None,
                assignee: str = "author", actor: str = "reviewer",
                sha: str = SHA) -> tuple[int, str]:
        """Un lot open en qa, gelé sur `sha`, avec un verdict `blocked`.

        Le verdict et le gel sont des jalons DÉCLARÉS (la table L10) : c'est la
        source du calcul unique (#3033), comme un vrai gel suivi de son avis.
        """
        item = work.add(self.db, title="blocage non corrigé", app="ameesh",
                        assignee=assignee, priority=priority)
        lot = int(item["id"])
        work.move(self.db, lot, "build")
        work.move(self.db, lot, "qa")
        self.db.execute(
            "INSERT INTO work_item_milestones (work_item_id, kind, at, sha, actor)"
            " VALUES (%s, 'frozen', to_timestamp(%s), %s, 'author')",
            (lot, time.time() - float(age) - 600.0, sha))
        self.db.execute(
            "INSERT INTO work_item_milestones"
            " (work_item_id, kind, at, sha, actor, verdict)"
            " VALUES (%s, 'verdict', to_timestamp(%s), %s, %s, 'blocked')",
            (lot, time.time() - float(age), sha, actor))
        return lot, sha

    def verdict(self, lot: int, *, kind: str, actor: str = "reviewer",
                sha: str = SHA, age: float = 0.0) -> None:
        self.db.execute(
            "INSERT INTO work_item_milestones"
            " (work_item_id, kind, at, sha, actor, verdict)"
            " VALUES (%s, 'verdict', to_timestamp(%s), %s, %s, %s)",
            (lot, time.time() - float(age), sha, actor, kind))

    def refreeze(self, lot: int, sha: str, *, age: float = 0.0) -> None:
        self.db.execute(
            "INSERT INTO work_item_milestones (work_item_id, kind, at, sha, actor)"
            " VALUES (%s, 'frozen', to_timestamp(%s), %s, 'author')",
            (lot, time.time() - float(age), sha))

    def brief(self, name: str) -> str:
        return reprise.deterministic_brief(
            self.cfg, self.db,
            {"name": name, "harness": "deepseek", "host": self.cfg.host,
             "cwd": self.tmp, "team": "ameesh"})

    def slo_alerts(self) -> list[dict]:
        listing = storage.of(self.db).operations.listing()
        return [a for a in exploitation._slo_breaches(
                    self.cfg, self.db, listing, now=time.time())
                if a.get("step") == "blocked_freeze"]


class DueFixesTest(_Base):
    """Le calcul unique des corrections dues (la moitié `fixes` du CTE #3033)."""

    def test_witness_3033_and_3195(self):
        # Les deux témoins du lot : un avis bloquant, aucun gel plus récent,
        # l'auteur travaille ailleurs (rien dans le calcul ne dépend de ses
        # tours : le debiteur est l'assigné).
        old, old_sha = self.blocked(age=22 * H)          # #3033
        recent, recent_sha = self.blocked(age=9 * H)     # #3195
        rows = activation.due_fixes(self.db)
        self.assertEqual([(r["lot"], r["name"], r["sha"], r["blocked_by"]) for r in rows],
                         [(old, "author", old_sha, "reviewer"),
                          (recent, "author", recent_sha, "reviewer")])
        self.assertIsNotNone(rows[0]["blocked_ts"])
        self.assertLess(rows[0]["blocked_ts"], rows[1]["blocked_ts"])

    def test_the_debtor_is_the_assignee(self):
        lot, sha = self.blocked()
        self.assertEqual([(r["lot"], r["name"], r["sha"])
                          for r in activation.due_fixes(self.db, "AUTHOR")],
                         [(lot, "author", sha)])
        self.assertEqual(activation.due_fixes(self.db, "other"), [])

    def test_a_new_freeze_on_a_new_head_clears_the_fix(self):
        lot, _sha = self.blocked()
        self.refreeze(lot, "b" * 40)
        self.assertEqual(activation.due_fixes(self.db), [])

    def test_a_block_on_an_older_head_is_not_due(self):
        # Un verdict bloquant sur une tête antérieure ne retient rien : le gel
        # plus récent est la tête courante (#2681).
        lot, _sha = self.blocked()
        self.refreeze(lot, "b" * 40)
        self.assertEqual(activation.due_fixes(self.db), [])

    def test_a_later_verdict_of_the_same_reviewer_cancels_it(self):
        # « non annulé » : le même relecteur se ravise sur la même tête.
        lot, _sha = self.blocked()
        self.verdict(lot, kind="ok", age=60.0)
        self.assertEqual(activation.due_fixes(self.db), [])

    def test_a_refreeze_of_the_same_head_does_not_clear_it(self):
        # Re-geler la MÊME tête ne corrige rien : le verdict porte toujours
        # sur elle (même SHA, la règle du gel renouvelé) et la correction
        # reste due.
        lot, sha = self.blocked()
        self.refreeze(lot, sha)
        self.assertEqual([(r["lot"], r["sha"]) for r in activation.due_fixes(self.db)],
                         [(lot, sha)])

    def test_another_reviewer_cannot_cancel_it(self):
        # Avec des sièges désignés, un GO d'un autre relecteur n'efface pas le
        # blocage (#2231) : la correction reste due, et l'horloge part du
        # PREMIER avis bloquant.
        lot, _sha = self.blocked(age=3 * H)
        self.db.execute("UPDATE work_items SET reviewers = ARRAY[%s, %s] WHERE id = %s",
                        ("reviewer", "other", lot))
        self.verdict(lot, kind="ok", actor="other", age=10.0)
        rows = activation.due_fixes(self.db)
        self.assertEqual([(r["lot"], r["blocked_by"]) for r in rows],
                         [(lot, "reviewer")])
        self.assertGreater(time.time() - rows[0]["blocked_ts"], 2 * H)

    def test_two_blocks_of_the_same_reviewer_keep_the_first_start(self):
        # B1 de #17656 : deux `blocked` du même relecteur sur la même tête ne
        # repoussent pas l'horloge ; le début du blocage continu est le
        # premier (l'escalade partait du dernier).
        lot, _sha = self.blocked(age=3 * H)
        self.db.execute("UPDATE work_items SET reviewers = ARRAY[%s] WHERE id = %s",
                        ("reviewer", lot))
        self.verdict(lot, kind="blocked", age=1 * H)
        rows = activation.due_fixes(self.db)
        self.assertEqual([(r["lot"], r["blocked_by"]) for r in rows],
                         [(lot, "reviewer")])
        self.assertGreater(time.time() - rows[0]["blocked_ts"], 2 * H)

    def test_an_ok_between_two_blocks_restarts_the_clock(self):
        # Un `ok` entre deux blocages repart l'horloge au blocage suivant :
        # le début du blocage continu n'est pas le premier `blocked` du lot.
        lot, _sha = self.blocked(age=3 * H)
        self.verdict(lot, kind="ok", age=2 * H)
        self.verdict(lot, kind="blocked", age=1 * H)
        rows = activation.due_fixes(self.db)
        self.assertEqual([(r["lot"], r["blocked_by"]) for r in rows],
                         [(lot, "reviewer")])
        age = time.time() - rows[0]["blocked_ts"]
        self.assertGreater(age, 0.5 * H)
        self.assertLess(age, 2 * H)

    def test_without_a_designation_the_last_verdict_decides(self):
        # Sans siège (colonne vide ou -none), le dernier verdict de la tête
        # tranche, comme `progress._work_item_milestones` : un GO postérieur
        # lève le blocage, un blocage postérieur le rouvre.
        lot, _sha = self.blocked()
        self.verdict(lot, kind="ok", actor="other", age=60.0)
        self.assertEqual(activation.due_fixes(self.db), [])
        self.verdict(lot, kind="blocked", actor="other", age=30.0)
        self.assertEqual([(r["lot"], r["blocked_by"])
                          for r in activation.due_fixes(self.db)],
                         [(lot, "other")])

    def test_a_shared_block_is_the_green_verdict_clock(self):
        # Le même verdict bloquant ouvre la correction de l'assigné et retient
        # l'avis du relecteur qui l'a rendu ; une désignation sans verdict,
        # elle, reste due. Les deux lectures ne se contredisent pas.
        lot, sha = self.blocked()
        self.db.execute("UPDATE work_items SET reviewers = ARRAY[%s, %s] WHERE id = %s",
                        ("reviewer", "other", lot))
        self.assertEqual(activation.pending_reviews(self.db, "reviewer"), [])
        self.assertEqual([(r["lot"], r["sha"])
                          for r in activation.pending_reviews(self.db, "other")],
                         [(lot, sha)])
        self.assertEqual([(r["lot"], r["name"])
                          for r in activation.due_fixes(self.db)],
                         [(lot, "author")])
        self.verdict(lot, kind="ok", age=60.0)
        self.assertEqual(activation.due_fixes(self.db), [])

    def test_an_unreadable_read_is_said_never_empty(self):
        from unittest import mock
        with mock.patch.object(self.db, "query", side_effect=RuntimeError("boom")):
            lines = reprise.fixes_owed_lines(self.db, "author")
        self.assertEqual(len(lines), 1)
        self.assertIn("unreadable", lines[0])

    def test_the_command_reads_the_bound_identity(self):
        lot, sha = self.blocked()
        out = self.mesh("work", "fixes-owed", env=self.env(AGENT_MAIL_NAME="author"))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("#%d [qa]" % lot, out.stdout)
        self.assertIn(sha[:12], out.stdout)
        self.assertIn("blocked by reviewer", out.stdout)
        out = self.mesh("work", "fixes-owed", "other")
        self.assertEqual((out.returncode, out.stdout.strip()),
                         (0, "no fix owed by other"), out.stderr)
        out = self.mesh("work", "fixes-owed", "--all", "--json")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn('"name": "author"', out.stdout)


class BriefAndIdleTest(_Base):
    """La correction due passe en tête du brief et de la relance d'inactivité."""

    def test_the_section_opens_the_work_before_everything_else(self):
        lot, sha = self.blocked()
        text = self.brief("author")
        self.assertIn(reprise.FIX_OWED_HEADER, text)
        fixes = text.index(reprise.FIX_OWED_HEADER)
        self.assertLess(fixes, text.index("## Work items assigned to you"))
        self.assertLess(fixes, text.index("## Reviews you owe"))
        self.assertLess(fixes, text.index("## Mail"))
        section = text[fixes:text.index("\n## ", fixes + 1)]
        self.assertIn("#%d [qa]" % lot, section)
        self.assertIn("blocking verdict by reviewer", section)
        self.assertIn(sha[:12], section)
        self.assertIn("ameesh work milestone %d frozen" % lot, section)
        self.assertIn("ameesh work move %d qa" % lot, section)

    def test_an_agent_with_no_fix_owes_nothing(self):
        self.blocked()
        text = self.brief("other")
        self.assertIn(reprise.NO_FIX_OWED, text)

    def test_the_idle_turn_carries_the_fix_before_the_reviews(self):
        lot, _sha = self.blocked()
        # #3273 (rebase) : la relance nomme le pilote ; le worker factice n'en
        # a pas, donc la base est `IDLE_PROMPT_NO_PILOT`.
        worker = types.SimpleNamespace(db=self.db, name="author",
                                       _idle_pilot=lambda: "")
        prompt = runner.AgentWorker._idle_prompt(worker)
        self.assertTrue(prompt.endswith(adapters.IDLE_PROMPT_NO_PILOT))
        self.assertIn(adapters.IDLE_FIXES_HEADER, prompt)
        self.assertIn("#%d" % lot, prompt)
        free = types.SimpleNamespace(db=self.db, name="other",
                                     _idle_pilot=lambda: "")
        self.assertEqual(runner.AgentWorker._idle_prompt(free),
                         adapters.IDLE_PROMPT_NO_PILOT)


class BlockedFreezeSLOTest(_Base):
    """§9 « avis bloquant → nouveau gel » : 2 h, 1 h en P1, escalade §9."""

    def line(self, *, priority: int = 2, age: float = 3 * H):
        lot, _sha = self.blocked(age=age, priority=priority)
        item = work.get(self.db, lot)
        events = list(storage.of(self.db).progress.lot_events([lot]))
        milestones = list(storage.of(self.db).progress.lot_milestones([lot]))
        jalons = progress._work_item_milestones(item, events, [], milestones)
        fixes = [f for f in activation.due_fixes(self.db) if f["lot"] == lot]
        rows = slo.lot_steps(item, jalons, events, now=time.time(), fixes=fixes)
        got = [r for r in rows if r["step"] == "blocked_freeze"]
        self.assertEqual(len(got), 1, rows)
        return got[0]

    def test_the_step_is_measured_and_two_hours(self):
        step = slo.step_of("blocked_freeze")
        self.assertTrue(step.measured)
        self.assertEqual(step.source, slo.LOT_EVENTS)
        self.assertEqual((step.target_s, step.critical_s), (7200, 3600))

    def test_assignee_then_pilot_then_bureau(self):
        line = self.line(age=0.0)
        self.assertEqual((line["level"], line["targets"]), (0, ()))
        line = self.line(age=2 * H)
        self.assertEqual((line["level"], tuple(line["targets"])),
                         (1, ("assignee",)))
        self.assertEqual(line["blocked_by"], "reviewer")
        line = self.line(age=4 * H)
        self.assertEqual((line["level"], tuple(line["targets"])),
                         (2, ("pilot",)))
        line = self.line(age=6 * H)
        self.assertEqual((line["level"], tuple(line["targets"])),
                         (3, ("bureau", "auditeur")))

    def test_p1_is_one_hour(self):
        line = self.line(priority=1, age=H)
        self.assertEqual(line["level"], 1)
        self.assertEqual(line["target_s"], 3600)
        self.assertEqual(tuple(line["targets"]),
                         ("assignee", "bureau", "auditeur"))

    def test_a_closed_lot_owes_no_fix(self):
        self.blocked()
        j = progress._work_item_milestones(
            {"id": 1, "state": "merged", "assignee": "author"}, [], [])
        rows = slo.lot_steps({"id": 1, "state": "merged", "assignee": "author"},
                             j, [], now=time.time(),
                             fixes=[{"lot": 1, "blocked_ts": time.time() - 6 * H}])
        self.assertEqual([r for r in rows if r["step"] == "blocked_freeze"], [])

    def test_the_alert_names_the_fix_the_reviewer_and_the_head(self):
        lot, sha = self.blocked(age=3 * H)
        alerts = self.slo_alerts()
        self.assertEqual(len(alerts), 1, alerts)
        alert = alerts[0]
        self.assertEqual((alert["step"], alert["agent"], alert["level"]),
                         ("blocked_freeze", "author", 1))
        self.assertEqual(alert["blocked_by"], "reviewer")
        self.assertEqual(alert["sha"], sha)
        self.assertIn("blocking verdict of reviewer", alert["detail"])
        text = alert_briefs._slo_breach(alert)
        self.assertIn("fix owed by author", text)
        self.assertIn("blocking verdict of reviewer", text)
        self.assertIn("ameesh work milestone %d frozen" % lot, text)
        text = alert_briefs._slo_breach(dict(alert, level=2))
        self.assertIn("pilot:", text)
        self.assertIn("same family", text)
        self.assertIn("ameesh work assign %d" % lot, text)
        text = alert_briefs._slo_breach(dict(alert, level=3))
        self.assertIn("bureau or auditor:", text)
        # #3487 : le porteur porte le correctif ; le niveau 3 ne le réassigne pas.
        self.assertIn("do not reassign the holder", text)
        self.assertIn("carries the fix", text)
        self.assertNotIn("ameesh work assign", text)
        self.assertNotIn("reassign it now", text)

    def test_the_witness_ages_breach_at_the_expected_levels(self):
        # #3033 (22 h) et #3195 (9 h) auraient dû escalader au niveau 3.
        self.blocked(age=22 * H)
        self.blocked(age=9 * H)
        alerts = self.slo_alerts()
        self.assertEqual(sorted((a["level"] for a in alerts)), [3, 3])
        self.assertEqual({a["agent"] for a in alerts}, {"author"})

    def test_a_new_freeze_stops_the_alert(self):
        lot, _sha = self.blocked(age=3 * H)
        self.assertEqual(len(self.slo_alerts()), 1)
        self.refreeze(lot, "b" * 40)
        self.assertEqual(self.slo_alerts(), [])


if __name__ == "__main__":
    unittest.main()
