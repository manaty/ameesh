# SPDX-License-Identifier: AGPL-3.0-only
"""#4361 : pause bornée du paquet horaire, minuterie active.

Témoins : le tic saute pendant la pause sans aucun geste (ni fraîcheur, ni
schéma, ni composition) et le dit ; il reprend seul à l'échéance ; une pause
sans échéance, sans raison, échue ou au-delà de 12 h est refusée ; l'état vit
au journal partagé (le dernier événement fait foi, `resume` lève) ;
`role timers --check` nomme la pause sans échouer.
"""
from __future__ import annotations

import argparse
import importlib.util
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest import mock

from ameesh import packet_pause, role_timers

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "paquet_horaire_4361", ROOT / "scripts" / "paquet-horaire.py")
ph = importlib.util.module_from_spec(SPEC)
sys.modules["paquet_horaire_4361"] = ph
SPEC.loader.exec_module(ph)

ZONE = ph.ZoneInfo("Europe/Paris")
NOW = datetime(2026, 10, 28, 19, 0, tzinfo=ZONE).timestamp()


class FakeEvents:
    def __init__(self):
        self.rows: list[dict] = []

    def record(self, type, object, project, data):
        self.rows.append({"id": len(self.rows) + 1, "type": type, "object": object,
                          "project": project, "data": dict(data)})
        return len(self.rows)

    def recent_type(self, type, days, limit, object=None):
        rows = [row for row in reversed(self.rows)
                if row["type"] == type and (object is None or row["object"] == object)]
        return rows[:limit]


class Store:
    def __init__(self, events):
        self.events = events


class Base(unittest.TestCase):
    def setUp(self):
        self.events = FakeEvents()
        patcher = mock.patch.object(packet_pause.storage, "of",
                                    lambda db: Store(self.events))
        patcher.start()
        self.addCleanup(patcher.stop)

    def pause(self, **kw):
        args = dict(project="ameesh", target="main", until=NOW + 3600,
                    reason="staging hors créneau", by="mesh-design", now=NOW)
        args.update(kw)
        return packet_pause.pause(object(), **args)


class PauseStateTest(Base):
    def test_pause_active_puis_echue(self):
        self.pause()
        data = packet_pause.active(object(), project="ameesh", target="main", now=NOW + 60)
        self.assertEqual(data["reason"], "staging hors créneau")
        self.assertEqual(self.events.rows[0]["type"], "packet.pause")
        self.assertEqual(self.events.rows[0]["object"], "paquet-horaire:ameesh:main")
        self.assertIsNone(packet_pause.active(object(), project="ameesh", target="main",
                                              now=NOW + 3600))

    def test_resume_leve_et_autre_cible_intacte(self):
        self.pause()
        self.pause(target="develop")
        packet_pause.resume(object(), project="ameesh", target="main", by="x")
        self.assertIsNone(packet_pause.active(object(), project="ameesh", target="main",
                                              now=NOW + 60))
        self.assertIsNotNone(packet_pause.active(object(), project="ameesh",
                                                 target="develop", now=NOW + 60))

    def test_refus(self):
        for kw in ({"until": None}, {"until": NOW - 1}, {"until": NOW},
                   {"until": NOW + packet_pause.MAX_S + 1}, {"reason": "  "},
                   {"reason": "x" * (packet_pause.REASON_MAX + 1)}):
            with self.subTest(kw=kw), self.assertRaises(packet_pause.PauseError):
                self.pause(**kw)
        self.assertEqual(self.events.rows, [])


class TickTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        saved = (ph.check_fresh, ph.align_schema, ph.tick)
        ph.check_fresh = lambda ctx: self.calls.append("fraîcheur")
        ph.align_schema = lambda ctx: self.calls.append("schéma")
        ph.tick = lambda ctx: self.calls.append("tic") or "tic fait"
        self.addCleanup(lambda: setattr(ph, "check_fresh", saved[0])
                        or setattr(ph, "align_schema", saved[1]) or setattr(ph, "tick", saved[2]))

    def ctx(self, pause_of):
        return ph.Context(repo=ROOT, target="main", project="ameesh", pilot="p",
                          actor="p", shell=None, zone=ZONE, now=NOW, pause_of=pause_of)

    def test_le_tic_saute_pendant_la_pause(self):
        data = {"state": "paused", "until": int(NOW + 3600), "reason": "staging #1155",
                "by": "mesh-design"}
        said = ph.run(self.ctx(lambda: data))
        self.assertEqual(self.calls, [])
        self.assertIn("en pause jusqu'à 2026-10-28 20:00", said)
        self.assertIn("staging #1155", said)

    def test_le_tic_reprend_a_l_echeance(self):
        self.assertEqual(ph.run(self.ctx(lambda: None)), "tic fait")
        self.assertEqual(self.calls, ["fraîcheur", "schéma", "tic"])

    def test_pause_illisible_aucun_geste(self):
        def broken():
            raise RuntimeError("base injoignable")
        with self.assertRaises(ph.HoraireError):
            ph.run(self.ctx(broken))
        self.assertEqual(self.calls, [])


class CommandTest(Base):
    class Mail:
        def __init__(self):
            self.sent = []

        def run(self, cmd, *, cwd=None, input_text=None, timeout=0):
            self.sent.append((cmd[2], input_text))
            return ph.Result(0)

    def run_cmd(self, *argv):
        ns = argparse.Namespace(project="ameesh", target="main", pilot="mesh-design",
                                by="ameesh-dev4", until=None, reason=None)
        ns.command = argv[0]
        for flag, value in zip(argv[1::2], argv[2::2]):
            setattr(ns, flag.lstrip("-"), value)
        out, err, mail = io.StringIO(), io.StringIO(), self.Mail()
        with mock.patch.object(ph, "_zone", lambda: (ZONE, None)), \
                redirect_stdout(out), redirect_stderr(err):
            rc = ph.main_pause(ns, connect=object, shell=mail, now=NOW)
        return rc, out.getvalue() + err.getvalue(), mail.sent

    def test_pause_journalisee_et_dite(self):
        rc, text, sent = self.run_cmd("pause", "--until", "20:15", "--reason", "staging")
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.events.rows[-1]["data"]["until"],
                         int(datetime(2026, 10, 28, 20, 15, tzinfo=ZONE).timestamp()))
        self.assertEqual([to for to, _ in sent], ["mesh-design", "bureau"])
        rc, text, _ = self.run_cmd("status")
        self.assertIn("en pause jusqu'à", text)
        rc, text, sent = self.run_cmd("resume")
        self.assertEqual(rc, 0)
        self.assertIn("pause levée par ameesh-dev4", text)
        rc, text, _ = self.run_cmd("status")
        self.assertIn("aucune pause", text)

    def test_pause_refusee_sans_echeance_valable(self):
        for until in ("", "demain", "25:00", "18:00:00x"):
            rc, _, sent = self.run_cmd("pause", "--until", until, "--reason", "x")
            self.assertEqual(rc, 2, until)
            self.assertEqual(sent, [])
        rc, _, _ = self.run_cmd("pause", "--until", "13h", "--reason", "x")
        self.assertEqual(rc, 2)
        self.assertEqual(self.events.rows, [])

    def test_argparse_exige_echeance_et_raison(self):
        for argv in (["pause", "--reason", "x"], ["pause", "--until", "1h"]):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                ph.main(argv)
            self.assertEqual(caught.exception.code, 2)

    def test_parse_until(self):
        self.assertEqual(ph.parse_until("90m", NOW, ZONE), NOW + 5400)
        self.assertEqual(ph.parse_until("2h", NOW, ZONE), NOW + 7200)
        # heure passée : le lendemain
        self.assertEqual(ph.parse_until("18:30", NOW, ZONE),
                         datetime(2026, 10, 29, 18, 30, tzinfo=ZONE).timestamp())
        self.assertEqual(ph.parse_until("2026-10-28T21:00:00+01:00", NOW, ZONE),
                         datetime(2026, 10, 28, 21, 0, tzinfo=ZONE).timestamp())


class RoleTimersTest(Base):
    REPORT = {"timers": [{"name": role_timers.PAQUET_PASS, "blocked": ""}]}

    def note(self, report=None):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / "paquet-horaire.env"
            env.write_text("AMEESH_PAQUET_REPO=/x\n", encoding="utf-8")
            return role_timers.paquet_pause_note(object(), report or self.REPORT,
                                                 str(env), now=NOW + 60)

    def test_check_nomme_la_pause(self):
        self.assertEqual(self.note(), "")
        self.pause()
        self.assertIn("paquet-horaire: en pause jusqu'à", self.note())
        self.assertIn("timer active", self.note())

    def test_sans_passe_paquet_rien(self):
        self.pause()
        blocked = {"timers": [{"name": role_timers.PAQUET_PASS, "blocked": "missing"}]}
        self.assertEqual(self.note(blocked), "")

    def test_lecture_impossible_jamais_un_echec(self):
        with mock.patch.object(packet_pause, "active", side_effect=RuntimeError("db")):
            self.assertEqual(self.note(), "")


if __name__ == "__main__":
    unittest.main()
