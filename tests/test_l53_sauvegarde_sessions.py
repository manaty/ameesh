# SPDX-License-Identifier: AGPL-3.0-only
"""L53 (0032 §2, étude v2 A7) : sauvegarde des sessions hors de l'appareil."""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import tarfile
import tempfile
import threading
import types
import unittest

from ameesh import harnesses, session_backup as sb
from ameesh.runner import AgentWorker

SESSION = "session-bae71c17-331f-421b-b645-d3a590b676a3"
DSH = ("sessions/*/{session}",)
CLAUDE = ("projects/*/{session}.jsonl", "projects/*/{session}")


def ecrire(chemin: str, texte: str = "x") -> None:
    os.makedirs(os.path.dirname(chemin), exist_ok=True)
    with open(chemin, "w", encoding="utf-8") as fh:
        fh.write(texte)


class _Tmp(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="ameesh-l53-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.cible = os.path.join(self.tmp, "cible")
        self.etat = os.path.join(self.tmp, "etat")
        ecrire(os.path.join(self.home, "sessions/--dossier--", SESSION, "session.v4.jsonl.zstd"),
               "contenu")
        ecrire(os.path.join(self.home, "sessions/--dossier--", SESSION, "session.lock"), "")
        ecrire(os.path.join(self.home, "sessions/--dossier--/autre/session.v4.jsonl.zstd"), "non")

    def conf(self, **extra):
        raw = {"target": self.cible, "encrypt": False, "min_interval_s": 0}
        raw.update(extra)
        return sb.parse(raw)


class ConfigurationTest(unittest.TestCase):
    def test_absente(self):
        self.assertIsNone(sb.parse({}))

    def test_chiffrement_sans_destinataire_refuse(self):
        with self.assertRaises(sb.BackupError):
            sb.parse({"target": "/srv/copies"})

    def test_cible_relative_refusee(self):
        with self.assertRaises(sb.BackupError):
            sb.parse({"target": "copies", "encrypt": False})

    def test_s3(self):
        conf = sb.parse({"target": "s3://seau/mesh/", "recipients": ["ABCD"]})
        self.assertTrue(conf.is_s3)
        self.assertEqual(conf.target, "s3://seau/mesh")


class FichiersTest(_Tmp):
    def test_dossier_de_la_session_seulement(self):
        paths = sb.session_paths(self.home, DSH, SESSION)
        self.assertEqual([os.path.relpath(p, self.home) for p in paths],
                         ["sessions/--dossier--/" + SESSION])

    def test_claude_fichier_et_dossier(self):
        ecrire(os.path.join(self.home, "projects/-a/s1.jsonl"))
        ecrire(os.path.join(self.home, "projects/-a/s1/subagents/x.jsonl"))
        paths = sb.session_paths(self.home, CLAUDE, "s1")
        self.assertEqual(sorted(os.path.relpath(p, self.home) for p in paths),
                         ["projects/-a/s1", "projects/-a/s1.jsonl"])

    def test_identifiant_force_refuse(self):
        for mauvais in ("*", "../x", "a/b", "", "s[1]"):
            with self.assertRaises(sb.BackupError):
                sb.session_paths(self.home, DSH, mauvais)

    def test_lien_hors_du_dossier_ignore(self):
        dehors = os.path.join(self.tmp, "dehors")
        os.makedirs(dehors)
        os.symlink(dehors, os.path.join(self.home, "sessions/--dossier--/lien"))
        self.assertEqual(sb.session_paths(self.home, DSH, "lien"), [])


class AllerRetourTest(_Tmp):
    def test_copie_inchangee_puis_nouvelle_copie(self):
        conf = self.conf()
        r = sb.backup(conf, host="pc", agent="deepseek2", home=self.home, patterns=DSH,
                      session=SESSION, state_dir=self.etat, now=1000.0)
        self.assertEqual(r["status"], "copiee")
        self.assertTrue(r["key"].startswith("pc/deepseek2/%s/" % SESSION))
        self.assertTrue(r["key"].endswith(".tar.gz"))
        r2 = sb.backup(conf, host="pc", agent="deepseek2", home=self.home, patterns=DSH,
                       session=SESSION, state_dir=self.etat, now=1001.0)
        self.assertEqual(r2["status"], "inchangee")
        ecrire(os.path.join(self.home, "sessions/--dossier--", SESSION, "session.v4.jsonl.zstd"),
               "contenu plus long")
        r3 = sb.backup(conf, host="pc", agent="deepseek2", home=self.home, patterns=DSH,
                       session=SESSION, state_dir=self.etat, now=1002.0)
        self.assertEqual(r3["status"], "copiee")
        self.assertEqual(len(sb.list_keys(conf, "pc/")), 2)

    def test_intervalle_minimal(self):
        conf = self.conf(min_interval_s=600)
        sb.backup(conf, host="pc", agent="a", home=self.home, patterns=DSH, session=SESSION,
                  state_dir=self.etat, now=1000.0)
        ecrire(os.path.join(self.home, "sessions/--dossier--", SESSION, "n"), "nouveau")
        r = sb.backup(conf, host="pc", agent="a", home=self.home, patterns=DSH,
                      session=SESSION, state_dir=self.etat, now=1100.0)
        self.assertEqual(r["status"], "trop_tot")

    def test_restauration_sur_un_autre_hote(self):
        conf = self.conf()
        sb.backup(conf, host="pc", agent="a", home=self.home, patterns=DSH, session=SESSION,
                  state_dir=self.etat, now=1000.0)
        ailleurs = os.path.join(self.tmp, "serveur")
        key = sb.latest(conf, agent="a", session=SESSION)
        files = sb.restore(conf, key, ailleurs)
        restaure = os.path.join(ailleurs, "sessions/--dossier--", SESSION, "session.v4.jsonl.zstd")
        with open(restaure, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "contenu")
        self.assertNotIn("session.lock", " ".join(files))       # verrous exclus
        with self.assertRaises(sb.BackupError):                  # pas d'écrasement implicite
            sb.restore(conf, key, ailleurs)
        sb.restore(conf, key, ailleurs, overwrite=True)

    def test_copie_qui_sort_du_dossier_refusee(self):
        conf = self.conf()
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo("../evasion")
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
        sb.put(conf, "pc/a/s/1.tar.gz", buf.getvalue())
        with self.assertRaises(sb.BackupError):
            sb.restore(conf, "pc/a/s/1.tar.gz", os.path.join(self.tmp, "serveur"))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "evasion")))


@unittest.skipUnless(shutil.which("gpg"), "gpg absent")
class ChiffrementTest(_Tmp):
    def setUp(self) -> None:
        super().setUp()
        self.gnupg = tempfile.mkdtemp(prefix="g", dir="/tmp")    # chemin court (socket)
        self.addCleanup(shutil.rmtree, self.gnupg, True)
        os.chmod(self.gnupg, 0o700)
        ancien = os.environ.get("GNUPGHOME")
        os.environ["GNUPGHOME"] = self.gnupg
        self.addCleanup(lambda: os.environ.pop("GNUPGHOME") if ancien is None
                        else os.environ.__setitem__("GNUPGHOME", ancien))
        subprocess.run(["gpg", "--batch", "--quiet", "--passphrase", "", "--quick-gen-key",
                        "essai-l53@ameesh.invalid", "default", "default", "never"],
                       check=True, capture_output=True)
        self.addCleanup(subprocess.run, ["gpgconf", "--kill", "gpg-agent"], capture_output=True)

    def test_aller_retour_chiffre(self):
        conf = sb.parse({"target": self.cible, "recipients": ["essai-l53@ameesh.invalid"],
                         "min_interval_s": 0})
        r = sb.backup(conf, host="pc", agent="a", home=self.home, patterns=DSH, session=SESSION,
                      state_dir=self.etat)
        self.assertTrue(r["key"].endswith(".tar.gz.gpg"))
        with open(os.path.join(self.cible, r["key"]), "rb") as fh:
            self.assertNotIn(b"contenu", fh.read())               # rien en clair
        ailleurs = os.path.join(self.tmp, "serveur")
        sb.restore(conf, r["key"], ailleurs)
        with open(os.path.join(ailleurs, "sessions/--dossier--", SESSION,
                               "session.v4.jsonl.zstd"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "contenu")


class DescripteursTest(unittest.TestCase):
    def test_les_trois_harnais_declarent_leurs_fichiers(self):
        for h in ("claude", "codex", "deepseek"):
            d = harnesses.get(h)
            self.assertTrue(d.session_files, h)
            self.assertTrue(all("{session}" in f for f in d.session_files), h)

    def test_motif_invalide_refuse(self):
        for mauvais in (["sessions/*"], ["/abs/{session}"], ["../{session}"], []):
            findings = harnesses.validate({"id": "x", "name": "x", "version": "1",
                                           "schema_version": "1", "description": "x",
                                           "ameesh": {"protocol": "cli", "binary": "x",
                                                      "backup": {"session_files": mauvais}}})
            self.assertIn("harness-backup-invalid", {f.code for f in findings}, mauvais)


class ExecuteurTest(_Tmp):
    def test_copie_en_fin_de_tour(self):
        journal = []
        worker = types.SimpleNamespace(
            name="deepseek2", agent={"harness": "deepseek"},
            runner=types.SimpleNamespace(dry_run=False),
            cfg=types.SimpleNamespace(session_backup={"target": self.cible, "encrypt": False,
                                                       "min_interval_s": 0},
                                      host="pc", state_dir=self.etat))
        adapter = types.SimpleNamespace(descriptor=types.SimpleNamespace(session_files=DSH))
        os.environ["DSH_HOME"] = self.home
        self.addCleanup(os.environ.pop, "DSH_HOME", None)
        import ameesh.runner as runner_mod
        ancien_log = runner_mod.log
        runner_mod.log = journal.append
        self.addCleanup(setattr, runner_mod, "log", ancien_log)
        AgentWorker._sauvegarde_session(worker, adapter, None, SESSION)
        for t in threading.enumerate():
            if t.name == "deepseek2-sauvegarde-session":
                t.join(10)
        self.assertTrue(any("sauvegardée" in line for line in journal), journal)
        self.assertEqual(len(sb.list_keys(sb.parse(worker.cfg.session_backup), "pc/")), 1)

    def test_configuration_invalide_journalisee_une_fois(self):
        journal = []
        worker = types.SimpleNamespace(
            name="a", agent={"harness": "deepseek"}, runner=types.SimpleNamespace(dry_run=False),
            cfg=types.SimpleNamespace(session_backup={"target": "/srv"}, host="pc",
                                      state_dir=self.etat))
        import ameesh.runner as runner_mod
        ancien_log = runner_mod.log
        runner_mod.log = journal.append
        self.addCleanup(setattr, runner_mod, "log", ancien_log)
        AgentWorker._sauvegarde_session(worker, None, None, SESSION)
        AgentWorker._sauvegarde_session(worker, None, None, SESSION)
        self.assertEqual(len([j for j in journal if "désactivée" in j]), 1)


if __name__ == "__main__":
    unittest.main()
