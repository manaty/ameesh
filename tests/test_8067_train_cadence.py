# SPDX-License-Identifier: AGPL-3.0-only
"""#8067 : un écart de canon qui ne bouge que la cadence garde le plan en vol.

L'admission d'un paquet est la preuve de revue de chaque membre (classe,
nombre, chemins classés, verdicts). Le commit du canon qui l'a lue est une
provenance, pas une preuve : une requalification qui ne change que
`packets.cutoffs` ne doit pas jeter la CI d'un paquet déjà composé.
"""
from __future__ import annotations

import dataclasses
import os
import re
import tempfile
import unittest

from ameesh import mechanics, train, train_policy

from .test_1467_train import MemoryPorts, row
from .test_l1757_paquets_horaires import MECHANICS, _Repo


def _full() -> str:
    return MECHANICS


def _narrow() -> str:
    # Seul le bloc `packets:` (deux espaces d'indentation) change.
    return re.sub(r'(?m)^  cutoffs: \[[^\]]*\]$',
                  '  cutoffs: ["11:00", "23:00"]', _full(), count=1)


class PolicyPorts(MemoryPorts):
    """MemoryPorts dont la preuve d'admission porte une politique versionnée."""

    def __init__(self, rows):
        super().__init__(rows)
        self.policy_commit = "c" * 40
        self.reviews = 2
        self.classified = [{"path": "db/schema.py", "class": "sensitive",
                            "rule": ["db/**", "**/*.sql"]}]

    def qualify(self, rows, base, mode):
        proof = super().qualify(rows, base, mode)
        proof["members"] = [
            dict(admission=member,
                 policy={"lot": int(member[0]), "policy_commit": self.policy_commit,
                         "reviews": self.reviews, "files": self.classified})
            for member in proof["members"]]
        return proof


class EngineTest(unittest.TestCase):
    def engine(self):
        self.ports = PolicyPorts([row(1)])
        return train.Engine(self.ports, source="github.com/o/r", target="main",
                            policy=train.Policy(enabled=True, build_beta=False))

    def test_a_cadence_only_canon_move_keeps_the_packet_in_flight(self):
        engine = self.engine()
        self.assertEqual(engine.step(3600)["result"], "ci")
        self.ports.policy_commit = "d" * 40
        result = engine.step(3601)
        self.assertEqual(result["result"], "ci", result)
        state = self.ports.load()
        self.assertEqual(state["phase"], "ci")
        self.assertNotIn("error", state)

    def test_a_changed_requirement_still_invalidates_the_packet(self):
        engine = self.engine()
        self.assertEqual(engine.step(3600)["result"], "ci")
        self.ports.reviews = 1
        result = engine.step(3601)
        self.assertEqual(result["result"], "refused", result)
        self.assertIn("admission evidence changed", result["error"])
        self.assertEqual(self.ports.load()["phase"], "draining")

    def test_a_changed_classified_path_still_invalidates_the_packet(self):
        engine = self.engine()
        self.assertEqual(engine.step(3600)["result"], "ci")
        self.ports.classified = [{"path": "db/schema.py", "class": "light",
                                  "rule": None}]
        result = engine.step(3601)
        self.assertEqual(result["result"], "refused", result)
        self.assertIn("admission evidence changed", result["error"])


class RealCanonTest(unittest.TestCase):
    """Deux canons réels dont seul `packets.cutoffs` diffère."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name in ("canon", "lot"):
            os.makedirs(os.path.join(self.tmp.name, name))
        self.canon = _Repo(os.path.join(self.tmp.name, "canon"))
        self.first = self.canon.git("rev-parse", "HEAD")
        self.canon.write("processus/mecanique.yaml", _narrow())
        self.second = self.canon.commit("cadence only")
        self.lot = _Repo(os.path.join(self.tmp.name, "lot"))
        self.base = self.lot.git("rev-parse", "HEAD")
        self.lot.write("src/ameesh/mechanics.py", "changed\n")
        self.head = self.lot.commit("member")
        self.item = dict(id=7, app="ameesh", type="evolution", branch_target="main")
        self.row = dict(work_item_id=7, generation=1, target="main", frozen_id=1,
                        frozen_sha=self.head, source="github.com/o/r")

    def requirement(self, commit):
        rules = mechanics.load(self.canon.root, commit, "ameesh")
        return train_policy.Compiled(rules)(self.item, self.row, self.base, self.lot.root)

    def proof(self, requirement):
        return {"schema": "ameesh-train-admissions/1", "source": "github.com/o/r",
                "base": self.base, "mode": "packet",
                "members": [{"admission": list(train.identity(self.row)),
                             "policy": dataclasses.asdict(requirement)}]}

    def test_the_two_canons_read_the_same_requirement_at_another_commit(self):
        first = mechanics.load(self.canon.root, self.first, "ameesh")
        second = mechanics.load(self.canon.root, self.second, "ameesh")
        self.assertNotEqual(first.packet_cutoffs, second.packet_cutoffs)
        left, right = self.requirement(self.first), self.requirement(self.second)
        self.assertNotEqual(left.policy_commit, right.policy_commit)
        self.assertEqual(dataclasses.replace(left, policy_commit=right.policy_commit),
                         right)

    def test_evidence_is_identical_across_the_cadence_only_move(self):
        left, right = self.requirement(self.first), self.requirement(self.second)
        # La preuve brute diffère (provenance) ; l'évidence d'admission non.
        self.assertNotEqual(train.digest(self.proof(left)),
                            train.digest(self.proof(right)))
        self.assertEqual(train.digest(train.evidence(self.proof(left))),
                         train.digest(train.evidence(self.proof(right))))

    def test_evidence_still_sees_a_changed_requirement(self):
        left = self.requirement(self.first)
        self.assertNotEqual(train.digest(train.evidence(self.proof(left))),
                            train.digest(train.evidence(self.proof(
                                dataclasses.replace(left, reviews=1)))))


if __name__ == "__main__":
    unittest.main()
