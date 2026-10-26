# SPDX-License-Identifier: AGPL-3.0-only
"""#3540 — git des exécuteurs de pc-smichea en HTTPS+`gh` après le masque #3444.

Sur le poste, `~/.ssh` est masqué (garde-fou #3444) et
`/etc/ssh/ssh_config.d/20-omarchy-keepalive.conf` apparaît au propriétaire
`nobody` dans l'unité : un `git fetch`/`push` d'un remote SSH échoue, et la
désignation au gel avec (`cannot refresh target …: git fetch failed`).

Le drop-in `deploy/poste/cles-owner.conf` pose donc, pour les unités
d'agents seulement :

* `GIT_CONFIG_SYSTEM=~/.config/ameesh/git-agents.conf`, un fichier lu comme
  configuration système de git qui réécrit `git@github.com:` (et
  `ssh://git@github.com/`) en `https://github.com/` et prend `gh` comme
  assistant d'identification ;
* `GIT_SSH_COMMAND=ssh -F /dev/null`, pour que ssh ne lise plus la
  configuration système de l'espace de noms.

Le fichier système est choisi à dessein : `canon._git_env()` retire
`GIT_CONFIG_COUNT/KEY/VALUE` de l'environnement de ses git, et la relève de
la cible au gel passe par `canon._git` — ces variables y seraient invisibles.
`~/.gitconfig` et `~/.ssh` ne sont pas touchés.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from unittest import mock

from ameesh import canon, review_target

from .support import REPO

POSTE = os.path.join(REPO, "deploy", "poste")
DROPIN = os.path.join(POSTE, "cles-owner.conf")
GITCONF = os.path.join(POSTE, "git-agents.conf")
UNITE = os.path.join(REPO, "deploy", "systemd", "ameesh-runner-agent@.service")
DOCS = os.path.join(REPO, "docs", "EXPLOITATION.md")


def _texte(chemin: str) -> str:
    with open(chemin, encoding="utf-8") as fh:
        return fh.read()


def _env(**extra) -> dict:
    """Environnement de test : pas de configuration git héritée du banc."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("GIT_CONFIG")
           and k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")}
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env.update(extra)
    return env


def _git(*args: str, cwd: str | None = None, env: dict | None = None):
    return subprocess.run(["git", *args], cwd=cwd, env=env or _env(),
                          capture_output=True, text=True, timeout=60)


class DropInGitTest(unittest.TestCase):
    """Les deux variables du drop-in, et le fichier qu'elles désignent."""

    def _environnement(self) -> list[str]:
        """Les assignations `Environment=` du [Service], hors commentaires."""
        return [l.strip() for l in _texte(DROPIN).splitlines()
                if l.strip().startswith("Environment=")]

    def test_le_dropin_pose_la_config_systeme(self):
        self.assertIn("Environment=GIT_CONFIG_SYSTEM=%h/.config/ameesh/git-agents.conf",
                      self._environnement())
        # La pose se fait avec le drop-in : l'installation est documentée.
        self.assertIn("deploy/poste/git-agents.conf", _texte(DROPIN))

    def test_le_dropin_ne_laisse_pas_ssh_lire_ssh_config_d(self):
        self.assertIn('Environment="GIT_SSH_COMMAND=ssh -F /dev/null"',
                      self._environnement())

    def test_le_gabarit_reste_universel(self):
        # La réécriture SSH→HTTPS ne vaut que là où ~/.ssh est masqué : sur
        # agents-1/2, ~/.ssh est lisible et merge-pr pousse en SSH (le jeton
        # d'un rôle peut manquer la portée workflow). Une réinstallation du
        # gabarit ne défait pas le drop-in, qui porte ces réglages.
        lignes = [l.strip() for l in _texte(UNITE).splitlines()]
        self.assertFalse([l for l in lignes
                          if l.startswith(("Environment=GIT_CONFIG_SYSTEM",
                                           "Environment=GIT_SSH_COMMAND"))])
        self.assertIn("#3540", _texte(UNITE))

    def test_le_fichier_reecrit_les_deux_formes_ssh(self):
        env = _env()
        valeurs = _git("config", "--file", GITCONF, "--get-all",
                       "url.https://github.com/.insteadOf", env=env)
        self.assertEqual(valeurs.returncode, 0, valeurs.stderr)
        self.assertEqual(valeurs.stdout.splitlines(),
                         ["git@github.com:", "ssh://git@github.com/"])

    def test_le_fichier_prend_l_assistant_gh(self):
        env = _env()
        helper = _git("config", "--file", GITCONF, "--get",
                      "credential.https://github.com.helper", env=env)
        self.assertEqual(helper.stdout.strip(), "!gh auth git-credential")

    def test_le_fichier_garde_la_config_systeme_de_l_hote(self):
        env = _env()
        include = _git("config", "--file", GITCONF, "--get", "include.path", env=env)
        self.assertEqual(include.stdout.strip(), "/etc/gitconfig")


class ReecritureTest(unittest.TestCase):
    """Un remote SSH est bien lu en HTTPS, sans réseau : la config du fichier."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = os.path.join(self.tmp.name, "clone")
        self.assertEqual(_git("init", "-q", "-b", "main", self.repo).returncode, 0)
        self.assertEqual(_git("remote", "add", "origin",
                              "git@github.com:manaty/ameesh.git",
                              cwd=self.repo).returncode, 0)

    def _url(self, conf: str) -> str:
        proc = _git("remote", "get-url", "origin", cwd=self.repo,
                    env=_env(GIT_CONFIG_SYSTEM=conf))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def test_un_remote_scp_ssh_devient_https(self):
        self.assertEqual(self._url(GITCONF),
                         "https://github.com/manaty/ameesh.git")

    def test_un_remote_url_ssh_devient_https(self):
        self.assertEqual(_git("remote", "set-url", "origin",
                              "ssh://git@github.com/manaty/ameesh.git",
                              cwd=self.repo).returncode, 0)
        self.assertEqual(self._url(GITCONF),
                         "https://github.com/manaty/ameesh.git")

    def test_sans_le_fichier_le_remote_reste_ssh(self):
        # Le garde-fou de portée : hors du drop-in, rien ne change.
        self.assertEqual(self._url(os.devnull), "git@github.com:manaty/ameesh.git")


class CibleDuGelTest(unittest.TestCase):
    """La relève de la cible du gel passe par canon._git : le fichier y est lu."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = self.tmp.name
        self.remote = os.path.join(base, "remote.git")
        self.seed = os.path.join(base, "seed")
        self.work = os.path.join(base, "work")
        env = _env()
        self.assertEqual(_git("init", "-q", "--bare", self.remote, env=env).returncode, 0)
        self.assertEqual(_git("symbolic-ref", "HEAD", "refs/heads/main",
                              cwd=self.remote, env=env).returncode, 0)
        self.assertEqual(_git("init", "-q", "-b", "main", self.seed, env=env).returncode, 0)
        with open(os.path.join(self.seed, "f"), "w", encoding="utf-8") as fh:
            fh.write("contenu\n")
        self.assertEqual(_git("add", "f", cwd=self.seed, env=env).returncode, 0)
        commit = _git("-c", "user.name=test", "-c", "user.email=test@invalid",
                      "commit", "-qm", "un", cwd=self.seed, env=env)
        self.assertEqual(commit.returncode, 0, commit.stderr)
        self.assertEqual(_git("push", "-q", self.remote, "main",
                              cwd=self.seed, env=env).returncode, 0)
        self.assertEqual(_git("clone", "-q", self.remote, self.work, env=env).returncode, 0)
        self.assertEqual(_git("remote", "set-url", "origin",
                              "git@github.com:manaty/ameesh.git",
                              cwd=self.work, env=env).returncode, 0)
        self.sha = _git("rev-parse", "main", cwd=self.seed, env=env).stdout.strip()
        # La cible est lue en SSH dans la config du clone ; seule la
        # réécriture (vers un dépôt local, ici) rend le fetch possible.
        self.assertIn("git@github.com:", _git("remote", "get-url", "origin",
                                              cwd=self.work, env=env).stdout)

    def test_canon_lit_git_config_system_et_pas_les_variables_count(self):
        with mock.patch.dict(os.environ, {
                "GIT_CONFIG_SYSTEM": "/tmp/fichier",
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "core.x",
                "GIT_CONFIG_VALUE_0": "1"}, clear=True):
            env = canon._git_env()
        self.assertEqual(env.get("GIT_CONFIG_SYSTEM"), "/tmp/fichier")
        self.assertNotIn("GIT_CONFIG_COUNT", env)
        self.assertFalse([k for k in env if k.startswith(("GIT_CONFIG_KEY_",
                                                          "GIT_CONFIG_VALUE_"))])

    def test_le_fetch_de_la_cible_suit_la_reecriture(self):
        conf = os.path.join(self.tmp.name, "test-git.conf")
        with open(conf, "w", encoding="utf-8") as fh:
            fh.write('[url "file://%s"]\n\tinsteadOf = git@github.com:manaty/ameesh.git\n'
                     % self.remote)
        env = _env(GIT_CONFIG_SYSTEM=conf)
        with mock.patch.dict(os.environ, env, clear=True):
            fetch = canon._git(self.work, "fetch", "--quiet", "--no-tags", "origin",
                               "+refs/heads/main:refs/remotes/origin/main")
            self.assertEqual(fetch.returncode, 0, fetch.stderr)
            self.assertEqual(review_target.resolve(self.work, "origin/main"), self.sha)


class DocumentationTest(unittest.TestCase):
    def test_la_pose_et_la_preuve_sont_documentees(self):
        # Le texte est normalisé : les phrases de la doc sont repliées, pas
        # coupées par la mise en page.
        texte = " ".join(_texte(DOCS).split())
        for attendu in ("### Git des exécuteurs après le masque : HTTPS par `gh` (#3540)",
                        "deploy/poste/git-agents.conf",
                        "GIT_CONFIG_SYSTEM",
                        "GIT_CONFIG_COUNT/KEY/VALUE",
                        "cannot refresh target origin/main: git fetch failed",
                        "GIT_SSH_COMMAND=ssh -F /dev/null",
                        "ameesh work reviewers <lot> --apply",
                        "réinstallation du gabarit ne défait pas le drop-in"):
            self.assertTrue(attendu in texte,
                            "absent de docs/EXPLOITATION.md : %s" % attendu)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
