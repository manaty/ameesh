# SPDX-License-Identifier: AGPL-3.0-only
"""#3969 : un compte de harnais invalide est une configuration, pas un budget.

À la mise en service d'ameesh-agents-3 (27/10), les profils
`~/.claude-primary` et `~/.claude-secondary` laissés en 755 ont tenu deux
agents en « pause budget » jusqu'au reset du dernier compte valide (19 h),
sous un `ameesh doctor` vert. Témoin : un profil en 755 fait échouer doctor,
le choix dit « no valid … account » avec le remède, et l'exécuteur bloque
l'agent sous `accounts : ` (alerte `account_config_invalid`), sans pause
budget ; le profil réparé, l'agent repart seul.
"""
from __future__ import annotations

import dataclasses
import os
import time
import unittest

from ameesh import accounts, alert_briefs, cost, exploitation, notify, registry
from ameesh.runner import AgentWorker, Runner

from .support import PgTestCase
from .test_l30_accounts import _codex_rollout, _dossier


class DoctorTest(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="ameesh-3969-")

    def _cfg(self, comptes):
        return type("Cfg", (), {"accounts": comptes})()

    def test_profil_en_755_fait_echouer_doctor_avec_le_remede(self):
        ouvert = _dossier(os.path.join(self.tmp, "ouvert"), mode=0o755,
                          creds=".credentials.json")
        prive = _dossier(os.path.join(self.tmp, "prive"), creds=".credentials.json")
        ko, lignes = accounts.doctor_lines(self._cfg({"claude": [
            {"name": "primaire", "path": ouvert}, {"name": "secondaire", "path": prive}]}))
        self.assertTrue(ko)
        self.assertEqual(len(lignes), 1)
        self.assertIn("claude/primaire", lignes[0])
        self.assertIn("chmod 700 %s" % ouvert, lignes[0])

    def test_comptes_valides_et_cle_d_environnement_ne_changent_rien(self):
        prive = _dossier(os.path.join(self.tmp, "prive"), creds=".credentials.json")
        ko, lignes = accounts.doctor_lines(self._cfg({
            "claude": [{"name": "primaire", "path": prive}],
            # la clé vit dans l'environnement de l'exécuteur, pas de doctor
            "deepseek": [{"name": "k", "type": "api_key_env",
                          "key_env": "AMEESH_3969_ABSENTE"}]}))
        self.assertFalse(ko)
        self.assertEqual(lignes, [])

    def test_profil_absent_et_configuration_invalide(self):
        absent = os.path.join(self.tmp, "absent")
        ko, lignes = accounts.doctor_lines(self._cfg({"claude": [
            {"name": "primaire", "path": absent}]}))
        self.assertTrue(ko)
        self.assertIn("install -d -m 700 %s" % absent, lignes[0])
        ko, lignes = accounts.doctor_lines(self._cfg({"claude": "pas une liste"}))
        self.assertTrue(ko)
        self.assertIn("invalid account configuration", lignes[0])


class ChoixEtExecuteurTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        for table in ("account_active", "account_holds", "account_switches",
                      "quota_gauge_readings", "account_identities"):
            self.db.execute("DELETE FROM %s" % table)

    def _comptes(self):
        a = _dossier(os.path.join(self.tmp, "comptes", "codex-1"), creds="auth.json")
        b = _dossier(os.path.join(self.tmp, "comptes", "codex-2"), creds="auth.json")
        return [{"name": "primaire", "path": a, "max_concurrent": 0, "min_start_gap_s": 0},
                {"name": "secondaire", "path": b, "max_concurrent": 0,
                 "min_start_gap_s": 0}]

    def test_invalide_et_l_autre_au_seuil_pas_de_pause_budget(self):
        """Le cas d'agents-3 : un compte au seuil (reset daté), les autres
        invalides — la raison est la configuration, sans échéance de pause."""
        comptes = self._comptes()
        os.chmod(comptes[1]["path"], 0o755)
        items = accounts.parse({"codex": comptes})["codex"]
        now = time.time()
        _codex_rollout(items[0].path, 95, now + 19 * 3600)
        book = cost.CostBook(state_dir=self.cfg.state_dir, db=self.db)
        choix = accounts.choose(self.db, self.cfg.host, "codex", items, book, now=now)
        self.assertIsNone(choix.profile)
        self.assertTrue(choix.reason.startswith("no valid codex account: secondaire: "),
                        choix.reason)
        self.assertIn("chmod 700 %s" % comptes[1]["path"], choix.reason)
        self.assertIn("other accounts: primaire:", choix.reason)
        self.assertNotIn("pause (", choix.reason)

    def test_executeur_bloque_sous_accounts_puis_repart(self):
        comptes = self._comptes()
        for compte in comptes:
            os.chmod(compte["path"], 0o755)
        cfg = dataclasses.replace(self.cfg, accounts={"codex": comptes},
                                  budget_usd_per_hour=10.0, budget_check_interval=0.0)
        runner = Runner(cfg, self.db, once=True)
        self.register("ouvrier", "codex", cwd=self.tmp)
        lease = registry.claim(self.db, "ouvrier", runner.runner_id, 3600)
        worker = AgentWorker(runner, registry.get(self.db, "ouvrier"), lease)

        self.assertFalse(worker.budget_ok())
        ligne = registry.get(self.db, "ouvrier")
        self.assertEqual(ligne["status"], "blocked")
        self.assertTrue(ligne["status_text"].startswith(
            accounts.CONFIG_BLOCK_PREFIX + "no valid codex account"), ligne["status_text"])
        alertes = [a for a in exploitation.alerts(self.cfg, self.db)
                   if a["agent"] == "ouvrier"]
        types = [a["type"] for a in alertes]
        self.assertIn("account_config_invalid", types)
        self.assertNotIn("budget_pause_long", types)
        alerte = next(a for a in alertes if a["type"] == "account_config_invalid")
        self.assertIn("chmod 700", alerte["detail"])

        for compte in comptes:
            os.chmod(compte["path"], 0o700)
        worker.agent = registry.get(self.db, "ouvrier")
        self.assertTrue(worker.budget_ok())
        self.assertEqual(registry.get(self.db, "ouvrier")["status"], "idle")


class AlerteTest(unittest.TestCase):
    def test_alerte_declaree_urgente_et_adressee_a_l_equipe(self):
        self.assertIn("account_config_invalid", exploitation.ALERT_TYPES)
        self.assertIn("account_config_invalid", notify.URGENT_TYPES)
        alerte = {"type": "account_config_invalid", "agent": "ouvrier",
                  "orchestrators": ["orch"],
                  "detail": "ouvrier has no valid account: chmod 700 /x"}
        listing = [{"name": "ouvrier", "team": "ameesh"},
                   {"name": "orch", "team": "ameesh"}]
        out = alert_briefs.briefs(alerte, listing, auditor_names=[],
                                  orchestrator_names=("orch",))
        self.assertIn("orch", out)
        self.assertNotIn("ouvrier", out)
        self.assertIn("ameesh doctor", out["orch"])


if __name__ == "__main__":
    unittest.main()
