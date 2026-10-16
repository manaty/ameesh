# SPDX-License-Identifier: AGPL-3.0-only
"""#2096 : mémoire du runner (bornes du tour, lectures du journal, arènes).

Diagnostic du 16/10 (impl-05) : le socle du runner est sous 150 Mo, mais le
pic d'un gros tour (flux de plusieurs Mo) est retenu par l'allocateur et le
texte final restait vivant sans borne jusqu'au tour suivant. Deuxième volet
(mesure du bureau, 16/10 19:35) : la taille de `events.jsonl` suivait la
mémoire des exécuteurs — les lecteurs du journal le chargeaient et
l'analysaient entier à chaque tour (plusieurs fois), pour n'en garder qu'une
tranche.

Ces tests fixent les bornes (texte final, affichage du repli de résumé), la
décision de rendre les arènes seulement après un tour qui a fait croître le
processus, les lectures du journal (tranche, comptage, session par la fin) et
le journal du flux inchangé.

Le seuil de RSS avant/après vit dans la note du lot, pas ici : un chiffre de
RSS dépend de l'allocateur de la machine (canon §3, test d'environnement).
"""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import stat
import tempfile
import unittest
from unittest import mock

from ameesh import adapters, cost, registry
from ameesh.platform import process
from ameesh.runner import (
    AgentWorker,
    Runner,
    SUMMARY_DISPLAY_MAX_CHARS,
    SUMMARY_DISPLAY_MAX_FRAGMENTS,
    TURN_TRIM_RSS_GROWTH,
)
from .support import FAKEBIN, PgTestCase

#: faux harnais Claude : un flux dont le résultat final fait `BIG_N`
#: caractères, la forme qui a servi à mesurer le pic (sonde du 16/10).
#: L'affichage reste court : c'est le résultat final qui était retenu sans
#: borne, et un affichage de plusieurs Mo noierait la sortie de la CI.
BIGFAKE = r'''#!/usr/bin/env python3
import json, os
n = int(os.environ.get("BIG_N", "2000000"))
text = "x" * n
print(json.dumps({"type": "assistant", "message": {"content": [
    {"type": "text", "text": "tour de mesure"}]}}), flush=True)
print(json.dumps({"type": "result", "subtype": "success", "result": text,
                  "is_error": False, "total_cost_usd": 0.0125,
                  "usage": {"input_tokens": 100, "output_tokens": 20},
                  "session_id": "big-sess-1"}), flush=True)
'''


class BareWorker:
    """Un worker sans base ni harnais : les attributs que ces méthodes lisent.

    `AgentWorker` est un `Thread` : on ne l'instancie pas avec `__new__` (son
    `name` exige `Thread.__init__`), on porte les trois méthodes testées."""

    def __init__(self, **attrs):
        self.name = "memoire"
        self.summary_display = []
        self._summary_chars = 0
        self.__dict__.update(attrs)

    def _keep_summary_display(self, text):
        return AgentWorker._keep_summary_display(self, text)

    def _release_turn_memory(self, before):
        return AgentWorker._release_turn_memory(self, before)

    def turn_summary(self):
        return AgentWorker.turn_summary(self)


def bare_worker(**attrs) -> BareWorker:
    return BareWorker(**attrs)


class FinalBoundTest(unittest.TestCase):
    """La borne du texte final ne change rien de ce que ses lecteurs lisent."""

    def test_short_text_is_kept_whole(self):
        self.assertEqual(adapters.bound_final("court"), "court")
        self.assertEqual(adapters.bound_final(None), "")

    def test_head_only_beyond_the_bound(self):
        huge = "a" * (adapters.FINAL_MAX_CHARS * 3)
        garde = adapters.bound_final(huge)
        self.assertEqual(len(garde), adapters.FINAL_MAX_CHARS)
        self.assertTrue(huge.startswith(garde))

    def test_turn_summary_head_is_identical_for_a_huge_final(self):
        # Seule la note de coupe compte la tête gardée au lieu du texte
        # d'origine ; la tête elle-même (ce que la rotation reprend) est
        # identique à celle du texte entier.
        worker = bare_worker()
        huge = "paragraph\n\n" * 20_000
        worker.last_final = adapters.bound_final(huge)
        self.assertLessEqual(len(worker.last_final), adapters.FINAL_MAX_CHARS)
        garde = adapters.bound_summary(huge).split("\n\n[Summary cut")[0]
        self.assertTrue(worker.turn_summary().startswith(garde))

    def test_auth_rule_still_sees_only_short_results(self):
        # `auth_failure.classify` ignore un résultat au-dessus de 600
        # caractères : la borne (64 000) ne change pas ce tri.
        self.assertGreater(adapters.FINAL_MAX_CHARS, 600)
        self.assertGreater(adapters.FINAL_MAX_CHARS, adapters.SUMMARY_MAX_CHARS)


class SummaryDisplayBoundTest(unittest.TestCase):
    """Le repli d'affichage du résumé est borné en caractères, pas seulement en
    fragments (10 000 fragments d'un flux bavard font des mégaoctets)."""

    def test_fragments_bound(self):
        worker = bare_worker()
        with mock.patch("ameesh.runner.SUMMARY_DISPLAY_MAX_FRAGMENTS", 2):
            for _ in range(3):
                worker._keep_summary_display("court")
        self.assertEqual(len(worker.summary_display), 2)
        # le défaut reste large : la borne de caractères fait le vrai travail
        self.assertGreaterEqual(SUMMARY_DISPLAY_MAX_FRAGMENTS, 10_000)

    def test_chars_bound_stops_the_flood(self):
        worker = bare_worker()
        fragment = "x" * 30_000
        gardes = [worker._keep_summary_display(fragment) for _ in range(5)]
        # Le dernier fragment est coupé au budget restant, séparateurs inclus.
        self.assertEqual(gardes, [True, True, True, False, False])
        self.assertEqual(worker._summary_chars, SUMMARY_DISPLAY_MAX_CHARS)
        self.assertEqual(len("\n".join(worker.summary_display)), SUMMARY_DISPLAY_MAX_CHARS)

    def test_one_huge_fragment_cannot_exceed_the_bound(self):
        worker = bare_worker()
        fragment = "x" * 2_000_000
        self.assertTrue(worker._keep_summary_display(fragment))
        self.assertEqual(worker.summary_display, [fragment[:SUMMARY_DISPLAY_MAX_CHARS]])
        self.assertEqual(worker._summary_chars, SUMMARY_DISPLAY_MAX_CHARS)
        self.assertFalse(worker._keep_summary_display("suite"))

    def test_last_fragment_uses_only_the_remaining_budget(self):
        worker = bare_worker()
        prefix = "x" * (SUMMARY_DISPLAY_MAX_CHARS - 2)
        self.assertTrue(worker._keep_summary_display(prefix))
        self.assertTrue(worker._keep_summary_display("suite"))
        self.assertEqual("\n".join(worker.summary_display), prefix + "\ns")
        self.assertEqual(worker._summary_chars, SUMMARY_DISPLAY_MAX_CHARS)

    def test_end_of_turn_markers_are_not_kept(self):
        # `turn_summary` les ôte à la lecture : les garder consommerait le
        # budget sans jamais rien rendre au résumé.
        worker = bare_worker()
        self.assertFalse(worker._keep_summary_display(
            adapters.END_OF_TURN_MARK + ": fin"))
        self.assertEqual(worker.summary_display, [])
        self.assertTrue(worker._keep_summary_display("vrai affichage"))

    def test_head_of_the_display_is_what_turn_summary_reads(self):
        worker = bare_worker()
        worker.last_final = None
        tete = "premier paragraphe\n\n"
        worker._keep_summary_display(tete)
        with mock.patch("ameesh.runner.SUMMARY_DISPLAY_MAX_CHARS", 10):
            self.assertFalse(worker._keep_summary_display("la suite est perdue"))
        self.assertEqual(worker.turn_summary(), tete.strip())


class TurnMemoryReleaseTest(unittest.TestCase):
    """Rendre les arènes au noyau seulement après un tour qui a fait croître le
    processus ; jamais d'exception, jamais de geste inutile."""

    def _release(self, before, after, failing=False):
        worker = bare_worker()
        rss = mock.Mock(side_effect=OSError("mesure impossible")) if failing \
            else mock.Mock(return_value=after)
        with mock.patch("ameesh.platform.process.rss_bytes", rss), \
                mock.patch("ameesh.platform.process.release_free_memory") as rel, \
                mock.patch("ameesh.runner.log_async"):
            worker._release_turn_memory(before)
        return rel

    def test_no_release_below_the_growth_threshold(self):
        rel = self._release(100 * 2 ** 20, 100 * 2 ** 20 + TURN_TRIM_RSS_GROWTH - 1)
        rel.assert_not_called()

    def test_release_after_a_big_turn(self):
        rel = self._release(100 * 2 ** 20, 100 * 2 ** 20 + TURN_TRIM_RSS_GROWTH)
        rel.assert_called_once_with()

    def test_unknown_before_never_releases(self):
        rel = self._release(None, 800 * 2 ** 20)
        rel.assert_not_called()

    def test_a_failing_measure_never_raises(self):
        rel = self._release(0, None, failing=True)
        rel.assert_not_called()


class PlatformMemoryTest(unittest.TestCase):
    """Les deux gestes plateforme : mesure best-effort, retour best-effort."""

    def test_rss_is_positive_or_unknown(self):
        value = process.rss_bytes()
        self.assertTrue(value is None or value > 0)

    def test_release_calls_malloc_trim(self):
        calls = []

        class FakeLibc:
            def __init__(self):
                # attribut d'instance, pas une méthode liée : `argtypes` et
                # `restype` s'y posent comme sur une fonction C
                self.malloc_trim = lambda pad: (calls.append(pad), 1)[1]

        with mock.patch("ctypes.CDLL", return_value=FakeLibc()):
            self.assertTrue(process.release_free_memory())
        self.assertEqual(calls, [0])

    def test_release_without_the_symbol_is_false(self):
        with mock.patch("ctypes.CDLL", return_value=object()):
            self.assertFalse(process.release_free_memory())

    def test_release_without_a_libc_is_false(self):
        # TypeError : `CDLL(None)` sous Windows (pas de handle du processus)
        for exc in (OSError("no libc"), TypeError("expected str, got NoneType")):
            with self.subTest(exc=type(exc).__name__), \
                    mock.patch("ctypes.CDLL", side_effect=exc):
                self.assertFalse(process.release_free_memory())


class JournalReadTest(unittest.TestCase):
    """#2096 : les lecteurs du journal lisent une tranche, jamais tout le flux.

    L'ancien `events()` chargeait le fichier entier puis analysait **toutes**
    ses lignes (pic de 620 Mo mesuré pour un journal de 75 Mo, plusieurs fois
    par tour) ; `session_of()` faisait de même pour n'en garder qu'un
    identifiant."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-2096-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.state = os.path.join(self.tmp, "state")
        self.agent = "a1"
        os.makedirs(os.path.join(self.state, self.agent), exist_ok=True)
        self.path = os.path.join(self.state, self.agent, "events.jsonl")

    def write(self, *lignes, saut_final=True):
        with open(self.path, "wb") as fh:
            for index, ligne in enumerate(lignes):
                if isinstance(ligne, bytes):
                    brut = ligne
                elif isinstance(ligne, dict):
                    brut = json.dumps(ligne).encode("utf-8")
                else:
                    brut = str(ligne).encode("utf-8")
                fin = b"\n" if (saut_final or index < len(lignes) - 1) else b""
                fh.write(brut + fin)
        return cost.CostBook(state_dir=self.state, db=None,
                             tools={self.agent: "claude"})

    def test_events_slice_equals_the_whole_read(self):
        book = self.write(*[{"type": "x", "n": i} for i in range(50)])
        tout = book.events(self.agent)
        self.assertEqual(len(tout), 50)
        for start, end in ((0, 50), (10, 20), (49, None), (50, None), (0, 0)):
            self.assertEqual(book.events(self.agent, start=start, end=end),
                             tout[start:end], "tranche %s:%s" % (start, end))

    def test_events_parses_only_the_requested_slice(self):
        book = self.write(*[{"type": "x", "n": i} for i in range(500)])
        appels = []
        vrai = cost.json.loads

        def compte(*args, **kwargs):
            appels.append(1)
            return vrai(*args, **kwargs)

        with mock.patch.object(cost.json, "loads", side_effect=compte):
            out = book.events(self.agent, start=498)
        self.assertEqual(len(out), 2)
        self.assertEqual(len(appels), 2)  # les 498 premières lignes sont comptées, pas analysées

    def test_events_skips_an_unreadable_line(self):
        book = self.write(b'{"type": "x", "n": 0}', b'\xff\xfe not json',
                          b'{"type": "x", "n": 2}')
        self.assertEqual(book.events(self.agent),
                         [{"type": "x", "n": 0}, {"type": "x", "n": 2}])

    def test_last_line_without_a_newline_is_read(self):
        book = self.write({"n": 1}, {"n": 2}, saut_final=False)
        self.assertEqual(book.events(self.agent), [{"n": 1}, {"n": 2}])
        self.assertEqual(book.events(self.agent, start=1), [{"n": 2}])

    def test_reverse_lines_across_block_boundaries(self):
        lignes = [b"x" * (index * 7 % 40) + b"ligne-%d" % index for index in range(200)]
        self.write(*lignes)
        with open(self.path, "rb") as fh:
            attendu = [ligne for ligne in fh.read().split(b"\n") if ligne]
        # petit bloc : chaque ligne franchit une frontière de bloc
        rendu = [ligne for ligne in cost._lines_reverse(self.path, bloc=16) if ligne]
        self.assertEqual(rendu, list(reversed(attendu)))

    def test_session_of_reads_from_the_end(self):
        book = self.write(*[{"type": "x", "n": i} for i in range(300)],
                          {"type": "result", "session_id": "s-fin"})
        appels = []
        vrai = cost.json.loads

        def compte(*args, **kwargs):
            appels.append(1)
            return vrai(*args, **kwargs)

        with mock.patch.object(cost.json, "loads", side_effect=compte):
            self.assertEqual(book.session_of(self.agent), "s-fin")
        self.assertLessEqual(len(appels), 3)  # pas les 301 lignes

    def test_session_of_matches_the_forward_scan(self):
        book = self.write({"type": "x", "session_id": "vieux"},
                          {"type": "assistant", "parent_tool_use_id": "t",
                           "session_id": "sous-agent"},
                          {"type": "session", "sessionId": "deepseek-1"},
                          {"type": "result", "session_id": "a", "thread_id": "b"})
        self.assertEqual(book.session_of(self.agent), "b")
        self.assertEqual(book.session_of(self.agent),
                         cost._last_session(book.events(self.agent)))
        self.write({"type": "x"})
        self.assertIsNone(book.session_of(self.agent))

    def test_session_reading_parses_only_the_prefix(self):
        # `session_reading` lit les événements AVANT le tour : la tranche
        # `end=start` doit s'arrêter là, sans analyser le tour ni la suite.
        book = self.write(*[{"type": "x", "n": i} for i in range(200)])
        appels = []
        vrai = cost.json.loads

        def compte(*args, **kwargs):
            appels.append(1)
            return vrai(*args, **kwargs)

        with mock.patch.object(cost.json, "loads", side_effect=compte):
            book.session_reading(self.agent, "claude", "s", 5)
        self.assertEqual(len(appels), 5)  # 5 lignes du préfixe, pas les 200


class BigTurnMemoryTest(PgTestCase):
    """Bout en bout : un gros tour garde son journal intact, mais ne garde ni
    le texte final entier, ni l'affichage, et laisse la place au tour suivant."""

    def test_big_turn_keeps_a_bounded_final_and_an_intact_journal(self):
        cwd = os.path.join(self.tmp, "work")
        os.makedirs(cwd, exist_ok=True)
        bigfake = os.path.join(self.tmp, "bigfake.py")
        with open(bigfake, "w", encoding="utf-8") as fh:
            fh.write(BIGFAKE)
        os.chmod(bigfake, os.stat(bigfake).st_mode | stat.S_IEXEC)
        self.register("big", "claude", cwd=cwd)
        runner = Runner(dataclasses.replace(self.cfg, persona_memory={}),
                        self.db, once=True)
        lease = registry.claim(self.db, "big", runner.runner_id, 3600)
        self.assertIsNotNone(lease)
        worker = AgentWorker(runner, registry.get(self.db, "big"), lease)
        self.addCleanup(worker.watchdog_stop.set)
        taille = 2_000_000
        env = {"AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log,
               "AMEESH_CLAUDE_BIN": bigfake, "BIG_N": str(taille)}
        with mock.patch.dict(os.environ, env):
            ok = worker.run_turn({"kind": "prompt", "prompt": "gros tour", "ids": []})
        self.assertTrue(ok)
        # le texte final gardé est la tête, bornée
        self.assertIsNotNone(worker.last_final)
        self.assertEqual(len(worker.last_final), adapters.FINAL_MAX_CHARS)
        # l'affichage d'un tour normal n'est pas gardé (il ne sert qu'au repli
        # d'un tour de résumé)
        self.assertEqual(worker.summary_display, [])
        # le journal du flux, lui, garde la ligne entière : la réduction du pic
        # ne change pas ce qui est écrit sur disque.
        with open(os.path.join(worker.state_dir, "events.jsonl"),
                  encoding="utf-8") as fh:
            lignes = [json.loads(ligne) for ligne in fh if ligne.strip()]
        resultats = [ligne for ligne in lignes if ligne.get("type") == "result"]
        self.assertEqual(len(resultats), 1)
        self.assertEqual(len(resultats[0]["result"]), taille)
        # et les lecteurs du grand livre relisent ce journal par tranche : la
        # session par la fin, la dernière ligne par son index, sans le charger.
        book = cost.CostBook(state_dir=self.cfg.state_dir, db=self.db,
                             tools={"big": "claude"})
        self.assertEqual(book.session_of("big"), "big-sess-1")
        self.assertEqual(len(book.events("big", start=len(lignes) - 1)), 1)


if __name__ == "__main__":
    unittest.main()
