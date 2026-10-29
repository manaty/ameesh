# SPDX-License-Identifier: AGPL-3.0-only
"""#4533 — un atterrissage de coupure ne fait plus échouer la ci-full d'un paquet.

Séquence reproduite (constat du 29/10, paquet h-05 #4523) : la composition part
de la tête de la cible ; pendant ses tests, la release pose sur la cible son
commit d'atterrissage (ligne de version et notes, `[skip ci]`) ; la ci-full est
demandée avec la nouvelle pointe pour base alors que la référence de fusion de
GitHub est encore sur la base de composition. Le plan doit accepter ce delta —
le contenu testé n'a pas changé — et sélectionner contre la base réellement
extraite ; un delta qui touche autre chose que la version et les notes reste
refusé, comme une autre tête.

Le témoin extrait l'étape `gel` réelle de `.github/workflows/tests.yml` et la
joue dans un dépôt temporaire : la porte, pas une copie de sa logique.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

BRANCHE = "lot/4533-paquet-ci-full-base"
WORKFLOW = (Path(__file__).resolve().parents[1]
            / ".github/workflows/tests.yml").read_text(encoding="utf-8")


def gate_script() -> str:
    """Le shell de l'étape `gel`, tel quel (mêmes ancres que #2328)."""
    step = WORKFLOW.split("      - name: gel (", 1)[1].split(
        "      # une poussée garde", 1)[0]
    return textwrap.dedent(step.split("        run: |\n", 1)[1])


class CutoffLandingGateTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="ci-4533-")
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        self.env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_CONFIG_NOSYSTEM": "1"}
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "test")
        self.git("config", "user.email", "test@example.invalid")
        self.write("pyproject.toml",
                   '[project]\nname = "ameesh"\nversion = "0.0.1"\n')
        self.write("base.txt", "base\n")
        self.old = self.commit("base")
        self.git("checkout", "-q", "-b", BRANCHE)
        self.write("lot.txt", "frozen head\n")
        self.head = self.commit("frozen head")
        self.git("checkout", "-q", "main")

    # -- dépôt -------------------------------------------------------------

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-c", "commit.gpgsign=false", *args], cwd=self.repo,
            env=self.env, text=True, capture_output=True, check=True).stdout.strip()

    def write(self, path: str, content: str) -> None:
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def landing(self, version: str = "0.0.2", *, code: bool = False,
                trailer: bool = True, extra_version: bool = False) -> str:
        """Un commit d'atterrissage de coupure sur la cible (`main`)."""
        self.write("pyproject.toml",
                   '[project]\nname = "ameesh"\nversion = "%s"\n%s'
                   % (version, "# note\n" if extra_version else ""))
        self.write("docs/versions/%s.md" % version, "notes %s\n" % version)
        if code:
            self.write("src/code.py", "x = 1\n")
        message = "release: %s (coupure de 11:00 du 29/10/2026) [skip ci]" % version
        if trailer:
            message += "\n\nameesh-release: %s" % version
        return self.commit(message)

    def merge_ref(self, base: str) -> None:
        """La référence de fusion composée par GitHub sur `base` et la tête."""
        self.git("checkout", "-q", "--detach", base)
        self.git("merge", "-q", "--no-ff", BRANCHE, "-m", "merge-ref")

    def gate(self, base: str):
        out = self.repo / "output"
        envfile = self.repo / "env"
        for path in (out, envfile):
            path.unlink(missing_ok=True)
        environ = {**self.env,
                   "AMEESH_CI_LABEL": "ci-full-%s" % self.head,
                   "AMEESH_CI_HEAD": self.head,
                   "AMEESH_CI_BASE": base,
                   "BASE_REF": "main",
                   "HEAD_REF": BRANCHE,
                   "GITHUB_OUTPUT": str(out),
                   "GITHUB_ENV": str(envfile)}
        result = subprocess.run(
            ["bash", "-e", "-o", "pipefail", "-c", gate_script()],
            cwd=self.repo, env=environ, text=True, capture_output=True)
        return result, out, envfile

    def assert_refused(self, base: str) -> None:
        result, out, _env = self.gate(base)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("the merge-ref base is", result.stdout)
        self.assertFalse(out.exists())

    # -- la séquence du constat -------------------------------------------

    def test_composition_then_cutoff_landing_then_ci_full_label(self):
        landing = self.landing()
        # GitHub n'a pas encore recalculé refs/pull/N/merge : il pointe sur la
        # base de composition, comme le 29/10 à 10:23Z.
        self.merge_ref(self.old)
        result, out, env = self.gate(landing)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("cutoff landing", result.stdout)
        self.assertIn("base=%s\n" % landing, out.read_text(encoding="utf-8"))
        # le plan sélectionne contre la composition réellement extraite
        self.assertIn("AMEESH_CI_BASE=%s\n" % self.old,
                      env.read_text(encoding="utf-8"))

    def test_a_merge_ref_ahead_of_the_label_is_admitted(self):
        # L'autre sens de la course : le merge-ref a rattrapé l'atterrissage
        # avant que l'étiquette (base de composition) ne parte.
        landing = self.landing()
        self.merge_ref(landing)
        result, out, env = self.gate(self.old)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("base=%s\n" % self.old, out.read_text(encoding="utf-8"))
        self.assertIn("AMEESH_CI_BASE=%s\n" % landing,
                      env.read_text(encoding="utf-8"))

    def test_successive_cutoff_landings_are_admitted(self):
        self.landing("0.0.2")
        second = self.landing("0.0.3")
        self.merge_ref(self.old)
        result, out, env = self.gate(second)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("AMEESH_CI_BASE=%s\n" % self.old,
                      env.read_text(encoding="utf-8"))

    # -- ce qui reste refusé ----------------------------------------------

    def test_a_code_change_in_the_landing_is_refused(self):
        landing = self.landing(code=True)
        self.merge_ref(self.old)
        self.assert_refused(landing)

    def test_a_diverged_base_is_refused(self):
        self.git("checkout", "-q", "--orphan", "elsewhere")
        self.write("other.txt", "other\n")
        other = self.commit("other history")
        self.merge_ref(self.old)
        self.assert_refused(other)

    def test_code_after_a_landing_is_refused(self):
        self.landing()
        self.write("src/code.py", "x = 1\n")
        code = self.commit("fix: code")
        self.merge_ref(self.old)
        self.assert_refused(code)

    def test_a_version_bump_without_the_release_trailer_is_refused(self):
        landing = self.landing(trailer=False)
        self.merge_ref(self.old)
        self.assert_refused(landing)

    def test_a_pyproject_change_that_is_not_only_the_version_line_is_refused(self):
        landing = self.landing(extra_version=True)
        self.merge_ref(self.old)
        self.assert_refused(landing)


if __name__ == "__main__":
    unittest.main()
