# SPDX-License-Identifier: AGPL-3.0-only
"""#8390 : le binaire d'une unité de réveil ne vient pas du PATH de l'appelant.

#8380 a rendu canonique le `Environment=PATH=` des unités de veille et de
réveil. Restait le binaire écrit dans `ExecStart` : `_ameesh_binary` prenait le
premier `ameesh` du `PATH` de l'appelant, donc un réveil armé depuis un shell
qui préfixe un répertoire privé (le `.menage/bin` d'un agent, un arbre
temporaire) écrivait ce répertoire dans son unité. Deux shells rendaient deux
unités différentes pour le même réveil.

Témoins : le lanceur stable `~/.local/bin/ameesh`, que `UNIT_PATH` nomme, est
préféré au binaire du `PATH` ; un réveil armé sous `PATH=/tmp/x:…` ne contient
pas `/tmp/x` ; deux `PATH` différents rendent le même `ExecStart` ; `env` de la
ligne d'exécution vient de `/usr/bin/env`, pas du `PATH`.

#8458 : sans lanceur local, le choix est REFUSÉ — jamais de repli sur le
`PATH` de l'appelant (deux shells auraient rendu deux `ExecStart`, et un
chemin privé peut disparaître avant l'échéance). Le repli de #4754 reste dans
`stable_binary` pour la réécriture d'une unité existante (`repin_units`).
"""
from __future__ import annotations

import dataclasses
import os
import shutil
import tempfile
import unittest
from unittest import mock

from ameesh import watch

from .support import PgTestCase

AGENT = "w8390"
SLUG = "deadline"


def _exe(path: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\n")
    os.chmod(path, 0o755)
    return path


class StableLauncherTest(unittest.TestCase):
    """Le rendu d'une unité ne dépend pas du premier `ameesh` du `PATH`."""

    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="t8390-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home = os.path.join(self.root, "home")
        self.stable = _exe(os.path.join(self.home, ".local", "bin", "ameesh"))
        #: the private directory an agent's turn puts first in its PATH.
        self.private = os.path.join(self.root, "private")
        _exe(os.path.join(self.private, "ameesh"))
        _exe(os.path.join(self.private, "env"))

    def test_the_stable_launcher_wins_over_the_caller_path(self):
        env = {"HOME": self.home, "PATH": self.private + ":/usr/bin:/bin"}
        self.assertEqual(watch._ameesh_binary(env), self.stable)

    def test_two_paths_render_the_same_exec_start(self):
        services = []
        for path in (self.private + ":/usr/bin:/bin", "/usr/bin:/bin"):
            env = {"HOME": self.home, "PATH": path}
            files = watch.wake_units(AGENT, SLUG, 1800,
                                     watch._ameesh_binary(env), self.root)
            services.append(files[watch.wake_name(AGENT, SLUG, "service")])
        self.assertEqual(services[0], services[1])
        self.assertNotIn(self.private, services[0])
        self.assertIn('"%s" watch fire --agent %s --wake %s'
                      % (self.stable, AGENT, SLUG), services[0])

    def test_without_the_launcher_the_choice_is_refused(self):
        # #8458: no fallback to the caller's PATH, not even for a versioned
        # venv: the unit would depend on who armed it.
        bare = os.path.join(self.root, "bare")
        os.makedirs(bare)
        base = os.path.join(self.root, "share", "ameesh")
        versioned = _exe(os.path.join(base, "venv-52d1e28", "bin", "ameesh"))
        env = {"HOME": bare, "PATH": os.path.dirname(versioned) + ":/usr/bin:/bin"}
        with self.assertRaises(watch.WatchError) as raised:
            watch._ameesh_binary(env)
        self.assertIn(os.path.join(bare, ".local", "bin", "ameesh"),
                      str(raised.exception))
        self.assertIn("PATH", str(raised.exception))

    def test_two_paths_without_the_launcher_render_no_exec_start(self):
        # #8458: two shells can no longer render two different ExecStart for
        # the same wake — without the stable launcher both choices are refused
        # before a unit is written.
        bare = os.path.join(self.root, "bare")
        os.makedirs(bare)
        for path in (self.private + ":/usr/bin:/bin", "/usr/bin:/bin"):
            with self.assertRaises(watch.WatchError):
                watch._ameesh_binary({"HOME": bare, "PATH": path})

    def test_stable_binary_still_remaps_a_versioned_venv(self):
        # #4754 is preserved where it belongs: rewriting an existing unit's
        # exec line (`repin_units`), not choosing a binary to arm a new one.
        bare = os.path.join(self.root, "bare")
        os.makedirs(bare)
        base = os.path.join(self.root, "share", "ameesh")
        versioned = _exe(os.path.join(base, "venv-52d1e28", "bin", "ameesh"))
        live = _exe(os.path.join(base, "venv-courant", "bin", "ameesh"))
        self.assertEqual(watch.stable_binary(versioned, home=bare), live)

    @unittest.skipUnless(os.access("/usr/bin/env", os.X_OK), "no /usr/bin/env")
    def test_env_is_not_taken_from_the_caller_path(self):
        with mock.patch.dict(os.environ,
                             {"PATH": self.private + ":/usr/bin:/bin"}):
            self.assertEqual(watch._env_binary(), "/usr/bin/env")


class ArmedWakeTest(PgTestCase):
    """`arm` écrit le lanceur stable, quel que soit le PATH qui l'appelle."""

    def setUp(self) -> None:
        self.register(AGENT, "claude")
        self.root = tempfile.mkdtemp(prefix="ameesh-8390-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.units = os.path.join(self.root, "units")
        self.home = os.path.join(self.root, "home")
        self.stable = _exe(os.path.join(self.home, ".local", "bin", "ameesh"))
        self.private = os.path.join(self.root, "private")
        _exe(os.path.join(self.private, "ameesh"))
        _exe(os.path.join(self.private, "env"))
        self.here = dataclasses.replace(self.cfg, host="laptop")
        patcher = mock.patch.object(watch.platform, "is_linux", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.calls: list[list[str]] = []
        self.unit_env = "PATH=/usr/bin:/bin AGENT_MAIL_NAME=%s" % AGENT
        self.exec_start = ("{ path=/usr/bin/env ; argv[]=/usr/bin/env "
                           "AGENT_MAIL_NAME=%s %s watch fire --agent %s --wake %s ; "
                           "ignore_errors=no ; status=0/0 }"
                           % (AGENT, self.stable, AGENT, SLUG))

    def run_(self, argv, **kw):
        self.calls.append(argv[2:])
        if argv[2:4] == ["show", "-p"]:
            prop = argv[4]
            if prop == "Environment":
                return mock.Mock(returncode=0, stdout=self.unit_env, stderr="")
            if prop == "ExecStart":
                return mock.Mock(returncode=0, stdout=self.exec_start, stderr="")
            if prop == "ActiveState":
                return mock.Mock(returncode=0, stdout="active\n", stderr="")
        return mock.Mock(returncode=0, stdout="", stderr="")

    def arm_env(self, path: str) -> dict:
        return {watch.UNIT_DIR_ENV: self.units, "PATH": path, "HOME": self.home,
                "AMEESH_DSN": "postgres://u:secret@db/x", "AMEESH_HOST": "laptop"}

    def arm(self, path: str) -> str:
        return watch.arm(self.here, self.db, AGENT, SLUG, 1800,
                         "REVEIL #8390", run=self.run_, env=self.arm_env(path),
                         actor="human:owner")

    def service(self) -> str:
        with open(os.path.join(self.units,
                               watch.wake_name(AGENT, SLUG, "service")),
                  encoding="utf-8") as fh:
            return fh.read()

    def test_a_wake_without_the_stable_launcher_is_refused_and_writes_nothing(self):
        # #8458: the refusal happens before any unit file is written.
        bare = os.path.join(self.root, "bare")
        os.makedirs(bare)
        env = self.arm_env(self.private + ":/usr/bin:/bin")
        env["HOME"] = bare
        with self.assertRaises(watch.WatchError) as raised:
            watch.arm(self.here, self.db, AGENT, SLUG, 1800, "REVEIL #8458",
                      run=self.run_, env=env, actor="human:owner")
        self.assertIn("launcher", str(raised.exception))
        self.assertFalse(os.path.exists(os.path.join(
            self.units, watch.wake_name(AGENT, SLUG, "service"))))

    def test_a_wake_armed_from_two_paths_has_one_exec_start(self):
        # `arm` used to read the process PATH through `_ameesh_binary`, not the
        # environment it was given: patch both, as a shell would differ.
        with mock.patch.dict(os.environ,
                             {"PATH": self.private + ":/usr/bin:/bin"}):
            self.arm(self.private + ":/usr/bin:/bin")
        first = self.service()
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}):
            self.arm("/usr/bin:/bin")
        second = self.service()
        self.assertEqual(first, second)
        self.assertNotIn(self.private, first)
        self.assertIn('"%s" watch fire --agent %s --wake %s'
                      % (self.stable, AGENT, SLUG), first)
        self.assertIn('Environment="PATH=%s"' % watch.UNIT_PATH, first)
