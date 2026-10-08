# SPDX-License-Identifier: AGPL-3.0-only
"""`ameesh … | head` : une sortie fermée par le lecteur n'est pas une erreur
(code 141, aucune trace), mais un tube cassé ailleurs remonte."""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from unittest import mock

from ameesh import main as main_mod


class _SortieFermee:
    def write(self, _):
        raise BrokenPipeError

    def flush(self):
        raise BrokenPipeError

    def fileno(self):
        return self._fd

    def __init__(self, fd):
        self._fd = fd


class TubeFermeTest(unittest.TestCase):
    def test_sortie_fermee_code_141(self):
        r, w = os.pipe()
        os.close(r)
        try:
            with mock.patch.object(sys, "stdout", _SortieFermee(w)), \
                    mock.patch.object(main_mod, "_dispatch", side_effect=BrokenPipeError):
                self.assertEqual(main_mod.main(["alerts"]), 141)
        finally:
            os.close(w)

    def test_tube_casse_ailleurs_remonte(self):
        with mock.patch.object(main_mod, "_dispatch", side_effect=BrokenPipeError):
            with self.assertRaises(BrokenPipeError):
                main_mod.main(["alerts"])

    def test_de_bout_en_bout_sans_trace(self):
        # l'aide complète dépasse une ligne : `head -1` ferme le tube tôt
        code = ("import sys\nfrom ameesh import main\n"
                "for _ in range(2000):\n"
                "    rc = main.main(['--help'])\n"
                "    if rc:\n"
                "        sys.exit(rc)\n")
        env = dict(os.environ, PYTHONPATH=os.path.join(os.path.dirname(__file__), "..", "src"))
        p = subprocess.run("%s -c %s | head -1" % (sys.executable, _quote(code)), shell=True,
                           capture_output=True, text=True, env=env, timeout=60)
        self.assertNotIn("BrokenPipeError", p.stderr)
        self.assertNotIn("Traceback", p.stderr)


def _quote(s):
    return "'" + s.replace("'", "'\\''") + "'"


if __name__ == "__main__":
    unittest.main()
