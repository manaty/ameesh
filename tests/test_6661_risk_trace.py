# SPDX-License-Identifier: AGPL-3.0-only
"""#6661 — la trace du risque au gel (M2 de l'étude #6650).

La marque de désignation gardait la politique, les auteurs et le quorum, pas
la classe : un quorum 2 ne prouve pas la classe sensible (plancher explicite)
et un quorum 0 ne prouve pas léger. La marque porte maintenant `risk_trace` :
la classe lue au gel, d'où vient le compte requis, la politique exacte, la tête
et la base exactes. Observation seule : ni le compte ni les familles ne
changent, et une marque N-1 ou reportée se lit `unknown`.
"""
from __future__ import annotations

import json
import types
import unittest
from unittest import mock

from ameesh import registry, storage, verdict_carry, work
from ameesh import review_designation as rd
from ameesh import review_designation_activation as activation
from ameesh import review_designation_pg as pg

from .support import PgTestCase

FAMILIES = ("claude", "codex", "deepseek")
SHA_A = "a" * 40
SHA_B = "b" * 40
BASE = "c" * 40
COMMIT = "d" * 40
RULES = (rd.Rule(paths=("**",), reviews=2),)


def policy(risk: str, *, required: int, policy_required: int, floor: int = 0,
           lowering: int = 0) -> activation.Policy:
    return activation.Policy(
        rules=RULES, families=FAMILIES, risk=risk, required=required,
        ref="canon@%s" % COMMIT[:12], commit=COMMIT, source="manaty/ameesh-canon",
        canon_lowering={"floor": lowering} if lowering else None,
        policy_required=policy_required, floor=floor)


def inputs(found: activation.Policy):
    """Seule la politique est lue par `risk_trace`."""
    return types.SimpleNamespace(policy=found)


class RiskTraceReaderTest(unittest.TestCase):
    """Le lecteur : une trace absente ou reportée n'est jamais devinée."""

    def mark(self, **extra) -> dict:
        payload = {"state": rd.DESIGNATED, "required": 2, "reviewers": [],
                   "missing": 0, "why": "", "freeze": {"id": 7, "sha": SHA_A},
                   "authors": [], "unknown_authors": [], "policy_ref": "canon@x",
                   "sent": []}
        payload.update(extra)
        return pg.designation_of("%s %s" % (pg.mark_prefix(3, 7), json.dumps(payload)))

    def test_an_n1_mark_reads_unknown_with_its_quorum(self):
        found = pg.risk_trace_of(self.mark())
        self.assertEqual(found["quality"], "absent")
        self.assertEqual(found["risk"], pg.UNKNOWN)
        # le quorum reste lu, jamais traduit en classe
        self.assertEqual(found["required"], 2)
        self.assertEqual(found["required_from"], {})
        self.assertEqual(found["freeze"], {"id": 7, "sha": SHA_A})

    def test_a_foreign_schema_is_not_a_trace(self):
        found = pg.risk_trace_of(self.mark(risk_trace={"schema": "other/9",
                                                       "risk": "sensitive"}))
        self.assertEqual((found["quality"], found["risk"]), ("absent", pg.UNKNOWN))

    def test_a_carried_mark_reads_unknown(self):
        note = verdict_carry.carried_mark_note(
            3, {"id": 8, "sha": SHA_B},
            {"carried": [{"actor": "r1"}], "detail": "alignment"})
        found = pg.risk_trace_of(verdict_carry.carried_mark_of(note))
        self.assertEqual(found["quality"], "carried")
        self.assertEqual(found["risk"], pg.UNKNOWN)
        self.assertIsNone(found["required"])
        self.assertEqual(found["freeze"], {"id": 8, "sha": SHA_B})

    def test_no_mark_no_trace(self):
        self.assertIsNone(pg.risk_trace_of(None))

    def test_the_activation_trace_names_each_source(self):
        trace = activation.risk_trace(inputs(policy(
            "light", required=2, policy_required=1, floor=2)))
        self.assertEqual(trace["risk"], "light")
        self.assertEqual(trace["required_from"],
                         {"policy": 1, "canon_lowering": 0, "floor": 2})
        self.assertEqual(trace["policy"], {"ref": "canon@%s" % COMMIT[:12],
                                           "commit": COMMIT,
                                           "source": "manaty/ameesh-canon"})

    def test_selection_hands_the_trace_to_every_commit(self):
        seen = {}

        def fake_apply(item, **kwargs):
            seen["port"] = kwargs["port"]
            return {}

        found = types.SimpleNamespace(
            item={"id": 1}, policy=policy("sensitive", required=2, policy_required=2),
            paths=[], freeze={"sha": SHA_A}, authors=[], candidates=[], base="",
            notify="", unknown_authors=[], manual_seats=[], correctors=[],
            doubts=[], answered=[], advances=None, manual=[])
        with mock.patch.object(activation.rd, "apply", fake_apply), \
                mock.patch.object(activation, "_held_manual", return_value=[]):
            activation._selection(found, object(), force=True)
        port = seen["port"]
        self.assertIsInstance(port, activation._TracedPort)
        # le report forcé reste sous la trace : le port forcé est enveloppé
        self.assertIsInstance(port.port, activation._ForcedPort)
        self.assertEqual(port.trace["risk"], "sensitive")


class RiskTraceMarkTest(PgTestCase):
    """La marque écrite par le port réel, et ce que la trace ne change pas."""

    def setUp(self):
        super().setUp()
        for name, harness in (("r1", "claude"), ("r2", "codex"), ("pilot", "claude")):
            registry.upsert(self.db, name, harness=harness, host=self.cfg.host,
                            status="idle")

    def lot(self) -> int:
        return int(work.add(self.db, title="Tracer le risque au gel", app="ameesh")["id"])

    def freeze(self, lot: int, sha: str = SHA_A) -> dict:
        return storage.of(self.db).work.add_milestone(
            lot, "frozen", sha=sha, actor="author", verdict=None, note="")

    def marks(self, lot: int) -> list:
        return [pg.designation_of(row["note"]) for row in self.db.query(
            "SELECT note FROM work_item_events WHERE work_item_id = %s ORDER BY id",
            (lot,)) if str(row["note"]).startswith(pg.MARK_PREFIX)]

    def mailbox(self, lot: int) -> list:
        return [row["recipient"] for row in self.db.query(
            "SELECT recipient FROM agent_mailbox WHERE work_item_id = %s ORDER BY id",
            (str(lot),))]

    def designate(self, lot: int, found: activation.Policy, *, traced: bool = True,
                  paths=("src/ameesh/x.py",)) -> dict:
        port = pg.PgPort(self.db)
        if traced:
            port = activation._TracedPort(port, activation.risk_trace(inputs(found)))
        sha = port.latest_freeze(lot)["sha"]
        return rd.apply({"id": lot, "state": "build", "title": "t", "app": "ameesh"},
                        families=FAMILIES, policy=found.rules, paths=list(paths),
                        port=port, freeze_sha=sha,
                        authors=[{"name": "author", "family": "deepseek"}],
                        candidates=[rd.Candidate("r1", "claude"),
                                    rd.Candidate("r2", "codex")],
                        risk=found.risk, base=BASE, policy_ref=found.ref,
                        notify="pilot", required=found.required)

    def test_two_identical_quorums_keep_two_classes(self):
        light, sensitive = self.lot(), self.lot()
        self.freeze(light)
        self.freeze(sensitive)
        self.designate(light, policy("light", required=2, policy_required=1, floor=2))
        self.designate(sensitive, policy("sensitive", required=2, policy_required=2))
        a = pg.risk_trace_of(self.marks(light)[-1])
        b = pg.risk_trace_of(self.marks(sensitive)[-1])
        self.assertEqual((a["required"], b["required"]), (2, 2))
        self.assertEqual((a["risk"], b["risk"]), ("light", "sensitive"))
        self.assertEqual(a["quality"], "recorded")

    def test_an_explicit_floor_is_named_not_reclassified(self):
        lot = self.lot()
        self.freeze(lot)
        self.designate(lot, policy("light", required=2, policy_required=1, floor=2))
        found = pg.risk_trace_of(self.marks(lot)[-1])
        self.assertEqual(found["risk"], "light")
        self.assertEqual(found["required_from"],
                         {"policy": 1, "canon_lowering": 0, "floor": 2})
        self.assertEqual(found["policy"]["commit"], COMMIT)
        self.assertEqual(found["policy"]["source"], "manaty/ameesh-canon")

    def test_a_canon_lowering_is_named(self):
        lot = self.lot()
        self.freeze(lot)
        self.designate(lot, policy("light", required=2, policy_required=1, lowering=2))
        found = pg.risk_trace_of(self.marks(lot)[-1])
        self.assertEqual(found["required_from"]["canon_lowering"], 2)

    def test_a_changed_head_writes_its_own_exact_freeze_and_base(self):
        lot = self.lot()
        first = self.freeze(lot, SHA_A)
        found = policy("sensitive", required=2, policy_required=2)
        self.designate(lot, found)
        second = self.freeze(lot, SHA_B)
        self.designate(lot, found)
        traces = [pg.risk_trace_of(mark) for mark in self.marks(lot)]
        self.assertEqual([t["freeze"] for t in traces],
                         [{"id": int(first["id"]), "sha": SHA_A},
                          {"id": int(second["id"]), "sha": SHA_B}])
        self.assertEqual({t["base"] for t in traces}, {BASE})
        # l'ancienne marque garde sa trace, attachée à son gel
        old = pg.PgPort(self.db).recorded(lot, int(first["id"]))
        self.assertEqual(pg.risk_trace_of(old)["freeze"]["sha"], SHA_A)

    def test_a_requalification_is_one_class_per_freeze(self):
        lot = self.lot()
        first = self.freeze(lot, SHA_A)
        self.designate(lot, policy("light", required=1, policy_required=1),
                       paths=("docs/x.md",))
        second = self.freeze(lot, SHA_B)
        self.designate(lot, policy("sensitive", required=2, policy_required=2),
                       paths=("docs/x.md", "src/ameesh/x.sql"))
        port = pg.PgPort(self.db)
        self.assertEqual(pg.risk_trace_of(port.recorded(lot, int(first["id"])))["risk"],
                         "light")
        self.assertEqual(pg.risk_trace_of(port.recorded(lot, int(second["id"])))["risk"],
                         "sensitive")

    def test_a_port_call_without_trace_writes_an_n1_mark(self):
        lot = self.lot()
        self.freeze(lot)
        self.designate(lot, policy("sensitive", required=2, policy_required=2),
                       traced=False)
        mark = self.marks(lot)[-1]
        self.assertNotIn("risk_trace", mark)
        self.assertEqual(pg.risk_trace_of(mark)["risk"], pg.UNKNOWN)

    def test_the_trace_changes_neither_quorum_nor_families(self):
        plain, traced = self.lot(), self.lot()
        self.freeze(plain)
        self.freeze(traced)
        found = policy("light", required=2, policy_required=1, floor=2)
        out_plain = self.designate(plain, found, traced=False)
        out_traced = self.designate(traced, found)
        # même forme rendue, mêmes sièges, mêmes familles, mêmes courriers ;
        # seuls les gels (deux lots) diffèrent
        out_plain.pop("freeze")
        out_traced.pop("freeze")
        self.assertEqual(out_plain, out_traced)
        self.assertEqual(out_plain["required"], 2)
        self.assertEqual(self.mailbox(plain), self.mailbox(traced))
        a, b = self.marks(plain)[-1], self.marks(traced)[-1]
        b.pop("risk_trace")
        a.pop("freeze")
        b.pop("freeze")
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
