# SPDX-License-Identifier: AGPL-3.0-only
"""#8466 — une série déportée sonde les moteurs de conteneurs du guest.

Le guest n'a pas l'environnement du composeur : `docker`/`podman` peuvent y
manquer ou leur démon y être injoignable. Le chemin local sonde déjà les
exécutables cités par un échec (`probe_engines`, #6567) ; le job déporté le
fait désormais dans le guest, après une série rouge, et rend ses marques au
composeur. Un 127 de docker causé par cette absence n'est donc pas un rouge
réel (canon §3) ; un moteur sain garde son rouge, et la sonde ne change jamais
le code de sortie de la série.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "paquet-auto.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("test_8466_paquet_auto", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["test_8466_paquet_auto"] = module
    spec.loader.exec_module(module)
    return module


paquet_auto = _load_script()

SEP = "=" * 70 + "\n"
DASH = "-" * 70 + "\n"

#: le cas h-16 de #6567, repris ici : le shim du ménage sort en 127
H16 = (SEP + "ERROR: setUpClass (tests.test_5162_controller_identities.RealIdentityTest)\n"
       + DASH
       + "Traceback (most recent call last):\n"
       + '  File "/x/tests/test_5162_controller_identities.py", line 9, in setUpClass\n'
       + "    result = subprocess.run(cmd, check=True)\n"
       + "subprocess.CalledProcessError: Command '['docker', 'run', '-d', "
         "'postgres:16']' returned non-zero exit status 127.\n")


def marker(executable: str, state: str) -> str:
    return paquet_auto.OFFLOAD_ENGINE_MARK + json.dumps(
        {"exe": executable, "state": state})


def write_engine(directory: pathlib.Path, name: str, body: str) -> pathlib.Path:
    path = directory / name
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(0o755)
    return path


class GuestProbeScriptTest(unittest.TestCase):
    """Le programme embarqué dans le job du guest, joué ici avec de faux moteurs."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(prefix="t8466-")
        self.addCleanup(self.dir.cleanup)
        self.root = pathlib.Path(self.dir.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.out = self.root / "suite.out"

    def run_probe(self, output: str) -> subprocess.CompletedProcess:
        self.out.write_text(output)
        env = dict(os.environ)
        env["PATH"] = str(self.bin) + os.pathsep + env.get("PATH", "")
        return subprocess.run([sys.executable, "-", str(self.out)],
                              input=paquet_auto.offload_engine_probe(),
                              capture_output=True, text=True, env=env, timeout=30)

    def markers(self, proc: subprocess.CompletedProcess) -> list[dict]:
        return [json.loads(line[len(paquet_auto.OFFLOAD_ENGINE_MARK):])
                for line in proc.stdout.splitlines()
                if line.startswith(paquet_auto.OFFLOAD_ENGINE_MARK)]

    def test_only_the_engine_cited_by_the_failure_is_probed(self):
        write_engine(self.bin, "docker", 'echo "ameesh: container engine not found '
                                        '(docker)" >&2\nexit 127')
        sentinel = self.root / "podman-called"
        write_engine(self.bin, "podman", 'touch %s\nexit 127' % sentinel)
        proc = self.run_probe(H16)
        self.assertEqual(self.markers(proc), [{"exe": "docker", "state": "absent"}])
        self.assertFalse(sentinel.exists())

    def test_a_healthy_engine_keeps_its_real_red(self):
        write_engine(self.bin, "docker", "exit 0")
        proc = self.run_probe(H16)
        self.assertEqual(self.markers(proc), [])

    def test_an_unreachable_daemon_is_reported(self):
        write_engine(self.bin, "docker", "echo 'Cannot connect to the Docker "
                                        "daemon at unix:///var/run/docker.sock' >&2\n"
                                        "exit 1")
        proc = self.run_probe(H16.replace("127.", "125."))
        self.assertEqual(self.markers(proc),
                         [{"exe": "docker", "state": "unreachable"}])

    def test_a_relative_path_is_never_probed(self):
        write_engine(self.bin, "docker", "exit 127")
        proc = self.run_probe(H16.replace("'docker'", "'./docker'"))
        self.assertEqual(self.markers(proc), [])


class GuestSeriesScriptTest(unittest.TestCase):
    """Le script complet : la sonde suit une série rouge, jamais une verte."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(prefix="t8466s-")
        self.addCleanup(self.dir.cleanup)
        self.root = pathlib.Path(self.dir.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        write_engine(self.bin, "docker", 'echo "ameesh: container engine not found '
                                        '(docker)" >&2\nexit 127')
        self.out = self.root / "suite.out"
        self.out.write_text(H16)
        # `python3 -m unittest` rend la sortie simulée et son code ; les autres
        # appels (la sonde, lue sur stdin) vont au vrai interpréteur.
        write_engine(self.bin, "python3",
                     'if [ "$1" = "-m" ]; then cat "$FAKE_TEST_OUT"; exit "$FAKE_RC"; fi\n'
                     'exec %s "$@"' % shlex.quote(sys.executable))
        self.script = paquet_auto.offload_tests_script(["tests.test_a"])

    def run_script(self, rc: int) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env.update({"PATH": str(self.bin) + os.pathsep + env.get("PATH", ""),
                    "FAKE_TEST_OUT": str(self.out), "FAKE_RC": str(rc)})
        return subprocess.run(["bash", "-lc", self.script], capture_output=True,
                              text=True, env=env, timeout=30)

    def test_a_red_series_is_probed_and_keeps_its_code(self):
        proc = self.run_script(1)
        self.assertEqual(proc.returncode, 1)
        self.assertIn(paquet_auto.OFFLOAD_ENGINE_MARK + '{"exe": "docker", '
                      '"state": "absent"}', proc.stdout)

    def test_a_green_series_is_not_probed(self):
        self.out.write_text("Ran 3 tests in 0.1s\n\nOK\n")
        proc = self.run_script(0)
        self.assertEqual(proc.returncode, 0)
        self.assertNotIn(paquet_auto.OFFLOAD_ENGINE_MARK, proc.stdout)


class OffloadUnusableEnginesTest(unittest.TestCase):
    def test_a_cited_engine_proved_absent_is_unusable(self):
        self.assertEqual(
            paquet_auto.offload_unusable_engines(H16 + "\n" + marker("docker", "absent")),
            frozenset({("docker", "absent")}))

    def test_a_marker_without_a_cited_failure_is_ignored(self):
        self.assertEqual(
            paquet_auto.offload_unusable_engines(
                "Ran 3 tests\n\nFAILED (errors=1)\n" + marker("docker", "absent")),
            frozenset())

    def test_another_engine_or_an_unknown_state_proves_nothing(self):
        output = H16 + "\n" + marker("podman", "absent") + "\n" \
            + marker("docker", "unknown") + "\n" + marker("docker", "ok")
        self.assertEqual(paquet_auto.offload_unusable_engines(output), frozenset())

    def test_a_malformed_marker_is_ignored(self):
        output = H16 + "\n" + paquet_auto.OFFLOAD_ENGINE_MARK + "{not json"
        self.assertEqual(paquet_auto.offload_unusable_engines(output), frozenset())


class OffloadedSeriesVerdictTest(unittest.TestCase):
    """`run_tests_offloaded` rend la sonde du guest au verdict de la série."""

    def setUp(self):
        for patch in (mock.patch.object(paquet_auto, "_offload_snapshot",
                                        return_value="f" * 40),
                      mock.patch.object(paquet_auto, "save_test_log",
                                        side_effect=lambda run: run)):
            patch.start()
            self.addCleanup(patch.stop)

    def run_series(self, output: str, returncode: int = 1):
        proc = subprocess.CompletedProcess([], returncode, output.encode(), b"")
        with mock.patch.object(paquet_auto, "_offload_call",
                               return_value=(proc, "green")) as call:
            run = paquet_auto.run_tests_offloaded(
                pathlib.Path("."), "unité #1", ["tests.test_a"],
                host="ameesh-agents-3", timeout=300, wall=900)
        return run, call

    def test_a_guest_engine_absence_makes_the_red_inconclusive(self):
        run, call = self.run_series(H16 + "\n" + marker("docker", "absent"))
        self.assertEqual(run.returncode, 1)
        self.assertEqual(run.unusable_engines, frozenset({("docker", "absent")}))
        self.assertFalse(run.real_failure)
        self.assertTrue(run.inconclusive)
        self.assertIn("docker info : absent", run.motive())
        self.assertEqual(paquet_auto.unittest_failures(
            run.output, run.unusable_engines), [])
        script = call.call_args.args[2]
        self.assertIn('"$py" -m unittest', script)
        self.assertIn('"$py" -c ', script)
        self.assertIn('tee "$out"', script)

    def test_a_real_red_without_a_proven_engine_stays_red(self):
        run, _ = self.run_series(H16)
        self.assertEqual(run.unusable_engines, frozenset())
        self.assertTrue(run.real_failure)
        self.assertFalse(run.inconclusive)

    def test_a_green_series_never_takes_markers(self):
        run, _ = self.run_series("Ran 3 tests\n\nOK\n" + marker("docker", "absent"),
                                 returncode=0)
        self.assertEqual(run.unusable_engines, frozenset())


if __name__ == "__main__":
    unittest.main()
