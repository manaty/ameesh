# SPDX-License-Identifier: AGPL-3.0-only
"""#5632 : un compte déclaré sans identifiants n'est pas un hôte KO.

AG2, 03/11 : codex/secondaire déclaré en attente de la connexion device-auth
de l'owner (#5566) ; `auth.json` absent faisait rendre `KO` à `ameesh doctor`
et bloquait le staging 1.35. Le compte en attente est dit, avec son remède,
sans changer le verdict tant qu'un autre compte du même harnais est utilisable ;
seul, il laisse le harnais sans compte et le KO demeure. Un autre défaut
(droits, dossier, clé) reste un KO, et n'est jamais masqué par l'attente.
"""
from __future__ import annotations

import os
import tempfile
import unittest

from ameesh import accounts

from .test_l30_accounts import _dossier


class CompteSansIdentifiantsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ameesh-5632-")

    def _cfg(self, comptes):
        return type("Cfg", (), {"accounts": comptes})()

    def test_compte_en_attente_avertit_sans_ko_si_un_autre_est_utilisable(self):
        primaire = _dossier(os.path.join(self.tmp, "codex-primaire"), creds="auth.json")
        secondaire = _dossier(os.path.join(self.tmp, "codex-secondaire"))
        ko, lignes = accounts.doctor_lines(self._cfg({"codex": [
            {"name": "primaire", "path": primaire},
            {"name": "secondaire", "path": secondaire}]}), environ={})
        self.assertFalse(ko)
        self.assertEqual(len(lignes), 1)
        self.assertIn("warning", lignes[0])
        self.assertIn("codex/secondaire", lignes[0])
        self.assertIn("credentials missing (auth.json)", lignes[0])
        self.assertIn("CODEX_HOME=%s" % secondaire, lignes[0])
        self.assertNotIn("KO", lignes[0])

    def test_compte_en_attente_seul_laisse_le_harnais_sans_compte_et_le_ko(self):
        seul = _dossier(os.path.join(self.tmp, "codex-seul"))
        ko, lignes = accounts.doctor_lines(self._cfg({"codex": [
            {"name": "seul", "path": seul}]}), environ={})
        self.assertTrue(ko)
        self.assertIn("KO — codex/seul", lignes[0])
        self.assertIn("credentials missing (auth.json)", lignes[0])

    def test_un_autre_defaut_reste_un_ko_et_n_est_pas_masque(self):
        primaire = _dossier(os.path.join(self.tmp, "codex-primaire"), creds="auth.json")
        ouvert = _dossier(os.path.join(self.tmp, "codex-ouvert"), mode=0o755)
        ko, lignes = accounts.doctor_lines(self._cfg({"codex": [
            {"name": "primaire", "path": primaire},
            {"name": "ouvert", "path": ouvert}]}), environ={})
        self.assertTrue(ko)
        self.assertEqual(len(lignes), 1)
        self.assertIn("KO — codex/ouvert", lignes[0])
        self.assertIn("directory not private", lignes[0])

    def test_trousseau_vide_est_une_attente_pas_un_ko(self):
        self.assertTrue(accounts._awaiting_login(
            "credentials missing in the Keychain (Claude Code-credentials): "
            "a human must log in"))
        self.assertFalse(accounts._awaiting_login(
            "cannot check Keychain credentials (security not found): "
            "a human must log in"))


if __name__ == "__main__":
    unittest.main()
