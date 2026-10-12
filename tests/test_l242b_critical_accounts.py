# SPDX-License-Identifier: AGPL-3.0-only
"""L242b (PR 1) : un agent critique (orchestrateur, conception, auditeur, ou
déclaré) n'est jamais en pause budget tant qu'un compte de son harnais a de la
marge — choix de compte propre à l'agent (ni le dernier choix partagé, ni un
forçage ne bougent), puis le compte par défaut au-delà de son plafond de 50 %
(décision du propriétaire du 2026-10-12, 10:00), jamais au-delà du rythme."""
from __future__ import annotations

import json
import os
import time
import types
import unittest
from unittest import mock

from ameesh import account_turn, accounts, config, cost, registry, runner, storage

from .test_l226b_auth_failure import _AuthBase
from .test_l30_accounts import _Base as _L30Base, _codex_rollout


class _Comptes(_AuthBase):
    """Codex : `primaire` (compte par défaut, CODEX_HOME) et `secondaire`."""

    def setUp(self) -> None:
        super().setUp()
        self.home = os.path.join(self.tmp, "codex-default")
        os.makedirs(self.home, mode=0o700, exist_ok=True)
        env = mock.patch.dict(os.environ, {"CODEX_HOME": self.home})
        env.start()
        self.addCleanup(env.stop)
        self.secondaire = self._codex_comptes()[1]
        self.items = accounts.parse({"codex": [{"name": "primaire"}, self.secondaire]})["codex"]
        self.now = time.time()
        self.book = cost.CostBook(state_dir=self.cfg.state_dir, db=self.db)

    def jauge(self, name, pct):
        home = self.home if name == "primaire" else self.secondaire["path"]
        _codex_rollout(home, pct, self.now + 1800)   # 90 % de la fenêtre écoulée

    def choose(self, **kw):
        return accounts.choose(self.db, self.cfg.host, "codex", self.items, self.book,
                               now=self.now, **kw)

    def shared(self):
        return storage.of(self.db).accounts.active(self.cfg.host, "codex")


class PrimaireAuDelaDuPlafondTest(_Comptes):
    def test_critique_seul_au_dela_de_50_jamais_au_dela_du_rythme(self):
        self.jauge("primaire", 60)       # au-dessus de 50 %, sous le rythme (90 %)
        self.jauge("secondaire", 96)     # au seuil
        ordinaire = self.choose()
        self.assertIsNone(ordinaire.profile)
        self.assertIn("at threshold", ordinaire.reason)
        critique = self.choose(critical=True)
        self.assertEqual(critique.profile.name, "primaire")
        self.assertTrue(critique.per_agent)
        self.assertIn("critical agent: primaire beyond its 50% cap", critique.why)
        # au-delà du rythme : pause, même pour un agent critique
        self.jauge("primaire", 95)
        critique = self.choose(critical=True)
        self.assertIsNone(critique.profile)

    def test_primaire_en_dernier_et_quitte_des_qu_un_compte_se_libere(self):
        self.jauge("primaire", 60)
        self.jauge("secondaire", 20)
        critique = self.choose(critical=True, session_account="primaire")
        self.assertEqual(critique.profile.name, "secondaire")
        self.assertFalse(critique.kept)
        self.assertEqual(critique.switched["kind"], "agent")
        self.assertEqual((critique.switched["from"], critique.switched["to"]),
                         ("primaire", "secondaire"))

    def test_plafond_explicite_tenu(self):
        self.items = accounts.parse({"codex": [{"name": "primaire", "cap": 0.5},
                                               self.secondaire]})["codex"]
        self.jauge("primaire", 60)
        self.jauge("secondaire", 96)
        self.assertIsNone(self.choose(critical=True).profile)


class ChoixParAgentTest(_Comptes):
    def test_le_choix_partage_ne_bouge_pas(self):
        self.jauge("primaire", 10)
        self.jauge("secondaire", 10)
        critique = self.choose(critical=True)
        # secondaire passe avant le compte par défaut (L226b), pour l'agent seul
        self.assertEqual(critique.profile.name, "secondaire")
        self.assertTrue(critique.per_agent)
        self.assertEqual(critique.switched["kind"], "agent")
        self.assertEqual(self.shared()["account"], "primaire")
        ordinaire = self.choose()
        self.assertEqual(ordinaire.profile.name, "secondaire")
        self.assertEqual(self.shared()["account"], "secondaire")
        # déjà sur le choix partagé, sans session : rien à journaliser
        self.assertIsNone(self.choose(critical=True).switched)

    def test_un_forcage_au_seuil_ne_retient_pas_un_critique_et_reste_aux_autres(self):
        self.jauge("primaire", 30)
        self.jauge("secondaire", 96)
        accounts.force(self.db, self.cfg.host, "codex", self.items, "secondaire",
                       by="human:proprio")
        critique = self.choose(critical=True)
        self.assertEqual(critique.profile.name, "primaire")
        self.assertTrue(critique.per_agent)
        row = self.shared()
        self.assertEqual((row["account"], row["forced"]), ("secondaire", True))
        # un forçage sous son seuil s'applique aussi aux critiques
        self.jauge("secondaire", 10)
        critique = self.choose(critical=True)
        self.assertTrue(critique.forced)
        self.assertEqual(critique.profile.name, "secondaire")

    def test_continuite(self):
        self.jauge("primaire", 10)
        self.jauge("secondaire", 10)
        critique = self.choose(critical=True, session_account="primaire")
        self.assertTrue(critique.kept)
        self.assertEqual(critique.profile.name, "primaire")


class ExecuteurTest(_L30Base):
    def test_critique_bascule_seul_dans_la_meme_session(self):
        comptes = self._claude_comptes(partage=True)
        _runner, worker = self._worker("orch", "claude", {"claude": comptes},
                                       critical_agents=("orch",))
        try:
            haut = json.dumps({"five_hour": {"utilization": 0.95,
                                             "resetsAt": time.time() + 3600}})
            self.assertIsNotNone(self._tour(worker, AMEESH_TEST_RATE_LIMIT=haut))
            self.assertEqual(self.turns()[-1]["compte"]["CLAUDE_CONFIG_DIR"], comptes[0]["path"])
            # 2e tour : l'agent seul passe sur secondaire, même session reprise
            self.assertIsNotNone(self._tour(worker, "suite"))
            dernier = self.turns()[-1]
            self.assertEqual(dernier["compte"]["CLAUDE_CONFIG_DIR"], comptes[1]["path"])
            self.assertIn("--resume", dernier["argv"])
            self.assertIn("claude-sess-1", dernier["argv"])
            # le choix partagé n'a pas bougé : aucune bascule en base
            self.assertEqual(self._bascules(), [])
            row = storage.of(self.db).accounts.active(self.cfg.host, "claude")
            self.assertEqual(row["account"], "primaire")
            self.assertIn("for this critical agent only", self._fil())
            self.assertEqual(registry.get(self.db, "orch")["session_account"], "secondaire")
        finally:
            worker.watchdog_stop.set()


class _Worker:
    def __init__(self, critical):
        self.name = "orchestrateur"
        self.agent = {"name": "orchestrateur"}
        self.runner = types.SimpleNamespace(is_critical=lambda name, agent: critical)
        self.notes = []

    def _fil_note(self, text, meta=None):
        self.notes.append((text, meta))


class IdentiteTest(unittest.TestCase):
    def fake_runner(self, **kw):
        fake = types.SimpleNamespace(
            cfg=types.SimpleNamespace(critical_agents=kw.get("declared", ())),
            _audit_canons=(), is_orchestrator=lambda n: n in kw.get("orchestras", ()),
            is_auditor=lambda n, a=None: n == "auditeur")
        return lambda name: runner.Runner.is_critical(fake, name)

    def test_qui_est_critique(self):
        is_critical = self.fake_runner(declared=("mesh-design",), orchestras=("orchestrateur",))
        self.assertTrue(is_critical("mesh-design"))
        self.assertTrue(is_critical("orchestrateur"))
        self.assertTrue(is_critical("auditeur"))
        self.assertFalse(is_critical("dev7"))

    def test_role_de_conception_au_canon(self):
        fake = types.SimpleNamespace(
            cfg=types.SimpleNamespace(critical_agents=()), _audit_canons=("canon",),
            is_orchestrator=lambda n: False, is_auditor=lambda n, a=None: False)
        with mock.patch("ameesh.sous_utilisation.role_orchestrators",
                        lambda canons, roles: {"mesh-design"} if roles == runner.DESIGN_ROLES
                        else set()):
            self.assertTrue(runner.Runner.is_critical(fake, "mesh-design"))
            self.assertFalse(runner.Runner.is_critical(fake, "dev7"))

    def test_worker(self):
        self.assertTrue(account_turn.is_critical(_Worker(True)))
        self.assertFalse(account_turn.is_critical(_Worker(False)))
        self.assertFalse(account_turn.is_critical(types.SimpleNamespace(name="x", agent={})))

    def test_configuration(self):
        cfg = config.load({"AMEESH_CONFIG": "/nonexistent", "AMEESH_CRITICAL_AGENTS":
                           "orchestrateur, mesh-design"})
        self.assertEqual(cfg.critical_agents, ("orchestrateur", "mesh-design"))
        import json
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({"critical_agents": ["auditeur", "mesh-design"]}, fh)
        self.addCleanup(os.unlink, fh.name)
        self.assertEqual(config.load({"AMEESH_CONFIG": fh.name}).critical_agents,
                         ("auditeur", "mesh-design"))

    def test_journal_d_une_bascule_propre_a_l_agent_une_fois(self):
        worker = _Worker(True)
        switched = {"from": "primaire", "to": "secondaire", "kind": "agent", "reason": "r"}
        with mock.patch.object(account_turn, "_log"):
            account_turn.journal(worker, "codex", switched)
            account_turn.journal(worker, "codex", dict(switched))
        self.assertEqual(len(worker.notes), 1)
        self.assertIn("for this critical agent only", worker.notes[0][0])
        self.assertEqual(worker.notes[0][1]["type"], "agent")


if __name__ == "__main__":
    unittest.main()
