# SPDX-License-Identifier: AGPL-3.0-only
"""#254: the owner holds a host's runners on purpose (`ameesh hosts HOST
--hold --until … --reason …`, table `host_holds`, 0392). While it lasts, the
alerts that would advise waking, resuming or reassigning its agents stay
quiet — one `host_hold` line says what was muted — and the hold always ends
(`--until`, 24 h at most). Independently, `idle_capacity` never suggests the
idle agents of a host under pressure."""
from __future__ import annotations

import json
import time
import unittest

from ameesh import backlog, exploitation, host_holds, mail, registry, relocation, storage, work

from . import test_l119_backlog
from .test_l31_relocation import _MoveCase
from .test_l94_sous_utilisation import HEURE, JOUR, _Base, _seuils, _types


class ParseUntilTest(unittest.TestCase):
    def test_a_hold_always_ends_and_soon(self):
        self.assertEqual(host_holds.parse_until("90m"), (5400.0, None))
        self.assertEqual(host_holds.parse_until("2h"), (7200.0, None))
        self.assertEqual(host_holds.parse_until("1d"), (86400.0, None))
        for bad in ("", "soon", "0m", "25h", "2d"):
            with self.assertRaises(host_holds.HoldError, msg=bad):
                host_holds.parse_until(bad)
        # an instant is the database's to judge, on its own clock
        later = time.time() + 3600
        iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(later))
        seconds, until = host_holds.parse_until(iso)
        self.assertIsNone(seconds)
        self.assertAlmostEqual(until, later, delta=1)


class HostHoldTest(_Base):
    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE host_holds, host_hold_events")
        self.hosts = storage.of(self.db).hosts

    def hold(self, host, hours=2.0):
        return self.hosts.hold(host, seconds=hours * HEURE, reason="disk migration",
                               author="human:proprio")

    def expire(self):
        self.db.execute("UPDATE host_holds SET created_at = now() - interval '2 hours', "
                        "until = now() - interval '1 second'")

    def alerts(self, **kw):
        return exploitation.alerts(self.cfg, self.db, now=time.time(), **_seuils(**kw))

    def night(self):
        """The night of 2026-10-12: runners stopped on `pc`, one left on `vm`."""
        for name, host in (("a-dead", "pc"), ("a-mail", "pc"), ("a-lot", "pc"),
                           ("b-dead", "vm")):
            self.agent(name, host=host)
        self.db.execute("UPDATE agent_registry SET lease_expires_at = now() - interval "
                        "'5 minutes' WHERE name IN ('a-dead', 'b-dead')")
        self.db.execute("UPDATE agent_registry SET lease_owner = NULL, "
                        "lease_expires_at = NULL WHERE name = 'a-mail'")
        mail.send(self.db, "orch", "a-mail", "à lire")
        self.db.execute("UPDATE agent_mailbox SET created_at = now() - interval '10 minutes'")
        lot = work.add(self.db, title="Lot du soir", assignee="a-lot", app="p")
        work.move(self.db, lot["id"], "build")
        for sql in ("UPDATE work_items SET created_at = now() - interval '8 hours',"
                    " updated_at = now() - interval '7 hours'",
                    "UPDATE work_item_events SET created_at = now() - interval '7 hours'",
                    "UPDATE work_item_milestones SET at = now() - interval '7 hours'"):
            self.db.execute(sql)
        return lot

    def seen(self, alerts):
        return {(a["type"], a.get("agent")) for a in alerts
                if a["type"] in host_holds.MUTED_TYPES + ("host_hold", "dead_runner")}

    def test_a_hold_mutes_its_agents_and_says_so(self):
        self.night()
        before = self.seen(self.alerts())
        self.assertLessEqual({("dead_runner", "a-dead"), ("idle_with_mail", "a-mail"),
                              ("stale_lot", "a-lot"), ("dead_runner", "b-dead")}, before)
        self.hold("pc")
        out = self.alerts()
        # r2 (P1): a lost lease stays loud, hold or not
        self.assertEqual(self.seen(out), {("dead_runner", "a-dead"), ("dead_runner", "b-dead"),
                                          ("host_hold", None)})
        line = _types(out, "host_hold")[0]
        self.assertEqual((line["host"], line["author"], line["reason"], line["agents"]),
                         ("pc", "human:proprio", "disk migration", ["a-dead", "a-lot", "a-mail"]))
        self.assertEqual(line["muted"], {"idle_with_mail": 1, "stale_lot": 1})
        self.assertEqual(line["value"], 2)
        self.assertIn("ameesh hosts pc --release", line["detail"])
        # past its end, the hold is ignored: everything is back, without cleanup
        self.expire()
        self.assertEqual(self.seen(self.alerts()), before)
        self.assertTrue(self.db.query("SELECT 1 FROM host_holds"))
        # lifted
        self.hold("pc")
        self.assertTrue(self.hosts.release("pc", author="human:proprio"))
        self.assertFalse(self.hosts.release("pc", author="human:proprio"))
        self.assertEqual(self.seen(self.alerts()), before)

    def test_an_agent_is_held_when_every_admitted_host_is(self):
        self.agent("multi", host="pc")
        self.agent("ailleurs", host="vm")
        self.db.execute("UPDATE agent_registry SET admitted_hosts = ARRAY['pc', 'vm'] "
                        "WHERE name = 'multi'")
        self.db.execute("UPDATE agent_registry SET admitted_hosts = ARRAY['pc'] "
                        "WHERE name = 'ailleurs'")
        self.db.execute("UPDATE agent_registry SET lease_owner = NULL, lease_expires_at = NULL")
        for name in ("multi", "ailleurs"):
            mail.send(self.db, "orch", name, "à lire")
        self.db.execute("UPDATE agent_mailbox SET created_at = now() - interval '10 minutes'")
        self.hold("pc")
        waiting = {a["agent"] for a in _types(self.alerts(), "idle_with_mail")}
        self.assertEqual(waiting, {"multi"})       # it can still run on vm
        self.hold("vm")
        self.assertEqual(_types(self.alerts(), "idle_with_mail"), [])

    def test_idle_capacity_and_plan_leave_held_agents_out(self):
        self.agent("pc1", host="pc")
        self.agent("pc-orch", host="pc", status="stopped", owner="")
        self.agent("vm1", host="vm")
        # admitted on vm too, but running on the held host: never suggested
        self.agent("pc2", host="pc")
        self.db.execute("UPDATE agent_registry SET admitted_hosts = ARRAY['pc', 'vm'] "
                        "WHERE name = 'pc2'")
        work.add(self.db, title="à prendre")
        self.hold("pc")
        out = _types(self.alerts(orchestrators_declared=("pc-orch",)), "idle_capacity")
        self.assertEqual(out[0]["agents"], ["vm1"])
        self.assertIn("left out, their host on hold: pc2 on pc", out[0]["detail"])
        self.assertNotIn("wake the orchestrator pc-orch", out[0]["detail"])
        self.hold("vm")
        self.assertEqual(_types(self.alerts(), "idle_capacity"), [])
        # a plan whose agents are all held loses nothing by negligence
        self.jauge("claude", "seven_day", 0.20, 4 * JOUR, 7 * JOUR, account="primaire")
        self.assertEqual(_types(self.alerts(), "plan_underused"), [])
        self.hosts.release("vm", author="human:proprio")
        self.assertEqual(len(_types(self.alerts(), "plan_underused")), 1)

    def test_cli_hold_and_release(self):
        # as for `work set-app`: a human, an orchestrator or a design agent
        proc = self.mesh("hosts", "pc", "--hold", "--until", "2h", "--reason", "migration")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("say who holds", proc.stderr)
        self.agent("pc1", host="pc")
        proc = self.mesh("hosts", "pc", "--hold", "--until", "2h", "--reason", "migration",
                         "--actor", "pc1")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("hold refused: pc1 is neither a human", proc.stderr)
        self.assertEqual(host_holds.live(self.db), {})
        proc = self.mesh("hosts", "pc", "--hold", "--until", "2h", "--reason", "migration",
                         "--actor", "human:proprio")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("host pc on hold until", proc.stdout)
        hold = host_holds.live(self.db)["pc"]
        self.assertEqual((hold["reason"], hold["author"]), ("migration", "human:proprio"))
        self.assertAlmostEqual(hold["until_ts"], time.time() + 2 * HEURE, delta=60)
        proc = self.mesh("hosts", "pc", "--json")
        self.assertEqual(json.loads(proc.stdout.splitlines()[0])["hold"]["reason"], "migration")
        self.assertIn("ON HOLD", self.mesh("hosts", "pc").stdout)
        for args in (("pc", "--hold", "--reason", "x"),                 # no end
                     ("pc", "--hold", "--until", "2h"),                 # no reason
                     ("pc", "--hold", "--until", "3d", "--reason", "x"),
                     ("--hold", "--until", "2h", "--reason", "x"),       # no host
                     ("pc", "--until", "2h")):
            proc = self.mesh("hosts", *args)
            self.assertEqual(proc.returncode, 2, args)
        proc = self.mesh("hosts", "pc", "--release", "--actor", "human:proprio")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("hold lifted", proc.stdout)
        self.assertEqual(self.mesh("hosts", "pc", "--release", "--actor",
                                   "human:proprio").returncode, 1)
        self.assertEqual(host_holds.live(self.db), {})
        # r2 (P5): every gesture is kept, in the same transaction as the change
        self.assertEqual([(e["host"], e["action"], e["author"], e["reason"])
                          for e in self.hosts.hold_events()],
                         [("pc", "hold", "human:proprio", "migration"),
                          ("pc", "release", "human:proprio", None)])

    def test_cli_refuses_an_unknown_host(self):
        # r2 (P4): neither a Host of the canon nor a host of the registry
        proc = self.mesh("hosts", "does-not-exist", "--hold", "--until", "2h", "--reason",
                         "x", "--actor", "human:proprio")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("neither a Host of the canon nor a host of the registry", proc.stderr)
        self.assertEqual(host_holds.live(self.db), {})
        self.assertNotIn("does-not-exist", self.mesh("hosts").stdout)

    def test_a_replaced_or_lifted_hold_keeps_its_history(self):
        # r2 (P5)
        self.hold("pc", hours=1)
        self.hosts.hold("pc", seconds=HEURE, reason="second", author="human:autre")
        self.hosts.release("pc", author="human:proprio")
        self.assertEqual([(e["action"], e["author"], e["reason"])
                          for e in self.hosts.hold_events("pc")],
                         [("hold", "human:proprio", "disk migration"),
                          ("hold", "human:autre", "second"),
                          ("release", "human:proprio", None)])

    def test_the_end_is_bounded_on_database_time(self):
        # r2 (P3): the caller's clock decides nothing
        db_now = float(self.db.query("SELECT extract(epoch from now())::float8 AS db_now")
                       [0]["db_now"])
        hold = self.hosts.hold("pc", seconds=2 * HEURE, reason="r", author="human:proprio")
        self.assertAlmostEqual(hold["until_ts"] - hold["created_ts"], 2 * HEURE, delta=0.01)
        self.assertAlmostEqual(hold["created_ts"], db_now, delta=60)
        # an instant past or beyond 24 h of database time is refused
        for until in (db_now - 60, db_now + 24 * HEURE + 120, db_now + 26 * HEURE):
            self.assertIsNone(self.hosts.hold("pc", until_ts=until, reason="r",
                                              author="human:proprio"), until)
        self.assertIsNone(self.hosts.hold("pc", seconds=25 * HEURE, reason="r",
                                          author="human:proprio"))
        self.assertAlmostEqual(host_holds.live(self.db)["pc"]["until_ts"],
                               hold["until_ts"], delta=0.01)
        # and the table itself refuses a longer one, whoever writes it
        with self.assertRaises(Exception):
            self.db.execute("INSERT INTO host_holds (host, reason, author, until) "
                            "VALUES ('vm', 'r', 'human:x', now() + interval '26 hours')")
        self.agent("pc1", host="pc")
        proc = self.mesh("hosts", "pc", "--hold", "--reason", "x", "--actor", "human:proprio",
                         "--until", time.strftime("%Y-%m-%dT%H:%M:%S",
                                                  time.localtime(db_now + 26 * HEURE)))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("database time", proc.stderr)


class PressureTest(_Base):
    def reading(self, host, *, mem):
        self.db.execute(
            "INSERT INTO host_resources (host, sampled_at, mem_available_bytes, "
            "swap_used_bytes, load1, cpu_count, disk_free_bytes, turns_in_progress) "
            "VALUES (%s, now(), %s, 0, 0.1, 4, %s, 0)", (host, mem, 50 * 1024 ** 3))

    def test_idle_capacity_never_suggests_a_host_under_pressure(self):
        self.agent("chaud1", host="chaud")
        self.agent("frais1", host="frais")
        self.reading("chaud", mem=100 * 1024 ** 2)
        self.reading("frais", mem=8 * 1024 ** 3)
        work.add(self.db, title="à prendre")
        out = _types(exploitation.alerts(self.cfg, self.db, now=time.time(), **_seuils()),
                     "idle_capacity")
        self.assertEqual(out[0]["agents"], ["frais1"])
        self.assertIn("left out, their host under pressure: chaud1 on chaud", out[0]["detail"])
        # a stale reading says nothing about the host now
        self.db.execute("UPDATE host_resources SET sampled_at = now() - interval '1 hour' "
                        "WHERE host = 'chaud'")
        out = _types(exploitation.alerts(self.cfg, self.db, now=time.time(), **_seuils()),
                     "idle_capacity")
        self.assertEqual(out[0]["agents"], ["chaud1", "frais1"])
        # every idle agent's host under pressure: nothing to suggest
        self.db.execute("DELETE FROM host_resources")
        self.reading("chaud", mem=100 * 1024 ** 2)
        self.reading("frais", mem=100 * 1024 ** 2)
        self.assertEqual(_types(exploitation.alerts(self.cfg, self.db, now=time.time(),
                                                    **_seuils()), "idle_capacity"), [])


class HeldHostTakesNoWorkTest(_MoveCase):
    """r2 (P2): a held host takes no lease, receives no relocated agent."""

    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE host_holds, host_hold_events")
        self.hosts = storage.of(self.db).hosts

    def test_no_new_lease_on_a_held_host_the_live_one_renews(self):
        self._prepare()
        self.hosts.hold("atelier", seconds=HEURE, reason="r", author="human:proprio")
        self.assertEqual(registry.claimable(self.db, "atelier", require_responsible=False), [])
        self.assertIsNone(registry.claim(self.db, "ouvrier", "runner-a", 600))
        self.hosts.release("atelier", author="human:proprio")
        lease = registry.claim(self.db, "ouvrier", "runner-a", 600)
        self.assertIsNotNone(lease)
        # held meanwhile: the runner renews the lease it holds, takes no other
        self.hosts.hold("atelier", seconds=HEURE, reason="r", author="human:proprio")
        self.assertIsNotNone(registry.claim(self.db, "ouvrier", "runner-a", 600))
        self.assertIsNone(registry.claim(self.db, "ouvrier", "runner-b", 600))

    def test_nothing_moves_onto_a_held_host(self):
        _row, owner, epoch = self._pret()
        plan = self._plan()
        self.assertEqual(plan["target"], "banc")
        self.hosts.hold("banc", seconds=HEURE, reason="r", author="human:proprio")
        # the healthy, held host is no candidate
        self.assertIsNone(self._plan())
        # and the write refuses it, even from a plan made before the hold
        self.assertIsNone(relocation.move(self.db, registry.get(self.db, "ouvrier"), plan,
                                          current_host="atelier", owner=owner, epoch=epoch))
        self.assertEqual(registry.get(self.db, "ouvrier")["host"], "atelier")


class HeldHostIntakeTest(test_l119_backlog._Base):
    def test_automatic_intake_skips_a_held_host(self):
        # r2 (P2): its live idle agents stay out of automatic work
        self.agent("pc1", host="pc")
        self.element("dette", 80)
        hosts = storage.of(self.db).hosts
        hosts.hold("pc", seconds=HEURE, reason="r", author="human:proprio")
        self.assertEqual(self.take(dry_run=True), [])
        hosts.release("pc", author="human:proprio")
        self.assertEqual([t["agent"] for t in self.take(dry_run=True)], ["pc1"])
