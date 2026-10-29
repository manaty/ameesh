# SPDX-License-Identifier: AGPL-3.0-only
"""#4433 : la PSI mémoire d'un hôte expliquée par un cgroup de runner.

Un jest ciblé de `mty-nxl-site-01` (six workers d'environ 1 Go) amène son
cgroup `ameesh-runner-agent@mty-nxl-site-01.service` à 5,9 Gio pour un
`memory.max` de 6 Gio : le cgroup thrashe, la PSI de l'hôte suit (56 % some),
alors que `MemAvailable` reste à 18 Go. En périmètre `host`, la garde
`max_mem_pressure` mettait alors TOUS les agents de l'hôte en pause.

Attendu : quand la PSI de l'hôte s'explique par un cgroup de runner à son
propre plafond (son `memory.current`/`memory.max` ≥ 90 %, sa propre PSI au
moins celle de l'hôte) et que la mémoire de l'hôte reste au-dessus de son
plancher, la brèche est imputée à ce runner — ses nouveaux tours sont retenus,
une alerte le nomme — et pas à l'hôte. Sans ces mesures, la garde de l'hôte
reste inchangée (on ne devine pas).
"""
from __future__ import annotations

import os
import tempfile
import threading
import types
import unittest
from unittest import mock

from ameesh import exploitation, notify, resources, runner, storage

from .support import PgTestCase

GIB = 1024 ** 3
HOST = "ameesh-agents-2"
AGENT = "mty-nxl-site-01"
UNIT = "ameesh-runner-agent@%s.service" % AGENT


def limits(**declared):
    return resources.thresholds({"resources": declared})


def write_runner(root: str, unit: str, *, current: int = int(5.9 * GIB),
                 maximum: int | None = 6 * GIB, psi: float = 56.0) -> str:
    """Un cgroup de runner dans l'arbre factice `root` (systemd y range les
    unités de `ameesh-agents.slice`)."""
    path = os.path.join(root, resources.slice_path("ameesh-agents.slice"), unit)
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "memory.current"), "w", encoding="ascii") as fh:
        fh.write("%d\n" % current)
    with open(os.path.join(path, "memory.max"), "w", encoding="ascii") as fh:
        fh.write("max\n" if maximum is None else "%d\n" % maximum)
    with open(os.path.join(path, "memory.pressure"), "w", encoding="ascii") as fh:
        fh.write("some avg10=0.00 avg60=%.2f avg300=0.00 total=0\n"
                 "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n" % psi)
    return path


def entry(agent: str = AGENT, *, used: float = 0.98, psi: float = 56.0,
          path: str | None = None, maximum: int = 6 * GIB) -> dict:
    """Un cgroup de runner tel que le publie le relevé (`cgroup.runners`)."""
    return {
        "agent": agent,
        "unit": "ameesh-runner-agent@%s.service" % agent,
        "path": path or ("/sys/fs/cgroup/ameesh.slice/ameesh-agents.slice/"
                         "ameesh-runner-agent@%s.service" % agent),
        "mem_current_bytes": int(used * maximum),
        "mem_max_bytes": maximum,
        "mem_used": used,
        "mem_some": psi,
    }


def reading(*runners, host_psi: float = 56.0, available: int = 18 * GIB) -> dict:
    """Le relevé simulé du constat : PSI d'hôte 56, cgroup X à 98 % de 6 Gio
    avec PSI 56, `MemAvailable` 18 Gio."""
    return {"host": HOST, "psi_mem_some": host_psi,
            "mem_available_bytes": available,
            "cgroup": {"path": "/sys/fs/cgroup/own", "runners": list(runners)}}


class AttributionTest(unittest.TestCase):
    def test_a_runner_at_its_ceiling_explains_the_host_pressure(self):
        verdict = resources.pressure(reading(entry()), limits=limits())
        # le constat exact : aucun agent de l'hôte n'est retenu...
        self.assertFalse(verdict["blocked"])
        self.assertFalse(verdict["critical"])
        self.assertEqual(verdict["breaches"], [])
        # ...et la brèche est nommée, avec son runner
        self.assertEqual([b["agent"] for b in verdict["attributed"]], [AGENT])
        breach = verdict["attributed"][0]
        self.assertEqual(breach["key"], "max_mem_pressure")
        self.assertEqual(breach["cgroup"], entry()["path"])
        self.assertIn("runner %s" % AGENT, breach["label"])
        self.assertTrue(breach["critical"])

    def test_only_the_attributed_runner_is_held(self):
        data = reading(entry())
        own = resources.pressure(data, limits=limits(), own_path=entry()["path"])
        self.assertTrue(own["blocked"])
        self.assertTrue(own["critical"])
        self.assertEqual([b["key"] for b in own["breaches"]], ["max_mem_pressure"])
        self.assertIn("runner %s" % AGENT, resources.queue_text(HOST, own))
        other = resources.pressure(data, limits=limits(), own_path="/autre/cgroup")
        self.assertFalse(other["blocked"])
        self.assertEqual(other["breaches"], [])
        self.assertEqual([b["agent"] for b in other["attributed"]], [AGENT])

    def test_the_agent_name_holds_the_runner_when_its_path_is_unknown(self):
        data = reading(entry())
        own = resources.pressure(data, limits=limits(), own_agents=[AGENT])
        self.assertTrue(own["blocked"])
        other = resources.pressure(data, limits=limits(), own_agents=["un-autre-agent"])
        self.assertFalse(other["blocked"])

    def test_a_known_path_wins_over_the_agent_name(self):
        verdict = resources.pressure(reading(entry()), limits=limits(),
                                     own_path="/autre/cgroup", own_agents=[AGENT])
        self.assertFalse(verdict["blocked"])

    def test_two_runners_at_their_ceiling_are_both_named(self):
        second = entry("autre-runner")
        verdict = resources.pressure(reading(entry(), second), limits=limits())
        self.assertFalse(verdict["blocked"])
        self.assertEqual(sorted(b["agent"] for b in verdict["attributed"]),
                         sorted([AGENT, "autre-runner"]))
        own = resources.pressure(reading(entry(), second), limits=limits(),
                                 own_path=second["path"])
        self.assertTrue(own["blocked"])
        self.assertEqual([b["agent"] for b in own["breaches"]], ["autre-runner"])

    def test_the_host_keeps_the_breach_when_its_memory_is_short(self):
        # MemAvailable sous le plancher : l'hôte manque réellement de mémoire,
        # le runner n'explique plus rien (et `min_mem_available` le dit déjà).
        verdict = resources.pressure(reading(entry(), available=512 * 1024 ** 2),
                                     limits=limits())
        self.assertTrue(verdict["blocked"])
        self.assertEqual(verdict["attributed"], [])
        keys = [b["key"] for b in verdict["breaches"]]
        self.assertIn("min_mem_available", keys)
        self.assertIn("max_mem_pressure", keys)

    def test_a_runner_below_its_ceiling_does_not_explain_the_host(self):
        verdict = resources.pressure(reading(entry(used=0.80)), limits=limits())
        self.assertTrue(verdict["blocked"])
        self.assertEqual(verdict["attributed"], [])

    def test_the_runners_own_psi_must_reach_the_hosts(self):
        # 20 % pour le cgroup, 56 % pour l'hôte : les stalls viennent d'ailleurs.
        verdict = resources.pressure(reading(entry(psi=20.0)), limits=limits())
        self.assertTrue(verdict["blocked"])
        self.assertEqual(verdict["attributed"], [])

    def test_an_unmeasured_runner_is_not_guessed(self):
        for missing in ("mem_used", "mem_some"):
            with self.subTest(missing=missing):
                data = entry()
                data[missing] = None
                verdict = resources.pressure(reading(data), limits=limits())
                self.assertTrue(verdict["blocked"])
                self.assertEqual(verdict["attributed"], [])

    def test_a_reading_without_runner_measures_keeps_the_host_behaviour(self):
        # Relevé d'avant #4433 : pas de `cgroup.runners` — la garde de l'hôte
        # reste exactement celle d'avant.
        data = reading()
        data["cgroup"] = {"path": "/sys/fs/cgroup/own"}
        verdict = resources.pressure(data, limits=limits())
        self.assertTrue(verdict["blocked"])
        self.assertEqual([b["key"] for b in verdict["breaches"]], ["max_mem_pressure"])

    def test_pressure_under_its_own_ceiling_is_not_attributed(self):
        verdict = resources.pressure(reading(entry(), host_psi=5.0), limits=limits())
        self.assertFalse(verdict["blocked"])
        self.assertEqual(verdict["attributed"], [])


class RunnerCgroupsTest(unittest.TestCase):
    def test_reads_both_unit_templates_and_their_measures(self):
        with tempfile.TemporaryDirectory() as root:
            system = write_runner(root, "ameesh-runner@serveur.service",
                                  current=1 * GIB, maximum=2 * GIB, psi=12.0)
            user_base = os.path.join(root, "user.slice", "user-1001.slice",
                                     "user@1001.service")
            poste = write_runner(user_base, UNIT)
            got = {item["agent"]: item for item in resources.runner_cgroups(root)}
        self.assertEqual(set(got), {"serveur", AGENT})
        self.assertEqual(got["serveur"]["path"], system)
        self.assertEqual(got["serveur"]["mem_used"], 0.5)
        self.assertEqual(got["serveur"]["mem_some"], 12.0)
        self.assertEqual(got[AGENT]["path"], poste)
        self.assertEqual(got[AGENT]["mem_current_bytes"], int(5.9 * GIB))
        self.assertEqual(got[AGENT]["mem_max_bytes"], 6 * GIB)
        self.assertEqual(got[AGENT]["mem_used"], round(5.9 / 6, 4))
        self.assertEqual(got[AGENT]["mem_some"], 56.0)
        self.assertEqual(got[AGENT]["unit"], UNIT)

    def test_an_unreadable_or_unlimited_ceiling_leaves_the_share_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            path = write_runner(root, UNIT, maximum=None)
            got = resources.runner_cgroups(root)
            self.assertEqual(len(got), 1)
            self.assertIsNone(got[0]["mem_max_bytes"])
            self.assertIsNone(got[0]["mem_used"])
            self.assertEqual(got[0]["mem_some"], 56.0)
            os.unlink(os.path.join(path, "memory.pressure"))
            got = resources.runner_cgroups(root)
            self.assertIsNone(got[0]["mem_some"])

    def test_a_host_without_agents_slice_measures_nothing(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(resources.runner_cgroups(root), [])

    def test_interface_files_do_not_hide_the_user_managers(self):
        # cgroupfs exposes `cgroup.controllers`/`cgroup.events` beside the
        # slice directories of `user.slice`; opening one as a directory used
        # to abort the walk before the user manager was found (real host,
        # 29/10: `runner_cgroups()` returned nothing).
        with tempfile.TemporaryDirectory() as root:
            users = os.path.join(root, "user.slice")
            manager = os.path.join(users, "user-1001.slice", "user@1001.service")
            os.makedirs(manager)
            with open(os.path.join(users, "cgroup.controllers"), "w",
                      encoding="ascii") as fh:
                fh.write("cpu memory\n")
            with open(os.path.join(users, "cgroup.events"), "w",
                      encoding="ascii") as fh:
                fh.write("populated 1\n")
            self.assertIn(manager, resources.host_cgroup_bases(root))
            write_runner(manager, UNIT)
            got = resources.runner_cgroups(root)
        self.assertEqual([item["agent"] for item in got], [AGENT])

    def test_the_reading_publishes_the_runner_cgroups(self):
        sentinel = entry()
        with mock.patch.object(resources, "runner_cgroups", return_value=[sentinel]):
            out = resources.sample(HOST)
        self.assertEqual(out["cgroup"]["runners"], [sentinel])


class RunnerVerdictTest(PgTestCase):
    """Le verdict de l'exécuteur retient le cgroup qui est le sien, et lui seul."""

    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM host_resources")
        storage.of(self.db).hosts.record(reading(entry()))

    def runner_with(self, own_path: str) -> types.SimpleNamespace:
        return types.SimpleNamespace(
            db=self.db, host=HOST,
            _pressure_lock=threading.Lock(), _pressure_at=0.0, _pressure={},
            _host_limits=limits(), power_reading=lambda: {},
            _cgroup=types.SimpleNamespace(read=lambda: {"path": own_path}),
            agents_filter=[AGENT], capacity=None,
            _pressure_episode_update=lambda verdict: None)

    def test_the_culprit_runner_is_held(self):
        verdict = runner.Runner.host_pressure(self.runner_with(entry()["path"]))
        self.assertTrue(verdict["blocked"])
        self.assertTrue(verdict["critical"])
        self.assertIn("runner %s" % AGENT, resources.queue_text(HOST, verdict))

    def test_another_runner_is_not_held(self):
        verdict = runner.Runner.host_pressure(self.runner_with("/autre/cgroup"))
        self.assertFalse(verdict["blocked"])
        self.assertEqual(verdict["breaches"], [])


class RunnerPressureAlertTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM host_resources")

    def test_the_alert_names_the_runner_and_not_the_host(self):
        storage.of(self.db).hosts.record(reading(entry()))
        found = [a for a in exploitation.alerts(self.cfg, self.db)
                 if a.get("host") == HOST]
        kinds = [a["type"] for a in found]
        self.assertNotIn("host_pressure", kinds)
        self.assertIn("runner_pressure", kinds)
        alert = next(a for a in found if a["type"] == "runner_pressure")
        self.assertEqual(alert["agent"], AGENT)
        self.assertIn(AGENT, alert["detail"])
        self.assertEqual(alert["mem_used"], 0.98)
        self.assertEqual(alert["cgroup"], entry()["path"])

    def test_the_new_type_is_declared(self):
        self.assertIn("runner_pressure", exploitation.ALERT_TYPES)
        self.assertIn("runner_pressure", notify.TYPE_LABELS)
        # il ne bloque pas la prise des AUTRES agents de l'hôte
        from ameesh import backlog
        self.assertNotIn("runner_pressure", backlog.HOST_BLOCKING_ALERTS)


if __name__ == "__main__":
    unittest.main()
