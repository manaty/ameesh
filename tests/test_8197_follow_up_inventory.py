# SPDX-License-Identifier: AGPL-3.0-only
"""#8197 (tranche 6 de #8191, issue manaty/ameesh#6403) : attente nommée des
suivis (`work wait --on|--clear`) et inventaire d'ouverture
(`work list --assignee <agent> --type follow-up --by-subject --json`)."""
import json
import time

from ameesh import registry, storage, work, work_follow_ups, work_subjects

from .support import PgTestCase

APP = "t8197"


class FollowUpWaitTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE work_subjects RESTART IDENTITY")
        for name in ("avatar", "other"):
            registry.upsert(self.db, name, harness="codex", host=self.cfg.host,
                            cwd=self.tmp, mode="execute")

    def follow_up(self, title="Check the requested answer", **kwargs):
        fields = dict(title=title, type="follow-up", assignee="avatar", app=APP,
                      actor="avatar", cfg=self.cfg)
        fields.update(kwargs)
        return work.add(self.db, **fields)

    def entries(self, assignee="avatar"):
        data = work_follow_ups.inventory(self.db, assignee)
        return data, {item["id"]: item for group in data["subjects"]
                      for item in group["follow_ups"]}

    def test_wait_names_who_and_since_then_clear_reopens(self):
        lot = self.follow_up()
        before = time.time()
        waited = work_follow_ups.wait(self.db, lot["id"], "  Sébastien \n (devis) ",
                                      actor="avatar")
        self.assertEqual(waited["state"], "blocked")
        _, items = self.entries()
        item = items[lot["id"]]
        self.assertEqual((item["state"], item["waiting_on"]), ("waiting", "Sébastien (devis)"))
        self.assertGreaterEqual(item["since_ts"], before - 5)
        since = item["since_ts"]
        # Re-wait on someone else: who changes, since stays.
        work_follow_ups.wait(self.db, lot["id"], "lot #12", actor="avatar")
        _, items = self.entries()
        self.assertEqual(items[lot["id"]]["waiting_on"], "lot #12")
        self.assertEqual(items[lot["id"]]["since_ts"], since)
        reopened = work_follow_ups.clear(self.db, lot["id"], actor="avatar", why="answered")
        self.assertEqual(reopened["state"], "intake")
        _, items = self.entries()
        self.assertEqual(items[lot["id"]]["state"], "open")
        self.assertIsNone(items[lot["id"]]["waiting_on"])
        self.assertIsNone(items[lot["id"]]["since"])
        self.assertIn("waiting cleared: answered",
                      [e["note"] for e in work.events(self.db, lot["id"])])

    def test_wait_reserved_to_open_follow_ups(self):
        code = work.add(self.db, title="Development", app=APP, assignee="avatar",
                        actor="avatar", footprint=[])
        with self.assertRaisesRegex(work.WorkError, "reserved to follow-up"):
            work_follow_ups.wait(self.db, code["id"], "x", actor="avatar")
        lot = self.follow_up()
        for on in ("", "   ", "x" * 201):
            with self.subTest(on=on), self.assertRaises(work.WorkError):
                work_follow_ups.wait(self.db, lot["id"], on, actor="avatar")
        with self.assertRaisesRegex(work.WorkError, "not waiting"):
            work_follow_ups.clear(self.db, lot["id"], actor="avatar")
        work.close(self.db, lot["id"], done=True, actor="avatar")
        with self.assertRaisesRegex(work.WorkError, "closed"):
            work_follow_ups.wait(self.db, lot["id"], "x", actor="avatar")

    def test_note_by_assignee_or_human_reopens_wake_note_does_not(self):
        lot = self.follow_up()
        work_follow_ups.wait(self.db, lot["id"], "Sébastien", actor="avatar")
        # a wake re-armed while waiting keeps the wait
        work.note(self.db, lot["id"], "still nothing, check tomorrow", actor="avatar",
                  wake_ts=time.time() + 86400)
        self.assertEqual(work.get(self.db, lot["id"])["state"], "blocked")
        # another agent's note does not take the follow-up out of waiting
        work.note(self.db, lot["id"], "seen", actor="other")
        self.assertEqual(work.get(self.db, lot["id"])["state"], "blocked")
        work.note(self.db, lot["id"], "Sébastien answered", actor="avatar")
        self.assertEqual(work.get(self.db, lot["id"])["state"], "intake")
        work_follow_ups.wait(self.db, lot["id"], "Sébastien", actor="avatar")
        work.note(self.db, lot["id"], "done on my side", actor="human:smichea")
        self.assertEqual(work.get(self.db, lot["id"])["state"], "intake")

    def test_inventory_grouped_by_subject_with_wake_and_issue(self):
        work_subjects.add(self.db, "pricing-v2", "Pricing v2", project=APP)
        a = self.follow_up("A", issue_ref="manaty/ameesh#1")
        b = self.follow_up("B")
        c = self.follow_up("C")
        closed = self.follow_up("closed")
        mine_elsewhere = self.follow_up("other's", assignee="other")
        code = work.add(self.db, title="Development", app=APP, assignee="avatar",
                        actor="avatar", footprint=[])
        for lot in (a, b):
            work_subjects.attach(self.db, lot["id"], "pricing-v2", actor="avatar")
        work.close(self.db, closed["id"], done=True, actor="avatar")
        wake = time.time() + 3600
        work_follow_ups.wait(self.db, a["id"], "Sébastien", actor="avatar", wake_ts=wake)
        data, items = self.entries()
        self.assertEqual((data["count"], data["waiting"]), (3, 1))
        self.assertEqual(sorted(items), [a["id"], b["id"], c["id"]])
        self.assertNotIn(code["id"], items)
        self.assertNotIn(mine_elsewhere["id"], items)
        self.assertEqual([g["subject"] for g in data["subjects"]], ["pricing-v2", None])
        self.assertEqual(data["subjects"][1]["title"], "sans sujet")
        self.assertEqual([i["id"] for i in data["subjects"][0]["follow_ups"]],
                         [a["id"], b["id"]])
        item = items[a["id"]]
        self.assertEqual((item["subject"], item["issue_ref"], item["waiting_on"]),
                         ("pricing-v2", "manaty/ameesh#1", "Sébastien"))
        self.assertAlmostEqual(item["next_wake_ts"], wake, delta=1)
        self.assertTrue(item["next_wake"].endswith("Z"))
        for key in ("waiting_on", "since", "next_wake", "subject", "issue_ref"):
            self.assertIn(key, items[c["id"]])
        text = work_follow_ups.render(data)
        self.assertIn("pricing-v2 — Pricing v2 (2)", text)
        self.assertIn("sans sujet (1)", text)
        self.assertIn("waiting on Sébastien", text)
        self.assertIn("no check scheduled", text)

    def test_blocked_by_move_has_no_named_wait(self):
        lot = self.follow_up()
        # an earlier, cleared wait is not the current one
        work_follow_ups.wait(self.db, lot["id"], "Sébastien", actor="avatar")
        work_follow_ups.clear(self.db, lot["id"], actor="avatar")
        work.move(self.db, lot["id"], "blocked", note="parked", actor="avatar")
        _, items = self.entries()
        self.assertEqual(items[lot["id"]]["state"], "waiting")
        self.assertIsNone(items[lot["id"]]["waiting_on"])
        self.assertIsNotNone(items[lot["id"]]["since_ts"])

    def test_storage_one_read(self):
        lot = self.follow_up()
        rows = storage.of(self.db).work.follow_ups(
            "avatar", waiting_prefix=work_follow_ups.WAITING_PREFIX)
        self.assertEqual([r["id"] for r in rows], [lot["id"]])
        self.assertIsNone(rows[0]["subject_slug"])

    def test_cli_wait_and_inventory(self):
        lot = self.follow_up()
        proc = self.mesh("work", "wait", str(lot["id"]), "--on", "Sébastien",
                         "--wake-in", "2h", "--actor", "avatar")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("waiting on Sébastien, next check", proc.stdout)
        proc = self.mesh("work", "list", "--assignee", "avatar", "--type", "follow-up",
                         "--by-subject", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        self.assertEqual(data["subjects"][0]["subject"], None)
        self.assertEqual(data["subjects"][0]["follow_ups"][0]["waiting_on"], "Sébastien")
        proc = self.mesh("work", "list", "--assignee", "avatar", "--type", "follow-up",
                         "--by-subject")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("sans sujet (1)", proc.stdout)
        for argv in (("work", "wait", str(lot["id"])),
                     ("work", "wait", str(lot["id"]), "--on", "x", "--clear"),
                     ("work", "list", "--type", "follow-up"),
                     ("work", "list", "--assignee", "avatar", "--by-subject",
                      "--state", "intake")):
            with self.subTest(argv=argv):
                self.assertNotEqual(self.mesh(*argv).returncode, 0)
        proc = self.mesh("work", "wait", str(lot["id"]), "--clear", "--actor", "avatar")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(work.get(self.db, lot["id"])["state"], "intake")
