# SPDX-License-Identifier: AGPL-3.0-only
"""L65 : services launchd macOS (`ameesh service`) et trousseau des comptes.

Aucune base, aucun vrai launchd : les plists sont écrits dans un dossier
temporaire (`AMEESH_LAUNCHD_DIR`) et `launchctl` est injecté. Le job CI macOS
exécute aussi ce module : il y vérifie le vrai binaire `security` sur une
entrée absente, sans lire de secret.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import plistlib
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from ameesh import accounts, platform
from ameesh import service as service_cli
from ameesh.platform import service as launchd


def _result(code: int = 0, out: str = "", err: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], code, out, err)


def _fake_run(codes: dict | None = None):
    """Un `run` qui note ses appels et rend les codes par action launchctl.

    `codes` : `{"bootstrap": 1, "print:out": "state = running…", …}` ; toute
    action absente réussit.
    """
    codes = codes or {}
    calls: list[list[str]] = []

    def run(argv, **_kwargs):
        calls.append(list(argv))
        if argv and argv[0] == "launchctl" and len(argv) > 1:
            action = argv[1]
            return _result(codes.get(action, 0),
                           codes.get(action + ":out", ""),
                           codes.get(action + ":err", ""))
        return _result(0)

    run.calls = calls
    return run


class ServicePayloadTest(unittest.TestCase):
    """Le contenu des plists, sans toucher au système."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ameesh-l65-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.env = {
            "HOME": self.tmp,
            "USER": "tester",
            "PATH": "/usr/bin:/bin",
            launchd.LAUNCHD_DIR_ENV: os.path.join(self.tmp, "LaunchAgents"),
            launchd.LOG_DIR_ENV: os.path.join(self.tmp, "Logs"),
        }

    def test_runner_et_notify_par_defaut(self):
        selected = service_cli.services([], hostname="mac")
        self.assertEqual([s.label for s in selected],
                         ["ameesh.runner", "ameesh.notify"])
        runner = launchd.payload(selected[0], "/opt/ameesh", self.env)
        notify = launchd.payload(selected[1], "/opt/ameesh", self.env)
        self.assertEqual(runner["Label"], "ameesh.runner")
        self.assertEqual(notify["Label"], "ameesh.notify")
        self.assertEqual(runner["ProgramArguments"][:2], ["/bin/sh", "-c"])
        self.assertIn("exec /opt/ameesh run --poll 5", runner["ProgramArguments"][2])
        self.assertIn("exec /opt/ameesh notify --interval 30",
                      notify["ProgramArguments"][2])
        # le fichier de l'hôte est sourcé ; le plist ne porte aucun secret
        self.assertIn(os.path.join(self.tmp, ".config/ameesh/env"),
                      runner["ProgramArguments"][2])
        self.assertNotIn("AMEESH_DSN", str(runner))
        self.assertEqual(runner["KeepAlive"], {"SuccessfulExit": False})
        self.assertTrue(runner["RunAtLoad"])
        self.assertEqual(runner["EnvironmentVariables"]["HOME"], self.tmp)
        self.assertIn(os.path.join(self.tmp, ".local/bin"),
                      runner["EnvironmentVariables"]["PATH"])
        self.assertEqual(runner["StandardOutPath"],
                         os.path.join(self.tmp, "Logs", "runner.log"))
        self.assertEqual(runner["StandardErrorPath"],
                         os.path.join(self.tmp, "Logs", "runner.err"))
        self.assertEqual(runner["ThrottleInterval"], 10)

    def test_un_executeur_par_agent(self):
        selected = service_cli.services(["coord", "review"], hostname="mac")
        self.assertEqual([s.label for s in selected],
                         ["ameesh.runner.coord", "ameesh.runner.review",
                          "ameesh.notify"])
        coord = launchd.payload(selected[0], "/opt/ameesh", self.env)
        self.assertIn("run --agents coord --runner-id runner-coord@mac",
                      coord["ProgramArguments"][2])
        self.assertEqual(selected[0].throttle, 30)
        self.assertEqual(launchd.plist_name(selected[0]),
                         "ameesh-runner-coord.plist")

    def test_le_plist_est_un_xml_valide(self):
        service = service_cli.services([], hostname="mac")[0]
        payload = launchd.payload(service, "/opt/ameesh", self.env)
        self.assertEqual(plistlib.loads(launchd.plist_bytes(service, "/opt/ameesh",
                                                             self.env)), payload)

    def test_aucun_secret_de_l_environnement_dans_le_plist(self):
        env = dict(self.env, AMEESH_DSN="postgresql://ameesh:secret@db/x",
                   PGPASSWORD="secret", GH_TOKEN="secret")
        service = service_cli.services([], hostname="mac")[0]
        texte = launchd.plist_bytes(service, "/opt/ameesh", env).decode("utf-8")
        self.assertNotIn("secret", texte)
        self.assertNotIn("AMEESH_DSN", texte)

    def test_les_chemins_sont_cites(self):
        home = os.path.join(self.tmp, "un dossier")
        env = dict(self.env, HOME=home)
        service = service_cli.services([], hostname="mac")[0]
        script = launchd.payload(service, "/opt/ame esh/ameesh", env)[
            "ProgramArguments"][2]
        self.assertIn("exec '/opt/ame esh/ameesh' run --poll 5", script)
        self.assertIn("'%s/.config/ameesh/env'" % home, script)

    def test_le_dry_run_n_ecrit_rien(self):
        sortie = io.StringIO()
        with contextlib.redirect_stdout(sortie):
            code = service_cli.main(["install", "--dry-run", "--agent", "coord,a"],
                                    env=self.env, run=_fake_run())
        self.assertEqual(code, 0)
        texte = sortie.getvalue()
        self.assertIn("ameesh.runner.coord", texte)
        self.assertIn("ameesh.runner.a", texte)
        self.assertIn("ameesh.notify", texte)
        self.assertIn("<plist", texte)
        self.assertFalse(os.path.exists(self.env[launchd.LAUNCHD_DIR_ENV]))

    def test_le_binaire_peut_etre_force(self):
        env = dict(self.env, AMEESH_BIN="/opt/ame/bin/ameesh")
        self.assertEqual(service_cli._ameesh_binary(env), "/opt/ame/bin/ameesh")

    def test_un_nom_d_agent_invalide_est_refuse(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = service_cli.main(["install", "--dry-run", "--agent", "../x"],
                                    env=self.env, run=_fake_run())
        self.assertEqual(code, 1)
        self.assertIn("invalid agent name", err.getvalue())

    def test_rien_a_faire(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = service_cli.main(["install", "--no-runner", "--no-notify"],
                                    env=self.env, run=_fake_run())
        self.assertEqual(code, 1)
        self.assertIn("nothing to do", err.getvalue())


class ServiceInstallTest(unittest.TestCase):
    """Écriture, chargement launchd et retrait, avec un `launchctl` injecté."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ameesh-l65-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.env = {
            "HOME": self.tmp,
            "USER": "tester",
            "PATH": "/usr/bin:/bin",
            launchd.LAUNCHD_DIR_ENV: os.path.join(self.tmp, "LaunchAgents"),
            launchd.LOG_DIR_ENV: os.path.join(self.tmp, "Logs"),
        }
        self.path = os.path.join(self.env[launchd.LAUNCHD_DIR_ENV],
                                 "ameesh-runner.plist")

    def _install(self, run=None):
        run = run or _fake_run()
        with mock.patch.object(platform, "SYSTEM", "macos"), \
                contextlib.redirect_stdout(io.StringIO()):
            code = service_cli.main(["install", "--no-notify"],
                                    env=self.env, run=run)
        return code, run

    def test_installe_et_charge_le_service(self):
        code, run = self._install()
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(self.path))
        with open(self.path, "rb") as handle:
            document = plistlib.load(handle)
        self.assertEqual(document["Label"], "ameesh.runner")
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(os.stat(self.env[launchd.LOG_DIR_ENV]).st_mode),
                         0o700)
        actions = [call[1] for call in run.calls]
        self.assertEqual(actions, ["bootout", "bootstrap"])
        self.assertEqual(run.calls[0],
                         ["launchctl", "bootout", "gui/%d/ameesh.runner" % os.getuid()])
        self.assertEqual(run.calls[1][:3],
                         ["launchctl", "bootstrap", "gui/%d" % os.getuid()])
        self.assertEqual(run.calls[1][3], self.path)

    def test_recharge_un_service_deja_charge(self):
        run = _fake_run()
        self.assertEqual(self._install(run)[0], 0)
        self.assertEqual(self._install(run)[0], 0)
        self.assertEqual([call[1] for call in run.calls],
                         ["bootout", "bootstrap", "bootout", "bootstrap"])

    def test_repli_sur_load_quand_bootstrap_echoue(self):
        run = _fake_run({"bootstrap": 1, "load": 0})
        code, run = self._install(run)
        self.assertEqual(code, 0)
        self.assertIn(["launchctl", "load", "-w", self.path], run.calls)

    def test_echec_des_deux_chemins(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code, run = self._install(_fake_run(
                {"bootstrap": 1, "load": 1, "bootstrap:err": "Bootstrap failed: 5"}))
        self.assertEqual(code, 1)
        self.assertIn("Bootstrap failed: 5", err.getvalue())
        # le plist reste écrit : l'utilisateur peut recharger à la main
        self.assertTrue(os.path.isfile(self.path))
        self.assertIn(["launchctl", "load", "-w", self.path], run.calls)

    def test_desinstalle_et_decharge(self):
        run = _fake_run()
        self.assertEqual(self._install(run)[0], 0)
        out = io.StringIO()
        with mock.patch.object(platform, "SYSTEM", "macos"), \
                contextlib.redirect_stdout(out):
            code = service_cli.main(["uninstall", "--no-notify"],
                                    env=self.env, run=run)
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(self.path))
        self.assertIn("removed", out.getvalue())
        self.assertIn(["launchctl", "bootout", "gui/%d/ameesh.runner" % os.getuid()],
                      run.calls)

    def test_desinstalle_un_service_absent(self):
        out = io.StringIO()
        with mock.patch.object(platform, "SYSTEM", "macos"), \
                contextlib.redirect_stdout(out):
            code = service_cli.main(["uninstall", "--no-notify"],
                                    env=self.env, run=_fake_run())
        self.assertEqual(code, 0)
        self.assertIn("absent", out.getvalue())

    def test_refuse_de_supprimer_un_plist_etranger(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "wb") as handle:
            plistlib.dump({"Label": "autre.service"}, handle)
        err = io.StringIO()
        with mock.patch.object(platform, "SYSTEM", "macos"), \
                contextlib.redirect_stderr(err):
            code = service_cli.main(["uninstall", "--no-notify"],
                                    env=self.env, run=_fake_run())
        self.assertEqual(code, 1)
        self.assertIn("label mismatch", err.getvalue())
        self.assertTrue(os.path.isfile(self.path))

    def test_status_lit_launchd(self):
        self._install(_fake_run())
        run = _fake_run({"print:out": "state = running\n\tpid = 4321\n"})
        out = io.StringIO()
        with mock.patch.object(platform, "SYSTEM", "macos"), \
                contextlib.redirect_stdout(out):
            code = service_cli.main(["status", "--no-notify"],
                                    env=self.env, run=run)
        self.assertEqual(code, 0)
        self.assertIn("running, pid 4321", out.getvalue())
        self.assertIn(["launchctl", "print", "gui/%d/ameesh.runner" % os.getuid()],
                      run.calls)

    def test_status_json(self):
        self._install(_fake_run())
        run = _fake_run({"print:out": "state = running\npid = 4321\n"})
        out = io.StringIO()
        with mock.patch.object(platform, "SYSTEM", "macos"), \
                contextlib.redirect_stdout(out):
            code = service_cli.main(["status", "--no-notify", "--json"],
                                    env=self.env, run=run)
        self.assertEqual(code, 0)
        document = json.loads(out.getvalue())
        self.assertEqual(document["schema"], "ameesh-services/1")
        self.assertEqual(document["system"], "macos")
        self.assertEqual(document["services"][0]["label"], "ameesh.runner")
        self.assertTrue(document["services"][0]["loaded"])
        self.assertEqual(document["services"][0]["pid"], 4321)

    def test_status_d_un_service_absent(self):
        out = io.StringIO()
        with mock.patch.object(platform, "SYSTEM", "macos"), \
                contextlib.redirect_stdout(out):
            code = service_cli.main(["status", "--no-notify"],
                                    env=self.env, run=_fake_run())
        self.assertEqual(code, 0)
        self.assertIn("not installed", out.getvalue())

    def test_linux_refuse(self):
        err = io.StringIO()
        with mock.patch.object(platform, "SYSTEM", "linux"), \
                contextlib.redirect_stderr(err):
            code = service_cli.main(["install", "--no-notify"],
                                    env=self.env, run=_fake_run())
        self.assertEqual(code, 1)
        self.assertIn("deploy/systemd", err.getvalue())
        self.assertFalse(os.path.exists(self.path))

    def test_dry_run_fonctionne_hors_macos(self):
        out = io.StringIO()
        with mock.patch.object(platform, "SYSTEM", "linux"), \
                contextlib.redirect_stdout(out):
            code = service_cli.main(["install", "--dry-run", "--no-notify"],
                                    env=self.env, run=_fake_run())
        self.assertEqual(code, 0)
        self.assertIn("ameesh.runner", out.getvalue())
        self.assertFalse(os.path.exists(self.path))


class KeychainTest(unittest.TestCase):
    """La vérification des identifiants macOS (L65) : présence seulement."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ameesh-l65-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.directory = os.path.join(self.tmp, "claude-2")
        os.makedirs(self.directory, mode=0o700)

    def _profile(self, **extra):
        raw = {"name": "secondaire", "path": self.directory, **extra}
        return accounts.parse({"claude": [raw]})["claude"][0]

    def _digest(self, path: str) -> str:
        return hashlib.sha256(path.encode("utf-8")).hexdigest()[:8]

    def test_service_du_compte_a_dossier(self):
        profile = self._profile()
        self.assertEqual(accounts.keychain_services(profile),
                         ["Claude Code-credentials-" + self._digest(self.directory)])

    def test_service_du_compte_par_defaut(self):
        profile = accounts.parse({"claude": [{"name": "primaire"}]})["claude"][0]
        self.assertEqual(accounts.keychain_services(profile),
                         ["Claude Code-credentials"])

    def test_profil_synthetique_avec_dossier_herite(self):
        profile = accounts.Profile(harness="claude", name="(default)")
        inherited = os.path.join(self.tmp, "claude-herite")
        environ = {"HOME": self.tmp, "CLAUDE_CONFIG_DIR": inherited}
        self.assertEqual(accounts.keychain_services(profile, environ),
                         ["Claude Code-credentials",
                          "Claude Code-credentials-" + self._digest(inherited)])

    def test_le_trousseau_est_lu_sans_le_secret(self):
        recorded = {}

        def run(argv, **kwargs):
            recorded["argv"] = list(argv)
            return _result(0)

        ok = accounts._keychain_item("Claude Code-credentials", "moi", run=run,
                                     which=lambda name: "/usr/bin/security")
        self.assertTrue(ok)
        self.assertIn("find-generic-password", recorded["argv"])
        self.assertIn("Claude Code-credentials", recorded["argv"])
        self.assertIn("moi", recorded["argv"])
        self.assertNotIn("-w", recorded["argv"])

    def test_compte_macos_verifie_dans_le_trousseau(self):
        profile = self._profile()
        with mock.patch.object(platform, "SYSTEM", "macos"), \
                mock.patch.object(accounts, "_keychain_available", return_value=True), \
                mock.patch.object(accounts, "_keychain_item", return_value=True) as item:
            self.assertEqual(accounts.check(profile), [])
        self.assertEqual(item.call_args[0][0],
                         "Claude Code-credentials-" + self._digest(self.directory))
        expected = os.environ.get("USER") or accounts._keychain_account()
        self.assertEqual(item.call_args[0][1], expected)

    def test_compte_macos_absent_du_trousseau(self):
        profile = self._profile()
        with mock.patch.object(platform, "SYSTEM", "macos"), \
                mock.patch.object(accounts, "_keychain_available", return_value=True), \
                mock.patch.object(accounts, "_keychain_item", return_value=False):
            problems = accounts.check(profile)
        self.assertTrue(any("Keychain" in p for p in problems), problems)

    def test_security_absent_dit_qu_il_n_a_pas_pu_verifier(self):
        profile = self._profile()
        with mock.patch.object(platform, "SYSTEM", "macos"), \
                mock.patch.object(accounts, "_keychain_available", return_value=False):
            problems = accounts.check(profile)
        self.assertTrue(any("security not found" in p for p in problems), problems)

    def test_le_fichier_reste_une_preuve(self):
        profile = self._profile()
        open(os.path.join(self.directory, ".credentials.json"), "w").close()
        with mock.patch.object(platform, "SYSTEM", "macos"), \
                mock.patch.object(accounts, "_keychain_item") as item:
            self.assertEqual(accounts.check(profile), [])
        item.assert_not_called()

    def test_check_credentials_faux_ne_verifie_rien(self):
        profile = self._profile(check_credentials=False)
        with mock.patch.object(platform, "SYSTEM", "macos"), \
                mock.patch.object(accounts, "_keychain_item") as item:
            self.assertEqual(accounts.check(profile), [])
        item.assert_not_called()

    def test_compte_par_defaut_macos_verifie_le_trousseau(self):
        profile = accounts.parse({"claude": [{"name": "primaire"}]})["claude"][0]
        environ = {"HOME": self.tmp, "USER": "tester"}
        with mock.patch.object(platform, "SYSTEM", "macos"), \
                mock.patch.object(accounts, "_keychain_available", return_value=True), \
                mock.patch.object(accounts, "_keychain_item", return_value=True):
            self.assertEqual(accounts.check(profile, environ), [])
        with mock.patch.object(platform, "SYSTEM", "macos"), \
                mock.patch.object(accounts, "_keychain_available", return_value=True), \
                mock.patch.object(accounts, "_keychain_item", return_value=False):
            self.assertTrue(accounts.check(profile, environ))

    def test_le_defaut_linux_garde_son_comportement(self):
        profile = accounts.parse({"claude": [{"name": "primaire"}]})["claude"][0]
        with mock.patch.object(platform, "SYSTEM", "linux"):
            self.assertEqual(accounts.check(profile, {"HOME": self.tmp}), [])

    def test_codex_par_defaut_macos_ne_consulte_pas_le_trousseau(self):
        profile = accounts.parse({"codex": [{"name": "primaire"}]})["codex"][0]
        with mock.patch.object(platform, "SYSTEM", "macos"), \
                mock.patch.object(accounts, "_keychain_item") as item:
            self.assertEqual(accounts.check(profile, {"HOME": self.tmp}), [])
        item.assert_not_called()

    def test_codex_macos_garde_le_fichier(self):
        profile = accounts.parse({"codex": [{"name": "secondaire",
                                             "path": self.directory}]})["codex"][0]
        with mock.patch.object(platform, "SYSTEM", "macos"):
            self.assertTrue(any("auth.json" in p for p in accounts.check(profile)))
        open(os.path.join(self.directory, "auth.json"), "w").close()
        with mock.patch.object(platform, "SYSTEM", "macos"):
            self.assertEqual(accounts.check(profile), [])

    @unittest.skipUnless(platform.is_macos(), "security réel : macOS")
    def test_security_reel_sur_une_entree_absente(self):
        # vrai binaire macOS, entrée qui n'existe pas : aucune lecture de
        # secret et aucun dialogue du trousseau.
        self.assertFalse(accounts._keychain_item(
            "ameesh-ci-l65-no-such-item", os.environ.get("USER") or "nobody"))


if __name__ == "__main__":
    unittest.main()
