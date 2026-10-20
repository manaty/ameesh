# SPDX-License-Identifier: AGPL-3.0-only
"""#2853 — refus de `gh pr merge --auto` quand GitHub ne peut pas attendre le vert.

Constat : la PR manaty/nexlink#1849 (paquet 111) a été fusionnée 25 s après
le dispatch de la CI. Sur un dépôt privé sans GitHub Pro,
`allow_auto_merge=false` : `gh pr merge --auto --match-head-commit` fusionne
aussitôt au lieu d'attendre les checks. Le `gh` généré refuse désormais ce
geste quand le dépôt n'a pas l'auto-merge, ou quand la branche n'a pas de
protection portant des checks requis ; il renvoie vers la file de fusion.

Le faux `gh` répond aux lectures (`repo view`, `api`, `pr view`) et n'exécute
la commande d'origine que si la garde l'autorise : la dernière ligne du
journal des appels dit alors ce qui a été lancé.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from ameesh import github_identity as gi  # noqa: E402

#: faux `gh` : journalise chaque appel, répond aux lectures de la garde, et
#: n'exécute la commande d'origine (code 3) que si elle a été autorisée.
FAUX_GH = """#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
log = os.environ.get("FAKE_LOG")
if log:
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(" ".join(args) + "\\n")
if args[:2] == ["repo", "view"]:
    print(os.environ.get("FAKE_REPO", "acme/private"))
    sys.exit(0)
if args[:1] == ["api"]:
    path = args[1]
    if path.endswith("/protection"):
        sys.stderr.write(os.environ.get("FAKE_PROT_ERR", ""))
        print(os.environ.get("FAKE_PROT", "{}"))
        sys.exit(int(os.environ.get("FAKE_PROT_RC", "0")))
    sys.stderr.write(os.environ.get("FAKE_REPO_ERR", ""))
    print(os.environ.get("FAKE_REPO_INFO", '{"allow_auto_merge": false}'))
    sys.exit(int(os.environ.get("FAKE_REPO_RC", "0")))
if args[:2] == ["pr", "view"]:
    sys.stderr.write(os.environ.get("FAKE_VIEW_ERR", ""))
    print(os.environ.get("FAKE_VIEW", '{"baseRefName": "develop", "url": "https://github.com/acme/private/pull/12"}'))
    sys.exit(int(os.environ.get("FAKE_VIEW_RC", "0")))
print(" ".join(args))
sys.exit(3)
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.racine = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.racine, True)
        self.config = self.racine / "config"
        self.state = self.racine / "state"
        self.bin = self.racine / "bin"
        for dossier in (self.config, self.state, self.bin):
            dossier.mkdir()
        gh = self.bin / "gh"
        gh.write_text(FAUX_GH)
        gh.chmod(0o755)
        self.log = self.racine / "appels.log"
        self.env = {"PATH": str(self.bin) + os.pathsep + "/usr/bin:/bin",
                    "FAKE_LOG": str(self.log), "AGENT_MAIL_NAME": "dev-x",
                    "AMEESH_TURN_ID": "tour-1", "AMEESH_HOST": "hote"}
        gi.apply(self.env, "dev-x", str(self.config), str(self.state), "2853")
        self.shim = os.path.join(str(self.state), "github", "bin", "gh")

    def lancer(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        tour = dict(self.env)
        tour.update(env)
        return subprocess.run([self.shim, *args], env=tour, capture_output=True,
                              text=True, timeout=60)

    def appels(self) -> list[str]:
        if not self.log.exists():
            return []
        return self.log.read_text(encoding="utf-8").splitlines()

    def gestes(self) -> list[dict]:
        return gi.read_journal(str(self.state))

    def info_repo(self, **valeurs: str) -> dict:
        return {"FAKE_REPO_INFO": json.dumps({"allow_auto_merge": True}), **valeurs}


class RefusTest(Base):
    def test_sans_auto_merge_le_merge_est_refuse(self):
        sortie = self.lancer("pr", "merge", "12", "--auto", "-R", "acme/private")
        self.assertEqual(sortie.returncode, 1, sortie.stderr)
        self.assertIn("allow_auto_merge=false", sortie.stderr)
        self.assertIn("ameesh work queue add", sortie.stderr)
        # la commande d'origine n'a jamais rejoint le vrai gh
        self.assertEqual(self.appels(), [
            "pr view 12 --repo acme/private --json baseRefName,url",
            "api repos/acme/private"])

    def test_sans_protection_le_merge_est_refuse(self):
        sortie = self.lancer(
            "pr", "merge", "12", "--auto", "-R", "acme/private",
            **self.info_repo(FAKE_PROT_RC="403",
                             FAKE_PROT_ERR="Upgrade to GitHub Pro"))
        self.assertEqual(sortie.returncode, 1, sortie.stderr)
        self.assertIn("sans protection lisible", sortie.stderr)
        self.assertEqual(self.appels()[1], "api repos/acme/private")
        self.assertIn("branches/develop/protection", self.appels()[-1])

    def test_protection_sans_check_requis_refuse(self):
        sortie = self.lancer(
            "pr", "merge", "12", "--auto", "-R", "acme/private",
            **self.info_repo(FAKE_PROT=json.dumps({"required_status_checks": None})))
        self.assertEqual(sortie.returncode, 1, sortie.stderr)
        self.assertIn("sans check requis", sortie.stderr)

    def test_protection_json_illisible_refuse(self):
        sortie = self.lancer("pr", "merge", "12", "--auto", "-R", "acme/private",
                             **self.info_repo(FAKE_PROT="pas du json"))
        self.assertEqual(sortie.returncode, 1, sortie.stderr)
        self.assertIn("illisible (JSON)", sortie.stderr)

    def test_repo_illisible_refuse(self):
        sortie = self.lancer("pr", "merge", "12", "--auto", "-R", "acme/private",
                             FAKE_REPO_RC="1", FAKE_REPO_ERR="HTTP 500")
        self.assertEqual(sortie.returncode, 1, sortie.stderr)
        self.assertIn("réglages du dépôt illisibles", sortie.stderr)

    def test_pr_illisible_refuse(self):
        sortie = self.lancer("pr", "merge", "12", "--auto", "-R", "acme/private",
                             **self.info_repo(FAKE_VIEW="pas du json"))
        self.assertEqual(sortie.returncode, 1, sortie.stderr)
        self.assertIn("PR illisible", sortie.stderr)

    def test_base_illisible_refuse(self):
        sortie = self.lancer("pr", "merge", "12", "--auto", "-R", "acme/private",
                             **self.info_repo(FAKE_VIEW="{}"))
        self.assertEqual(sortie.returncode, 1, sortie.stderr)
        self.assertIn("base illisible", sortie.stderr)

    def test_admin_avec_auto_refuse_sans_lire(self):
        sortie = self.lancer("pr", "merge", "12", "--auto", "--admin",
                             "-R", "acme/private")
        self.assertEqual(sortie.returncode, 1, sortie.stderr)
        self.assertIn("--admin", sortie.stderr)
        self.assertEqual(self.appels(), [])

    def test_le_geste_reste_au_journal(self):
        self.lancer("pr", "merge", "12", "--auto", "-R", "acme/private")
        gestes = self.gestes()
        self.assertEqual(len(gestes), 1)
        self.assertEqual(gestes[0]["argv"],
                         ["pr", "merge", "12", "--auto", "-R", "acme/private"])
        self.assertEqual(gestes[0]["lot"], "2853")


class AutorisationTest(Base):
    def test_auto_avec_contextes_requis_est_autorise(self):
        sortie = self.lancer(
            "pr", "merge", "12", "--auto", "-R", "acme/private",
            **self.info_repo(FAKE_PROT=json.dumps(
                {"required_status_checks": {"contexts": ["tests"]}})))
        self.assertEqual(sortie.returncode, 3, sortie.stderr)
        self.assertEqual(self.appels()[-1], "pr merge 12 --auto -R acme/private")

    def test_auto_avec_checks_requis_est_autorise(self):
        sortie = self.lancer(
            "pr", "merge", "12", "--auto", "-R", "acme/private",
            **self.info_repo(FAKE_PROT=json.dumps(
                {"required_status_checks": {"checks": [
                    {"context": "tests", "app_id": 15368}]}})))
        self.assertEqual(sortie.returncode, 3, sortie.stderr)
        self.assertEqual(self.appels()[-1], "pr merge 12 --auto -R acme/private")

    def test_sans_pr_numerote_la_branche_courante_est_lue(self):
        sortie = self.lancer(
            "pr", "merge", "--auto", "-R", "acme/private",
            **self.info_repo(FAKE_PROT=json.dumps(
                {"required_status_checks": {"contexts": ["tests"]}})))
        self.assertEqual(sortie.returncode, 3, sortie.stderr)
        self.assertTrue(self.appels()[0].startswith("pr view"))

    def test_repo_deduit_de_l_url_quand_l_option_manque(self):
        # FAKE_REPO_INFO reste false : la garde refuse, mais le dépôt a bien
        # été résolu par l'URL de la PR et non supposé.
        sortie = self.lancer("pr", "merge", "12", "--auto")
        self.assertEqual(sortie.returncode, 1, sortie.stderr)
        self.assertIn("allow_auto_merge=false", sortie.stderr)
        self.assertTrue(self.appels()[0].startswith("pr view"))
        self.assertEqual(self.appels()[1], "api repos/acme/private")

    def test_merge_sans_auto_n_est_pas_filtre(self):
        sortie = self.lancer("pr", "merge", "12", "--squash")
        self.assertEqual(sortie.returncode, 3, sortie.stderr)
        self.assertEqual(self.appels(), ["pr merge 12 --squash"])

    def test_disable_auto_n_est_pas_filtre(self):
        sortie = self.lancer("pr", "merge", "12", "--disable-auto")
        self.assertEqual(sortie.returncode, 3, sortie.stderr)
        self.assertEqual(self.appels(), ["pr merge 12 --disable-auto"])

    def test_repo_colle_et_egal(self):
        for option in ("-Racme/private", "--repo=acme/private"):
            with self.subTest(option=option):
                self.log.unlink(missing_ok=True)
                sortie = self.lancer("pr", "merge", "12", "--auto", option)
                self.assertEqual(sortie.returncode, 1, sortie.stderr)
                self.assertIn("acme/private", sortie.stderr)
                self.assertEqual(self.appels()[-1], "api repos/acme/private")


if __name__ == "__main__":
    unittest.main()
