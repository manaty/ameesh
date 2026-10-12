# SPDX-License-Identifier: AGPL-3.0-only
"""L218 : le résumé de rotation est la réponse ENTIÈRE du tour de résumé.

Avant, l'exécuteur gardait les 4 000 derniers caractères affichés : un résumé
de ≈ 6,5 k caractères perdait sa tête et finissait sur « == fin du tour : »
suivi d'une copie tronquée de son début (auditeur, 2026-10-11 06:02)."""
from __future__ import annotations

import dataclasses
import json
import os
import unittest
from unittest import mock

from ameesh import adapters, registry
from ameesh.runner import AgentWorker, Runner

from .support import FAKEBIN, PgTestCase


def long_resume(paragraphes: int = 40) -> str:
    """Un résumé à paragraphes numérotés, > 6 000 caractères pour 40."""
    return "\n\n".join(
        "## Paragraphe %02d\nÉtat du lot, décisions et sources du paragraphe %02d. %s"
        % (i, i, ("Détail factuel. " * 8).strip()) for i in range(paragraphes))


class BorneTest(unittest.TestCase):
    def test_resume_court_inchange(self):
        self.assertEqual(adapters.bound_summary("  un résumé  \n"), "un résumé")

    def test_resume_long_garde_la_tete_et_coupe_sur_un_paragraphe(self):
        texte = long_resume(200)
        self.assertGreater(len(texte), adapters.SUMMARY_MAX_CHARS)
        borne = adapters.bound_summary(texte)
        self.assertTrue(borne.startswith("## Paragraphe 00\n"))
        tete, note = borne.split("\n\n[Summary cut by the runner: ")
        self.assertTrue(texte.startswith(tete))
        # la coupe tombe juste avant un paragraphe, jamais au milieu
        self.assertTrue(texte[len(tete):].startswith("\n\n## Paragraphe "))
        self.assertIn("%d characters kept out of %d" % (len(tete), len(texte)), note)
        self.assertLessEqual(len(tete), adapters.SUMMARY_MAX_CHARS)

    def test_sans_paragraphe_coupe_sur_une_ligne_puis_net(self):
        lignes = "\n".join("ligne %04d" % i for i in range(100))
        borne = adapters.bound_summary(lignes, limit=200)
        tete = borne.split("\n\n[Summary cut")[0]
        self.assertTrue(lignes[len(tete):].startswith("\n"))
        bloc = "x" * 500
        net = adapters.bound_summary(bloc, limit=200)
        tete = net.split("\n\n[")[0]
        self.assertEqual(tete, "x" * len(tete))
        self.assertGreater(len(tete), 50)
        self.assertLessEqual(len(net), 200)

    def test_codex_rend_son_dernier_message_comme_reponse(self):
        with mock.patch.dict(os.environ, {"AMEESH_BIN_DIR": FAKEBIN}):
            adapter = adapters.adapter_for("codex")
        item = adapter.parse(json.dumps({"type": "item.completed", "item": {
            "type": "agent_message", "text": "le résumé"}}))
        self.assertEqual(item["final"], "le résumé")

    def test_codex_message_vide_compte_comme_reponse(self):
        """Verdict codex #178 (2) : un dernier agent_message vide est la
        réponse du tour ; le commentaire qui le précède n'en tient pas lieu."""
        with mock.patch.dict(os.environ, {"AMEESH_BIN_DIR": FAKEBIN}):
            adapter = adapters.adapter_for("codex")
        item = adapter.parse(json.dumps({"type": "item.completed", "item": {
            "type": "agent_message", "text": ""}}))
        self.assertEqual(item.get("final"), "")
        self.assertNotIn("display", item)

    def test_note_de_coupe_comprise_dans_la_borne(self):
        """Verdict codex #178 (3) : la note tient dans la borne (16 001
        caractères donnaient 16 105)."""
        for texte in ("x" * (adapters.SUMMARY_MAX_CHARS + 1), long_resume(200),
                      "\n".join("ligne %05d" % i for i in range(5000))):
            borne = adapters.bound_summary(texte)
            self.assertLessEqual(len(borne), adapters.SUMMARY_MAX_CHARS)
            self.assertIn("[Summary cut by the runner: ", borne)
        for limit in (200, 300):
            self.assertLessEqual(len(adapters.bound_summary("y" * 1000, limit=limit)), limit)


class RotationResumeEntierTest(PgTestCase):
    def _worker(self, name: str = "rot218", harness: str = "claude"):
        cfg = dataclasses.replace(self.cfg, session_min_turns=1,
                                  session_max_turn_seconds=0.0, session_max_tokens=0.0)
        runner = Runner(cfg, self.db, once=True)
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        self.register(name, harness, cwd=cwd)
        lease = registry.claim(self.db, name, runner.runner_id, 3600)
        return AgentWorker(runner, registry.get(self.db, name), lease)

    def test_resume_de_plus_de_6000_caracteres_repris_en_entier(self):
        worker = self._worker()
        resume = long_resume()
        self.assertGreater(len(resume), 6000)
        env = {"AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log}
        try:
            with mock.patch.dict(os.environ, env, clear=False):
                self.assertTrue(worker.run_turn(
                    {"kind": "prompt", "prompt": "premier tour", "ids": []}))
                with mock.patch.dict(os.environ, {"AMEESH_TEST_RESULT": resume}):
                    self.assertTrue(worker.maybe_rotate())
                self.assertEqual(worker.resume_summary, resume)
                self.assertNotIn("== end of turn", worker.resume_summary)
                with open(os.path.join(worker.state_dir, "session-history.jsonl"),
                          encoding="utf-8") as fh:
                    self.assertEqual(json.loads(fh.readline())["resume"], resume)
                # la session neuve s'ouvre sur le résumé entier, tête comprise
                self.assertTrue(worker.run_turn(
                    {"kind": "prompt", "prompt": "deuxième tour", "ids": []}))
            argv = " ".join(self.turns()[-1]["argv"])
            self.assertIn(resume, argv)
        finally:
            worker.watchdog_stop.set()

    def _rotation_sans_resume(self, worker, env_resume: dict):
        env = {"AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log}
        try:
            with mock.patch.dict(os.environ, env, clear=False):
                self.assertTrue(worker.run_turn(
                    {"kind": "prompt", "prompt": "premier tour", "ids": []}))
                ancienne = worker.current_session()
                self.assertTrue(ancienne)
                with mock.patch.dict(os.environ, env_resume):
                    self.assertFalse(worker.maybe_rotate())
                    # la bascule de compte (account_turn) lit le même résumé :
                    # vide, elle prend le brief déterministe L39
                    self.assertTrue(worker.run_turn({
                        "kind": "prompt", "prompt": adapters.SUMMARY_PROMPT, "ids": []}))
                    self.assertEqual(worker.turn_summary(), "")
                self.assertEqual(worker.current_session(), ancienne)
                self.assertEqual(worker.resume_summary, "")
                self.assertFalse(os.path.exists(
                    os.path.join(worker.state_dir, "session-history.jsonl")))
        finally:
            worker.watchdog_stop.set()

    def test_finale_vide_pas_de_resume(self):
        """Verdict codex #178 (1) : un tour de résumé réussi à réponse vide
        n'a pas de résumé ; « == fin du tour » n'en tient jamais lieu."""
        self._rotation_sans_resume(self._worker("rot218v"), {"AMEESH_TEST_RESULT": ""})

    def test_codex_commentaire_puis_finale_vide_pas_de_resume(self):
        """Verdict codex #178 (2) : commentaire initial puis réponse vide."""
        worker = self._worker("rot218c", harness="codex")
        self._rotation_sans_resume(worker, {"AMEESH_TEST_MESSAGES": json.dumps(
            ["Je rédige le résumé de reprise.", ""])})


if __name__ == "__main__":
    unittest.main()
