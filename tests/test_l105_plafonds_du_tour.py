# SPDX-License-Identifier: AGPL-3.0-only
"""L105 — les plafonds agissent aussi PENDANT un tour.

Constat : un tour d'orchestrateur a duré 3 h 27 en relisant 181 M jetons ; le
hook lui remettait le courrier pendant le tour, qui ne finissait jamais, et la
rotation sur la taille (L26/L60) n'agissait qu'entre deux tours. Désormais :
plafond de contexte et durée maximale ferment le tour au prochain point sûr,
le hook borne le courrier d'un tour, et la rotation a son passage.
Postgres réel ; faux harnais `claude` du banc.
"""
from __future__ import annotations

import json
import os
import time
import unittest
from unittest import mock

from ameesh import adapters, cli, mail, registry, storage
from ameesh.mesh_cli import parse_duration
from ameesh.runner import AgentWorker, Runner

from .support import FAKEBIN, PgTestCase


class LectureDuFluxTest(unittest.TestCase):
    def test_claude_usage_par_appel_et_point_sur(self):
        lecteur = adapters.ClaudeStream()
        appel = lecteur.parse(json.dumps({"type": "assistant", "message": {
            "id": "m1", "usage": {"input_tokens": 3, "cache_read_input_tokens": 7},
            "content": [{"type": "tool_use", "id": "t", "name": "x", "input": {}}]}}))
        self.assertEqual(appel["call_id"], "m1")
        self.assertEqual(adapters.reread_tokens("claude", appel["call_usage"]), 10)
        self.assertNotIn("safe_point", appel)
        resultat = lecteur.parse(json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t", "content": "ok"}]}}))
        self.assertTrue(resultat.get("safe_point"))
        # un message utilisateur sans résultat d'outil n'est pas un point sûr
        self.assertNotIn("safe_point", lecteur.parse(json.dumps(
            {"type": "user", "message": {"content": "texte"}})))

    def test_codex_et_dsh_points_surs(self):
        codex = adapters.CodexStream()
        self.assertTrue(codex.parse(json.dumps({"type": "item.completed", "item": {
            "type": "command_execution"}})).get("safe_point"))
        self.assertNotIn("safe_point", codex.parse(json.dumps({
            "type": "item.completed", "item": {"type": "agent_message", "text": "x"}})))
        dsh = adapters.DshStream().parse(json.dumps({
            "type": "status", "phase": "step_end",
            "usage": {"inputTokens": 2, "cacheReadTokens": 5}}))
        self.assertTrue(dsh["safe_point"])
        self.assertEqual(adapters.reread_tokens("deepseek", dsh["call_usage"]), 7)
        # Codex compte déjà le cache dans l'entrée
        self.assertEqual(adapters.reread_tokens(
            "codex", {"input_tokens": 9, "cached_input_tokens": 6}), 9)

    def test_duree_lisible(self):
        self.assertEqual(parse_duration("30m"), 1800)
        self.assertEqual(parse_duration("1.5h"), 5400)
        self.assertEqual(parse_duration("90s"), 90)
        self.assertEqual(parse_duration("0"), 0)
        for mauvais in ("", "-1", "abc", "nan", "3j"):
            with self.assertRaises(ValueError, msg=mauvais):
                parse_duration(mauvais)


class _Base(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("TRUNCATE turn_costs, spend_pending CASCADE")

    def _worker(self, name: str, **reglages):
        runner = Runner(self.cfg, self.db, once=True)
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        self.register(name, "claude", cwd=cwd)
        if reglages:
            storage.of(self.db).operations.set_settings(
                name, {k: str(v) for k, v in reglages.items()})
        lease = registry.claim(self.db, name, runner.runner_id, 3600)
        self.assertIsNotNone(lease)
        worker = AgentWorker(runner, registry.get(self.db, name), lease)
        self.addCleanup(worker.watchdog_stop.set)
        return runner, worker

    def _env(self, appels=None):
        env = {"AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log,
               "AMEESH_TEST_CLAUDE_CALLS": json.dumps(appels or [])}
        return mock.patch.dict(os.environ, env, clear=False)

    def _evenements(self, worker) -> str:
        with open(worker._path("events.jsonl"), encoding="utf-8") as fh:
            return fh.read()



class PlafondDeContexteTest(_Base):
    #: le premier appel relit 1 200 jetons (plafond 1 000) puis appelle un outil ;
    #: le second ne doit jamais avoir lieu
    APPELS = [{"reread": 1200, "tool": True},
              {"reread": 50, "tool": True, "sleep": 3}]

    def test_tour_clos_au_point_sur_puis_rotation_et_suite(self):
        _runner, worker = self._worker("orch-ctx", context_max_tokens=1000)
        debut = time.monotonic()
        with self._env(self.APPELS), mock.patch.object(
                worker, "_fil_note", wraps=worker._fil_note) as note:
            self.assertTrue(worker.run_turn({"kind": "prompt", "prompt": "travail",
                                             "ids": []}))
        self.assertLess(time.monotonic() - debut, 20)
        self.assertEqual(note.call_args.kwargs["meta"]["action"], "tour-clos")
        flux = self._evenements(worker)
        self.assertIn('"tool_use_id": "outil-0"', flux)   # l'outil en cours a fini
        self.assertNotIn('"tool_use_id": "outil-1"', flux)  # le suivant, jamais
        self.assertTrue(worker.last_turn_closed.startswith("plafond de contexte"))
        agent = registry.get(self.db, "orch-ctx")
        self.assertEqual(agent["status"], "idle")
        self.assertIn("clos : plafond de contexte", agent["status_text"])
        # le grand livre relit l'usage par appel d'un tour sans `result`
        self.assertGreaterEqual(worker.last_turn_reread, 1200)
        self.assertTrue(worker.rotation_forcee)
        # entre deux tours : rotation avec résumé de reprise, dans l'ancienne session
        self.clear_turns()
        with self._env():
            self.assertTrue(worker.maybe_rotate())
        self.assertIn("--resume", self.turns()[-1]["argv"])
        self.assertIsNone(worker.current_session())
        self.assertTrue(worker.resume_summary)
        self.assertIsNone(worker.rotation_forcee)
        # puis la suite du travail, dans la session neuve ouverte sur le résumé
        spec = worker.pick()
        self.assertEqual(spec["kind"], "suite")
        self.clear_turns()
        with self._env():
            self.assertTrue(worker.run_turn(spec))
        argv = self.turns()[-1]["argv"]
        self.assertNotIn("--resume", argv)
        self.assertIn("Reprise de session après rotation", argv[-1])
        self.assertIn("Suite : ton tour précédent a été clos", argv[-1])
        self.assertIsNone(worker.suite)

    def test_politique_jamais_suite_sans_rotation(self):
        _runner, worker = self._worker("orch-jamais", context_max_tokens=1000,
                                       session_policy="jamais")
        with self._env(self.APPELS):
            self.assertTrue(worker.run_turn({"kind": "prompt", "prompt": "travail",
                                             "ids": []}))
        # la politique `jamais` se dispense du plafond de contexte
        self.assertIn("msg-1", self._evenements(worker))
        self.assertIsNone(worker.last_turn_closed)

    def test_tour_de_resume_jamais_plafonne(self):
        _runner, worker = self._worker("orch-resume", context_max_tokens=1000)
        with self._env([{"reread": 1200, "tool": True}, {"reread": 10, "tool": True}]):
            self.assertTrue(worker.run_turn({"kind": "prompt",
                                             "prompt": adapters.SUMMARY_PROMPT, "ids": []}))
        self.assertIn("msg-1", self._evenements(worker))
        self.assertIsNone(worker.last_turn_closed)
        self.assertIsNone(worker.suite)


class DureeMaximaleTest(_Base):
    def test_tour_clos_a_la_duree_maximale_suite_dans_la_meme_session(self):
        _runner, worker = self._worker("orch-duree", turn_max_seconds=1)
        self.assertEqual(worker.turn_max_seconds(), 1)
        appels = [{"reread": 10, "tool": True, "sleep": 1.5},
                  {"reread": 10, "tool": True, "sleep": 30}]
        debut = time.monotonic()
        with self._env(appels):
            self.assertTrue(worker.run_turn({"kind": "prompt", "prompt": "long",
                                             "ids": []}))
        self.assertLess(time.monotonic() - debut, 20)
        flux = self._evenements(worker)
        self.assertIn('"tool_use_id": "outil-0"', flux)   # l'outil en cours a fini
        self.assertNotIn('"tool_use_id": "outil-1"', flux)
        self.assertTrue(worker.last_turn_closed.startswith("durée maximale du tour"))
        self.assertIsNone(worker.rotation_forcee)
        session = worker.current_session()
        self.assertTrue(session)
        spec = worker.pick()
        self.assertEqual(spec["kind"], "suite")
        self.clear_turns()
        with self._env():
            self.assertTrue(worker.run_turn(spec))
        argv = self.turns()[-1]["argv"]
        self.assertEqual(argv[argv.index("--resume") + 1], session)
        self.assertIn("durée maximale du tour", argv[-1])

    def test_sans_limite_le_tour_va_au_bout(self):
        _runner, worker = self._worker("orch-libre", turn_max_seconds=0)
        with self._env([{"reread": 10, "tool": True, "sleep": 0.3},
                        {"reread": 10, "tool": True}]):
            self.assertTrue(worker.run_turn({"kind": "prompt", "prompt": "x", "ids": []}))
        self.assertIn("msg-1", self._evenements(worker))
        self.assertIsNone(worker.last_turn_closed)

    def test_defaut_et_environnement(self):
        from ameesh import config as config_mod
        self.assertEqual(config_mod.Config().turn_max_seconds, 1800)
        self.assertEqual(config_mod.Config().turn_mail_max, 20)  # 5 avant le 2026-10-11
        cfg = config_mod.load(env={"AMEESH_TURN_MAX_SECONDS": "600",
                                   "AMEESH_TURN_MAIL_MAX": "2",
                                   "AMEESH_CONFIG": "/nulle-part.json"})
        self.assertEqual((cfg.turn_max_seconds, cfg.turn_mail_max), (600.0, 2))
        cfg = config_mod.load(env={"AMEESH_TURN_MAIL_MAX": "nan",
                                   "AMEESH_CONFIG": "/nulle-part.json"})
        self.assertEqual(cfg.turn_mail_max, 20)


class CourrierBorneTest(_Base):
    def _hook(self, worker, tour: str, event: str = "PostToolUse", borne: int = 5) -> str:
        env = self.env(AGENT_MAIL_NAME=worker.name, AMEESH_RUNNER_ID=worker.runner.runner_id,
                       AMEESH_LEASE_EPOCH=str(worker.epoch), AMEESH_TURN_ID=tour,
                       AMEESH_TURN_MAIL_MAX=str(borne))
        proc = self.cli("hook", "claude", env=env,
                        stdin=json.dumps({"hook_event_name": event}))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def _livres(self, ids) -> int:
        return sum(1 for i in ids if mail.get(self.db, i)["delivered_ts"] is not None)

    def test_au_plus_cinq_messages_par_tour_puis_conclure(self):
        _runner, worker = self._worker("orch-mail")
        ids = [mail.send(self.db, "pair", "orch-mail", "message n°%d" % i) for i in range(7)]
        sortie = self._hook(worker, "tour-1")
        self.assertIn("message n°4", sortie)
        self.assertNotIn("message n°5", sortie)
        self.assertIn("2 message(s) en attente", sortie)
        self.assertIn("Conclus ce tour", sortie)
        self.assertEqual(self._livres(ids), 5)
        # borne atteinte : plus rien de remis ni de réservé, et le Stop ne
        # relance plus le tour
        self.assertEqual(self._hook(worker, "tour-1"), "")
        self.assertEqual(self._hook(worker, "tour-1", event="Stop"), "")
        self.assertEqual(self._livres(ids), 5)
        jeton = mail.new_token()
        restants = mail.reserve(self.db, "orch-mail", worker.runner.runner_id, worker.epoch,
                                jeton, porteur="consigne")
        self.assertEqual(len(restants), 2)  # rien ne restait réservé par le hook
        mail.release(self.db, "orch-mail", jeton, [int(r["id"]) for r in restants])
        # l'exécuteur le note après le tour
        worker._note_courrier_borne("tour-1", 12.0)
        self.assertEqual(worker.last_turn_closed, "courrier borné")
        self.assertIsNone(worker.suite)  # le reste du courrier ouvre le tour suivant

    def test_un_nouveau_tour_remet_la_suite(self):
        _runner, worker = self._worker("orch-mail2")
        ids = [mail.send(self.db, "pair", "orch-mail2", "message n°%d" % i) for i in range(7)]
        self._hook(worker, "tour-1", borne=5)
        sortie = self._hook(worker, "tour-2", borne=5)
        self.assertIn("message n°5", sortie)
        self.assertIn("message n°6", sortie)
        self.assertNotIn("en attente", sortie)
        self.assertEqual(self._livres(ids), 7)

    def test_stop_bloque_dans_la_borne(self):
        _runner, worker = self._worker("orch-stop")
        ids = [mail.send(self.db, "pair", "orch-stop", "message n°%d" % i) for i in range(3)]
        sortie = json.loads(self._hook(worker, "tour-1", event="Stop", borne=2))
        self.assertEqual(sortie["decision"], "block")
        self.assertIn("1 message(s) en attente", sortie["reason"])
        self.assertEqual(self._livres(ids), 2)

    def test_borne_zero_ou_hors_tour_mene_sans_borne(self):
        _runner, worker = self._worker("orch-libre")
        ids = [mail.send(self.db, "pair", "orch-libre", "message n°%d" % i) for i in range(7)]
        sortie = self._hook(worker, "tour-1", borne=0)
        self.assertIn("message n°6", sortie)
        self.assertEqual(self._livres(ids), 7)


class ReglagesTest(_Base):
    def test_ameesh_set_des_plafonds_du_tour(self):
        self._worker("orch-set")
        proc = self.mesh("set", "orch-set", "turn_max_seconds=45m", "turn_mail_max=3")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("tour_max_s=2700", proc.stdout)
        self.assertIn("courrier_par_tour=3", proc.stdout)
        agent = registry.get(self.db, "orch-set")
        self.assertEqual((agent["turn_max_seconds"], agent["turn_mail_max"]), (2700, 3))
        self.assertEqual(self.mesh("set", "orch-set", "turn_max_seconds=bientot").returncode, 2)
        self.assertEqual(self.mesh("set", "orch-set", "turn_mail_max=-1").returncode, 2)
        proc = self.mesh("set", "orch-set", "turn_max_seconds=", "turn_mail_max=")
        self.assertIn("tour_max_s=1800 (défaut)", proc.stdout)
        self.assertIn("courrier_par_tour=20 (défaut)", proc.stdout)

    def test_borne_du_courrier_passee_au_harnais(self):
        _runner, worker = self._worker("orch-env", turn_mail_max=2)
        with self._env():
            self.assertTrue(worker.run_turn({"kind": "prompt", "prompt": "x", "ids": []}))
        self.assertEqual(self.turns()[-1]["env"]["AMEESH_TURN_MAIL_MAX"], "2")
        self.assertTrue(self.turns()[-1]["env"]["AMEESH_TURN_ID"])


if __name__ == "__main__":
    unittest.main()
