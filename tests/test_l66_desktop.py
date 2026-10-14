# SPDX-License-Identifier: AGPL-3.0-only
"""L66 : la notification de bureau suit l'OS — `notify-send` sous Linux,
`osascript` sous macOS, toast Windows par `powershell.exe` sous WSL2 ; Windows
natif refuse (L67). Sans base, sauf la ligne `desktop` de `doctor
--notify-test` (schéma de test)."""
from __future__ import annotations

import base64
import contextlib
import dataclasses
import io
import json
import os
import re
import shutil
import tempfile
import unittest
from unittest import mock

from ameesh import cli, config, notify
from ameesh.platform import notify as pnotify

from .support import PgTestCase

_FAKE = '''#!/usr/bin/env python3
import json, os, sys
log = os.environ.get("AMEESH_FAKE_NOTIFY_LOG")
if log:
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"argv": sys.argv[1:], "stdin": sys.stdin.read()}) + "\\n")
code = int(os.environ.get("AMEESH_FAKE_NOTIFY_EXIT") or 0)
if code:
    print("faux backend : echec simule", file=sys.stderr)
sys.exit(code)
'''


class _Banc(unittest.TestCase):
    """Un bac à sable disque, un faux backend qui journalise argv et stdin."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-l66-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.empty = os.path.join(self.tmp, "vide")
        os.makedirs(self.empty, exist_ok=True)
        self.log = os.path.join(self.tmp, "backend.log")
        self.env = {"PATH": os.environ.get("PATH", ""),
                    "AMEESH_FAKE_NOTIFY_LOG": self.log}

    def fake(self, name: str) -> str:
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(_FAKE)
        os.chmod(path, 0o755)
        return path

    def entries(self) -> list:
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def toast_xml(self, script: str) -> str:
        """Le XML du toast, decode depuis le base64 porte par le script."""
        match = re.search(r"FromBase64String\('([A-Za-z0-9+/=]+)'\)", script)
        self.assertIsNotNone(match, "le script ne porte pas de XML en base64")
        return base64.b64decode(match.group(1)).decode("utf-8")


class PlatformNotifyTest(_Banc):
    def test_macos_osascript(self):
        env = dict(self.env, AMEESH_OSASCRIPT_BIN=self.fake("osascript"))
        backend = pnotify.send('Titre "x"', "corps\nligne", urgency="critical",
                               env=env, system="macos")
        self.assertEqual(backend, "osascript")
        entry = self.entries()[0]
        self.assertEqual(entry["argv"][0], "-e")
        self.assertEqual(
            entry["argv"][1],
            'display notification "corps\\nligne" with title "Titre \\"x\\""'
            ' sound name "Basso"')
        self.assertEqual(entry["stdin"], "")

    def test_macos_normal_sans_son(self):
        env = dict(self.env, AMEESH_OSASCRIPT_BIN=self.fake("osascript"))
        pnotify.send("t", "c", env=env, system="macos")
        self.assertNotIn("sound name", self.entries()[0]["argv"][1])

    def test_macos_sans_osascript(self):
        env = dict(self.env, AMEESH_OSASCRIPT_BIN=os.path.join(self.tmp, "absent"))
        with self.assertRaises(pnotify.NotifyError) as ctx:
            pnotify.send("t", "c", env=env, system="macos")
        self.assertTrue(ctx.exception.permanent)
        self.assertIn("osascript not found", str(ctx.exception))

    def test_wsl_toast(self):
        env = dict(self.env, AMEESH_POWERSHELL_BIN=self.fake("powershell.exe"))
        backend = pnotify.send("Titre & <x>", "corps 'apostrophe'", urgency="low",
                               env=env, system="linux", wsl=True)
        self.assertEqual(backend, "powershell")
        entry = self.entries()[0]
        self.assertEqual(entry["argv"], ["-NoProfile", "-NonInteractive",
                                         "-Command", "-"])
        script = entry["stdin"]
        self.assertTrue(script.isascii())
        self.assertNotIn("\n", script)
        xml = self.toast_xml(script)
        self.assertIn("<text>Titre &amp; &lt;x&gt;</text>", xml)
        self.assertIn("<text>corps 'apostrophe'</text>", xml)
        self.assertIn('<audio silent="true"/>', xml)
        self.assertIn("CreateToastNotifier", script)

    def test_wsl_toast_texte_hostile_hors_du_script(self):
        """B2 : aucun texte de l'appelant en clair, script ASCII sur une ligne."""
        env = dict(self.env, AMEESH_POWERSHELL_BIN=self.fake("powershell.exe"))
        titre = "alerte ’ ; é —"
        corps = "ligne 1\nligne 2 ’; Remove-Item C:\\ -Recurse"
        pnotify.send(titre, corps, env=env, system="linux", wsl=True)
        script = self.entries()[0]["stdin"]
        self.assertTrue(script.isascii())
        self.assertNotIn("\n", script)
        self.assertNotIn("alerte", script)
        self.assertNotIn("Remove-Item", script)
        xml = self.toast_xml(script)
        self.assertIn("<text>%s</text>" % titre, xml)
        self.assertIn("<text>%s</text>" % corps, xml)

    def test_wsl_toast_urgent(self):
        env = dict(self.env, AMEESH_POWERSHELL_BIN=self.fake("powershell.exe"))
        pnotify.send("t", "c", urgency="critical", env=env, system="linux", wsl=True)
        xml = self.toast_xml(self.entries()[0]["stdin"])
        self.assertIn('scenario="urgent"', xml)
        self.assertNotIn("silent", xml)

    def test_wsl_sans_powershell(self):
        env = {"PATH": self.empty}  # aucun backend dans ce PATH
        with self.assertRaises(pnotify.NotifyError) as ctx:
            pnotify.send("t", "c", env=env, system="linux", wsl=True)
        self.assertTrue(ctx.exception.permanent)
        self.assertIn("powershell.exe not found", str(ctx.exception))

    def test_windows_natif_refuse_jusqu_a_l67(self):
        with self.assertRaises(pnotify.NotifyError) as ctx:
            pnotify.send("t", "c", env=self.env, system="windows")
        self.assertTrue(ctx.exception.permanent)
        self.assertIn("L67", str(ctx.exception))

    def test_wsl_detecte(self):
        self.assertTrue(pnotify.is_wsl({"WSL_DISTRO_NAME": "Ubuntu"}))
        self.assertTrue(pnotify.is_wsl({"WSL_INTEROP": "/run/WSL/7_interop"}))
        self.assertIsInstance(pnotify.is_wsl({}), bool)

    def test_surcharge_explicite_sans_repli(self):
        env = {"PATH": os.environ.get("PATH", ""),
               "AMEESH_NOTIFY_SEND_BIN": os.path.join(self.tmp, "absent")}
        self.assertIsNone(pnotify.notify_send_bin(env))

    def test_describe_par_os(self):
        notify_send = self.fake("notify-send")
        self.assertEqual(
            pnotify.describe(env=dict(self.env, AMEESH_NOTIFY_SEND_BIN=notify_send),
                             system="linux", wsl=False),
            ("notify-send", notify_send, None))
        backend, binary, problem = pnotify.describe(env={"PATH": self.empty},
                                                    system="linux", wsl=False)
        self.assertEqual((backend, binary), ("notify-send", None))
        self.assertIn("notify-send not found", problem)
        osascript = self.fake("osascript")
        self.assertEqual(
            pnotify.describe(env=dict(self.env, AMEESH_OSASCRIPT_BIN=osascript),
                             system="macos"),
            ("osascript", osascript, None))
        powershell = self.fake("powershell.exe")
        self.assertEqual(
            pnotify.describe(env=dict(self.env, AMEESH_POWERSHELL_BIN=powershell),
                             system="linux", wsl=True),
            ("powershell", powershell, None))
        backend, binary, problem = pnotify.describe(env=self.env, system="windows")
        self.assertEqual((backend, binary), (None, None))
        self.assertIn("L67", problem)


class CanalDesktopTest(_Banc):
    """Le canal `desktop` de `notify` passe par la couche plateforme (L66)."""

    def test_le_canal_suit_le_backend(self):
        env = dict(self.env, AMEESH_NOTIFY_SEND_BIN=self.fake("notify-send"))
        notify.Sender(env=env).send(notify.Channel("desktop", "desktop"),
                                    notify.Message("titre", "corps", urgent=True))
        self.assertIn("--urgency=critical", self.entries()[0]["argv"])

    def test_erreur_permanente_du_backend(self):
        env = dict(self.env, AMEESH_NOTIFY_SEND_BIN=os.path.join(self.tmp, "absent"))
        with self.assertRaises(notify.ChannelError) as ctx:
            notify.Sender(env=env).send(notify.Channel("desktop", "desktop"),
                                        notify.Message("t", "c"))
        self.assertTrue(ctx.exception.permanent)
        self.assertIn("notify-send not found", str(ctx.exception))


class DesktopProbeTest(_Banc):
    """`doctor --notify-test` : la ligne `desktop`, sans base."""

    def cfg(self, **kw):
        return dataclasses.replace(config.load(), host="hote-test",
                                   state_dir=tempfile.gettempdir(), **kw)

    def test_sans_route_desktop(self):
        env = dict(self.env, AMEESH_NOTIFY_SEND_BIN=self.fake("notify-send"))
        ok, line = notify.desktop_probe(self.cfg(), env=env)
        self.assertTrue(ok)
        self.assertIn("notify-send", line)
        self.assertIn("no `desktop` route configured", line)
        self.assertEqual(self.entries(), [])

    def test_sans_route_desktop_et_sans_binaire(self):
        """B1 : pas de route desktop, pas de binaire -> ok, jamais KO."""
        env = {"PATH": self.empty}
        ok, line = notify.desktop_probe(self.cfg(), env=env)
        self.assertTrue(ok)
        self.assertIn("notify-send not found", line)
        self.assertIn("no `desktop` route configured, nothing sent", line)

    def test_route_desktop_envoie_le_test(self):
        cfg = self.cfg(notify={"default_human": "human:test",
                               "default": ["desktop"]})
        env = dict(self.env, AMEESH_NOTIFY_SEND_BIN=self.fake("notify-send"))
        ok, line = notify.desktop_probe(cfg, env=env)
        self.assertTrue(ok)
        self.assertIn("test displayed to human:test", line)
        self.assertEqual(len(self.entries()), 1)

    def test_route_desktop_sans_binaire(self):
        cfg = self.cfg(notify={"default_human": "human:test", "default": ["desktop"]})
        env = {"PATH": self.empty}
        ok, line = notify.desktop_probe(cfg, env=env)
        self.assertFalse(ok)
        self.assertIn("KO", line)
        self.assertIn("notify-send not found", line)

    def test_macos_et_wsl_dits(self):
        with mock.patch.object(pnotify, "SYSTEM", "macos"):
            env = dict(self.env, AMEESH_OSASCRIPT_BIN=self.fake("osascript"))
            ok, line = notify.desktop_probe(self.cfg(), env=env)
            self.assertTrue(ok)
            self.assertIn("osascript (macos)", line)
        with mock.patch.object(pnotify, "SYSTEM", "linux"), \
                mock.patch.object(pnotify, "is_wsl", return_value=True):
            env = dict(self.env, AMEESH_POWERSHELL_BIN=self.fake("powershell.exe"))
            ok, line = notify.desktop_probe(self.cfg(), env=env)
            self.assertTrue(ok)
            self.assertIn("powershell (WSL2)", line)


class DoctorDesktopTest(PgTestCase):
    """`cmd_doctor(notify_test=True)` publie la ligne `desktop` (L66)."""

    def test_doctor_publie_la_ligne_desktop(self):
        out = io.StringIO()
        with mock.patch.object(notify, "desktop_probe",
                               return_value=(False, "KO — backend absent")) as probe, \
                contextlib.redirect_stdout(out):
            code = cli.cmd_doctor(self.cfg, notify_test=True)
        self.assertIn("desktop    : KO — backend absent", out.getvalue())
        self.assertEqual(code, 1)
        probe.assert_called_once()

    def test_doctor_ne_ko_pas_sans_route_desktop(self):
        """B1 : backend absent et aucune route desktop -> la ligne `desktop` est OK.

        Le binaire est rendu introuvable par surcharge explicite (et non par un
        PATH vide) : le docteur a besoin de `psql` dans le PATH quand le banc
        tourne sur ce pilote."""
        absent = os.path.join(self.tmp, "notify-send-absent")
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"AMEESH_NOTIFY_SEND_BIN": absent}), \
                mock.patch.object(pnotify, "SYSTEM", "linux"), \
                mock.patch.object(pnotify, "is_wsl", return_value=False), \
                contextlib.redirect_stdout(out):
            cli.cmd_doctor(self.cfg, notify_test=True)
        lignes = [l for l in out.getvalue().splitlines() if l.startswith("desktop")]
        self.assertEqual(len(lignes), 1, out.getvalue())
        self.assertNotIn("KO", lignes[0])
        self.assertIn("no `desktop` route configured", lignes[0])
        self.assertIn("notify-send not found", lignes[0])


if __name__ == "__main__":
    unittest.main()
