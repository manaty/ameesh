# SPDX-License-Identifier: AGPL-3.0-only
"""#8437 — les séries heavy du composeur jouées sur l'hôte admis le moins chargé.

Constat (28/11) : sur ameesh-agents-2 chargé au-delà de son seuil, la passe de
contrats finissait non concluante à chaque composition (borne de 1200 s, 35
contrats sur 50 sans créneau heavy) pendant qu'ameesh-agents-3 restait vide.
Les contrats partent en un seul job du guest (une base, un créneau), les
séries directes chacune en un job ; les modules exclus du guest (#4071)
restent ici. Un hôte sans relevé frais (arrêté par la veille) n'est jamais
réveillé : repli local, comme un refus du job avant qu'il ne parte.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.test_4304_paquet_poussee_precoce import paquet_auto as auto

TOOLING = auto.Tooling(project="ameesh", hosts=("ameesh-agents-3", "ameesh-agents-1"))


def reading(load1, cpus=8, age=30.0, gate="available"):
    return {"now_ts": 1000.0 + age, "sampled_ts": 1000.0, "load1": load1,
            "cpu_count": cpus, "gate_state": gate}


def event(**fields):
    return json.dumps(dict(schema="ameesh-test-event/1", **fields))


def flow(*tests, fin="succes"):
    lines = ["ameesh-prevalidation-part=psycopg:1", event(type="debut")]
    for module, name, statut in tests:
        lines.append(event(type="test", module=module, test=name, statut=statut))
    if fin:
        lines.append(event(type="fin", statut=fin))
    return "\n".join(lines) + "\n"


def reset_offload():
    auto._OFFLOAD.update(at=None, host="", down=set())


class HostChoiceTest(unittest.TestCase):
    def test_least_loaded_fresh_open_host_wins(self):
        host, why = auto.choose_offload_host({
            "ameesh-agents-1": reading(6.0), "ameesh-agents-3": reading(1.0),
            "ameesh-agents-2": reading(20.0, cpus=16)})
        self.assertEqual(host, "ameesh-agents-3")
        self.assertIn("par cœur", why)

    def test_a_stopped_host_is_not_woken_and_a_closed_gate_refuses(self):
        host, why = auto.choose_offload_host({
            "ameesh-agents-3": reading(0.1, age=3600),
            "ameesh-agents-1": reading(0.2, gate="draining"),
            "ameesh-agents-2": None})
        self.assertEqual(host, "")
        self.assertIn("réputé arrêté", why)
        self.assertIn("porte draining", why)
        self.assertIn("aucun relevé", why)

    def test_a_saturated_host_is_no_candidate(self):
        host, why = auto.choose_offload_host({"ameesh-agents-3": reading(9.0)})
        self.assertEqual(host, "")
        self.assertIn("saturé", why)


class OffloadHostTest(unittest.TestCase):
    def setUp(self):
        reset_offload()
        self.addCleanup(reset_offload)
        patcher = mock.patch.object(auto.shutil, "which", return_value="/bin/ameesh")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_legacy_tooling_or_switch_off_stays_local(self):
        with mock.patch.object(auto, "_offload_readings") as readings:
            self.assertEqual(auto.offload_host(auto.LEGACY_TOOLING), "")
            with mock.patch.dict(os.environ, {auto.OFFLOAD_ENV: "off"}):
                self.assertEqual(auto.offload_host(TOOLING), "")
        readings.assert_not_called()

    def test_unreadable_readings_fall_back_locally(self):
        with mock.patch.object(auto, "_offload_readings",
                               side_effect=RuntimeError("base absente")):
            self.assertEqual(auto.offload_host(TOOLING), "")

    def test_choice_is_cached_and_a_refusing_host_is_dropped(self):
        readings = {"ameesh-agents-3": reading(1.0), "ameesh-agents-1": reading(4.0)}
        with mock.patch.object(auto, "_offload_readings",
                               side_effect=lambda hosts: {h: readings[h] for h in hosts}) \
                as read:
            self.assertEqual(auto.offload_host(TOOLING), "ameesh-agents-3")
            self.assertEqual(auto.offload_host(TOOLING), "ameesh-agents-3")
            self.assertEqual(read.call_count, 1)
            auto.offload_down("ameesh-agents-3", "VM unreachable")
            self.assertEqual(auto.offload_host(TOOLING), "ameesh-agents-1")
            self.assertEqual(read.call_args.args[0], ["ameesh-agents-1"])


class SplitFlowTest(unittest.TestCase):
    NAMES = {"test_a": "tests.test_a", "test_b": "tests.test_b", "test_c": "tests.test_c"}

    def split(self, events, returncode=1, timed_out=False):
        with mock.patch.object(auto, "save_test_log", side_effect=lambda run: run):
            return {run.modules[0]: run for run in auto.split_flow(
                events, self.NAMES, host="ameesh-agents-3", seconds=12.0,
                returncode=returncode, timed_out=timed_out, output="")}

    def test_each_module_keeps_its_own_verdict(self):
        runs = self.split(flow(("test_a", "test_a.T.test_1", "succes"),
                               ("test_b", "test_b.T.test_1", "echec"),
                               ("test_c", "test_c.T.test_1", "saute"),
                               fin="echec"))
        self.assertTrue(runs["tests.test_a"].green)
        self.assertTrue(runs["tests.test_b"].real_failure)
        self.assertTrue(runs["tests.test_c"].inconclusive)
        self.assertFalse(runs["tests.test_c"].real_failure)

    def test_a_module_never_played_is_inconclusive(self):
        runs = self.split(flow(("test_a", "test_a.T.test_1", "succes")), returncode=0)
        self.assertTrue(runs["tests.test_a"].green)
        self.assertTrue(runs["tests.test_b"].inconclusive)
        self.assertIn("sans test", runs["tests.test_b"].motive())

    def test_a_lost_job_leaves_every_module_inconclusive(self):
        runs = self.split(flow(("test_a", "test_a.T.test_1", "succes"), fin=None),
                          returncode=None, timed_out=True)
        self.assertTrue(all(run.inconclusive and not run.real_failure
                            for run in runs.values()))


class OffloadedContractsTest(unittest.TestCase):
    MODULES = ["tests.test_a", "tests.test_5947_preflight_cli_n_moins_1"]

    def setUp(self):
        reset_offload()
        self.addCleanup(reset_offload)
        for patch in (mock.patch.object(auto, "_TOOLING", TOOLING),
                      mock.patch.object(auto, "save_test_log", side_effect=lambda run: run),
                      mock.patch.object(auto, "_offload_snapshot", return_value="f" * 40),
                      mock.patch.object(auto, "git", return_value=subprocess.CompletedProcess(
                          [], 0, b"a" * 40, b""))):
            patch.start()
            self.addCleanup(patch.stop)

    def local(self, worktree, modules, *, end, parallel):
        return auto.merge_contract_runs(modules, [
            auto.TestRun("contrats " + m, [m], 0, "Ran 1 test\n\nOK\n", 0.1)
            for m in modules])

    def test_guest_job_and_local_exclusions_merge(self):
        proc = subprocess.CompletedProcess(
            [], 0, flow(("test_a", "test_a.T.test_1", "succes")).encode(), b"")
        with mock.patch.object(auto, "_offload_call", return_value=(proc, "green")) as call, \
                mock.patch.object(auto, "_local_contract_modules",
                                  side_effect=self.local) as local:
            run = auto.run_offloaded_contracts(
                Path("."), self.MODULES, end=time.monotonic() + 600, parallel=2,
                host="ameesh-agents-3", tooling=TOOLING)
        self.assertEqual(local.call_args.args[1], ["tests.test_5947_preflight_cli_n_moins_1"])
        self.assertEqual(call.call_args.args[1], "ameesh-agents-3")
        self.assertIn("--parallel 2 --flow", call.call_args.args[2])
        self.assertEqual(run.returncode, 0)
        self.assertEqual(run.executor, "ameesh-agents-3")
        self.assertEqual(sorted(m for c in run.children for m in c.modules),
                         sorted(self.MODULES))

    def test_a_refused_job_reruns_locally_and_drops_the_host(self):
        proc = subprocess.CompletedProcess(
            [], 75, b"", b"ameesh offload: VM unreachable or refused; no fallback\n")
        with mock.patch.object(auto, "_offload_call", return_value=(proc, "no-vm")), \
                mock.patch.object(auto, "_local_contract_modules",
                                  side_effect=self.local) as local:
            run = auto.run_offloaded_contracts(
                Path("."), self.MODULES, end=time.monotonic() + 600, parallel=2,
                host="ameesh-agents-3", tooling=TOOLING)
        self.assertEqual(sorted(c.args[1][0] for c in local.call_args_list), sorted(self.MODULES))
        self.assertEqual(run.executor, "")
        self.assertIn("ameesh-agents-3", auto._OFFLOAD["down"])

    def test_a_lost_connection_is_not_rerun(self):
        proc = subprocess.CompletedProcess(
            [], 75, b"", b"ameesh offload: lost the connection to h (ssh exit 255)\n")
        with mock.patch.object(auto, "_offload_call",
                               return_value=(proc, "transport-lost")), \
                mock.patch.object(auto, "_local_contract_modules",
                                  side_effect=self.local) as local:
            run = auto.run_offloaded_contracts(
                Path("."), ["tests.test_a"], end=time.monotonic() + 600, parallel=1,
                host="ameesh-agents-3", tooling=TOOLING)
        local.assert_not_called()
        self.assertIsNone(run.returncode)
        self.assertTrue(run.children[0].inconclusive)


class SealedExecutorTest(unittest.TestCase):
    """L'hôte est décidé avant l'attestation ; un repli local n'atteste rien."""

    def run_pass(self, executed_by):
        seen = {}

        def context(*args, **kwargs):
            seen["executor"] = kwargs.get("executor")
            raise ValueError("isolated test: no canon")

        def contracts(*args, **kwargs):
            seen["host"] = kwargs.get("host")
            return auto.merge_contract_runs(["tests.test_a"], [auto.TestRun(
                "contrats tests.test_a", ["tests.test_a"], 0, "Ran 1 test\n\nOK\n", 0.1)],
                executed_by)

        with mock.patch.object(auto, "contract_scope", return_value=("a" * 40, "b" * 40)), \
                mock.patch.object(auto, "verify_contract_scope"), \
                mock.patch.object(auto.shutil, "which", return_value="/bin/ameesh"), \
                mock.patch.object(auto, "contract_modules", return_value=["tests.test_a"]), \
                mock.patch.object(auto, "offload_host", return_value="ameesh-agents-3"), \
                mock.patch.object(auto, "contract_attestation_context", side_effect=context), \
                mock.patch.object(auto, "run_contract_modules", side_effect=contracts), \
                mock.patch.object(auto, "save_test_log", side_effect=lambda run: run):
            report = auto.prepublication_contracts(Path("."), base="c" * 40, budget=600)
        return seen, report

    def test_the_decided_host_is_sealed_and_played(self):
        seen, report = self.run_pass("ameesh-agents-3")
        self.assertEqual(seen, {"executor": "ameesh-agents-3", "host": "ameesh-agents-3"})
        self.assertEqual(report.run.executor, "ameesh-agents-3")

    def test_a_local_fallback_writes_no_attestation(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            _, report = self.run_pass("")
        self.assertIsNone(report.evidence)
        self.assertIn("attestation contrats : non écrite", out.getvalue())


class OffloadedSeriesTest(unittest.TestCase):
    """Une série directe : un job, repli local au refus, exclus joués ici."""

    def setUp(self):
        reset_offload()
        self.addCleanup(reset_offload)
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        for args in (["init", "-q"], ["config", "user.email", "t@t"],
                     ["config", "user.name", "t"]):
            subprocess.run(["git", "-C", str(self.repo), *args], check=True)
        (self.repo / "README").write_text("x\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "x"], check=True)
        self.calls = root / "calls"
        bin_dir = root / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "ameesh"
        # `offload run` : records its argv, answers per $FAKE_OFFLOAD ;
        # `heavy` : runs the command at once
        fake.write_text(
            "#!/bin/sh\n"
            "echo \"$*\" >> \"$FAKE_CALLS\"\n"
            "if [ \"$1\" = heavy ]; then while [ \"$1\" != -- ]; do shift; done; shift;"
            " exec \"$@\"; fi\n"
            "case \"$FAKE_OFFLOAD\" in\n"
            "  refuse) echo 'ameesh offload: VM unreachable or refused (put --have, exit 255):"
            " x; no fallback (--fallback none)' >&2; exit 75;;\n"
            "  red) echo 'FAIL: test_x (tests.test_a.T.test_x)' >&2; exit 1;;\n"
            "  *) printf 'Ran 3 tests in 0.1s\\n\\nOK\\n' >&2; exit 0;;\n"
            "esac\n")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        env = {"PATH": "%s:%s" % (bin_dir, os.environ.get("PATH", "")),
               "FAKE_CALLS": str(self.calls)}
        for patch in (mock.patch.dict(os.environ, env),
                      mock.patch.object(auto, "_TOOLING", TOOLING),
                      mock.patch.object(auto, "offload_host", return_value="ameesh-agents-3"),
                      mock.patch.object(auto, "save_test_log", side_effect=lambda run: run),
                      mock.patch.object(auto, "project_python", return_value="true")):
            patch.start()
            self.addCleanup(patch.stop)

    def calls_made(self):
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def test_series_runs_on_the_guest_with_a_frozen_snapshot(self):
        run = auto.run_tests(self.repo, "unité #1", ["tests.test_a", "tests.test_b"],
                             timeout=300, heavy=True, wall=900)
        self.assertEqual(run.returncode, 0)
        self.assertIn("déport sur ameesh-agents-3", run.output)
        (call,) = self.calls_made()
        self.assertTrue(call.startswith("offload run --host ameesh-agents-3 --profile ameesh "
                                        "--fallback none --timeout 300 --wait-timeout 885 "
                                        "--snapshot "), call)
        self.assertIn("-m unittest -q tests.test_a tests.test_b", call)

    def test_a_red_on_the_guest_stays_red(self):
        with mock.patch.dict(os.environ, {"FAKE_OFFLOAD": "red"}):
            run = auto.run_tests(self.repo, "unité #1", ["tests.test_a"],
                                 timeout=300, heavy=True, wall=900)
        self.assertEqual(run.returncode, 1)
        self.assertEqual(len(self.calls_made()), 1)

    def test_a_refused_job_runs_here_under_heavy(self):
        with mock.patch.dict(os.environ, {"FAKE_OFFLOAD": "refuse"}), \
                mock.patch.object(auto, "offload_down") as down:
            run = auto.run_tests(self.repo, "unité #1", ["tests.test_a"],
                                 timeout=300, heavy=True, wall=900)
        down.assert_called_once()
        offload, heavy = self.calls_made()
        self.assertTrue(offload.startswith("offload run"))
        self.assertTrue(heavy.startswith("heavy --max-duration 300s -- true -m unittest"))
        self.assertEqual(run.returncode, 0)

    def test_a_guest_excluded_module_keeps_the_series_here(self):
        auto.run_tests(self.repo, "unité #1",
                       ["tests.test_a", "tests.test_503_agent_host"],
                       timeout=300, heavy=True, wall=900)
        (call,) = self.calls_made()
        self.assertTrue(call.startswith("heavy "), call)


if __name__ == "__main__":
    unittest.main()
