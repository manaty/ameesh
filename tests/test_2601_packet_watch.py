# SPDX-License-Identifier: AGPL-3.0-only
"""#2601: "missed slot" alert of the hourly packets.

The compiled cadence is a fake hourly calendar in Europe/Paris; the candidate
list, the carriers and the mail are injected. No network, no Git, no database.
"""
from __future__ import annotations

import argparse
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
from zoneinfo import ZoneInfo

from ameesh import merge_queue, packet_watch, release

PARIS = ZoneInfo("Europe/Paris")


def at(hour: int, minute: int = 0) -> float:
    return datetime(2026, 10, 19, hour, minute, tzinfo=PARIS).timestamp()


class Hourly:
    packet_cutoffs = tuple("%02d:00" % h for h in range(24))

    def zone(self):
        return PARIS

    def last_packet(self, now):
        return float(int(now // 3600) * 3600)

    def next_packet(self, now):
        return float((int(now // 3600) + 1) * 3600)


class NoCadence(Hourly):
    packet_cutoffs = ()


def cfg_with(state_dir: str, block=None, host="pc-smichea"):
    return types.SimpleNamespace(
        host=host, state_dir=state_dir,
        packet_watch=block if block is not None else {
            "host": "pc-smichea",
            "projects": {"ameesh": {"notify": ["composeur", "pilote"], "grace_s": 900}}})


def payload(ready=(2601,), errors=()):
    return {"target": "main", "base": "a" * 40, "ready": list(ready),
            "errors": list(errors),
            "candidates": [{"work_item": lot, "source": "b" * 40,
                            "branch": "lot/%d-x" % lot} for lot in ready]}


class Config(unittest.TestCase):
    def test_other_host_or_no_block_is_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(packet_watch.projects(cfg_with(tmp, block={})), {})
            self.assertEqual(packet_watch.projects(cfg_with(tmp, host="ameesh-agents-1")), {})

    def test_notify_is_required_and_keys_are_strict(self):
        with tempfile.TemporaryDirectory() as tmp:
            for entry in ({}, {"notify": []}, {"notify": ["x"], "oops": 1},
                          {"notify": ["x"], "grace_s": -1}):
                with self.subTest(entry=entry), self.assertRaises(packet_watch.WatchError):
                    packet_watch.projects(cfg_with(tmp, block={"projects": {"ameesh": entry}}))
            watch = packet_watch.projects(cfg_with(tmp))["ameesh"]
            self.assertEqual(watch.notify, ("composeur", "pilote"))
            self.assertEqual(watch.grace_s, 900.0)


    def test_inventory_needs_radar_or_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            watch = packet_watch.projects(cfg_with(tmp))["ameesh"]
            with mock.patch("ameesh.merge_queue.declared_projects", return_value={}), \
                    self.assertRaisesRegex(packet_watch.WatchError, "radar.projects"):
                packet_watch._inventory(cfg_with(tmp), None, watch, "ameesh")


class Slot(unittest.TestCase):
    def test_grace_then_watched_until_next(self):
        rules = Hourly()
        self.assertIsNone(packet_watch.slot(rules, at(4, 10), 900))
        found = packet_watch.slot(rules, at(4, 20), 900)
        self.assertEqual((found["label"], found["stamp"]), ("04:00", "20261019-0400"))
        self.assertEqual(found["next"], at(5))

    def test_no_cadence_invents_nothing(self):
        self.assertIsNone(packet_watch.slot(NoCadence(), at(4, 30), 900))


class Check(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = cfg_with(self.tmp.name)
        self.watch = packet_watch.projects(self.cfg)["ameesh"]
        self.sent = []
        free = mock.patch.object(packet_watch, "carriers",
                                 return_value={"open": [], "recent": [], "served": []})
        self.carriers = free.start()
        self.addCleanup(free.stop)

    def run_check(self, now, state, ready=(2601,), **kw):
        return packet_watch.check(
            self.cfg, None, self.watch, Hourly(), now=now, state=state,
            inventory=lambda: payload(ready), send=lambda *a: self.sent.append(a), **kw)

    def test_alerts_once_per_slot_to_every_recipient(self):
        state = {}
        result = self.run_check(at(4, 20), state)
        self.assertEqual(result["result"], "alerted")
        self.assertEqual([to for to, _t, _d in self.sent], ["composeur", "pilote"])
        to, text, data = self.sent[0]
        self.assertTrue(data["urgent"])
        self.assertEqual(data["packet_slot_missed"]["ready"], [2601])
        self.assertIn("#2601", text)
        self.assertIn("integration/paquet-h-20261019-0400", text)
        self.assertEqual(self.run_check(at(4, 40), state)["result"], "already-alerted")
        self.assertEqual(len(self.sent), 2)
        # the next slot is a new one
        self.assertEqual(self.run_check(at(5, 20), state)["result"], "alerted")
        self.assertEqual(len(self.sent), 4)

    def test_grace_busy_and_nothing_ready_stay_silent(self):
        self.assertEqual(self.run_check(at(4, 5), {})["result"], "grace")
        self.carriers.return_value = {"open": [2608], "recent": [], "served": []}
        self.assertEqual(self.run_check(at(4, 20), {})["result"], "busy")
        self.carriers.return_value = {"open": [], "recent": [2604], "served": []}
        self.assertEqual(self.run_check(at(4, 20), {})["result"], "busy")
        self.carriers.return_value = {"open": [], "recent": [], "served": []}
        self.assertEqual(self.run_check(at(4, 20), {}, ready=())["result"], "nothing-ready")
        self.assertEqual(self.sent, [])

    def test_dry_run_neither_sends_nor_records(self):
        state = {}
        result = self.run_check(at(4, 20), state, dry_run=True)
        self.assertEqual(result["result"], "would-alert")
        self.assertEqual(self.sent, [])
        self.assertNotIn("last_alert_slot", state.get("projects", {}).get("ameesh", {}))

    def test_failed_delivery_is_retried(self):
        state = {}

        def refuse(*_a):
            raise RuntimeError("down")
        result = packet_watch.check(self.cfg, None, self.watch, Hourly(), now=at(4, 20),
                                    state=state, inventory=lambda: payload(), send=refuse)
        self.assertEqual(result["result"], "not-delivered")
        self.assertEqual(self.run_check(at(4, 25), state)["result"], "alerted")


class Carriers(unittest.TestCase):
    def test_open_and_recent_carriers_of_the_project(self):
        now = at(4, 20)
        recent = datetime.fromtimestamp(now - 300, timezone.utc)
        old = recent - timedelta(hours=2)
        rows = {
            "build": [{"id": 2608, "branch": "integration/paquet-h-20261019-0400",
                       "app": "ameesh"},
                      {"id": 2600, "branch": "integration/paquet-84-nexlink", "app": "nexlink"},
                      {"id": 2601, "branch": "lot/2601-x", "app": "ameesh"}],
            "intake": [{"id": 2606, "branch": "integration/sync-main-11m", "app": "ameesh"}],
            "delivered": [{"id": 2590, "branch": "integration/paquet-h-20261019-0300",
                        "app": "ameesh", "updated_at": recent},
                       {"id": 2580, "branch": "integration/paquet-h-20261019-0100",
                        "app": "ameesh", "updated_at": old}],
        }
        work = types.SimpleNamespace(
            items=lambda state, assignee, limit: rows.get(state, []),
            delays=lambda limit, ids: [{"work_item_id": 2590, "merged_ts": now - 300},
                                       {"work_item_id": 2580, "merged_ts": old.timestamp()}])
        with mock.patch("ameesh.storage.of", return_value=types.SimpleNamespace(work=work)):
            found = packet_watch.carriers(object(), "ameesh", now, 900, slot_at=at(4))
        self.assertEqual(found, {"open": [2608], "recent": [2590], "served": [2590]})


class CompositionBeforePush(unittest.TestCase):
    def test_unpublished_composition_silences_alert_in_each_open_state(self):
        for phase in ("intake", "build", "qa", "validated"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tmp:
                row = {"id": 3404, "title": "Paquet horaire h-11 — composition",
                       "branch": None, "app": "ameesh", "assignee": "impl-05",
                       "created_ts": at(4, 5)}
                store = types.SimpleNamespace(
                    items=lambda state, assignee, limit: [row] if state == phase else [])
                inventory, send = mock.Mock(), mock.Mock()
                cfg = cfg_with(tmp)
                with mock.patch("ameesh.storage.of", return_value=types.SimpleNamespace(work=store)):
                    result = packet_watch.check(
                        cfg, None, packet_watch.projects(cfg)["ameesh"], Hourly(),
                        now=at(4, 20), state={}, inventory=inventory, send=send)
                self.assertEqual(result["result"], "busy")
                self.assertEqual(result["carriers"]["open"], [3404])
                inventory.assert_not_called()
                send.assert_not_called()

    def test_title_only_carrier_is_bounded_to_its_slot_and_the_previous_one(self):
        # #3407 N2: at 04:2x the current slot is 04:00, the previous 03:00.
        # A title-only composition from an older slot (dead composer) or of
        # unknown age must not silence the watch for good.
        for created, expected in ((at(3), "busy"), (at(2, 59), "alerted"),
                                  (None, "alerted")):
            with self.subTest(created=created), tempfile.TemporaryDirectory() as tmp:
                row = {"id": 3404, "title": "Paquet horaire h-11 — composition",
                       "branch": None, "app": "ameesh", "assignee": "impl-05",
                       "created_ts": created}
                store = types.SimpleNamespace(
                    items=lambda state, assignee, limit: [row] if state == "build" else [])
                inventory, send = mock.Mock(return_value=payload()), mock.Mock()
                cfg = cfg_with(tmp)
                with mock.patch("ameesh.storage.of", return_value=types.SimpleNamespace(work=store)):
                    result = packet_watch.check(
                        cfg, None, packet_watch.projects(cfg)["ameesh"], Hourly(),
                        now=at(4, 20), state={}, inventory=inventory, send=send)
                self.assertEqual(result["result"], expected)
                if expected == "busy":
                    inventory.assert_not_called()
                    send.assert_not_called()
                else:
                    self.assertEqual(send.call_count, 2)

    def test_radar_profile_error_falls_back_to_target_and_still_alerts(self):
        # #3407 N3: an incompatible radar profile must not turn the watch into
        # result=error; the configured target is the fallback.
        inventory, send = mock.Mock(return_value=payload()), mock.Mock()
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("ameesh.merge_queue.declared_projects",
                           side_effect=merge_queue.QueueError("bad radar profile")), \
                mock.patch("ameesh.storage.of",
                           return_value=types.SimpleNamespace(
                               work=types.SimpleNamespace(
                                   items=lambda state, assignee, limit: []))):
            cfg = cfg_with(tmp)
            result = packet_watch.check(
                cfg, None, packet_watch.projects(cfg)["ameesh"], Hourly(),
                now=at(4, 20), state={}, inventory=inventory, send=send)
        self.assertEqual(result["result"], "alerted")
        self.assertIn("QueueError", result["config_error"])
        self.assertEqual(send.call_count, 2)

    def test_title_is_anchored_and_project_and_explicit_target_are_respected(self):
        row = {"title": "Paquet horaire h-11", "app": "ameesh"}
        self.assertTrue(packet_watch._carrier(row, "ameesh", "main", composing=True))
        self.assertFalse(packet_watch._carrier(row, "nexlink", "main", composing=True))
        self.assertFalse(packet_watch._carrier(dict(row, title="packet-watch : Paquet horaire"),
                                              "ameesh", "main", composing=True))
        # #3406 N1: a bug title must not silence every subsequent slot.
        self.assertFalse(packet_watch._carrier(
            dict(row, title="Paquet horaire : corriger une alerte"),
            "ameesh", "main", composing=True))
        for branch in (None, "integration/paquet-h-11"):
            other = dict(row, branch=branch, branch_target="origin/develop")
            self.assertFalse(packet_watch._carrier(other, "ameesh", "main", composing=True))
            self.assertTrue(packet_watch._carrier(other, "ameesh", "refs/heads/develop",
                                                 composing=True))
        # A title alone never proves a merge served a slot.
        self.assertFalse(packet_watch._carrier(row, "ameesh", "main"))

    def test_one_composer_and_delayed_fallback_even_when_alert_is_late(self):
        watch = packet_watch.ProjectWatch("ameesh", ("impl-01", "impl-05", "pilot"))
        text = packet_watch.body(watch, packet_watch.slot(Hourly(), at(4, 50), 900),
                                 payload(), now=at(4, 50))
        self.assertIn("Composeur désigné : impl-01", text)
        self.assertIn("Geste pour impl-01", text)
        deadline = datetime.fromtimestamp(at(5, 5), timezone.utc).isoformat()
        self.assertIn("après " + deadline, text)
        self.assertIn("sans réponse", text)
        self.assertIn("un seul remplaçant", text)


class ServedSlot(unittest.TestCase):
    def test_early_merge_silences_whole_slot_but_not_the_next(self):
        rows = {"delivered": [{"id": 2639, "branch": "integration/paquet-h-20261019-0600",
                            "app": "ameesh", "updated_ts": at(6, 20)}]}
        store = types.SimpleNamespace(
            items=lambda state, assignee, limit: rows.get(state, []),
            delays=lambda limit, ids: [{"work_item_id": 2639, "merged_ts": at(6, 20)}])
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("ameesh.storage.of", return_value=types.SimpleNamespace(work=store)):
            cfg = cfg_with(tmp)
            watch = packet_watch.projects(cfg)["ameesh"]
            inventory, send = mock.Mock(return_value=payload()), mock.Mock()
            for now in (at(6, 20), at(6, 35), at(6, 59)):
                # Empty state also covers starting the watcher after the merge.
                result = packet_watch.check(cfg, None, watch, Hourly(), now=now, state={},
                                            inventory=inventory, send=send)
                self.assertEqual(result["result"], "served")
            inventory.assert_not_called()
            send.assert_not_called()
            result = packet_watch.check(cfg, None, watch, Hourly(), now=at(7, 15), state={},
                                        inventory=inventory, send=send)
            self.assertEqual(result["result"], "alerted")
            self.assertEqual(send.call_count, 2)

    def test_only_an_effective_merge_in_the_slot_serves_it(self):
        for merged, expected in ((at(6), [2639]), (at(5, 59), []),
                                 (at(6, 37), []), (None, [])):
            with self.subTest(merged=merged):
                rows = {"delivered": [{"id": 2639, "branch": "integration/paquet-h-x",
                                    "app": "ameesh", "updated_ts": at(6, 20)}]}
                store = types.SimpleNamespace(
                    items=lambda state, assignee, limit: rows.get(state, []),
                    delays=lambda limit, ids: [{"work_item_id": 2639, "merged_ts": merged}])
                with mock.patch("ameesh.storage.of", return_value=types.SimpleNamespace(work=store)):
                    found = packet_watch.carriers(None, "ameesh", at(6, 36), 900,
                                                  slot_at=at(6))
                self.assertEqual(found["served"], expected)
                self.assertEqual(found["recent"], [])

    def test_open_carrier_disappearing_without_merge_still_alerts(self):
        rows = {"build": [{"id": 2639, "branch": "integration/paquet-h-x", "app": "ameesh"}]}
        store = types.SimpleNamespace(items=lambda state, assignee, limit: rows.get(state, []))
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("ameesh.storage.of", return_value=types.SimpleNamespace(work=store)):
            cfg = cfg_with(tmp)
            watch = packet_watch.projects(cfg)["ameesh"]
            send = mock.Mock()
            state = {}
            def check(now):
                return packet_watch.check(cfg, None, watch, Hourly(), now=now, state=state,
                                          inventory=lambda: payload(), send=send)
            self.assertEqual(check(at(6, 20))["result"], "busy")
            rows.clear()  # abandonment is not a served slot
            self.assertEqual(check(at(6, 36))["result"], "alerted")
            self.assertEqual(send.call_count, 2)


class Cli(unittest.TestCase):
    def test_tick_runs_the_watch_without_a_release_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = cfg_with(tmp)
            cfg.release = {}
            args = argparse.Namespace(release_command="tick", now=at(4, 20), json=False,
                                      project=None)
            db = mock.Mock()
            with mock.patch("ameesh.db.open_db", return_value=db), \
                    mock.patch.object(packet_watch, "check",
                                      return_value={"project": "ameesh",
                                                    "result": "alerted",
                                                    "slot": "04:00"}) as check, \
                    mock.patch("ameesh.release_cutoff.compiled_rules",
                               return_value=(Hourly(), None)), \
                    mock.patch("ameesh.packet_automerge.watch", return_value=[]):
                self.assertEqual(release.run(cfg, args), 0)
            self.assertEqual(check.call_count, 1)
            db.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
