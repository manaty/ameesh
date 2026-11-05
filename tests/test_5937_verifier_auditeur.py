# SPDX-License-Identifier: AGPL-3.0-only
"""#5937 — le préflight des VM n'exige la minuterie de l'auditeur (#5863) que
si le code INSTALLÉ connaît sa passe.

Incident du 05/11/2026 : le staging 1.39.0 échouait au `agents verifier`
d'AG1, puis l'aurait fait sur chaque VM. `controler_agents` (partagé par
`verifier`, `controler` et le retour arrière) lançait
`role timers --role auditeur --check` avec le CLI encore installé (1.38.0),
dont le plan --role auditeur rend les passes génériques : cinq unités du vrai
observateur jugées périmées. Le contrôle reste dû quand le code installé
connaît la passe ; la question est posée au python du venv installé, sous le
compte ameesh (`python_agents`), jamais par une lecture root dans les dossiers
d'agent ni sur un chemin supposé (bureau, #193697). Le comportement est
couvert par tests/test_1252_vm_agents_install.py ; ce module garde la forme.
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
    def test_the_attribute_the_probe_reads_exists_in_the_source(self):
        # Un renommage ferait passer tout code installé pour antérieur à #5863.
        source = ROLE_TIMERS.read_text(encoding="utf-8")
        self.assertRegex(source, re.compile(r"^AUDITOR_PASS = ", re.M))
        self.assertIn('getattr(r, "AUDITOR_PASS", "")', _controler_agents())

    def test_the_probe_runs_as_the_agent_account_never_as_root(self):
        body = _controler_agents()
        probe = body.index("python_agents -c 'from ameesh import role_timers")
        check = body.index("role timers --role auditeur --check")
        self.assertLess(probe, check)
        # Ni grep ni cat root sur le code installé (frontière de #5797).
        self.assertNotRegex(body, r"ssh_agents [^\n]*role_timers")
        self.assertNotIn("src-${runtime_sha", body[probe - 600:check])

    def test_only_one_auditor_check_and_the_generic_one_stays(self):
        body = _controler_agents()
        self.assertEqual(body.count("role timers --role auditeur --check"), 1)
        self.assertRegex(body, r"\n  ameesh_agents role timers --check \|\| echec=1\n")


if __name__ == "__main__":
    unittest.main()
