# SPDX-License-Identifier: AGPL-3.0-only
"""#6567 — un moteur de conteneurs absent est un non-concluant, pas un rouge.

Sur h-16 (#6554), `test_5162_controller_identities.RealIdentityTest.setUpClass`
lançait `docker run` en `check=True` : le shim du ménage sort en 127 sans
`AMEESH_REAL_DOCKER`, d'où `CalledProcessError … exit status 127`, lu comme
un rouge réel, et #6372 a été retiré du paquet. Un environnement sans moteur
ne prouve aucun défaut du lot (canon §3) ; un vrai échec de `docker` (autre
code), un 127 d'un autre programme ou une assertion restent rouges.

Aucune base, aucune forge, aucun réseau, aucun moteur de conteneurs.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
AUTO = ROOT / "scripts" / "paquet-auto.py"


def _load_script(path: pathlib.Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


paquet_auto = _load_script(AUTO, "test_paquet_auto_6567")

SEP = "=" * 70 + "\n"
DASH = "-" * 70 + "\n"


def _error(header: str, exception: str, *, frames: str = "") -> str:
    return (SEP + "ERROR: %s\n" % header + DASH
            + "Traceback (most recent call last):\n"
            + (frames or '  File "/x/tests/test_t.py", line 9, in setUpClass\n'
                         "    result = subprocess.run(cmd, check=True)\n")
            + exception + "\n\n")


def _run(output: str, *, failures: int = 0, errors: int = 1) -> object:
    parts = []
    if failures:
        parts.append("failures=%d" % failures)
    if errors:
        parts.append("errors=%d" % errors)
    output += DASH + "Ran 3 tests in 0.2s\n\nFAILED (%s)\n" % ", ".join(parts)
    return paquet_auto.TestRun(label="unité #6372", modules=["tests.test_t"],
                               returncode=1, output=output, seconds=0.2)


#: la sortie du cas h-16, à la forme exacte de `subprocess.CalledProcessError`
H16 = _error(
    "setUpClass (tests.test_5162_controller_identities.RealIdentityTest)",
    "subprocess.CalledProcessError: Command '['docker', 'run', '-d', "
    "'--env-file', '/tmp/i5162-x/0/container.env', '-p', "
    "'127.0.0.1:41234:41234', 'postgres:16']' returned non-zero exit status 127.")


class ContainerEngineAbsentTest(unittest.TestCase):
    def assert_inconclusive(self, run) -> None:
        self.assertFalse(run.real_failure)
        self.assertTrue(run.inconclusive)

    def assert_red(self, run) -> None:
        self.assertTrue(run.real_failure)
        self.assertFalse(run.inconclusive)

    def test_h16_docker_run_127_en_setupclass_est_non_concluant(self) -> None:
        run = _run(H16)
        self.assert_inconclusive(run)
        self.assertIn("exit status 127", run.motive())

    def test_commande_en_chaine_et_chemin_absolu(self) -> None:
        for command in ("Command 'docker run -d postgres:16'",
                        "Command '['/usr/bin/docker', 'run', 'x']'",
                        "Command '['podman', 'run', 'x']'"):
            with self.subTest(command=command):
                self.assert_inconclusive(_run(_error(
                    "setUpClass (tests.test_t.T)",
                    "subprocess.CalledProcessError: %s returned non-zero "
                    "exit status 127." % command)))

    def test_executable_introuvable(self) -> None:
        for exception in (
                "FileNotFoundError: [Errno 2] No such file or directory: 'docker'",
                "FileNotFoundError: [Errno 2] No such file or directory: "
                "'/usr/local/bin/podman'"):
            with self.subTest(exception=exception):
                self.assert_inconclusive(_run(_error(
                    "test_value (tests.test_t.T.test_value)", exception)))

    def test_message_du_shim_sans_trace(self) -> None:
        # rc 1 sans en-tête ni résumé : seul le motif d'environnement compte
        run = paquet_auto.TestRun(
            label="unité", modules=["tests.test_t"], returncode=1, seconds=0.1,
            output="ameesh: container engine not found (docker)\n")
        self.assert_inconclusive(run)

    def test_docker_qui_echoue_vraiment_reste_rouge(self) -> None:
        self.assert_red(_run(_error(
            "setUpClass (tests.test_t.T)",
            "subprocess.CalledProcessError: Command '['docker', 'run', 'x']' "
            "returned non-zero exit status 125.")))

    def test_un_127_d_un_autre_programme_reste_rouge(self) -> None:
        for command in ("['git', 'status']", "['dockerize', 'run']",
                        "['./scripts/docker-up.sh']"):
            with self.subTest(command=command):
                self.assert_red(_run(_error(
                    "test_value (tests.test_t.T.test_value)",
                    "subprocess.CalledProcessError: Command '%s' returned "
                    "non-zero exit status 127." % command)))

    def test_une_assertion_qui_cite_le_127_reste_rouge(self) -> None:
        self.assert_red(_run(_error(
            "test_value (tests.test_t.T.test_value)",
            "AssertionError: Command '['docker', 'run']' returned non-zero "
            "exit status 127.")))

    def test_un_fail_a_cote_du_moteur_absent_reste_rouge(self) -> None:
        output = H16 + SEP + "FAIL: test_value (tests.test_t.T.test_value)\n" \
            + DASH + "Traceback (most recent call last):\n" \
            + "AssertionError: 1 != 2\n\n"
        self.assert_red(_run(output, failures=1, errors=1))


if __name__ == "__main__":
    unittest.main()
