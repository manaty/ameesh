# SPDX-License-Identifier: AGPL-3.0-only
"""#2814 : admission consciente du conteneur (`pressure_scope`), capacité
enfichable et état `queued` des tours différés.

Trois capacités demandées par TPK (issue manaty/ameesh#2110) :

* `own_cgroup` : lire la pression du cgroup v2 du processus (PSI, quota,
  throttling, mémoire) au lieu de la charge du nœud, avec
  `pressure_scope: own_cgroup | host` et un défaut `own_cgroup` dans un
  conteneur ; la charge 1 min est ignorée (au plus consultative) ;
* un fournisseur de capacité enfichable (no-op par défaut) qui émet
  « capacité nécessaire / libérée » sans lancer de tour ni contourner le
  budget, le bail ou l'epoch ;
* un état `queued` visible (raison, portée, âge, prochaine lecture), et aucune
  requête expirée ne démarre un tour.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

from ameesh import (capacity, canon, db as db_mod, mail, mesh_cli, platform,
                    registry, resources, runner, storage)

from . import test_canon
from .support import PgTestCase
from .test_l31_pressure import set_host_resources
GIB = 1024 ** 3


def limits(**declared):
    return resources.thresholds({"resources": declared})


def write_cgroup(root: str, *, cpu_psi: float = 0.0, mem_psi: float = 0.0,
                 usage_usec: int = 1_000_000, throttled_usec: int = 0,
                 current: int = 100 * 1024 ** 2, maximum: int | None = 1024 ** 3,
                 quota: str = "max") -> str:
    os.makedirs(root, exist_ok=True)

    def psi(path, some):
        with open(os.path.join(root, path), "w", encoding="utf-8") as fh:
            fh.write("some avg10=0.00 avg60=%.2f avg300=0.00 total=0\n"
                     "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n" % some)

    psi("cpu.pressure", cpu_psi)
    psi("memory.pressure", mem_psi)
    with open(os.path.join(root, "cpu.stat"), "w", encoding="utf-8") as fh:
        fh.write("usage_usec %d\nnr_periods 100\nnr_throttled 5\n"
                 "throttled_usec %d\n" % (usage_usec, throttled_usec))
    with open(os.path.join(root, "cpu.max"), "w", encoding="utf-8") as fh:
        fh.write("%s 100000\n" % quota)
    with open(os.path.join(root, "memory.current"), "w", encoding="utf-8") as fh:
        fh.write("%d\n" % current)
    with open(os.path.join(root, "memory.max"), "w", encoding="utf-8") as fh:
        fh.write("max\n" if maximum is None else "%d\n" % maximum)
    return root


class PlatformCgroupTest(unittest.TestCase):
    def test_own_cgroup_reads_the_v2_line(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as fh:
            fh.write("0::/kubepods/pod/container\n")
            path = fh.name
        try:
            self.assertEqual(platform.own_cgroup(path), "/kubepods/pod/container")
        finally:
            os.unlink(path)

    def test_cgroup_dir_resolves_the_path_or_the_root(self):
        self.assertEqual(platform.host.cgroup_dir(path="/x"), "/x")
        with tempfile.NamedTemporaryFile("w", delete=False) as fh:
            fh.write("0::/\n")
            path = fh.name
        try:
            with mock.patch.dict(os.environ, {"AMEESH_OWN_CGROUP_FILE": path,
                                              "AMEESH_CONTAINER": "0"}):
                self.assertEqual(platform.host.cgroup_dir(root="/sys/fs/cgroup"),
                                 "/sys/fs/cgroup")
                self.assertFalse(platform.host.in_container())
        finally:
            os.unlink(path)
        with mock.patch.dict(os.environ, {"AMEESH_CGROUP_DIR": "/fake/cgroup"}):
            self.assertEqual(platform.host.cgroup_dir(), "/fake/cgroup")

    def test_container_signals(self):
        with mock.patch.dict(os.environ, {"AMEESH_CONTAINER": "1"}):
            self.assertTrue(platform.host.in_container())
        with mock.patch.dict(os.environ, {"AMEESH_CONTAINER": "0"}):
            self.assertFalse(platform.host.in_container())
        with mock.patch.dict(os.environ, {"KUBERNETES_SERVICE_HOST": "10.0.0.1"}):
            self.assertTrue(platform.host.in_container())
        with tempfile.NamedTemporaryFile("w", delete=False) as fh:
            fh.write("0::/kubepods/besteffort/pod/abc\n")
            path = fh.name
        try:
            with mock.patch.dict(os.environ, {"AMEESH_OWN_CGROUP_FILE": path,
                                              "AMEESH_CONTAINER": ""}):
                # the explicit empty override is false, but the path still names
                # a runtime: the explicit answer wins
                self.assertFalse(platform.host.in_container())
        finally:
            os.unlink(path)

    def test_cpu_max_and_memory_max(self):
        with tempfile.TemporaryDirectory() as root:
            write_cgroup(root, maximum=512 * 1024 ** 2, quota="50000")
            self.assertEqual(platform.cgroup_cpu_max(root), (50000.0, 100000.0))
            self.assertEqual(platform.cgroup_memory_max(root), 512 * 1024 ** 2)
            write_cgroup(root, maximum=None, quota="max")
            self.assertEqual(platform.cgroup_cpu_max(root), (None, 100000.0))
            self.assertIsNone(platform.cgroup_memory_max(root))
            self.assertEqual(platform.cgroup_cpu_stat(root)["nr_throttled"], 5)


class CgroupMeterTest(unittest.TestCase):
    def test_first_reading_has_no_rate_then_the_delta_gives_one(self):
        clock = [100.0]
        with tempfile.TemporaryDirectory() as root:
            write_cgroup(root, cpu_psi=12.0, mem_psi=3.0, current=900 * 1024 ** 2,
                         maximum=1024 ** 3, quota="200000", usage_usec=1_000_000,
                         throttled_usec=100_000)
            meter = resources.CgroupMeter(root, clock=lambda: clock[0])
            first = meter.read()
            self.assertEqual(first["path"], root)
            self.assertEqual(first["cpu_some"], 12.0)
            self.assertEqual(first["mem_some"], 3.0)
            self.assertEqual(first["cpu_quota"], 2.0)
            self.assertEqual(first["mem_used"], round(900 / 1024, 4))
            self.assertIsNone(first["throttled"])
            self.assertIsNone(first["cpu_used"])
            clock[0] = 110.0
            # 2 s of throttling over 10 s of wall time; 5 CPU-seconds over 10 s
            write_cgroup(root, cpu_psi=12.0, mem_psi=3.0, current=900 * 1024 ** 2,
                         maximum=1024 ** 3, quota="200000", usage_usec=6_000_000,
                         throttled_usec=2_100_000)
            second = meter.read()
            self.assertEqual(second["throttled"], 0.2)
            self.assertEqual(second["cpu_used"], 0.5)

    def test_unreadable_max_leaves_memory_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            write_cgroup(root, maximum=None, current=123)
            out = resources.CgroupMeter(root).read()
            self.assertIsNone(out["mem_used"])
            self.assertEqual(out["mem_current_bytes"], 123)

    def test_missing_directory_is_no_guess(self):
        out = resources.CgroupMeter("/chemin/qui/n/existe/pas").read()
        self.assertIsNone(out["cpu_some"])
        self.assertIsNone(out["mem_used"])


class ScopeThresholdsTest(unittest.TestCase):
    def test_a_declared_scope_wins(self):
        self.assertEqual(limits(pressure_scope="own_cgroup")["pressure_scope"],
                         "own_cgroup")
        self.assertEqual(limits(pressure_scope="host")["pressure_scope"], "host")
        # an unknown value is not a scope: the environment default applies
        self.assertNotEqual(limits(pressure_scope="container")["pressure_scope"],
                            "container")

    def test_the_environment_override_is_read(self):
        with mock.patch.dict(os.environ, {"AMEESH_PRESSURE_SCOPE": "own_cgroup"}):
            self.assertEqual(resources.thresholds(None)["pressure_scope"], "own_cgroup")

    def test_default_is_own_cgroup_only_in_a_bounded_container(self):
        # #2825 N2 : un conteneur sans cgroup v2 lisible garde les gardes de
        # l'hôte au lieu de perdre CPU/mémoire en silence.
        with tempfile.TemporaryDirectory() as root:
            write_cgroup(root, maximum=1024 ** 3)
            with mock.patch.dict(os.environ, {"AMEESH_CONTAINER": "1",
                                              "AMEESH_CGROUP_DIR": root}):
                self.assertEqual(resources.thresholds(None)["pressure_scope"],
                                 "own_cgroup")
        with tempfile.TemporaryDirectory() as root:  # no cpu.max/memory.max
            with mock.patch.dict(os.environ, {"AMEESH_CONTAINER": "1",
                                              "AMEESH_CGROUP_DIR": root}):
                self.assertEqual(resources.thresholds(None)["pressure_scope"], "host")
        with mock.patch.dict(os.environ, {"AMEESH_CONTAINER": "0"}):
            self.assertEqual(resources.thresholds(None)["pressure_scope"], "host")

    def test_host_limits_never_read_the_callers_container(self):
        # #2825 N4 : `ameesh hosts` sur un autre hôte ne déduit pas son
        # périmètre du processus qui interroge.
        with tempfile.TemporaryDirectory() as root:
            write_cgroup(root, maximum=1024 ** 3)
            with mock.patch.dict(os.environ, {"AMEESH_CONTAINER": "1",
                                              "AMEESH_CGROUP_DIR": root}):
                got = resources.host_limits([], "ailleurs")
        self.assertEqual(got["limits"]["pressure_scope"], "host")

    def test_a_reading_sets_the_default_scope_of_its_host(self):
        # #2825 N4 : le défaut vient de la lecture de l'hôte nommé, et une
        # lecture ancienne (sans les clés) reste prudente.
        own = {"cgroup": {"container": True, "bounded": True, "mem_used": 0.2}}
        self.assertEqual(resources.host_limits([], "atelier", reading=own)
                         ["limits"]["pressure_scope"], "own_cgroup")
        blind = {"cgroup": {"container": True, "bounded": False}}
        self.assertEqual(resources.host_limits([], "atelier", reading=blind)
                         ["limits"]["pressure_scope"], "host")
        old = {"cgroup": {"mem_used": 0.5}}
        self.assertEqual(resources.host_limits([], "atelier", reading=old)
                         ["limits"]["pressure_scope"], "host")

    def test_an_own_cgroup_fiche_without_measure_falls_back_with_a_note(self):
        # #2825 N2 : même demandé par une fiche, `own_cgroup` sans aucune
        # mesure cgroup revient à `host` et le dit, au lieu de perdre les
        # gardes en silence.
        class Fiche:
            policy = type("P", (), {"resources": {"pressure_scope": "own_cgroup"},
                                    "max_agents": None})()

        class Canon:
            id = "a"

            def host(self, name):
                return Fiche()

        got = resources.host_limits([Canon()], "atelier",
                                    reading={"cgroup": {}})
        self.assertEqual(got["limits"]["pressure_scope"], "host")
        self.assertIn("no cgroup v2 measure", got["scope_fallback"])

    def test_cgroup_ceilings_have_prudent_defaults(self):
        self.assertEqual(limits()["max_cgroup_memory_used"],
                         resources.DEFAULT_MAX_CGROUP_MEMORY_USED)
        self.assertEqual(limits()["max_cgroup_throttled"],
                         resources.DEFAULT_MAX_CGROUP_THROTTLED)
        self.assertEqual(limits(max_cgroup_memory_used="80%")["max_cgroup_memory_used"],
                         0.8)
        self.assertEqual(limits(max_cgroup_throttled="25%")["max_cgroup_throttled"], 0.25)

    def test_an_own_cgroup_fiche_wins_over_a_host_fiche(self):
        class Fiche:
            def __init__(self, resources):
                self.policy = type("P", (), {"resources": resources, "max_agents": None})()

        class Canon:
            def __init__(self, ident, scope):
                self.id = ident
                self._fiche = Fiche({"pressure_scope": scope})

            def host(self, name):
                return self._fiche

        got = resources.host_limits([Canon("a", "host"), Canon("b", "own_cgroup")],
                                    "atelier")
        self.assertEqual(got["limits"]["pressure_scope"], "own_cgroup")
        self.assertEqual(got["origin"]["pressure_scope"], "b")


class BreachesScopeTest(unittest.TestCase):
    def test_host_scope_still_uses_the_node_load(self):
        found = resources.breaches({"load1": 100.0, "psi_mem_some": 0.0},
                                   limits=limits(pressure_scope="host", max_load=4))
        self.assertEqual([b["key"] for b in found], ["max_load"])

    def test_own_cgroup_admits_a_loaded_node_when_the_container_is_idle(self):
        # TPK, 19/10 : charge du nœud 19,6, conteneur à 377m CPU
        verdict = resources.pressure(
            {"load1": 19.6, "psi_mem_some": 0.0, "mem_available_bytes": 8 * GIB,
             "cgroup_cpu_some": 0.5, "cgroup_mem_some": 0.1,
             "cgroup_mem_used": 0.2, "cgroup_throttled": 0.0},
            limits=limits(pressure_scope="own_cgroup", max_load=8, max_mem_pressure="10%"))
        self.assertFalse(verdict["blocked"])
        self.assertEqual(verdict["scope"], "own_cgroup")

    def test_own_cgroup_uses_the_cgroup_psi_not_the_node_psi(self):
        verdict = resources.pressure(
            {"psi_cpu_some": 90.0, "cgroup_cpu_some": 1.0},
            limits=limits(pressure_scope="own_cgroup", max_cpu_pressure="20%"))
        self.assertFalse(verdict["blocked"])
        verdict = resources.pressure(
            {"psi_cpu_some": 1.0, "cgroup_cpu_some": 40.0},
            limits=limits(pressure_scope="own_cgroup", max_cpu_pressure="20%"))
        self.assertEqual([b["key"] for b in verdict["breaches"]],
                         ["max_cpu_pressure"])
        self.assertIn("own cgroup", verdict["breaches"][0]["label"])

    def test_cgroup_memory_and_throttling_close_the_gate(self):
        verdict = resources.pressure(
            {"cgroup_mem_used": 0.95, "cgroup_throttled": 0.75},
            limits=limits(pressure_scope="own_cgroup"))
        keys = [b["key"] for b in verdict["breaches"]]
        self.assertIn("max_cgroup_memory_used", keys)
        self.assertIn("max_cgroup_throttled", keys)
        self.assertTrue(verdict["blocked"])
        # thresholds: 90 % and 50 %; critical halfway to full
        self.assertTrue({b["key"]: b["critical"] for b in verdict["breaches"]}
                        ["max_cgroup_memory_used"])

    def test_cgroup_measures_are_ignored_in_host_scope(self):
        verdict = resources.pressure(
            {"cgroup_mem_used": 0.99, "cgroup_throttled": 0.99,
             "psi_mem_some": 0.0, "psi_cpu_some": 0.0},
            limits=limits(pressure_scope="host"))
        self.assertFalse(verdict["blocked"])

    def test_own_cgroup_without_any_cgroup_measure_falls_back_to_host(self):
        # #2825 N2 : sans aucune mesure cgroup, la charge du nœud n'est plus
        # écartée en silence : le périmètre `host` garde ses seuils, et le
        # repli est dit dans le verdict.
        verdict = resources.pressure(
            {"load1": 100.0},
            limits=limits(pressure_scope="own_cgroup", max_load=1))
        self.assertTrue(verdict["blocked"])
        self.assertEqual(verdict["scope"], "host")
        self.assertIn("no cgroup v2 measure", verdict["scope_fallback"])
        self.assertEqual([b["key"] for b in verdict["breaches"]], ["max_load"])

    def test_a_host_swap_stock_does_not_block_an_own_cgroup_host(self):
        # the container's /proc/meminfo shows the NODE's swap; without the
        # node's PSI it must not apply the 0028 fallback (a stock is not a
        # pressure) — the cgroup's memory.max is the real guard. The cgroup
        # must be measured, otherwise #2825 N2 keeps the host guards.
        reading = {"swap_used_bytes": 20 * GIB, "load1": 50.0,
                   "cgroup_mem_used": 0.2}
        self.assertEqual(resources.breaches(
            reading, limits(pressure_scope="own_cgroup")), [])
        # a declared swap ceiling still counts, corroborated by the cgroup's
        # own memory pressure
        found = resources.breaches(
            dict(reading, cgroup_mem_some=40.0),
            limits(pressure_scope="own_cgroup", max_swap_used="16GiB",
                   max_mem_pressure="50%"))
        self.assertEqual([b["key"] for b in found], ["max_swap_used"])


class QueueStatusTest(unittest.TestCase):
    def test_waiting_text_round_trips(self):
        verdict = resources.pressure(
            {"cgroup_mem_used": 0.95}, limits=limits(pressure_scope="own_cgroup"))
        text = "waiting: " + resources.queue_text("atelier", verdict, queued_s=42,
                                                  capacity="requested")
        queue = resources.parse_queue_status(text)
        self.assertEqual(queue["state"], "queued")
        self.assertEqual(queue["reason"], "pressure")
        self.assertEqual(queue["scope"], "own_cgroup")
        self.assertEqual(queue["queued_s"], 42)
        self.assertEqual(queue["retry_s"], resources.QUEUE_RETRY_S)
        self.assertEqual(queue["capacity"], "requested")
        self.assertIn("cgroup memory used", queue["detail"])

    def test_critical_text_and_host_scope(self):
        verdict = resources.pressure({"load1": 100.0},
                                     limits=limits(pressure_scope="host", max_load=1))
        text = "paused: critical " + resources.queue_text("atelier", verdict)
        queue = resources.parse_queue_status(text)
        self.assertTrue(queue["critical"])
        self.assertEqual(queue["scope"], "host")
        self.assertIn("1-min load", queue["detail"])

    def test_any_other_status_is_not_queued(self):
        for text in (None, "", "running: tool", "paused: budget"):
            with self.subTest(text=text):
                self.assertIsNone(resources.parse_queue_status(text))


# ==========================================================================
# fournisseur de capacité
# ==========================================================================

class _Provider:
    is_noop = False

    def __init__(self):
        self.events = []
        self.replies = []
        self.release_replies = []
        self.need_gate: threading.Event | None = None
        self.release_gate: threading.Event | None = None

    def need(self, event):
        self.events.append(event)
        if self.need_gate is not None:
            self.need_gate.wait(2.0)
        return self.replies.pop(0) if self.replies else {"action": "granted",
                                                         "capacity_id": "c1"}

    def release(self, event):
        self.events.append(event)
        if self.release_gate is not None:
            self.release_gate.wait(2.0)
        return self.release_replies.pop(0) if self.release_replies \
            else {"action": "none"}


class CapacityProviderTest(unittest.TestCase):
    def test_no_provider_is_the_noop_and_never_signals(self):
        provider = capacity.load_provider("", log=None)
        self.assertTrue(provider.is_noop)
        manager = capacity.Manager(provider, now=lambda: 100.0)
        self.assertFalse(manager.enabled)
        self.assertIsNone(manager.need(host="h", agent="a"))

    def test_command_provider_reads_the_last_json_line(self):
        with tempfile.TemporaryDirectory() as root:
            script = os.path.join(root, "scaler.py")
            log = os.path.join(root, "events.jsonl")
            with open(script, "w", encoding="utf-8") as fh:
                fh.write(
                    "import json, sys\n"
                    "event = json.load(sys.stdin)\n"
                    "with open(%r, 'a', encoding='utf-8') as out:\n"
                    "    out.write(json.dumps(event, sort_keys=True) + '\\n')\n"
                    "print('a log line')\n"
                    "print(json.dumps({'action': 'granted', 'capacity_id': 'pod-1'}))\n"
                    % log)
            provider = capacity.CommandProvider([sys.executable, script])
            reply = provider.need({"schema": capacity.SCHEMA, "host": "h",
                                   "agent": "a"})
            self.assertEqual(reply["action"], "granted")
            with open(log, encoding="utf-8") as fh:
                seen = json.loads(fh.readline())
            self.assertEqual(seen["event"], "need")
            self.assertEqual(seen["host"], "h")

    def test_a_broken_command_is_an_error_never_an_exception(self):
        provider = capacity.CommandProvider(["/chemin/qui/n/existe/pas"])
        self.assertEqual(provider.need({"event": "need"})["action"], "error")

    def test_manager_bounds_and_releases_after_calm(self):
        provider = _Provider()
        clock = [100.0]
        manager = capacity.Manager(provider, now=lambda: clock[0],
                                   min_interval_s=15, needs_per_hour=2,
                                   release_after_s=60)
        state = manager.need(host="h", agent="a", verdict={"scope": "own_cgroup"},
                             queued_s=7)
        self.assertEqual(state["action"], "granted")
        self.assertEqual(len(provider.events), 1)
        event = provider.events[0]
        self.assertEqual(event["schema"], capacity.SCHEMA)
        self.assertEqual(event["scope"], "own_cgroup")
        self.assertEqual(event["queued_s"], 7)
        # one outstanding request: no second event
        self.assertIsNotNone(manager.need(host="h", agent="b"))
        self.assertEqual(len(provider.events), 1)
        # calm starts at the first unblocked fresh verdict, release at +60
        self.assertIsNone(manager.observe(True))
        self.assertIsNone(manager.observe(False))
        clock[0] = 130.0
        self.assertIsNone(manager.observe(False))
        clock[0] = 161.0
        released = manager.observe(False)
        self.assertEqual(released["state"], "released")
        self.assertEqual(provider.events[-1]["event"], "release")
        self.assertEqual(provider.events[-1]["capacity_id"], "c1")

    def test_manager_never_exceeds_the_hourly_bound(self):
        provider = _Provider()
        clock = [100.0]
        manager = capacity.Manager(provider, now=lambda: clock[0],
                                   min_interval_s=0, needs_per_hour=1,
                                   release_after_s=0)
        self.assertIsNotNone(manager.need(host="h", agent="a"))
        clock[0] = 200.0
        manager.observe(False)
        manager.observe(False)  # release_after_s = 0: the calm is immediate
        clock[0] = 300.0
        self.assertIsNone(manager.need(host="h", agent="b"))

    def test_two_threads_share_one_outstanding_request(self):
        # B1 de la relecture Codex : le slot est réservé AVANT l'appel au
        # fournisseur ; deux workers ne lancent pas deux demandes.
        provider = _Provider()
        provider.need_gate = threading.Event()
        manager = capacity.Manager(provider, min_interval_s=0, needs_per_hour=5)
        done = []

        def first():
            done.append(manager.need(host="h", agent="a"))

        thread = threading.Thread(target=first)
        thread.start()
        deadline = time.time() + 2.0
        while not provider.events and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(provider.events), 1)
        second = manager.need(host="h", agent="b")
        self.assertEqual(second["state"], "requesting")
        self.assertEqual(len(provider.events), 1)
        provider.need_gate.set()
        thread.join(3.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(done[0]["state"], "requested")
        self.assertEqual(manager.snapshot()["state"], "requested")

    def test_a_release_in_flight_is_never_started_twice(self):
        # B1 (seconde sonde) : deux observe concurrents ne lancent pas deux
        # release ; le second ne doit ni effacer ni remplacer le slot.
        provider = _Provider()
        provider.release_gate = threading.Event()
        manager = capacity.Manager(provider, min_interval_s=0,
                                   needs_per_hour=5, release_after_s=0)
        self.assertEqual(manager.need(host="h", agent="a")["capacity_id"], "c1")
        clock = [101.0]
        manager._clock = lambda: clock[0]
        manager.observe(False)  # the calm starts
        clock[0] = 102.0
        done = []
        thread = threading.Thread(target=lambda: done.append(manager.observe(False)))
        thread.start()
        deadline = time.time() + 2.0
        while len(provider.events) < 2 and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual([e["event"] for e in provider.events], ["need", "release"])
        # a second fresh verdict while the RPC is in flight: no second call
        self.assertIsNone(manager.observe(False))
        self.assertEqual(len(provider.events), 2)
        provider.release_gate.set()
        thread.join(3.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(done[0]["state"], "released")
        self.assertIsNone(manager.snapshot())

    def test_a_release_wait_is_not_a_confirmation(self):
        # B2 (contrat de réponse) : wait/refused/objet vide ne libèrent pas.
        provider = _Provider()
        provider.release_replies = [{"action": "wait"},
                                    {"action": "none"}]
        clock = [100.0]
        manager = capacity.Manager(provider, now=lambda: clock[0],
                                   min_interval_s=0, needs_per_hour=5,
                                   release_after_s=0)
        manager.need(host="h", agent="a")
        clock[0] = 101.0
        manager.observe(False)
        clock[0] = 102.0
        failed = manager.observe(False)
        self.assertEqual(failed["state"], "release_failed")
        self.assertEqual(failed["capacity_id"], "c1")
        clock[0] = 110.0
        self.assertEqual(manager.need(host="h", agent="b")["state"], "release_failed")
        clock[0] = 133.0
        self.assertEqual(manager.observe(False)["state"], "released")

    def test_a_failed_release_keeps_the_slot_until_confirmed(self):
        # B2 de la relecture Codex : une libération en échec ne rouvre pas une
        # seconde allocation ; le même capacity_id est représenté.
        provider = _Provider()
        provider.release_replies = [{"action": "error", "error": "boom"},
                                    {"action": "none"}]
        clock = [100.0]
        manager = capacity.Manager(provider, now=lambda: clock[0],
                                   min_interval_s=0, needs_per_hour=5,
                                   release_after_s=0)
        self.assertEqual(manager.need(host="h", agent="a")["capacity_id"], "c1")
        clock[0] = 101.0
        manager.observe(False)          # the calm starts
        clock[0] = 102.0
        failed = manager.observe(False)  # release attempt 1 -> error
        self.assertEqual(failed["state"], "release_failed")
        self.assertEqual(failed["capacity_id"], "c1")
        # a new need returns the failed slot, it never calls need again
        clock[0] = 110.0
        self.assertEqual(manager.need(host="h", agent="b")["state"], "release_failed")
        self.assertEqual([e["event"] for e in provider.events],
                         ["need", "release"])
        # before the retry delay: no call
        clock[0] = 120.0
        self.assertIsNone(manager.observe(False))
        self.assertEqual(len(provider.events), 2)
        # retry with the same id, then the slot is free
        clock[0] = 140.0
        released = manager.observe(False)
        self.assertEqual(released["state"], "released")
        self.assertEqual(provider.events[-1]["capacity_id"], "c1")
        self.assertIsNone(manager.snapshot())
        clock[0] = 150.0
        self.assertEqual(manager.need(host="h", agent="c")["state"], "requested")
        self.assertEqual([e["event"] for e in provider.events],
                         ["need", "release", "release", "need"])

    def test_a_refused_or_failed_need_never_holds_the_slot(self):
        # #2825 N1 : un refus ou une erreur n'accorde rien ; le slot
        # `requested` est rendu (une fenêtre ultérieure peut redemander) et
        # aucun `release` n'est envoyé.
        provider = _Provider()
        provider.replies = [{"action": "refused", "detail": "no quota"},
                            {"action": "error", "error": "boom"},
                            {"action": "granted", "capacity_id": "c9"}]
        clock = [100.0]
        manager = capacity.Manager(provider, now=lambda: clock[0],
                                   min_interval_s=15, needs_per_hour=5,
                                   release_after_s=0)
        refused = manager.need(host="h", agent="a")
        self.assertEqual(refused["action"], "refused")
        self.assertEqual(refused["capacity_id"], None)
        self.assertIsNone(manager.snapshot())
        # the min interval still bounds the next attempt
        self.assertIsNone(manager.need(host="h", agent="a"))
        self.assertEqual(len(provider.events), 1)
        clock[0] = 116.0
        failed = manager.need(host="h", agent="a")
        self.assertEqual(failed["action"], "error")
        self.assertIsNone(manager.snapshot())
        # nothing was granted: a calm pressure releases nothing
        self.assertIsNone(manager.observe(False))
        self.assertEqual([e["event"] for e in provider.events], ["need", "need"])
        clock[0] = 132.0
        granted = manager.need(host="h", agent="a")
        self.assertEqual(granted["capacity_id"], "c9")
        self.assertEqual(manager.snapshot()["state"], "requested")

    def test_a_granted_without_capacity_id_is_not_a_releasable_slot(self):
        # #2825 N1 : un `granted` sans identifiant n'est pas une capacité
        # utilisable ; on ne libère jamais `capacity_id: None`.
        provider = _Provider()
        provider.replies = [{"action": "granted"}]
        clock = [100.0]
        manager = capacity.Manager(provider, now=lambda: clock[0],
                                   min_interval_s=0, needs_per_hour=5,
                                   release_after_s=0)
        state = manager.need(host="h", agent="a")
        self.assertEqual(state["action"], "error")
        self.assertIn("capacity_id", state["detail"])
        self.assertIsNone(manager.snapshot())
        self.assertIsNone(manager.observe(False))
        self.assertEqual([e["event"] for e in provider.events], ["need"])

    def test_a_wait_without_capacity_id_is_dropped_without_a_release(self):
        # #2825 N1 : `wait` n'accorde encore rien. Au calme, le slot est rendu
        # sans appeler le fournisseur (aucun identifiant à libérer).
        provider = _Provider()
        provider.replies = [{"action": "wait"}]
        clock = [100.0]
        manager = capacity.Manager(provider, now=lambda: clock[0],
                                   min_interval_s=0, needs_per_hour=5,
                                   release_after_s=0)
        state = manager.need(host="h", agent="a")
        self.assertEqual(state["action"], "wait")
        self.assertEqual(manager.snapshot()["state"], "requested")
        clock[0] = 101.0
        self.assertIsNone(manager.observe(False))  # the calm starts
        clock[0] = 102.0
        skipped = manager.observe(False)
        self.assertEqual(skipped["state"], "idle")
        self.assertEqual([e["event"] for e in provider.events], ["need"])
        self.assertIsNone(manager.snapshot())
        # the slot is free: the next window may ask again
        clock[0] = 110.0
        self.assertEqual(manager.need(host="h", agent="b")["action"], "granted")

    def test_a_slow_module_need_is_bounded(self):
        # #2825 N3 : un fournisseur `module:` qui pend ne retient pas le
        # worker ; son appel est borné comme celui de `cmd:`.
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "slow_capacity_need.py")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(
                    "import time\n"
                    "class SlowNeed:\n"
                    "    is_noop = False\n"
                    "    def need(self, event):\n"
                    "        time.sleep(3.0)\n"
                    "        return {'action': 'granted', 'capacity_id': 'late'}\n"
                    "    def release(self, event):\n"
                    "        return {'action': 'none'}\n")
            sys.path.insert(0, root)
            try:
                with mock.patch.dict(os.environ,
                                     {"AMEESH_CAPACITY_PROVIDER_TIMEOUT": "0.2"}):
                    provider = capacity.load_provider(
                        "slow_capacity_need:SlowNeed")
                    self.assertIsInstance(provider, capacity.ModuleProvider)
                    manager = capacity.Manager(provider, min_interval_s=0,
                                               needs_per_hour=5)
                    started = time.monotonic()
                    state = manager.need(host="h", agent="a")
                self.assertLess(time.monotonic() - started, 1.5)
                self.assertEqual(state["action"], "error")
                self.assertEqual(state["detail"], "timeout")
            finally:
                sys.path.remove(root)
                sys.modules.pop("slow_capacity_need", None)

    def test_a_slow_module_release_is_bounded_and_not_left_releasing(self):
        # #2825 N3 : un release qui pend ne laisse pas l'état `releasing` à
        # vie ; il repasse en `release_failed` (retenté avec le même id).
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "slow_capacity_release.py")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(
                    "import time\n"
                    "class SlowRelease:\n"
                    "    is_noop = False\n"
                    "    def need(self, event):\n"
                    "        return {'action': 'granted', 'capacity_id': 'c1'}\n"
                    "    def release(self, event):\n"
                    "        time.sleep(3.0)\n"
                    "        return {'action': 'none'}\n")
            sys.path.insert(0, root)
            try:
                with mock.patch.dict(os.environ,
                                     {"AMEESH_CAPACITY_PROVIDER_TIMEOUT": "0.2"}):
                    provider = capacity.load_provider(
                        "slow_capacity_release:SlowRelease")
                    manager = capacity.Manager(provider, min_interval_s=0,
                                               needs_per_hour=5,
                                               release_after_s=0)
                    manager.need(host="h", agent="a")
                    manager.observe(False)  # the calm starts
                    started = time.monotonic()
                    failed = manager.observe(False)
                self.assertLess(time.monotonic() - started, 1.5)
                self.assertEqual(failed["state"], "release_failed")
                self.assertEqual(failed["capacity_id"], "c1")
                self.assertEqual(failed["detail"], "timeout")
            finally:
                sys.path.remove(root)
                sys.modules.pop("slow_capacity_release", None)


class MailExpiryTest(unittest.TestCase):
    def test_a_signature_ttl_is_not_a_request_deadline(self):
        now = time.time()
        row = {"signature_expires_ts": now - 1, "payload": {}}
        self.assertIsNone(mail.request_deadline(row))
        self.assertFalse(mail.request_expired(row))
        # even a signed message past its signature TTL is not a request
        # deadline: an owner order must not disappear because its signature
        # has aged
        self.assertFalse(mail.request_expired(
            {"signature_expires_ts": 0, "payload": None}))

    def test_only_an_explicit_request_deadline_expires(self):
        now = time.time()
        self.assertTrue(mail.request_expired(
            {"payload": {"request_expires_ts": now - 1}}))
        self.assertFalse(mail.request_expired(
            {"payload": {"request_expires_ts": now + 60}}))
        for value in (None, 0, -1, True, "soon", float("nan")):
            with self.subTest(value=value):
                self.assertIsNone(mail.request_deadline(
                    {"payload": {"request_expires_ts": value}}))
                self.assertFalse(mail.request_expired(
                    {"payload": {"request_expires_ts": value}}))


class MailExpiryDbTest(PgTestCase):
    def test_drop_expired_settles_only_explicit_request_deadlines(self):
        now = time.time()
        expired = storage.of(self.db).mailbox.send(
            "orchestre", "relecteur", "requête expirée", host="atelier",
            kind="request",
            payload={"request_expires_ts": int(now - 60)},
            work_item_id=None, signature=None, signature_key=None,
            signed_payload=None, nonce=None, created_us=None, expires_us=None)
        live = storage.of(self.db).mailbox.send(
            "orchestre", "relecteur", "requête vivante", host="atelier",
            kind="request",
            payload={"request_expires_ts": int(now + 60)},
            work_item_id=None, signature=None, signature_key=None,
            signed_payload=None, nonce=None, created_us=None, expires_us=None)
        # a signed message past its signature TTL but with no request deadline
        # is NOT expired: it stays deliverable
        signed_old = storage.of(self.db).mailbox.send(
            "orchestre", "relecteur", "ordre signé ancien", host="atelier",
            kind="request", payload={}, work_item_id=None, signature="s",
            signature_key="k", signed_payload="{}", nonce="n3",
            created_us=None, expires_us=int((now - 60) * 1_000_000))
        unread = mail.unread(self.db, "relecteur")
        self.assertEqual([row["id"] for row in unread],
                         [expired["id"], live["id"], signed_old["id"]])
        self.assertEqual(mail.drop_expired(self.db, unread), 1)
        self.assertEqual([row["id"] for row in mail.unread(self.db, "relecteur")],
                         [live["id"], signed_old["id"]])


class QueueJsonDbTest(PgTestCase):
    def test_agent_detail_carries_the_queue_object(self):
        verdict = resources.pressure(
            {"cgroup_mem_used": 0.95}, limits=limits(pressure_scope="own_cgroup"))
        text = "waiting: " + resources.queue_text("atelier", verdict, queued_s=42)
        registry.upsert(self.db, "relecteur", harness="deepseek", host="atelier",
                        status="queued", status_text=text)
        row = mesh_cli.agent_detail(self.db, "relecteur")
        self.assertEqual(row["queue"]["state"], "queued")
        self.assertEqual(row["queue"]["scope"], "own_cgroup")
        self.assertEqual(row["queue"]["queued_s"], 42)
        # an ordinary status has no queue object, not an empty one
        registry.set_status(self.db, "relecteur", "queued", "reprise : même session")
        self.assertIsNone(mesh_cli.agent_detail(self.db, "relecteur")["queue"])


# ==========================================================================
# canon et exécuteur (base réelle)
# ==========================================================================

class CanonScopeTest(test_canon._TmpMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.root = self.example_copy()

    def load(self):
        return canon.load(self.root, untrusted=True)

    def codes(self, loaded, severity=None):
        return [f.code for f in canon.validate(loaded)
                if severity is None or f.severity == severity]

    def test_scope_and_cgroup_ceilings_are_read(self):
        set_host_resources(self.root, pressure_scope="own_cgroup",
                           max_cgroup_memory_used="80%",
                           max_cgroup_throttled="25%")
        loaded = self.load()
        policy_resources = loaded.host("atelier").policy.resources
        self.assertEqual(policy_resources["pressure_scope"], "own_cgroup")
        self.assertEqual(policy_resources["max_cgroup_memory_used"], 0.8)
        self.assertEqual(policy_resources["max_cgroup_throttled"], 0.25)
        self.assertNotIn("host-policy-invalid", self.codes(loaded))

    def test_an_unknown_scope_is_an_error(self):
        set_host_resources(self.root, pressure_scope="container")
        self.assertIn("host-policy-invalid", self.codes(self.load(), canon.ERROR))

    def test_an_invalid_fraction_is_an_error(self):
        set_host_resources(self.root, max_cgroup_memory_used="150%")
        self.assertIn("host-policy-invalid", self.codes(self.load(), canon.ERROR))


class HostCgroupStorageTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM host_resources")

    def test_record_keeps_the_cgroup_measure(self):
        hosts = storage.of(self.db).hosts
        hosts.record({"host": "vm", "mem_available_bytes": 8 * GIB,
                      "cgroup": {"path": "/kubepods/pod", "mem_used": 0.81,
                                 "throttled": 0.12}})
        latest = hosts.latest("vm")
        self.assertEqual(latest["cgroup"]["path"], "/kubepods/pod")
        self.assertEqual(latest["cgroup"]["mem_used"], 0.81)
        # a reading recorded before 0469 has no cgroup: {} not None
        hosts.record({"host": "old", "mem_available_bytes": 8 * GIB})
        self.assertEqual(hosts.latest("old")["cgroup"], {})

    def test_the_published_measure_drives_an_own_cgroup_verdict(self):
        hosts = storage.of(self.db).hosts
        hosts.record({"host": "vm", "mem_available_bytes": 8 * GIB,
                      "load1": 50.0, "psi_mem_some": 0.0,
                      "cgroup": {"mem_used": 0.95, "throttled": 0.0}})
        latest = hosts.latest("vm")
        verdict = resources.pressure(
            latest, limits=resources.thresholds(
                {"resources": {"pressure_scope": "own_cgroup"}}))
        self.assertTrue(verdict["blocked"])
        self.assertEqual([b["key"] for b in verdict["breaches"]],
                         ["max_cgroup_memory_used"])


class RunnerCgroupDbTest(test_canon._CanonDbCase):
    """Le verdict de l'exécuteur lit le cgroup de son processus (mesure
    vivante), pas seulement le dernier relevé du nœud."""

    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM host_resources")
        self.db.execute("DELETE FROM turn_resources")
        self.cgroup = write_cgroup(os.path.join(self.tmp, "cgroup"),
                                   current=970 * 1024 ** 2, maximum=1024 ** 3)

    def _reading(self, load1: float) -> None:
        storage.of(self.db).hosts.record({
            "host": "atelier", "mem_available_bytes": 8 * GIB,
            "swap_used_bytes": 0, "load1": load1, "cpu_count": 8,
            "psi_mem_some": 0.0, "psi_cpu_some": 0.0,
            "disk_free_bytes": 100 * GIB, "disk_path": "/tmp",
            "turns_in_progress": 0})

    def test_a_loaded_node_with_a_full_cgroup_container_is_held(self):
        set_host_resources(self.root, pressure_scope="own_cgroup",
                           max_cgroup_memory_used="90%")
        self.sync()
        self._reading(load1=50.0)
        registry.set_pending_prompt(self.db, "orchestre", "consigne qui attend")
        proc = self.runner("--once", "--wait", "0", env=self.env(
            AMEESH_CANON=self.root, AMEESH_CANON_UNTRUSTED="1",
            AMEESH_HOST="atelier", AMEESH_RESOURCE_INTERVAL="0",
            AMEESH_CGROUP_DIR=self.cgroup, AMEESH_CONTAINER="1"))
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertEqual(self.turns(), [])
        row = registry.get(self.db, "orchestre")
        self.assertIn("waiting: pressure on host atelier (own cgroup)", row["status_text"])
        self.assertIn("cgroup memory used", row["status_text"])
        self.assertTrue(row.get("pending_prompt"))

    def test_the_node_load_alone_no_longer_holds_an_idle_container(self):
        set_host_resources(self.root, pressure_scope="own_cgroup")
        self.sync()
        self._reading(load1=50.0)
        # cgroup well under its ceilings: memory 100M of 1G, no throttle
        write_cgroup(self.cgroup, current=100 * 1024 ** 2, maximum=1024 ** 3)
        registry.set_pending_prompt(self.db, "orchestre", "consigne qui doit partir")
        proc = self.runner("--once", "--wait", "0", env=self.env(
            AMEESH_CANON=self.root, AMEESH_CANON_UNTRUSTED="1",
            AMEESH_HOST="atelier", AMEESH_RESOURCE_INTERVAL="0",
            AMEESH_CGROUP_DIR=self.cgroup, AMEESH_CONTAINER="1"))
        # pas de retenue de pression : le relevé de nœud chargé ne bloque
        # plus, le tour suit son cours normal (ici le dossier de travail
        # manque, mais ce n'est plus la pression qui l'arrête)
        row = registry.get(self.db, "orchestre")
        self.assertNotIn("pressure on host", row.get("status_text") or "")
        self.assertNotIn("waiting: pressure", row.get("status_text") or "")
        self.assertIsNotNone(proc.returncode, proc.stdout + proc.stderr)

    def test_a_configured_capacity_provider_receives_the_need(self):
        set_host_resources(self.root, pressure_scope="own_cgroup",
                           max_cgroup_memory_used="90%")
        self.sync()
        self._reading(load1=0.0)
        events = os.path.join(self.tmp, "capacity.jsonl")
        script = os.path.join(self.tmp, "scaler.py")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(
                "import json, sys\n"
                "event = json.load(sys.stdin)\n"
                "with open(%r, 'a', encoding='utf-8') as out:\n"
                "    out.write(json.dumps(event, sort_keys=True) + '\\n')\n"
                "print(json.dumps({'action': 'wait'}))\n" % events)
        registry.set_pending_prompt(self.db, "orchestre", "consigne qui attend")
        proc = self.runner("--once", "--wait", "0", env=self.env(
            AMEESH_CANON=self.root, AMEESH_CANON_UNTRUSTED="1",
            AMEESH_HOST="atelier", AMEESH_RESOURCE_INTERVAL="0",
            AMEESH_CGROUP_DIR=self.cgroup, AMEESH_CONTAINER="1",
            AMEESH_CAPACITY_PROVIDER="cmd:%s %s" % (sys.executable, script)))
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        with open(events, encoding="utf-8") as fh:
            seen = [json.loads(line) for line in fh if line.strip()]
        needs = [event for event in seen if event.get("event") == "need"]
        self.assertEqual(len(needs), 1, seen)
        self.assertEqual(needs[0]["schema"], capacity.SCHEMA)
        self.assertEqual(needs[0]["scope"], "own_cgroup")
        self.assertEqual(needs[0]["agent"], "orchestre")
        row = registry.get(self.db, "orchestre")
        self.assertIn("capacity requested", row["status_text"])


class RunnerExpiredRequestDbTest(test_canon._CanonDbCase):
    def test_an_expired_request_notifies_the_sender_and_never_starts(self):
        self.sync()
        sent = mail.send(self.db, "relecteur", "orchestre", "requête à échéance",
                         host="atelier", kind="request",
                         payload={"request_expires_ts": int(time.time() - 60)})
        proc = self.runner("--once", "--wait", "0", "--agents", "orchestre",
                           env=self.env(
                               AMEESH_CANON=self.root, AMEESH_CANON_UNTRUSTED="1",
                               AMEESH_HOST="atelier", AMEESH_RESOURCE_INTERVAL="0"))
        self.assertIsNotNone(proc.returncode, proc.stdout + proc.stderr)
        self.assertEqual(self.turns(), [])
        self.assertEqual(mail.unread(self.db, "orchestre"), [])
        notices = mail.unread(self.db, "relecteur")
        self.assertEqual(len(notices), 1, notices)
        self.assertEqual(notices[0]["sender"], "ameesh")
        self.assertIn("expired", notices[0]["body"])
        self.assertEqual(mail.message_payload(notices[0]).get("request_expired"), sent)

    def test_a_failed_settlement_does_not_repeat_the_notice(self):
        # #2825 N5 : quand le soldage échoue, le message revient au sondage
        # suivant ; l'avis `request_expired` n'est envoyé qu'une fois et le
        # soldage est retenté.
        self.sync()
        sent = mail.send(self.db, "relecteur", "orchestre", "requête à échéance",
                         host="atelier", kind="request",
                         payload={"request_expires_ts": int(time.time() - 60)})
        expired = [row for row in mail.unread_active(self.db, "orchestre")
                   if mail.request_expired(row)]
        self.assertEqual([row["id"] for row in expired], [sent])
        worker = types.SimpleNamespace(name="orchestre", db=self.db)
        worker._notify_expired_request = types.MethodType(
            runner.AgentWorker._notify_expired_request, worker)
        with mock.patch.object(mail, "drop_expired",
                               side_effect=db_mod.DbError("boom")):
            runner.AgentWorker._live_mail(worker, expired)
            runner.AgentWorker._live_mail(worker, expired)
        notices = mail.unread(self.db, "relecteur")
        self.assertEqual(len(notices), 1, notices)
        # the settlement succeeds on the next poll, without a second notice
        runner.AgentWorker._live_mail(worker, expired)
        self.assertEqual(mail.unread(self.db, "orchestre"), [])
        self.assertEqual(len(mail.unread(self.db, "relecteur")), 1)
