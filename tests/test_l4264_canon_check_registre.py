"""#4264 : `canon check` dit les erreurs que le registre porte pour un état `ok`.

Constat (home#209, canon e62a9ed) : `canon sync` de l'ameesh installé avait
enregistré `ok — errors specific to agents … process-rules-block-too-long`,
mais un `canon check` lancé depuis une autre version (budget de brief relevé,
#4159) affichait « canon valid — 0 error(s) » sans rien dire du registre, que
suivent pourtant les briefs et la réclamation. Sans base : fakes seulement.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import types
import unittest
from unittest import mock

from ameesh import canon_cli, canon_sync

COMMIT = "e62a9ed364d3" + "0" * 28
DIAG = ("errors specific to agents (not claimable) or to the plan: "
        "process-rules-block-too-long (home:processus/demandes-github.md): …")


def _state(**extra) -> dict:
    st = {"status": canon_sync.CANON_OK, "checked_ts": None, "last_good_ts": None,
          "last_good_commit": COMMIT, "diagnostic": DIAG}
    st.update(extra)
    return st


def _canon(commit: str = COMMIT):
    return types.SimpleNamespace(
        root="/canon", untrusted=False, readable=True, id="", is_default=True,
        sources=[types.SimpleNamespace(commit=commit, to_dict=lambda: {})],
        source_label=lambda: "home @ %s (origin/main)" % commit[:12])


class RecordedLine(unittest.TestCase):
    def test_etat_ok_avec_diagnostic_le_dit(self):
        line = canon_cli._recorded_line("h", {"state": _state()})
        self.assertIn("process-rules-block-too-long", line)

    def test_etat_ok_sans_diagnostic_inchange(self):
        line = canon_cli._recorded_line("h", {"state": _state(diagnostic="")})
        self.assertNotIn(" — ", line)


class ErreursNonVues(unittest.TestCase):
    def test_meme_commit_erreurs_au_registre(self):
        self.assertIn("ERRORS recorded for e62a9ed364d3",
                      canon_cli._registry_errors_unseen(_canon(), {"state": _state()}, []))

    def test_autre_commit_ou_erreurs_locales_ou_sans_base(self):
        self.assertEqual(canon_cli._registry_errors_unseen(
            _canon("a" * 40), {"state": _state()}, []), "")
        self.assertEqual(canon_cli._registry_errors_unseen(
            _canon(), {"state": _state()}, ["erreur locale"]), "")
        self.assertEqual(canon_cli._registry_errors_unseen(_canon(), None, []), "")
        self.assertEqual(canon_cli._registry_errors_unseen(
            _canon(), {"state": _state(diagnostic="")}, []), "")


class CheckOne(unittest.TestCase):
    def _run(self, json_out: bool):
        args = argparse.Namespace(host="h", json=json_out)
        cfg = types.SimpleNamespace(host="h")
        out = io.StringIO()
        with mock.patch.object(canon_cli, "_recorded",
                               return_value={"state": _state(), "placements": {}}), \
                mock.patch.object(canon_cli.canon_mod, "validate", return_value=[]), \
                mock.patch.object(canon_cli.canon_sync, "assess",
                                  return_value=(canon_sync.CANON_OK, "")), \
                mock.patch.object(canon_cli.placement_mod, "verdicts", return_value=[]), \
                contextlib.redirect_stdout(out):
            code = canon_cli._check_one(cfg, args, _canon())
        return code, out.getvalue()

    def test_texte_echoue_et_le_dit(self):
        code, out = self._run(False)
        self.assertEqual(code, 1)
        self.assertNotIn("canon valid — ", out)
        self.assertIn("ERRORS in the registry", out)
        self.assertIn("process-rules-block-too-long", out)

    def test_json_echoue(self):
        code, out = self._run(True)
        data = json.loads(out)
        self.assertEqual(code, 1)
        self.assertFalse(data["ok"])
        self.assertIn("ERRORS recorded", data["registry_errors_unseen"])


if __name__ == "__main__":
    unittest.main()
