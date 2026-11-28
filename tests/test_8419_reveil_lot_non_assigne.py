# SPDX-License-Identifier: AGPL-3.0-only
"""#8419: a startable unassigned lot wakes an automatically drained host.

`return_targets` only saw the rows of `operations.listing`, where
`actionable_lots` counts lots assigned to the agent. A startable lot with no
assignee therefore woke nobody when every free agent sat on a stopped host.

The wake cause is the debit's own startable set (`waiting_project_lots`,
planned waits, footprint), one host per project, and only when no open host
already has idle capacity for that project. A manual drain stays an operator
window; `ameesh host wake --all` is the owner's gesture for the automatic
ones.
"""
from __future__ import annotations

import contextlib
import io
import unittest
from unittest import mock

from ameesh import host_lifecycle, host_release, storage, vm_adapter as vm, work

from .test_l94_underuse import _Base


HOST = "ag3"
HOST2 = "ag2"
OPEN = "hub"


def control(host=HOST, **over):
    row = dict(host=host, mode="draining", claims_open=False, automatic=True)
    row.update(over)
    return row


def agent(name="a", host=HOST, admitted=None, **over):
    row = dict(name=name, host=host, admitted_hosts=admitted or [host],
               releve="hote", mode="execute", status="idle",
               actionable_lots=0, unread=0, has_pending_prompt=False,
               restart_requested_ts=None, projects=["ameesh"])
    row.update(over)
    return row


def lot(ident=1, app="ameesh", **over):
    row = dict(id=ident, state="requested", type="evolution", app=app,
               assignee=None)
    row.update(over)
    return row


def controls(*hosts):
    out = {OPEN: {"host": OPEN, "mode": "available"}}
    for host in hosts or (HOST,):
        out[host] = control(host)
    return out


class ReturnTargetsUnassignedTest(unittest.TestCase):
    def targets(self, listing, startable, *, controls_=None, host=HOST):
        controls_ = controls_ or controls()
        return host_release.return_targets(
            controls_[host], listing, controls_, known_hosts=set(controls_),
            holds={}, startable=startable)

    def test_a_startable_lot_wakes_the_only_stopped_host(self):
        listing = [agent()]
        self.assertEqual(self.targets(listing, [lot()]), ["a"])
        self.assertTrue(vm.wake_due(HOST, control(), listing, [lot()],
                                    controls(), {}, now=0, horizon=900,
                                    target={}, startable=[lot()]))

    def test_open_idle_capacity_elsewhere_does_not_wake(self):
        listing = [agent(),
                   agent("b", host=OPEN, admitted=[OPEN], lease_live=True,
                         lease_owner="runner", active_lots=0,
                         assigned_lot_id=None)]
        self.assertEqual(self.targets(listing, [lot()]), [])
        self.assertFalse(vm.wake_due(HOST, control(), listing, [lot()],
                                     controls(), {}, now=0, horizon=900,
                                     target={}, startable=[lot()]))

    def test_open_capacity_on_another_project_does_not_count(self):
        listing = [agent(),
                   agent("b", host=OPEN, admitted=[OPEN], lease_live=True,
                         lease_owner="runner", active_lots=0,
                         assigned_lot_id=None, projects=["nexlink"])]
        self.assertEqual(self.targets(listing, [lot()]), ["a"])

    def test_a_critical_open_agent_is_not_roadmap_capacity(self):
        # The hub may hold an idle orchestrator; it does not take a roadmap
        # lot (the canon's capacity filter), so it must not keep a stopped
        # executor host asleep.
        listing = [agent(),
                   agent("orch", host=OPEN, admitted=[OPEN], lease_live=True,
                         lease_owner="runner", active_lots=0,
                         assigned_lot_id=None, roles=["orchestrateur"])]
        self.assertEqual(self.targets(listing, [lot()]), ["a"])

    def test_a_manual_drain_does_not_wake_for_unassigned_work(self):
        listing = [agent()]
        manual = controls()
        manual[HOST] = control(automatic=False)
        self.assertEqual(self.targets(listing, [lot()], controls_=manual), [])
        self.assertFalse(vm.wake_due(HOST, manual[HOST], listing, [lot()],
                                     manual, {}, now=0, horizon=900,
                                     target={}, startable=[lot()]))

    def test_no_startable_lot_does_not_wake(self):
        self.assertEqual(self.targets([agent()], []), [])

    def test_one_host_per_project_gets_the_wake(self):
        listing = [agent("a1"), agent("a2"),
                   agent("b1", host=HOST2, admitted=[HOST2])]
        controls_ = controls(HOST, HOST2)
        # HOST carries two free agents for the project, HOST2 one: only HOST
        # is woken, not every stopped host that could take the lot.
        self.assertEqual(self.targets(listing, [lot()], controls_=controls_,
                                      host=HOST), ["a1", "a2"])
        self.assertEqual(self.targets(listing, [lot()], controls_=controls_,
                                      host=HOST2), [])

    def test_tie_is_broken_by_host_name(self):
        listing = [agent("a", host=HOST), agent("b", host=HOST2, admitted=[HOST2])]
        controls_ = controls(HOST, HOST2)
        self.assertEqual(self.targets(listing, [lot()], controls_=controls_,
                                      host=HOST2), ["b"])  # "ag2" < "ag3"
        self.assertEqual(self.targets(listing, [lot()], controls_=controls_,
                                      host=HOST), [])

    def test_free_agent_does_not_need_a_live_lease_on_a_stopped_host(self):
        # A stopped host has no runner lease: the wake target is read from the
        # registry row, never from `idle_agents` (which needs a live lease).
        row = agent(lease_live=False)
        self.assertEqual(self.targets([row], [lot()]), ["a"])

    def test_an_elected_role_is_not_a_roadmap_taker(self):
        self.assertEqual(self.targets([agent(releve="election")], [lot()]), [])

    def test_a_reviewer_or_critical_role_is_not_a_roadmap_taker(self):
        for roles in (["relecture"], ["orchestrateur"], ["conception"]):
            with self.subTest(roles=roles):
                self.assertEqual(self.targets([agent(roles=roles)], [lot()]), [])

    def test_an_agent_not_admitted_on_the_host_does_not_wake_it(self):
        self.assertEqual(self.targets([agent(admitted=["elsewhere"])], [lot()]), [])


class WakeAutomaticTest(_Base):
    def setUp(self):
        super().setUp()
        self.api = storage.of(self.db).host_lifecycle
        self.db.execute("DELETE FROM host_lifecycle")

    def drain(self, host, **over):
        return self.api.drain(host, actor="test", automatic=True, **over)

    def test_wake_all_returns_only_the_automatic_drains(self):
        self.drain("auto-1")
        self.drain("auto-2")
        self.api.drain("manual", actor="test", automatic=False)
        self.drain("done")
        self.api.undrain("done", actor="test")
        self.drain("lost")
        self.db.execute("UPDATE host_lifecycle SET mode='lost' WHERE host='lost'")

        rows = host_lifecycle.wake_automatic(self.cfg, self.db,
                                             actor="human:tester")
        self.assertEqual([row["host"] for row in rows], ["auto-1", "auto-2"])
        for host in ("auto-1", "auto-2"):
            row = self.api.get(host)
            self.assertNotEqual(row["mode"], "draining")
            self.assertTrue(row["claims_open"])
        self.assertEqual(self.api.get("manual")["mode"], "draining")
        self.assertNotEqual(self.api.get("done")["mode"], "draining")
        self.assertEqual(self.api.get("lost")["mode"], "lost")

    def test_a_second_wake_all_has_nothing_left(self):
        self.drain("auto")
        host_lifecycle.wake_automatic(self.cfg, self.db, actor="human:tester")
        self.assertEqual(host_lifecycle.wake_automatic(
            self.cfg, self.db, actor="human:tester"), [])


class WakeCommandTest(unittest.TestCase):
    def run_main(self, argv, rows=()):
        db = mock.Mock()
        with contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(host_lifecycle.config, "load",
                                  return_value=object()), \
                mock.patch.object(host_lifecycle.db_mod, "connect",
                                  return_value=db), \
                mock.patch.object(host_lifecycle.storage, "of",
                                  return_value=mock.Mock()), \
                mock.patch.object(host_lifecycle.work, "corrector",
                                  return_value="human:test"), \
                mock.patch.object(host_lifecycle, "wake_automatic",
                                  return_value=list(rows)) as wake:
            code = host_lifecycle.main(argv)
        self.assertTrue(db.close.called)
        return code, wake

    def test_wake_all_is_the_owner_gesture(self):
        code, wake = self.run_main(["wake", "--all"], [{"host": "a"}])
        self.assertEqual(code, 0)
        self.assertEqual(wake.call_args.kwargs["actor"], "human:test")

    def test_wake_all_names_a_refusal(self):
        code, _wake = self.run_main(["wake", "--all"], [{"host": "a", "error": "x"}])
        self.assertEqual(code, 1)

    def test_wake_needs_a_host_or_all(self):
        self.assertEqual(self.run_main(["wake"])[0], 1)
        self.assertEqual(self.run_main(["wake", "a", "--all"])[0], 1)


class StartableUnassignedLotsTest(_Base):
    def setUp(self):
        super().setUp()
        self.agent("dev")

    def _footprint(self, item, *paths):
        storage.of(self.db).work_footprints.replace(
            int(item["id"]), "manual", list(paths), actor="test")

    def test_a_footprint_held_lot_is_not_startable(self):
        occupe = work.add(self.db, title="occupe", app="ameesh", assignee="dev")
        self.db.execute("UPDATE work_items SET state='build' WHERE id=%s",
                        (occupe["id"],))
        libre = work.add(self.db, title="libre", app="ameesh")
        recouvert = work.add(self.db, title="recouvert", app="ameesh")
        self._footprint(occupe, "src/ameesh/x.py")
        self._footprint(recouvert, "src/ameesh/x.py")
        lots = storage.of(self.db).operations.open_lots_activity(None)
        startable = host_release.startable_unassigned_lots(self.db, lots)
        ids = {int(item["id"]) for item in startable}
        self.assertIn(int(libre["id"]), ids)
        self.assertNotIn(int(recouvert["id"]), ids)

    def test_a_planned_dependency_is_not_startable(self):
        amont = work.add(self.db, title="amont", app="ameesh", assignee="dev")
        self.db.execute("UPDATE work_items SET state='build' WHERE id=%s",
                        (amont["id"],))
        bloque = work.add(self.db, title="bloque", app="ameesh")
        storage.of(self.db).work_dependencies.add(
            bloque["id"], amont["id"], actor="test")
        lots = storage.of(self.db).operations.open_lots_activity(None)
        ids = {int(item["id"]) for item in
               host_release.startable_unassigned_lots(self.db, lots)}
        self.assertNotIn(int(bloque["id"]), ids)

    def test_a_backlog_item_does_not_wake_a_host(self):
        item = work.add(self.db, title="panier", app="ameesh")
        self.db.execute(
            "UPDATE work_items SET type='improvement', expected_value='x',"
            " value_score=1 WHERE id=%s", (item["id"],))
        lots = storage.of(self.db).operations.open_lots_activity(None)
        ids = {int(row["id"]) for row in
               host_release.startable_unassigned_lots(self.db, lots)}
        self.assertNotIn(int(item["id"]), ids)


if __name__ == "__main__":
    unittest.main()
