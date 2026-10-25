# SPDX-License-Identifier: AGPL-3.0-only
"""#3619 : l'extra `crypto` des venvs d'hôtes, et l'activation fermée du canal nexlink.

Le canal `nexlink` (#3536) chiffre avec `cryptography` (extra `crypto` du
`pyproject.toml`). Les installeurs ne posaient que `[psycopg]`, et les venvs
versionnés des VM (`venv-<sha7>`) n'avaient pas le module : l'activation
aurait échoué par un `ModuleNotFoundError` au premier envoi.

Témoins :
* `install-pilot.sh` pose `ameesh[psycopg,crypto]`, importe le module après
  l'installation et `--check` nomme un venv qui ne l'a pas ;
* la mise à jour des hôtes (`bascule.py preparer`) installe l'extra et
  contrôle `import cryptography` avant la bascule ; `vm.sh` et `poste.sh`
  vérifient l'import après installation ;
* `ameesh doctor` refuse, fermé, un canal `nexlink` routé sans crypto — il le
  nomme ; avec crypto, ou avec un repli `ntfy`/`slack` utilisable, il est prêt.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ameesh import cli, config, notify
from ameesh import nexlink_alerts as nx

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
SPEC = importlib.util.spec_from_file_location(
    "bascule_3619", DEPLOY / "mise-a-jour" / "bascule.py")
bascule = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bascule)

MESH_URL = "https://api-staging.nexlink.ph/api"
SECRET = "s3cr3t-mesh"


def _notify(fallback=None):
    channel = {"type": "nexlink", "url": MESH_URL, "token_env": "MESH_SECRET"}
    raw = {"routes": {"human:owner": ["nx"]}, "channels": {"nx": channel}}
    if fallback:
        channel["fallback"] = fallback
        raw["channels"][fallback] = (
            {"type": "ntfy", "url": "https://ntfy.example", "topic": "ameesh-owner"}
            if fallback == "ntfy" else {"type": "slack"})
    return raw


class ActivationTest(unittest.TestCase):
    """Le contrôle d'activation : rouge sans crypto, vert avec (ou repli)."""

    def activation(self, raw, env=None):
        return notify.nexlink_activation(SimpleNamespace(notify=raw),
                                         env={} if env is None else env)

    def test_sans_crypto_le_canal_est_refuse_et_nomme(self):
        with mock.patch.object(nx, "crypto_available", return_value=False):
            ok, lines = self.activation(_notify())
        self.assertFalse(ok)
        self.assertEqual(len(lines), 1)
        self.assertIn("nx", lines[0])
        self.assertIn("cryptography", lines[0])
        self.assertIn("ameesh[psycopg,crypto]", lines[0])
        self.assertNotIn(SECRET, lines[0])

    def test_avec_crypto_le_canal_est_pret(self):
        with mock.patch.object(nx, "crypto_available", return_value=True):
            self.assertEqual(self.activation(_notify()),
                             (True, ["channel nx ready"]))

    def test_un_repli_ntfy_utilisable_couvre_le_manque(self):
        with mock.patch.object(nx, "crypto_available", return_value=False):
            self.assertEqual(self.activation(_notify("ntfy")),
                             (True, ["channel nx ready"]))

    def test_un_repli_slack_sans_secret_ne_couvre_pas(self):
        with mock.patch.object(nx, "crypto_available", return_value=False):
            ok, lines = self.activation(_notify("slack"), env={})
        self.assertFalse(ok)
        self.assertIn("cryptography", lines[0])
        with mock.patch.object(nx, "crypto_available", return_value=False):
            ok, lines = self.activation(
                _notify("slack"),
                env={"AMEESH_SLACK_WEBHOOK": "https://hooks.slack.com/services/x"})
        self.assertTrue(ok, lines)
        self.assertIn("ready", lines[0])

    def test_sans_canal_nexlink_route_rien_a_activer(self):
        self.assertEqual(self.activation({}), (True, []))
        self.assertEqual(self.activation({"routes": {"human:owner": ["app"]}}),
                         (True, []))

    def test_configuration_invalide_refusee(self):
        ok, lines = self.activation({"routes": 3})
        self.assertFalse(ok)
        self.assertIn("configuration refused", lines[0])

    def test_role_timers_check_nomme_aussi_le_manque(self):
        from ameesh import role_timers
        with mock.patch.object(nx, "crypto_available", return_value=False):
            humans, error = role_timers.notify_reach(
                SimpleNamespace(notify=_notify()), responsible="human:owner", env={})
        self.assertEqual(humans, [])
        self.assertIn("cryptography", error)


class DoctorTest(unittest.TestCase):
    """`ameesh doctor` nomme le canal refusé, avant la base."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="ameesh-test-3619-")
        self.addCleanup(tmp.cleanup)
        self.home = os.path.join(tmp.name, "home")
        directory = os.path.join(self.home, ".config", "ameesh")
        os.makedirs(directory)
        self.path = os.path.join(directory, "config.json")
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"host": "vm-test",
                       "dsn": "postgresql://nobody@127.0.0.1:1/none",
                       "notify": _notify()}, fh)

    def doctor(self):
        cfg = config.load(env={"AMEESH_CONFIG": self.path})
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"HOME": self.home}), \
                redirect_stdout(out):
            code = cli.cmd_doctor(cfg, notify_test=False)
        return code, out.getvalue()

    def test_sans_crypto_doctor_refuse_le_canal(self):
        with mock.patch.object(nx, "crypto_available", return_value=False):
            code, out = self.doctor()
        self.assertIn("notify     : channel nx refused", out)
        self.assertIn("cryptography", out)
        self.assertIn("ameesh[psycopg,crypto]", out)
        self.assertEqual(code, 1)

    def test_avec_crypto_doctor_nomme_le_canal_pret(self):
        with mock.patch.object(nx, "crypto_available", return_value=True):
            _code, out = self.doctor()
        self.assertIn("notify     : channel nx ready", out)
        self.assertNotIn("refused", out)


class InstallerTest(unittest.TestCase):
    """Les installeurs posent l'extra, puis le contrôlent."""

    def text(self, name):
        return (DEPLOY / name).read_text(encoding="utf-8")

    def test_install_pilot_pose_psycopg_et_crypto(self):
        text = self.text("guest/install-pilot.sh")
        self.assertIn('"$CHECKOUT[psycopg,crypto]"', text)
        self.assertNotIn('"$CHECKOUT[psycopg]"', text)
        self.assertIn("import cryptography", text)
        # --check nomme le venv qui ne l'a pas, au lieu d'un KO muet
        self.assertIn("lacks cryptography", text)
        self.assertIn("ameesh[psycopg,crypto]", text)

    def test_installer_serveur_pose_psycopg_et_crypto(self):
        text = self.text("serveur/installer-serveur.sh")
        self.assertIn('"$SRC[psycopg,crypto]"', text)
        self.assertIn("import psycopg, cryptography, ameesh", text)


class BasculeTest(unittest.TestCase):
    """La mise à jour des hôtes installe et contrôle l'extra avant la bascule."""

    def test_la_preparation_installe_les_extras_de_l_hote(self):
        with mock.patch.object(bascule, "_run") as run:
            bascule._pip_install("/opt/venv", "/srv/src")
        calls = [call.args[0] for call in run.call_args_list]
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][-1], "/srv/src")
        self.assertEqual(calls[1][-1], "/srv/src[%s]" % bascule.HOST_EXTRAS)
        self.assertEqual(bascule.HOST_EXTRAS, "psycopg,crypto")

    def test_les_controles_importent_cryptography(self):
        tmp = tempfile.TemporaryDirectory(prefix="bascule-3619-")
        self.addCleanup(tmp.cleanup)
        venv = Path(tmp.name) / "venv-v1"
        (venv / "bin").mkdir(parents=True)
        python = venv / "bin" / "python"
        python.write_text("#!/bin/sh\nexit 0\n")
        python.chmod(0o755)
        ameesh = venv / "bin" / "ameesh"
        ameesh.write_text("#!%s\nimport sys\n" % python)
        ameesh.chmod(0o755)
        with mock.patch.object(bascule, "_run") as run:
            bascule._controles(str(venv))
        commands = [call.args[0] for call in run.call_args_list]
        imports = [cmd for cmd in commands if cmd[1:2] == ["-c"]]
        self.assertEqual(len(imports), 1)
        self.assertIn("cryptography", imports[0][-1])

    def test_vm_et_poste_verifient_l_import_apres_installation(self):
        for name, expected in (
                ("mise-a-jour/vm.sh", "import psycopg, cryptography, ameesh"),
                ("mise-a-jour/poste.sh",
                 "import psycopg, cryptography, ameesh.budget")):
            with self.subTest(name=name):
                self.assertIn(expected, self.text(name))

    def text(self, name):
        return (DEPLOY / name).read_text(encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
