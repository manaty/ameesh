# SPDX-License-Identifier: AGPL-3.0-only
"""#2820 : rotation de `events.jsonl` au début d'un tour.

Constat de l'auditeur (20/10, agents-2, diagnostic impl-05) : le flux de
chaque agent croissait sans plafond ni rotation, ≈ 1 Go/h pour huit agents,
16 fichiers et 1,39 Go en une heure, disque vers le plancher. #2110 n'avait
borné que les lectures.

Témoins : sous le quota rien ne bouge ; au-delà, l'ancien flux passe en `.1`
et le neuf reprend sa fin en lignes entières, derrière les derniers marqueurs
de compte et de tour d'avant la coupe.
Les lecteurs du grand livre (compte, marqueur de tour, session, dernier total,
jauge) rendent au tour suivant ce qu'ils rendaient sans rotation. L'exécuteur
ne tourne qu'au point sûr : jamais avec un marqueur comptable en attente (ou
douteux, ou base injoignable), ni avec une mesure résidente en mémoire.
"""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

from ameesh import cost, db as db_mod, events_rotation, housekeeping, registry
from ameesh.runner import AgentWorker, Runner
from .support import FAKEBIN, PgTestCase

KIB = 1024


def ligne(event) -> bytes:
    return (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")


def compte(nom, ts):
    return {"type": cost.ACCOUNT_MARKER, "harness": "claude", "account": nom,
            "ts": ts, "agent": "a1"}


def tour(resume, ts):
    return {"type": cost.TURN_MARKER, "resume": resume, "ts": ts, "agent": "a1"}


def jauge(used, now):
    return {"type": "rate_limit_event", "rate_limit_info": {
        "unifiedWindows": {"five_hour": {"utilization": used, "resetsAt": now + 3600}}}}


def claude_turn(session, total, *, bourrage=0):
    """Les lignes d'un tour Claude : init, bourrage, résultat cumulé."""
    out = [{"type": "system", "subtype": "init", "session_id": session}]
    if bourrage:
        out.append({"type": "user", "session_id": session, "message": {
            "content": [{"type": "tool_result", "content": "x" * bourrage}]}})
    out.append({"type": "result", "subtype": "success", "result": "fini",
                "is_error": False, "total_cost_usd": total, "session_id": session,
                "usage": {"input_tokens": 10, "output_tokens": 2}})
    return out


class RotateTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-2820-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(os.path.join(self.state, "a1"))
        self.path = os.path.join(self.state, "a1", "events.jsonl")
        self.now = time.time()

    def write(self, *events, raw: bytes = b""):
        with open(self.path, "wb") as fh:
            for event in events:
                fh.write(ligne(event))
            fh.write(raw)

    def read(self, path=None):
        with open(path or self.path, "rb") as fh:
            return fh.read()

    def lines(self):
        return [json.loads(x) for x in self.read().splitlines() if x]

    def test_under_the_quota_nothing_moves(self):
        self.write(tour("", 1), *claude_turn("s1", 0.1))
        avant = self.read()
        self.assertIsNone(events_rotation.rotate(self.path, len(avant)))
        self.assertEqual(self.read(), avant)
        self.assertFalse(os.path.exists(self.path + ".1"))

    def test_missing_stream_or_null_quota(self):
        self.assertIsNone(events_rotation.rotate(self.path, 1))
        self.write(tour("", 1))
        self.assertIsNone(events_rotation.rotate(self.path, 0))

    def test_tail_carried_behind_the_markers_before_the_cut(self):
        ancien = [compte("primary", 1), tour("", 1), *claude_turn("s1", 0.1, bourrage=20000)]
        dernier = [compte("tertiary", 2), tour("s1", 2), jauge(0.4, self.now),
                   *claude_turn("s1", 0.2)]
        self.write(*ancien, *dernier)
        avant = self.read()
        fait = events_rotation.rotate(self.path, 8000, carry=2000)
        self.assertIsNotNone(fait)
        self.assertEqual(fait.size, len(avant))
        self.assertEqual(self.read(self.path + ".1"), avant)
        # le bourrage est coupé : reprise à la ligne entière suivante, le
        # résultat de l'ancien tour, derrière les marqueurs qui le précédaient
        self.assertEqual(self.lines(), [compte("primary", 1), tour("", 1), ancien[-1], *dernier])
        self.assertEqual(fait.carried, len(self.read()))

    def test_markers_of_different_turns_are_both_kept(self):
        # le tour coupé n'a pas de marqueur de compte : celui d'avant vaut pour lui
        self.write(compte("tertiary", 1), tour("", 1), *claude_turn("s0", 0.1),
                   tour("s1", 2), *claude_turn("s1", 0.2, bourrage=20000),
                   tour("s1", 3), *claude_turn("s1", 0.3))
        events_rotation.rotate(self.path, 8000, carry=2000)
        repris = self.lines()
        self.assertEqual(repris[:2], [compte("tertiary", 1), tour("s1", 2)])
        self.assertEqual(repris[-3:], [tour("s1", 3), *claude_turn("s1", 0.3)])

    def test_carry_is_whole_lines_within_the_budget(self):
        corps = [{"type": "assistant", "n": i, "pad": "y" * 100} for i in range(200)]
        self.write(compte("tertiary", 1), tour("s0", 1), *corps, *claude_turn("s1", 0.3))
        events_rotation.rotate(self.path, 4000, carry=1000)
        repris = self.lines()
        self.assertEqual(repris[:2], [compte("tertiary", 1), tour("s0", 1)])
        self.assertEqual(repris[-1]["type"], "result")
        tete = len(ligne(compte("tertiary", 1))) + len(ligne(tour("s0", 1)))
        self.assertLessEqual(len(self.read()), 1000 + tete)
        # que des lignes entières : la première reprise du corps est un événement complet
        self.assertIn(repris[2], corps + claude_turn("s1", 0.3))

    def test_carry_never_exceeds_a_quarter_of_the_quota(self):
        corps = [{"type": "assistant", "pad": "z" * 200} for _ in range(100)]
        self.write(tour("", 1), *corps)
        events_rotation.rotate(self.path, 8000)
        self.assertLessEqual(len(self.read()), 2000 + len(ligne(tour("", 1))))

    def test_stream_without_markers_keeps_its_tail(self):
        corps = [{"type": "assistant", "n": i} for i in range(500)]
        self.write(*corps)
        events_rotation.rotate(self.path, 2000, carry=300)
        repris = self.lines()
        self.assertTrue(repris)
        self.assertEqual(repris, corps[-len(repris):])

    def test_quoted_marker_is_not_a_marker(self):
        # un résultat d'outil qui cite un marqueur (un agent qui lit runner.py)
        cite = {"type": "user", "content": json.dumps(tour("faux", 9))}
        cite_long = {"type": "user", "content": '{"type": "ameesh.turn"} ' + "w" * 5000}
        self.write(compte("tertiary", 1), tour("s1", 1), *claude_turn("s1", 0.1, bourrage=3000),
                   cite, cite_long)
        events_rotation.rotate(self.path, 1000, carry=20000)
        self.assertEqual(self.lines()[1], tour("s1", 1))

    def test_partial_last_line_is_closed(self):
        self.write(tour("", 1), *claude_turn("s1", 0.1, bourrage=3000), raw=b'{"type": "assis')
        events_rotation.rotate(self.path, 1000, carry=20000)
        self.assertTrue(self.read().endswith(b'{"type": "assis\n'))

    def test_previous_generation_is_replaced(self):
        with open(self.path + ".1", "wb") as fh:
            fh.write(b"vieux\n" * 1000)
        self.write(tour("", 1), *claude_turn("s1", 0.1, bourrage=3000))
        avant = self.read()
        events_rotation.rotate(self.path, 1000)
        self.assertEqual(self.read(self.path + ".1"), avant)
        self.assertFalse(os.path.exists(self.path + events_rotation.TEMPORARY))

    def test_mode_is_kept(self):
        self.write(tour("", 1), *claude_turn("s1", 0.1, bourrage=3000))
        os.chmod(self.path, 0o600)
        events_rotation.rotate(self.path, 1000)
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_a_concurrent_append_cancels_the_rotation(self):
        self.write(tour("", 1), *claude_turn("s1", 0.1, bourrage=3000))
        reel = events_rotation._carried

        def puis_ecrit(flux, taille, carry):
            donnees = reel(flux, taille, carry)
            with open(self.path, "ab") as fh:
                fh.write(ligne({"type": "tardif"}))
            return donnees

        with mock.patch.object(events_rotation, "_carried", puis_ecrit):
            self.assertIsNone(events_rotation.rotate(self.path, 1000))
        self.assertEqual(self.lines()[-1], {"type": "tardif"})
        self.assertFalse(os.path.exists(self.path + ".1"))
        self.assertFalse(os.path.exists(self.path + events_rotation.TEMPORARY))

    def test_without_hard_links_the_stream_is_renamed(self):
        self.write(tour("", 1), *claude_turn("s1", 0.1, bourrage=3000))
        avant = self.read()
        with mock.patch.object(events_rotation.os, "link", side_effect=OSError("EPERM")):
            self.assertIsNotNone(events_rotation.rotate(self.path, 1000))
        self.assertEqual(self.read(self.path + ".1"), avant)
        self.assertEqual(self.lines()[0], tour("", 1))

    def test_without_hard_links_a_failed_swap_restores_the_stream(self):
        self.write(tour("", 1), *claude_turn("s1", 0.1, bourrage=3000))
        avant = self.read()
        reel = os.replace

        def refuse_le_neuf(src, dst):
            if src.endswith(events_rotation.TEMPORARY):
                raise OSError(28, "ENOSPC")
            return reel(src, dst)

        with mock.patch.object(events_rotation.os, "link", side_effect=OSError("EPERM")), \
                mock.patch.object(events_rotation.os, "replace", refuse_le_neuf):
            with self.assertRaises(OSError):
                events_rotation.rotate(self.path, 1000)
        self.assertEqual(self.read(), avant)
        self.assertFalse(os.path.exists(self.path + events_rotation.TEMPORARY))

    def test_a_full_disk_leaves_the_stream_in_place(self):
        self.write(tour("", 1), *claude_turn("s1", 0.1, bourrage=3000))
        avant = self.read()
        with mock.patch.object(events_rotation.os, "fsync", side_effect=OSError(28, "ENOSPC")):
            with self.assertRaises(OSError):
                events_rotation.rotate(self.path, 1000)
        self.assertEqual(self.read(), avant)
        self.assertFalse(os.path.exists(self.path + events_rotation.TEMPORARY))


class LedgerReadersTest(unittest.TestCase):
    """Au tour suivant, les lecteurs du flux rendent la même chose avec ou
    sans rotation : c'est ce que la reprise garantit."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-2820-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.now = time.time()
        # la fenêtre des jauges et les marqueurs à l'échelle du témoin : la
        # reprise est la fenêtre moins la place des deux marqueurs, comme en vrai
        for patch in (mock.patch.object(cost, "TAIL_BYTES", 3000),
                      mock.patch.object(events_rotation, "MARKER_MAX_BYTES", 200)):
            patch.start()
            self.addCleanup(patch.stop)

    def stream(self, name, rotate, nouveau_compte):
        state = os.path.join(self.tmp, name)
        os.makedirs(os.path.join(state, "a1"))
        path = os.path.join(state, "a1", "events.jsonl")
        with open(path, "wb") as fh:
            for event in [compte("primary", self.now - 50), tour("", self.now - 50),
                          jauge(0.2, self.now), *claude_turn("s1", 0.5, bourrage=20000),
                          compte("tertiary", self.now - 10), tour("s1", self.now - 10),
                          jauge(0.7, self.now), *claude_turn("s1", 0.9)]:
                fh.write(ligne(event))
        if rotate:
            carry = cost.TAIL_BYTES - 2 * events_rotation.MARKER_MAX_BYTES
            self.assertIsNotNone(events_rotation.rotate(path, 12000, carry=carry))
        # le tour suivant, comme l'exécuteur : marqueurs, puis index de début
        with open(path, "ab") as fh:
            if nouveau_compte:
                fh.write(ligne(compte("tertiary", self.now)))
            fh.write(ligne(tour("s1", self.now)))
        with open(path, "rb") as fh:
            start = sum(1 for _ in fh)
        book = cost.CostBook(state_dir=state, db=None, tools={"a1": "claude"})
        gauges = book.claude_gauges("tertiary", "primary")
        return {
            "account": book.account_at("a1", start, "claude"),
            "last_account": book.account_at("a1", sys.maxsize, "claude"),
            "resume": (book.turn_marker("a1", start) or {}).get("resume"),
            "session": book.session_of("a1"),
            "reading": book.session_reading("a1", "claude", "s1", start).get("cum_usd"),
            "gauge": [round(g.used, 3) for g in gauges],
            "primary_gauge": [round(g.used, 3) for g in book.claude_gauges("primary", "primary")],
        }

    def test_readers_agree_after_rotation(self):
        for nouveau_compte in (True, False):
            with self.subTest(nouveau_compte=nouveau_compte):
                sans = self.stream("sans-%s" % nouveau_compte, False, nouveau_compte)
                avec = self.stream("avec-%s" % nouveau_compte, True, nouveau_compte)
                self.assertEqual(avec, sans)
                self.assertEqual(avec["account"], "tertiary")
                self.assertEqual(avec["reading"], 0.9)
                self.assertEqual(avec["gauge"], [0.7])


class Stub:
    """Les attributs que `_rotation_du_flux` lit d'un `AgentWorker`."""

    def __init__(self, quota, pending=None, resident=(), raises=None):
        self.name = "a1"
        self.runner = mock.Mock()
        self.runner.housekeeping_policy.return_value = housekeeping.Policy(events_quota=quota)
        self._resident_usage_pending = list(resident)
        self._pending, self._raises = pending, raises

    def _compta_en_attente(self):
        if self._raises is not None:
            raise self._raises
        return self._pending


class SafePointTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-2820-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = os.path.join(self.tmp, "events.jsonl")
        with open(self.path, "wb") as fh:
            for event in [tour("", 1), *claude_turn("s1", 0.1, bourrage=3000)]:
                fh.write(ligne(event))

    def rotated(self, stub):
        with mock.patch("ameesh.runner.log"):
            AgentWorker._rotation_du_flux(stub, self.path)
        return os.path.exists(self.path + ".1")

    def test_rotates_without_pending_accounting(self):
        self.assertTrue(self.rotated(Stub(1000)))

    def test_under_the_quota_the_database_is_not_read(self):
        stub = Stub(1 << 20, raises=AssertionError("base lue sous le quota"))
        self.assertFalse(self.rotated(stub))

    def test_never_with_a_pending_or_doubtful_marker(self):
        for stub in (Stub(1000, pending={"start": 3, "created_ts": 1.0}),
                     Stub(1000, pending={}),          # marqueur illisible : doute
                     Stub(1000, raises=db_mod.Unavailable("base injoignable")),
                     Stub(1000, resident=[{"type": "resident.turn"}])):
            with self.subTest(pending=stub._pending, raises=stub._raises,
                              resident=stub._resident_usage_pending):
                self.assertFalse(self.rotated(stub))

    def test_a_failure_never_blocks_the_turn(self):
        with mock.patch.object(events_rotation, "rotate", side_effect=OSError("ENOSPC")):
            self.assertFalse(self.rotated(Stub(1000)))


class PolicyTest(unittest.TestCase):

    def test_default_and_parse(self):
        self.assertEqual(housekeeping.Policy().events_quota, 64 * 1024 ** 2)
        parsed, problems, unknown = housekeeping.parse_policy({"events_quota": "32MiB"})
        self.assertEqual((parsed, problems, unknown), ({"events_quota": 32 * 1024 ** 2}, [], []))
        for value in ("0", 0, "512KiB", "beaucoup", -1):
            with self.subTest(value=value):
                parsed, problems, _ = housekeeping.parse_policy({"events_quota": value})
                self.assertNotIn("events_quota", parsed)
                self.assertEqual(len(problems), 1)

    def test_strictest_card_wins(self):
        def canon(ident, quota):
            fiche = mock.Mock(policy=mock.Mock(housekeeping={"events_quota": quota},
                                               work_root=None, work_roots=None))
            return mock.Mock(id=ident, host=lambda host: fiche)

        policy = housekeeping.effective([canon("a", 64 << 20), canon("b", 16 << 20)], "h")
        self.assertEqual(policy.events_quota, 16 << 20)
        self.assertEqual(policy.origin["events_quota"], "b")


class RunnerRotationTest(PgTestCase):
    """Bout en bout : le tour qui suit le dépassement tourne le flux, et sa
    ligne de grand livre s'écrit, marqueur comptable soldé."""

    def test_turn_after_the_quota_rotates_and_is_booked(self):
        cwd = os.path.join(self.tmp, "work")
        os.makedirs(cwd, exist_ok=True)
        self.register("rot", "claude", cwd=cwd)
        runner = Runner(dataclasses.replace(self.cfg, persona_memory={}), self.db, once=True)
        lease = registry.claim(self.db, "rot", runner.runner_id, 3600)
        worker = AgentWorker(runner, registry.get(self.db, "rot"), lease)
        self.addCleanup(worker.watchdog_stop.set)
        path = os.path.join(worker.state_dir, "events.jsonl")
        env = {"AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log}
        with mock.patch.dict(os.environ, env):
            for numero in range(5):
                self.assertTrue(worker.run_turn(
                    {"kind": "prompt", "prompt": "tour %d" % numero, "ids": []}))
            taille = os.path.getsize(path)
            runner._housekeeping = housekeeping.Policy(events_quota=taille - 1)
            self.assertTrue(worker.run_turn({"kind": "prompt", "prompt": "tour 5", "ids": []}))
        with open(path + ".1", "rb") as fh:
            self.assertEqual(len(fh.read()), taille)
        self.assertLess(os.path.getsize(path), taille)
        def events(chemin):
            with open(chemin, encoding="utf-8") as fh:
                return [json.loads(x) for x in fh if x.strip()]

        ancien, neuf = events(path + ".1"), events(path)
        # le tour courant ouvre son flux neuf derrière la reprise : la fin de
        # l'ancien flux, précédée au plus des deux marqueurs d'avant la coupe
        courant = max(i for i, e in enumerate(neuf) if e.get("type") == cost.TURN_MARKER)
        repris = neuf[:courant]
        self.assertTrue(repris)
        self.assertTrue(any(repris[k:] == ancien[len(ancien) - len(repris) + k:]
                            for k in (0, 1, 2)), repris)
        self.assertEqual(neuf[-1]["type"], "result")
        self.assertIsNone(registry.pending_spend_get(self.db, "rot"))
        lignes = self.db.query(
            "select count(*)::int as n from turn_costs where agent = 'rot'")[0]["n"]
        self.assertEqual(lignes, 6)


if __name__ == "__main__":
    unittest.main()
