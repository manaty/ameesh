# SPDX-License-Identifier: AGPL-3.0-only
"""#4543 — la base du plan est exposée et la carte se lit sur elle.

N de la revue auditeur de #4533 (@804f5dd6) : le plan part de la base réelle
du merge-ref (`AMEESH_CI_BASE`), mais la trace du gel et la clé du cache de
carte suivaient encore la base *demandée* (description de l'étiquette). Un
atterrissage de coupure entre les deux faisait donc cohabiter deux bases dans
la même trace, et la carte restaurée était celle d'une autre base que celle
du diff.

Le témoin joue l'étape `gel` réelle de `.github/workflows/tests.yml` (la
porte, pas une copie) et vérifie les sorties ; un contrôle statique tient le
câblage du workflow : `plan_base` exposé, clé de carte sur `plan_base`, plan
sur `AMEESH_CI_BASE`.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

BRANCHE = "lot/4543-plan-base-carte"
WORKFLOW_PATH = (Path(__file__).resolve().parents[1]
                 / ".github/workflows/tests.yml")
WORKFLOW = WORKFLOW_PATH.read_text(encoding="utf-8")


def gate_script() -> str:
    """Le shell de l'étape `gel`, tel quel (mêmes ancres que #2328)."""
    step = WORKFLOW.split("      - name: gel (", 1)[1].split(
        "      # une poussée garde", 1)[0]
    return textwrap.dedent(step.split("        run: |\n", 1)[1])


class PlanBaseGateTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="ci-4543-")
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

    def landing(self, version: str = "0.0.2") -> str:
        """Un atterrissage de coupure sur la cible (`main`)."""
        self.write("pyproject.toml",
                   '[project]\nname = "ameesh"\nversion = "%s"\n' % version)
        self.write("docs/versions/%s.md" % version, "notes %s\n" % version)
        return self.commit(
            "release: %s (coupure de 11:00 du 29/10/2026) [skip ci]\n\n"
            "ameesh-release: %s" % (version, version))

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

    # -- la séquence du constat de #4533 ----------------------------------

    def test_the_plan_base_is_exposed_when_the_merge_ref_lags(self):
        landing = self.landing()
        # GitHub n'a pas encore recalculé refs/pull/N/merge : il pointe sur la
        # base de composition, comme le 29/10 à 10:23Z.
        self.merge_ref(self.old)
        result, out, env = self.gate(landing)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        outputs = out.read_text(encoding="utf-8")
        self.assertIn("base=%s\n" % landing, outputs)
        self.assertIn("plan_base=%s\n" % self.old, outputs)
        # le plan et la carte lisent la composition réellement extraite
        self.assertIn("AMEESH_CI_BASE=%s\n" % self.old,
                      env.read_text(encoding="utf-8"))
        # une seule trace dit les deux bases
        self.assertIn("base=%s plan_base=%s" % (landing, self.old),
                      result.stdout)

    def test_the_plan_base_is_the_composition_when_the_merge_ref_led(self):
        # L'autre sens : le merge-ref a rattrapé l'atterrissage avant que
        # l'étiquette (base de composition) ne parte.
        landing = self.landing()
        self.merge_ref(landing)
        result, out, env = self.gate(self.old)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        outputs = out.read_text(encoding="utf-8")
        self.assertIn("base=%s\n" % self.old, outputs)
        self.assertIn("plan_base=%s\n" % landing, outputs)
        self.assertIn("AMEESH_CI_BASE=%s\n" % landing,
                      env.read_text(encoding="utf-8"))

    # -- le câblage du workflow -------------------------------------------

    def test_the_map_cache_key_follows_the_plan_base(self):
        # la seule lecture de `base` est sa déclaration de sortie : la clé de
        # la carte ne doit plus passer par la base demandée.
        self.assertEqual(WORKFLOW.count("steps.gel.outputs.base"), 1)
        self.assertIn("base: ${{ steps.gel.outputs.base }}", WORKFLOW)
        self.assertIn(
            "plan_base: ${{ steps.gel.outputs.plan_base }}", WORKFLOW)
        self.assertIn(
            "key: carte-${{ steps.gel.outputs.plan_base", WORKFLOW)
        self.assertIn('echo "plan_base=$plan_base"', WORKFLOW)
        self.assertIn('echo "AMEESH_CI_BASE=$plan_base"', WORKFLOW)
        self.assertIn('--base "$AMEESH_CI_BASE"', WORKFLOW)


if __name__ == "__main__":
    unittest.main()
