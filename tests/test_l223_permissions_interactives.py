# SPDX-License-Identifier: AGPL-3.0-only
"""L223 — mode de permission des sessions interactives Claude (`ameesh attach`).

Les tours sous l'exécuteur passent `--dangerously-skip-permissions` ; une
session interactive (attach, chat) ne passait aucun mode et s'arrêtait sur des
demandes d'autorisation que le propriétaire, sur mobile, ne peut pas valider.
Elle passe désormais `--permission-mode auto` (jamais `bypassPermissions` par
défaut), réglable par `AMEESH_CLAUDE_PERMISSION_MODE` et par agent
(`ameesh set <agent> permission_mode=…`), avec repli sur `acceptEdits` quand
l'aide du binaire ne connaît pas le mode.
"""
from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from ameesh import adapters, harnesses, registry
from ameesh.runner import run_attach

from .support import FAKEBIN, PgTestCase

VARIABLE = "AMEESH_CLAUDE_PERMISSION_MODE"


def _sans_variable() -> dict:
    return {k: v for k, v in os.environ.items() if k != VARIABLE}


class ModeDePermissionTest(unittest.TestCase):
    def setUp(self):
        adapters._HELP_CACHE.clear()
        self.addCleanup(adapters._HELP_CACHE.clear)

    def _claude(self, **env):
        base = dict(_sans_variable(), AMEESH_BIN_DIR=FAKEBIN, **env)
        patch = mock.patch.dict(os.environ, base, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        return adapters.adapter_for("claude")

    def test_attach_passe_le_mode_auto_par_defaut(self):
        claude = self._claude()
        self.assertEqual(claude.interactive_command("s1"),
                         [claude.binary, "--permission-mode", "auto", "--resume", "s1"])
        self.assertEqual(claude.interactive_command(),
                         [claude.binary, "--permission-mode", "auto"])

    def test_jamais_bypass_par_defaut(self):
        regle = harnesses.get("claude").interactive_permission
        self.assertEqual(regle["default"], "auto")
        self.assertNotIn("bypassPermissions", (regle["default"], regle["fallback"]))
        # les tours sous l'exécuteur gardent leur propre mode, distinct
        self.assertIn("--dangerously-skip-permissions", harnesses.get("claude").command)

    def test_la_variable_remplace_le_mode(self):
        claude = self._claude(**{VARIABLE: "acceptEdits"})
        self.assertEqual(claude.interactive_command("s1")[1:3],
                         ["--permission-mode", "acceptEdits"])

    def test_le_reglage_de_l_agent_prime_sur_la_variable(self):
        claude = self._claude(**{VARIABLE: "acceptEdits"})
        argv = claude.interactive_command("s1", permission_mode="plan")
        self.assertEqual(argv[1:3], ["--permission-mode", "plan"])

    def test_mode_mal_forme_ignore(self):
        claude = self._claude(**{VARIABLE: "auto --dangerously-skip-permissions"})
        argv = claude.interactive_command(None, permission_mode="x;rm")
        self.assertEqual(argv[1:], ["--permission-mode", "auto"])

    def test_version_ancienne_repli_sur_accept_edits(self):
        claude = self._claude(AMEESH_TEST_CLAUDE_HELP=(
            '  --permission-mode <mode>  (choices: "acceptEdits", '
            '"bypassPermissions", "default", "plan")'))
        self.assertEqual(claude.interactive_command()[1:], ["--permission-mode", "acceptEdits"])

    def test_aide_illisible_garde_le_mode(self):
        self.assertTrue(adapters._help_mentions("/chemin/absent/claude", "auto"))

    def test_codex_et_deepseek_inchanges(self):
        self._claude()
        codex = adapters.adapter_for("codex")
        self.assertEqual(codex.permission_mode(), "")
        self.assertEqual(codex.interactive_command("t1"), [codex.binary, "resume", "t1"])
        dsh = adapters.adapter_for("deepseek")
        self.assertEqual(dsh.permission_mode("auto"), "")

    def test_descripteur_valide(self):
        with open(harnesses.get("claude").path, encoding="utf-8") as fh:
            document = json.load(fh)
        erreurs = [f for f in harnesses.validate(document) if f.severity == harnesses.ERROR]
        self.assertEqual(erreurs, [])
        self.assertNotIn("harness-key-unknown", [f.code for f in harnesses.validate(document)])
        document["ameesh"]["interactive_permission"] = {"flag": ["--permission-mode"],
                                                        "default": "auto"}
        codes = [f.code for f in harnesses.validate(document)]
        self.assertIn("harness-permission-mode-invalid", codes)


class AttachPermissionTest(PgTestCase):
    def setUp(self):
        super().setUp()
        adapters._HELP_CACHE.clear()
        self.addCleanup(adapters._HELP_CACHE.clear)
        cwd = os.path.join(self.tmp, "work", "perm")
        os.makedirs(cwd, exist_ok=True)
        self.register("perm", "claude", cwd=cwd)

    def _attach(self, **env) -> list[str]:
        base = dict(_sans_variable(), AMEESH_BIN_DIR=FAKEBIN,
                    AMEESH_TEST_LOG=self.turns_log, **env)
        with mock.patch.dict(os.environ, base, clear=True):
            self.assertEqual(run_attach(self.cfg, self.db, "perm", ttl=30), 0)
        return self.turns()[-1]["argv"]

    def test_attach_lance_claude_en_mode_auto(self):
        argv = self._attach()
        self.assertEqual(argv[:2], ["--permission-mode", "auto"])
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertNotIn("bypassPermissions", argv)

    def test_attach_variable_d_environnement(self):
        self.assertEqual(self._attach(**{VARIABLE: "acceptEdits"})[:2],
                         ["--permission-mode", "acceptEdits"])

    def test_attach_reglage_de_l_agent(self):
        proc = self.mesh("set", "perm", "permission_mode=plan")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("mode de permission interactif (attach) : plan", proc.stdout)
        self.assertEqual(self._attach(**{VARIABLE: "acceptEdits"})[:2],
                         ["--permission-mode", "plan"])
        proc = self.mesh("set", "perm", "permission_mode=")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._attach()[:2], ["--permission-mode", "auto"])
        self.assertEqual(self.mesh("set", "perm", "permission_mode=a b").returncode, 2)
        self.assertIsNotNone(registry.get(self.db, "perm"))


if __name__ == "__main__":
    unittest.main()
