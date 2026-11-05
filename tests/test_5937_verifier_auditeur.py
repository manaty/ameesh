# SPDX-License-Identifier: AGPL-3.0-only
"""#5937 — le préflight des VM n'exige la minuterie de l'auditeur (#5863) que
si le code INSTALLÉ connaît sa passe.

Incident du 05/11/2026 : le staging 1.39.0 échouait au `agents verifier`
d'AG1, puis l'aurait fait sur chaque VM. `controler_agents` (partagé par
`verifier`, `controler` et le retour arrière) lançait
`role timers --role auditeur --check` avec le CLI encore installé (1.38.0),
dont le plan --role auditeur rend les passes génériques : cinq unités du vrai
observateur jugées périmées. Le contrôle reste dû après l'installation ; il
est conditionné par la présence de `AUDITOR_PASS` dans le role_timers.py du
code installé, lu par ssh.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VM = ROOT / "deploy/mise-a-jour/vm.sh"
ROLE_TIMERS = ROOT / "src/ameesh/role_timers.py"


def _controler_agents() -> str:
    text = VM.read_text(encoding="utf-8")
    start = text.index("controler_agents() {")
    end = text.index("\n}\n", start)
    return text[start:end]


class VerifierAuditeurTest(unittest.TestCase):
    def test_the_marker_the_script_reads_exists_in_the_source(self):
        # Le script cherche `^AUDITOR_PASS = ` : un renommage ferait passer
        # tout code installé pour antérieur à #5863, sans contrôle.
        source = ROLE_TIMERS.read_text(encoding="utf-8")
        self.assertRegex(source, re.compile(r"^AUDITOR_PASS = ", re.M))
        self.assertIn("grep -q '^AUDITOR_PASS = '", _controler_agents())

    def test_the_auditor_check_runs_only_when_the_installed_code_knows_it(self):
        body = _controler_agents()
        checks = [m.start() for m in re.finditer(
            r"role timers --role auditeur --check", body)]
        self.assertEqual(len(checks), 1, "un seul contrôle auditeur")
        guard = body.index("grep -q '^AUDITOR_PASS = '")
        case = body.index('case "$rc" in', guard)
        self.assertLess(guard, case)
        self.assertLess(case, checks[0])
        branch = body[case:body.index("esac", case)]
        self.assertRegex(branch, r"0\) ameesh_agents role timers --role auditeur --check")
        # Code antérieur : étape nommée, pas d'échec ; illisible : échec.
        self.assertRegex(branch, r'1\) etape "minuterie de l\'auditeur \(#5863\)')
        self.assertRegex(branch, r"\*\) .*echec=1")

    def test_the_generic_role_timer_check_stays_unconditional(self):
        body = _controler_agents()
        self.assertRegex(body, r"\n  ameesh_agents role timers --check \|\| echec=1\n")


if __name__ == "__main__":
    unittest.main()
