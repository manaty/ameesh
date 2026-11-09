# SPDX-License-Identifier: AGPL-3.0-only
"""#6567 — un moteur de conteneurs absent est un non-concluant, pas un rouge.

Sur h-16 (#6554), `test_5162_controller_identities.RealIdentityTest.setUpClass`
lançait `docker run` en `check=True` : le shim du ménage sort en 127 sans
`AMEESH_REAL_DOCKER`, d'où `CalledProcessError … exit status 127`, lu comme
un rouge réel, et #6372 a été retiré du paquet. Un environnement sans moteur
ne prouve aucun défaut du lot (canon §3) ; un vrai échec de `docker` (autre
code), un 127 d'un autre programme ou une assertion restent rouges.

Un 127 de docker/podman ne suffit pas seul (B/P2 cx1) : un moteur sain rend
aussi 127 quand la commande lancée dans le conteneur est introuvable. Il ne
compte que si la sonde `<moteur> info`, dans l'environnement de la série, a
prouvé le moteur absent (`absent_engines`).

Aucune base, aucune forge, aucun réseau, aucun moteur de conteneurs.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

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


def _run(output: str, *, failures: int = 0, errors: int = 1,
         absent: frozenset = frozenset()) -> object:
    parts = []
    if failures:
        parts.append("failures=%d" % failures)
    if errors:
        parts.append("errors=%d" % errors)
    output += DASH + "Ran 3 tests in 0.2s\n\nFAILED (%s)\n" % ", ".join(parts)
    return paquet_auto.TestRun(label="unité #6372", modules=["tests.test_t"],
                               returncode=1, output=output, seconds=0.2,
                               absent_engines=absent)


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

    def test_h16_docker_run_127_moteur_prouve_absent_est_non_concluant(self) -> None:
        run = _run(H16, absent=frozenset({"docker"}))
        self.assert_inconclusive(run)
        self.assertIn("docker info : 127", run.motive())
        self.assertEqual(paquet_auto.unittest_failures(run.output, run.absent_engines), [])

    def test_127_avec_moteur_sain_reste_rouge(self) -> None:
        # B/P2 cx1 : `docker run busybox foo`, commande du conteneur absente
        run = _run(_error(
            "test_value (tests.test_t.T.test_value)",
            "subprocess.CalledProcessError: Command '['docker', 'run', 'busybox', "
            "'foo']' returned non-zero exit status 127."))
        self.assert_red(run)
        self.assertEqual([entry["test"] for entry in
                          paquet_auto.unittest_failures(run.output)],
                         ["tests.test_t.T.test_value"])

    def test_le_moteur_absent_ne_couvre_que_lui(self) -> None:
        # podman absent ne blanchit pas un 127 de docker
        self.assert_red(_run(H16, absent=frozenset({"podman"})))

    def test_l_absence_vaut_pour_l_executable_ecrit(self) -> None:
        # B/P2 cx1 (second tour) : `docker` (PATH) absent ne blanchit pas un
        # 127 de `/usr/bin/docker`, ni l'inverse.
        absolute = _error("setUpClass (tests.test_t.T)",
                          "subprocess.CalledProcessError: Command '['/usr/bin/docker', "
                          "'run', 'busybox', 'foo']' returned non-zero exit status 127.")
        self.assert_red(_run(absolute, absent=frozenset({"docker"})))
        self.assert_red(_run(H16, absent=frozenset({"/usr/bin/docker"})))

    def test_commande_en_chaine_et_chemin_absolu(self) -> None:
        for command, engine in (("Command 'docker run -d postgres:16'", "docker"),
                                ("Command '['/usr/bin/docker', 'run', 'x']'",
                                 "/usr/bin/docker"),
                                ("Command '['podman', 'run', 'x']'", "podman")):
            with self.subTest(command=command):
                self.assert_inconclusive(_run(_error(
                    "setUpClass (tests.test_t.T)",
                    "subprocess.CalledProcessError: %s returned non-zero "
                    "exit status 127." % command), absent=frozenset({engine})))

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
                    "non-zero exit status 127." % command),
                    absent=frozenset({"docker", "podman"})))

    def test_une_assertion_qui_cite_le_127_reste_rouge(self) -> None:
        self.assert_red(_run(_error(
            "test_value (tests.test_t.T.test_value)",
            "AssertionError: Command '['docker', 'run']' returned non-zero "
            "exit status 127."), absent=frozenset({"docker"})))

    def test_un_fail_a_cote_du_moteur_absent_reste_rouge(self) -> None:
        output = H16 + SEP + "FAIL: test_value (tests.test_t.T.test_value)\n" \
            + DASH + "Traceback (most recent call last):\n" \
            + "AssertionError: 1 != 2\n\n"
        self.assert_red(_run(output, failures=1, errors=1,
                             absent=frozenset({"docker"})))


class EngineProbeTest(unittest.TestCase):
    """La sonde `<moteur> info`, avec de faux moteurs sur un PATH privé."""

    def setUp(self) -> None:
        paquet_auto._ENGINE_PROBES.clear()
        self.addCleanup(paquet_auto._ENGINE_PROBES.clear)
        self.dir = tempfile.TemporaryDirectory(prefix="t6567-")
        self.addCleanup(self.dir.cleanup)
        self.bin = pathlib.Path(self.dir.name)

    def engine(self, name: str, body: str) -> dict:
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o755)
        return {"PATH": str(self.bin)}

    def test_shim_sans_moteur_reel_est_absent(self) -> None:
        env = self.engine("docker", 'echo "ameesh: container engine not found (docker)" >&2\nexit 127')
        self.assertTrue(paquet_auto.container_engine_absent("docker", env))

    def test_moteur_sain_n_est_pas_absent(self) -> None:
        env = self.engine("docker", "exit 0")
        self.assertFalse(paquet_auto.container_engine_absent("docker", env))

    def test_demon_arrete_ne_prouve_pas_l_absence(self) -> None:
        env = self.engine("docker", "echo 'Cannot connect to the Docker daemon' >&2\nexit 1")
        self.assertFalse(paquet_auto.container_engine_absent("docker", env))

    def test_executable_introuvable_est_absent(self) -> None:
        self.assertTrue(paquet_auto.container_engine_absent(
            "podman", {"PATH": str(self.bin)}))

    def test_la_sonde_ne_vise_que_les_moteurs_cites_par_un_127(self) -> None:
        env = self.engine("docker", "exit 127")
        self.engine("podman", "exit 127")
        run = paquet_auto.probe_engines(_run(H16), env)
        self.assertEqual(run.absent_engines, frozenset({"docker"}))
        self.assert_inconclusive_run(run)
        green = paquet_auto.probe_engines(paquet_auto.TestRun(
            label="u", modules=[], returncode=0, output="OK\n", seconds=0.1), env)
        self.assertEqual(green.absent_engines, frozenset())

    def test_moteur_explicite_sain_et_shim_du_path_absent(self) -> None:
        # B/P2 cx1 (second tour) : le PATH porte le shim sans moteur réel, mais
        # le test appelle un moteur sain par son chemin absolu, et la commande
        # du conteneur est introuvable : le 127 reste un rouge.
        env = self.engine("docker", 'echo "ameesh: container engine not found (docker)" >&2\nexit 127')
        real = pathlib.Path(self.dir.name) / "real"
        real.mkdir()
        healthy = real / "docker"
        healthy.write_text("#!/bin/sh\nexit 0\n")
        healthy.chmod(0o755)
        output = _error("setUpClass (tests.test_t.T)",
                        "subprocess.CalledProcessError: Command '['%s', 'run', "
                        "'busybox', 'foo']' returned non-zero exit status 127."
                        % healthy)
        run = paquet_auto.probe_engines(_run(output), env)
        self.assertEqual(run.absent_engines, frozenset())
        self.assertTrue(run.real_failure)
        self.assertFalse(run.inconclusive)
        # le même moteur par son nom nu, via le shim du PATH : absent prouvé
        shim = paquet_auto.probe_engines(_run(H16), env)
        self.assertEqual(shim.absent_engines, frozenset({"docker"}))
        self.assert_inconclusive_run(shim)

    def test_chemin_absolu_du_shim_est_sonde_tel_quel(self) -> None:
        self.engine("docker", "exit 127")
        shim = str(self.bin / "docker")
        self.assertTrue(paquet_auto.container_engine_absent(shim, {"PATH": "/nonexistent"}))

    def test_chemin_relatif_ne_se_prouve_jamais(self) -> None:
        # le cwd du test est inconnu : `./docker` n'est jamais sondé
        env = self.engine("docker", "exit 127")
        with mock.patch.object(paquet_auto.subprocess, "run") as run:
            self.assertFalse(paquet_auto.container_engine_absent("./docker", env))
            self.assertFalse(paquet_auto.container_engine_absent("bin/podman", env))
        run.assert_not_called()
        output = _error("setUpClass (tests.test_t.T)",
                        "subprocess.CalledProcessError: Command '['./docker', 'run', "
                        "'x']' returned non-zero exit status 127.")
        self.assertTrue(paquet_auto.probe_engines(_run(output), env).real_failure)

    def assert_inconclusive_run(self, run) -> None:
        self.assertFalse(run.real_failure)
        self.assertTrue(run.inconclusive)


if __name__ == "__main__":
    unittest.main()
