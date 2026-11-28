# SPDX-License-Identifier: AGPL-3.0-only
"""#8380 : le PATH d'une unité de veille ne vient pas de l'appelant.

Le constat (AG1, 27/11 22:11–22:13) : `ameesh role timers --install` lancé
depuis le shell d'un tour d'agent a écrit dans `ameesh-watch-orchestrateur.
service` le PATH de ce shell. Le même fichier passait `role timers --check`
dans ce shell, puis le préflight de staging (le même CLI sous `systemd-run
--user` + EnvironmentFile, donc un autre PATH) l'a jugé « out of date » :
`verifier/ko`, staging retardé.

Témoins : une pose depuis un PATH de shell puis un contrôle depuis le PATH du
préflight ne nomment aucun écart ; une unité écrite avant ce lot (PATH de
l'appelant) reste lue comme à jour par le nouveau contrôle, pour qu'aucun hôte
ne soit dit périmé pour la seule ligne que ce lot rend canonique ; un vrai
écart d'exécution reste nommé ; deux activations sous deux PATH rendent le
même fichier.
"""
from __future__ import annotations

import dataclasses
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ameesh import role_timers, storage, watch

from .support import PgTestCase

NAME = "gov-8380"
EVERY = 1800
BIN = "/opt/ameesh/bin/ameesh"
ROOT = Path(__file__).resolve().parents[1]
#: the PATH of an agent turn: its private bins and the live versioned venv
PATH_SHELL = ("/home/ameesh/.local/state/ameesh/.menage/bin-deps:"
              "/home/ameesh/.local/share/ameesh/venv-9995fb6/bin:"
              "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
#: the PATH a `systemd-run --user` transient unit sees
PATH_PREFLIGHT = ("/home/ameesh/.local/share/ameesh/venv-9995fb6/bin:"
                  "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")


class WatchUnitPathTest(PgTestCase):
    def setUp(self) -> None:
        self.db.execute("DELETE FROM agent_mailbox")
        self.db.execute("DELETE FROM agent_watches")
        self.register(NAME, "claude")
        self.db.execute(
            "UPDATE agent_registry SET releve = 'election',"
            " candidats = ARRAY['banc','atelier'], responsible = 'human:owner',"
            " status = 'idle', status_since = now() - interval '1 hour',"
            " last_turn_at = now() - interval '1 hour' WHERE name = %s", (NAME,))
        self.db.execute(
            "UPDATE agent_registry SET host = 'banc', lease_owner = 'runner-test',"
            " lease_epoch = 7, lease_expires_at = clock_timestamp()"
            " + interval '300 seconds' WHERE name = %s", (NAME,))
        self.banc = dataclasses.replace(self.cfg, host="banc")
        self.dir = Path(tempfile.mkdtemp(prefix="ameesh-8380-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        storage.of(self.db).watches.put(NAME, EVERY, "banc", actor="human:owner")
        self.report = {"schema": "ameesh-role-timers/1", "host": "banc",
                       "role": NAME, "releve": "election",
                       "candidates": ["banc", "atelier"], "candidate": True,
                       "observation": False, "auditor": False, "timers": [],
                       "missing_projects": []}

    def install(self, path: str) -> dict:
        with mock.patch.dict("os.environ", {"PATH": path}):
            return role_timers.install(self.report, directory=self.dir,
                                       bin_path=BIN, apply=False, db=self.db)

    def check(self, path: str) -> list[str]:
        with mock.patch.dict("os.environ", {"PATH": path}):
            return role_timers.check(self.report, directory=self.dir,
                                     apply=False, bin_path=BIN, db=self.db)

    def service(self) -> Path:
        return self.dir / watch.unit_name(NAME, "service")

    def test_install_from_a_shell_then_check_under_the_preflight(self):
        # the incident: the pose and the control each render the unit from
        # their own PATH; once the PATH is canonical, neither sees an écart.
        self.install(PATH_SHELL)
        self.assertEqual(self.check(PATH_PREFLIGHT), [])
        # and the other way round (the poser is the preflight, the control a
        # shell), so the property does not depend on the order.
        self.install(PATH_PREFLIGHT)
        self.assertEqual(self.check(PATH_SHELL), [])

    def test_a_unit_written_before_the_fix_is_not_out_of_date(self):
        # Hosts carry the PATH of whoever last installed the watch. The new
        # `role timers --check` must not call that line stale: the PATH is
        # not part of the unit's contract (the exec line is absolute).
        self.install(PATH_SHELL)
        legacy = re.sub(r'^Environment="PATH=[^"]*"$',
                        'Environment="PATH=%s"' % PATH_SHELL,
                        self.service().read_text(encoding="utf-8"),
                        count=1, flags=re.M)
        self.service().write_text(legacy, encoding="utf-8")
        self.assertEqual(self.check(PATH_PREFLIGHT), [])

    def test_a_real_drift_is_still_named(self):
        # normalising PATH must not hide an execution difference.
        self.install(PATH_SHELL)
        text = self.service().read_text(encoding="utf-8")
        self.service().write_text(
            text.replace("watch send --agent %s" % NAME,
                         "watch send --agent another"), encoding="utf-8")
        problems = self.check(PATH_PREFLIGHT)
        self.assertTrue(any("out of date" in problem for problem in problems),
                        problems)

    def test_enable_renders_the_same_unit_under_two_caller_paths(self):
        # `enable` (and `arm`) took the PATH from os.environ; the fix must
        # make the rendering itself independent.
        patcher = mock.patch.object(watch.platform, "is_linux", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

        def run_(argv, **kw):
            if argv[0] == "loginctl":
                return mock.Mock(returncode=0, stdout="yes\n", stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")

        texts = []
        with mock.patch.object(watch, "_ameesh_binary", return_value=BIN), \
                mock.patch.object(watch.fil, "record"):
            for path in (PATH_SHELL, PATH_PREFLIGHT):
                env = {watch.UNIT_DIR_ENV: str(self.dir), "PATH": path}
                watch.enable(self.banc, self.db, NAME, EVERY, run=run_, env=env,
                             actor="human:owner")
                texts.append(self.service().read_text(encoding="utf-8"))
        self.assertEqual(texts[0], texts[1])
        self.assertIn('Environment="PATH=%s"' % watch.UNIT_PATH, texts[0])


class RetourReposeLesUnitesTest(unittest.TestCase):
    """Un retour arrière repose les unités avec le CLI installé (N-1) avant de
    les contrôler : sans cela, une unité rendue par N (PATH canonique) serait
    dite périmée par le `check_host` de N-1."""

    def test_retour_repose_les_unites_avant_le_controle(self):
        text = (ROOT / "deploy" / "mise-a-jour" / "vm.sh").read_text(encoding="utf-8")
        debut = text.index("retour_agents() {")
        corps = text[debut:text.index("\n}\n", debut)]
        pose = corps.index("ameesh_agents role timers --install")
        self.assertLess(corps.index("redemarrer_agents"), pose)
        self.assertLess(pose, corps.index("controler_agents"))


if __name__ == "__main__":
    unittest.main()
