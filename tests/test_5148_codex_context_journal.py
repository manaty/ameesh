# SPDX-License-Identifier: AGPL-3.0-only
"""#5148 (CTX-02c) — le ``last_token_usage`` Codex alimente ``C_last``.

Constat : sur le corpus CTX-05a, les 335 tours Codex avaient ``C_last`` nul.
``codex exec --json`` ne publie qu'un usage cumulé de fin de tour ; le journal
de session du fil porte, lui, un ``last_token_usage`` par appel. Le
normalisateur CTX-02a ne recevait que le flux : la mesure existait dans le
journal (le legacy la lisait pour 251 tours) mais n'entrait jamais dans
``C_last``/``O_last``. Les 84 tours sans contexte legacy étaient les tours de
résumé de rotation (``plafonne=False``).

Ces tests couvrent :
* la lecture du journal **bornée au tour** (``task_started``) : un tour
  précédent ne prête jamais son usage ;
* les motifs d'absence distincts (``journal_missing``, ``no_call``,
  ``turn_not_in_journal``, ``last_token_usage_missing``) ;
* ``C_last``/``O_last`` alimentés au journal sans doubler ``R_turn`` ;
* un tour Codex réel du banc, y compris un tour de résumé, qui publie
  ``turn.context`` avec ``C_last`` mesuré (source ``journal``).

#6044 (volet B) ajoute, dans le même parcours du journal :
* les compactions natives observées (item ``compacted`` et événement
  ``context_compacted``), avec avant/après et nombre par tour ; une
  ``compaction_trigger`` (demande) n'est jamais une réussite ;
* ``C_peak``, plus gros contexte d'appel du tour, publié à côté de ``C_last``
  et repris par le corpus de rejeu.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from ameesh import adapters, context_corpus, context_metrics, cost, registry
from ameesh.runner import AgentWorker, Runner

from .support import FAKEBIN, PgTestCase

FIL = "codex-thread-1"
CALL = {"input_tokens": 61_000, "cached_input_tokens": 60_000,
        "output_tokens": 500}


def _event(payload: dict) -> dict:
    return {"type": "event_msg", "payload": payload}


def _task(started_at: float | None) -> dict:
    payload = {"type": "task_started"}
    if started_at is not None:
        payload["started_at"] = started_at
    return _event(payload)


def _token(usage: dict | None) -> dict:
    info = {} if usage is None else {"last_token_usage": usage}
    return _event({"type": "token_count", "info": info})


def _compacted() -> dict:
    """L'item de rollout écrit après une compaction réussie."""
    return {"type": "compacted", "payload": {"message": "resume du fil"}}


def _context_compacted() -> dict:
    """L'événement de réussite ; il double l'item `compacted`."""
    return _event({"type": "context_compacted"})


def _journal(sessions: str, events: list) -> str:
    dossier = os.path.join(sessions, "2026", "10", "31")
    os.makedirs(dossier, exist_ok=True)
    path = os.path.join(dossier, "rollout-2026-10-31T01-00-00-%s.jsonl" % FIL)
    with open(path, "w", encoding="utf-8") as fh:
        for event in events:
            fh.write(json.dumps(event) + "\n")
    return path


class JournalTourTest(unittest.TestCase):
    """Le journal est lu depuis la fin, borné au ``task_started`` du tour."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ameesh-5148-")
        self.sessions = os.path.join(self.tmp, "sessions")
        self.book = cost.CostBook(state_dir=self.tmp,
                                  codex_sessions=self.sessions)

    def test_dernier_appel_du_tour_dans_la_fenetre(self):
        _journal(self.sessions, [
            _task(1000), _token({"input_tokens": 50_000, "output_tokens": 100}),
            _task(2000),
            _token({"input_tokens": 60_000, "output_tokens": 400}),
            _token({"input_tokens": 61_000, "output_tokens": 500}),
        ])
        found = self.book.codex_turn_last_call_usage(FIL, 1995, 2010)
        self.assertIsNone(found["reason"])
        self.assertEqual(found["usage"]["input_tokens"], 61_000)
        self.assertEqual(found["turn_started"], 2000.0)

    def test_tour_precedent_ne_prete_pas_son_usage(self):
        # Le dernier tour du journal n'est pas celui de la fenêtre : aucun
        # usage ne doit être attribué au tour observé.
        _journal(self.sessions, [
            _task(1000), _token({"input_tokens": 50_000, "output_tokens": 100}),
            _task(2000), _token({"input_tokens": 60_000, "output_tokens": 400}),
        ])
        found = self.book.codex_turn_last_call_usage(FIL, 1500, 1600)
        self.assertIsNone(found["usage"])
        self.assertEqual(found["reason"],
                         context_metrics.REASON_TURN_NOT_IN_JOURNAL)

    def test_tour_commence_sans_appel(self):
        _journal(self.sessions, [_task(2000)])
        found = self.book.codex_turn_last_call_usage(FIL, 1995, 2010)
        self.assertIsNone(found["usage"])
        self.assertEqual(found["reason"], context_metrics.REASON_NO_CALL)

    def test_token_count_sans_last_usage(self):
        _journal(self.sessions, [
            _task(2000), _token(None),
        ])
        found = self.book.codex_turn_last_call_usage(FIL, 1995, 2010)
        self.assertIsNone(found["usage"])
        self.assertEqual(found["reason"],
                         context_metrics.REASON_LAST_USAGE_MISSING)

    def test_journal_absent(self):
        found = self.book.codex_turn_last_call_usage(FIL, 1995, 2010)
        self.assertIsNone(found["usage"])
        self.assertEqual(found["reason"],
                         context_metrics.REASON_JOURNAL_MISSING)

    def test_journal_sans_marqueur_de_tour(self):
        # Rien ne prouve que l'usage est celui du tour : il n'est pas repris.
        _journal(self.sessions, [
            _token({"input_tokens": 60_000, "output_tokens": 400}),
        ])
        found = self.book.codex_turn_last_call_usage(FIL, 1995, 2010)
        self.assertIsNone(found["usage"])
        self.assertEqual(found["reason"],
                         context_metrics.REASON_TURN_NOT_IN_JOURNAL)

    # -- #6044 : compaction observée et pic du tour ----------------------
    def test_compaction_avant_apres_et_pic(self):
        # Journal réel : deux appels avant, la preuve de compaction (item +
        # événement), puis l'appel qui mesure l'après.
        path = os.path.join(os.path.dirname(__file__), "fixtures", "context",
                            "codex_journal_compaction.json")
        with open(path, encoding="utf-8") as fh:
            events = json.load(fh)
        _journal(self.sessions, events)
        found = self.book.codex_turn_last_call_usage(FIL, 1995, 2010)
        self.assertEqual(found["usage"]["input_tokens"], 30_000)
        self.assertEqual(found["peak_usage"]["input_tokens"], 150_000)
        # Deux preuves de la même compaction ne comptent qu'une fois.
        self.assertEqual(found["compaction"], {
            "pre": 150_000, "post": 30_000, "count": 1})

    def test_deux_compactions_dans_le_meme_tour(self):
        _journal(self.sessions, [
            _task(2000),
            _token({"input_tokens": 100_000, "output_tokens": 100}),
            _compacted(),
            _token({"input_tokens": 40_000, "output_tokens": 100}),
            _context_compacted(),
            _token({"input_tokens": 30_000, "output_tokens": 50}),
        ])
        found = self.book.codex_turn_last_call_usage(FIL, 1995, 2010)
        self.assertEqual(found["peak_usage"]["input_tokens"], 100_000)
        self.assertEqual(found["compaction"]["count"], 2)
        self.assertEqual(found["compaction"]["pre"], 100_000)
        self.assertEqual(found["compaction"]["post"], 30_000)

    def test_compaction_sans_appel_apres_garde_l_avant(self):
        # L'après n'est pas inventé : il reste None, le contrat le dit.
        _journal(self.sessions, [
            _task(2000),
            _token({"input_tokens": 90_000, "output_tokens": 100}),
            _compacted(),
        ])
        found = self.book.codex_turn_last_call_usage(FIL, 1995, 2010)
        self.assertEqual(found["usage"]["input_tokens"], 90_000)
        self.assertEqual(found["compaction"]["pre"], 90_000)
        self.assertIsNone(found["compaction"]["post"])
        self.assertEqual(found["compaction"]["count"], 1)

    def test_demande_de_compaction_n_est_pas_une_reussite(self):
        # `compaction_trigger` (et l'item de réponse `context_compaction`)
        # demandent une compaction ; seuls `compacted`/`context_compacted`
        # prouvent qu'elle a eu lieu (contrat §4.2).
        _journal(self.sessions, [
            _task(2000),
            _token({"input_tokens": 90_000, "output_tokens": 100}),
            {"type": "response_item", "payload": {"type": "compaction_trigger"}},
            {"type": "response_item", "payload": {"type": "context_compaction"}},
        ])
        found = self.book.codex_turn_last_call_usage(FIL, 1995, 2010)
        self.assertIsNone(found["compaction"])

    def test_sans_compaction_le_pic_est_le_dernier_appel(self):
        _journal(self.sessions, [
            _task(2000),
            _token({"input_tokens": 20_000, "output_tokens": 100}),
            _token({"input_tokens": 30_000, "output_tokens": 200}),
        ])
        found = self.book.codex_turn_last_call_usage(FIL, 1995, 2010)
        self.assertIsNone(found["compaction"])
        self.assertEqual(found["peak_usage"]["input_tokens"],
                         found["usage"]["input_tokens"])

    def test_compaction_d_un_autre_tour_n_est_pas_reprise(self):
        _journal(self.sessions, [
            _task(1000), _token({"input_tokens": 90_000}),
            _compacted(), _token({"input_tokens": 20_000}),
            _task(2000), _token({"input_tokens": 50_000}),
        ])
        found = self.book.codex_turn_last_call_usage(FIL, 1995, 2010)
        self.assertIsNone(found["compaction"])
        self.assertEqual(found["peak_usage"]["input_tokens"], 50_000)


class NormalizerJournalTest(unittest.TestCase):
    def test_journal_alimente_c_last_sans_doubler_r_turn(self):
        normalizer = context_metrics.TurnNormalizer("codex", session="s")
        normalizer.total({"input_tokens": 600_000,
                          "cached_input_tokens": 590_000,
                          "output_tokens": 900})
        normalizer.journal_call(CALL)
        document = normalizer.finish()
        measures = document["measures"]
        self.assertEqual(measures["C_last"]["value"], 61_000)
        self.assertEqual(measures["C_last"]["source"], "journal")
        self.assertEqual(measures["C_last"]["quality"], "measured")
        self.assertEqual(measures["O_last"]["value"], 500)
        # Le journal ne publie qu'un appel : R_turn reste au cumul du flux,
        # ici inconnu faute de base, jamais la somme d'un seul appel.
        self.assertIsNone(measures["R_turn"]["value"])
        self.assertEqual(measures["R_turn"]["source"], "total")
        self.assertEqual(document["counts"]["journal_calls"], 1)
        # Sans compaction, le pic est ce dernier appel du journal.
        self.assertEqual(measures["C_peak"]["value"], 61_000)
        self.assertEqual(measures["C_peak"]["source"], "journal")

    def test_pic_du_journal_superieur_au_dernier_appel(self):
        normalizer = context_metrics.TurnNormalizer("codex", session="s")
        normalizer.journal_call(
            {"input_tokens": 30_000, "output_tokens": 50},
            peak_usage={"input_tokens": 150_000})
        measures = normalizer.finish()["measures"]
        self.assertEqual(measures["C_last"]["value"], 30_000)
        self.assertEqual(measures["C_peak"]["value"], 150_000)
        self.assertEqual(measures["C_peak"]["quality"], "measured")

    def test_pic_du_flux_est_le_max_des_appels(self):
        normalizer = context_metrics.TurnNormalizer("claude", session="s")
        normalizer.call({"input_tokens": 100,
                         "cache_read_input_tokens": 10,
                         "cache_creation_input_tokens": 5,
                         "output_tokens": 5}, "c1")
        normalizer.call({"input_tokens": 150,
                         "cache_read_input_tokens": 20,
                         "cache_creation_input_tokens": 5,
                         "output_tokens": 5}, "c2")
        measures = normalizer.finish()["measures"]
        self.assertEqual(measures["C_last"]["value"], 175)
        self.assertEqual(measures["C_peak"]["value"], 175)

    def test_pic_sans_releve_reste_null_avec_sa_raison(self):
        normalizer = context_metrics.TurnNormalizer("codex", session="s")
        normalizer.journal_unavailable(
            context_metrics.REASON_JOURNAL_MISSING)
        measures = normalizer.finish()["measures"]
        self.assertIsNone(measures["C_peak"]["value"])
        self.assertEqual(measures["C_peak"]["quality"], "unknown")
        self.assertIn("context_unknown", measures["C_peak"]["reasons"])
        self.assertIn("journal_missing", measures["C_peak"]["reasons"])

    def test_journal_muet_qualifie_l_absence(self):
        normalizer = context_metrics.TurnNormalizer("codex", session="s")
        normalizer.journal_unavailable(
            context_metrics.REASON_JOURNAL_MISSING)
        measures = normalizer.finish()["measures"]
        self.assertIsNone(measures["C_last"]["value"])
        self.assertEqual(measures["C_last"]["quality"], "unknown")
        self.assertIn("context_unknown", measures["C_last"]["reasons"])
        self.assertIn("journal_missing", measures["C_last"]["reasons"])
        self.assertIsNone(measures["O_last"]["value"])
        self.assertIn("output_unknown", measures["O_last"]["reasons"])

    def test_appel_de_flux_prioritaire_sur_le_journal(self):
        normalizer = context_metrics.TurnNormalizer("codex", session="s")
        normalizer.call({"input_tokens": 42_000, "output_tokens": 7}, "c1")
        normalizer.journal_call(CALL)
        measures = normalizer.finish()["measures"]
        self.assertEqual(measures["C_last"]["value"], 42_000)
        self.assertEqual(measures["C_last"]["source"], "stream")
        self.assertEqual(measures["R_turn"]["value"], 42_000)


class TurnContextCodexTest(PgTestCase):
    """Un tour Codex du banc publie ``C_last`` mesuré depuis son journal."""

    def setUp(self) -> None:
        super().setUp()
        self.db.execute("TRUNCATE turn_costs, spend_pending CASCADE")
        self.sessions = os.path.join(self.tmp, "codex-sessions")

    def _worker(self, name: str):
        runner = Runner(self.cfg, self.db, once=True)
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        self.register(name, "codex", cwd=cwd)
        lease = registry.claim(self.db, name, runner.runner_id, 3600)
        self.assertIsNotNone(lease)
        worker = AgentWorker(runner, registry.get(self.db, name), lease)
        self.addCleanup(worker.watchdog_stop.set)
        return worker

    def _tour(self, worker, prompt: str = "travail", *,
              journal: bool = True, compact: str | None = None) -> bool:
        env = {
            "AMEESH_BIN_DIR": FAKEBIN,
            "AMEESH_TEST_LOG": self.turns_log,
            "AMEESH_CODEX_SESSIONS": self.sessions,
            "AMEESH_TEST_CODEX_USAGE": json.dumps({
                "input_tokens": 600_000, "cached_input_tokens": 590_000,
                "output_tokens": 900}),
        }
        if journal:
            # Le faux harnais écrit le journal **pendant** le tour : son
            # `task_started` tombe dans la fenêtre observée, sans dépendre du
            # temps passé à créer le worker (CI lente).
            env["AMEESH_TEST_CODEX_JOURNAL"] = self.sessions
            env["AMEESH_TEST_CODEX_LAST_USAGE"] = json.dumps(CALL)
        if compact:
            env["AMEESH_TEST_CODEX_COMPACT"] = compact
        with mock.patch.dict(os.environ, env, clear=False):
            return worker.run_turn({"kind": "prompt", "prompt": prompt,
                                    "ids": []})

    def _published(self, agent: str) -> dict:
        rows = self.db.query(
            "select data from ameesh_events where type = %s"
            " and data->>'agent' = %s order by id desc limit 1",
            (cost.CONTEXT_EVENT, agent))
        self.assertTrue(rows, "aucun événement turn.context publié")
        return rows[0]["data"]

    def test_c_last_du_journal_dans_turn_context(self):
        worker = self._worker("cx-journal")
        self.assertTrue(self._tour(worker))
        data = self._published("cx-journal")
        measures = data["context"]["measures"]
        self.assertEqual(measures["C_last"]["value"], 61_000)
        self.assertEqual(measures["C_last"]["quality"], "measured")
        self.assertEqual(measures["C_last"]["source"], "journal")
        self.assertEqual(measures["O_last"]["value"], 500)
        # Le legacy lit le même journal et garde sa valeur : rien ne change
        # pour la rotation.
        self.assertEqual(data["legacy"]["context_tokens"], 61_000)

    def test_tour_de_resume_publie_c_last_sans_context_legacy(self):
        # `plafonne=False` : le legacy n'appelle pas le journal, mais
        # l'observation le lit et publie C_last (les 84 tours du corpus).
        worker = self._worker("cx-resume")
        self.assertTrue(self._tour(worker, prompt=adapters.SUMMARY_PROMPT))
        data = self._published("cx-resume")
        measures = data["context"]["measures"]
        self.assertEqual(measures["C_last"]["value"], 61_000)
        self.assertEqual(measures["C_last"]["source"], "journal")
        self.assertIsNone(data["legacy"]["context_tokens"])

    def test_journal_absent_reste_inconnu_avec_sa_cause(self):
        worker = self._worker("cx-sans-journal")
        self.assertTrue(self._tour(worker, journal=False))
        data = self._published("cx-sans-journal")
        measures = data["context"]["measures"]
        self.assertIsNone(measures["C_last"]["value"])
        self.assertEqual(measures["C_last"]["quality"], "unknown")
        self.assertIn("context_unknown", measures["C_last"]["reasons"])
        self.assertIn("journal_missing", measures["C_last"]["reasons"])
        self.assertIsNone(data["legacy"]["context_tokens"])

    # -- #6044 : pic et compaction dans turn.context ---------------------
    def test_compaction_codex_publiee_dans_turn_context(self):
        worker = self._worker("cx-compaction")
        self.assertTrue(self._tour(worker, compact="observed"))
        data = self._published("cx-compaction")
        measures = data["context"]["measures"]
        # Le dernier appel suit la compaction ; le pic est l'avant.
        self.assertEqual(measures["C_last"]["value"], 20_000)
        self.assertEqual(measures["C_peak"]["value"], 50_000)
        self.assertEqual(measures["C_peak"]["source"], "journal")
        compaction = data["context"]["compaction"]
        self.assertEqual(compaction["pre"], 50_000)
        self.assertEqual(compaction["post"], 20_000)
        self.assertEqual(compaction["count"], 1)
        self.assertEqual(compaction["quality"], "measured")
        self.assertIs(data["decision"]["compacted"], True)
        # Le legacy garde sa lecture : le dernier appel du journal.
        self.assertEqual(data["legacy"]["context_tokens"], 20_000)

    def test_pic_codex_publie_sans_compaction(self):
        worker = self._worker("cx-pic")
        self.assertTrue(self._tour(worker))
        data = self._published("cx-pic")
        measures = data["context"]["measures"]
        self.assertEqual(measures["C_last"]["value"], 61_000)
        self.assertEqual(measures["C_peak"]["value"], 61_000)
        self.assertIsNone(data["context"]["compaction"])
        self.assertIs(data["decision"]["compacted"], False)

    def test_demande_de_compaction_ne_publie_pas_compacted(self):
        worker = self._worker("cx-demande")
        self.assertTrue(self._tour(worker, compact="request"))
        data = self._published("cx-demande")
        self.assertIsNone(data["context"]["compaction"])
        self.assertIs(data["decision"]["compacted"], False)
        # La demande ne modifie pas la mesure du dernier appel.
        self.assertEqual(data["context"]["measures"]["C_last"]["value"], 61_000)


class CorpusPeakCompactionTest(unittest.TestCase):
    """#6044 : le corpus de rejeu porte C_peak et le nombre de compactions."""

    def _payload(self, *, compaction=None, peak=50_000):
        context = {
            "measures": {
                "C_last": {"value": 20_000, "quality": "measured",
                           "reasons": [], "granularity": "call",
                           "source": "journal"},
            },
            "partial": False,
        }
        if peak is not None:
            context["measures"]["C_peak"] = {
                "value": peak, "quality": "measured", "reasons": [],
                "granularity": "call", "source": "journal"}
        if compaction is not None:
            context["compaction"] = compaction
        return {
            "schema": "ameesh-context/1", "turn_id": "turn-6044",
            "agent": "cx-corpus", "harness": "codex", "model": "gpt-x",
            "model_quality": "requested", "mode": "non-resident",
            "role": "build", "session": "session-6044",
            "context": context,
            "decision": {
                "compacted": compaction is not None,
                "policy": {"session": "taille", "context_max_tokens": 15_000_000,
                           "session_max_tokens": 150_000},
            },
        }

    def test_c_peak_et_nombre_de_compactions_dans_le_corpus(self):
        line = context_corpus.corpus_line(self._payload(compaction={
            "pre": 50_000, "post": 20_000, "quality": "measured",
            "reasons": [], "trigger": None, "count": 2}))
        context_corpus.validate_line(line)
        self.assertEqual(line["C_peak"]["value"], 50_000)
        self.assertEqual(line["decision"]["compacted"], True)
        self.assertEqual(line["decision"]["compaction"]["count"], 2)

    def test_corpus_ancien_sans_c_peak_reste_lisible(self):
        line = context_corpus.corpus_line(self._payload(peak=None))
        context_corpus.validate_line(line)
        self.assertIsNone(line["C_peak"]["value"])
        self.assertIn("context_unknown", line["C_peak"]["reasons"])


if __name__ == "__main__":
    unittest.main()
