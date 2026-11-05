# SPDX-License-Identifier: AGPL-3.0-only
"""#5880 — un agent du registre qui commite après la tête relue est un auteur.

Constat nexlink #5852 (05/11 03:00Z) : la tête a9b6b2927 de claude2 avait ses
deux avis ; le pilote (orchestrateur) a ajouté deux adaptations de test, sans
reçu exact. Le report a porté les avis, mais la lecture de la plage tenait ces
commits pour ceux d'un tiers inconnu : la porte refusait (`error`), et le
report notait « the review gate is unreadable » cinq fois par hôte sans dire
pourquoi.

Attendu :
1. un commit sans reçu d'un agent du registre dont le journal des tours
   prouve la famille à l'instant du commit est un auteur tiers, avec cette
   famille (sa famille est exclue) ; sans preuve, il reste inconnu (#2721) ;
2. une plage lue qui ne nomme aucun auteur le dit, au lieu d'affirmer que
   l'assigné n'a pas de famille connue ;
3. la note de report d'une porte en `error` dit la vraie raison.
"""
from __future__ import annotations

import os
from unittest import mock

from ameesh import lot_class, merge_queue, provenance_scope, registry, review_authors
from ameesh import verdict_carry, work

from .support import PgTestCase
from .test_2447_review_designation_activation import _Repo
from .test_5481_report_before_designation import _Reviewed

FAMILIES = ("claude", "codex", "deepseek")
AUTHOR = "impl5880"                    # deepseek, la tête relue
PILOT = "pilot5880"                    # claude, les adaptations de test


class _PilotRange(PgTestCase):
    """La tête relue de l'assigné, puis une adaptation de test du pilote."""

    def setUp(self):
        super().setUp()
        for table in ("turn_costs", "provenance_receipts", "canon_state",
                      "quota_gauge_readings"):
            self.db.execute("DELETE FROM %s" % table)
        self.repo = _Repo(os.path.join(self.tmp, "depot"))
        for name, harness in ((AUTHOR, "deepseek"), (PILOT, "claude")):
            registry.upsert(self.db, name, harness=harness, host=self.cfg.host,
                            cwd=self.repo.root, status="idle")
            self.db.execute("UPDATE agent_registry SET team = 'ameesh'"
                            " WHERE name = %s", (name,))
        self.repo.write("src/keys.py", "base\n")
        self.base = self.repo.commit("base")
        self.item = work.add(self.db, title="tiers du registre #5880",
                             app="ameesh", assignee=AUTHOR)
        self.lot = int(self.item["id"])
        self.repo.write("src/keys.py", "def bootstrap_root():\n    return wallet\n")
        self.reviewed = self.repo.commit("impl: bootstrap", agent=AUTHOR)
        self.db.execute(
            "INSERT INTO provenance_receipts"
            " (subject, sha, actor, session, turn, harness, work_item_id)"
            " VALUES (%s, %s, %s, 's', 't', 'deepseek', %s)",
            ("commit:" + self.reviewed, self.reviewed, AUTHOR, self.lot))
        self.repo.write("tests/test_keys.py", "def test_bootstrap_root_adapted():\n"
                                              "    assert bootstrap_root()\n")
        self.adapted = self.repo.commit("pilot: test adaptation", agent=PILOT)

    def pilot_turn(self, harness="claude"):
        stamp = self.repo.git("log", "-1", "--format=%ct", self.adapted)
        self.db.execute(
            "INSERT INTO turn_costs (agent, harness, session, recorded_at)"
            " VALUES (%s, %s, %s, to_timestamp(%s))",
            (PILOT, harness, "s-" + harness, stamp))

    def reading(self, commits):
        return review_authors.reading(
            self.db, work.get(self.db, self.lot), None, commits,
            self.repo.root, FAMILIES, strict=True, base=self.base,
            head=self.adapted, target=self.base)

    @staticmethod
    def names(authors):
        return {row["name"]: row["family"] for row in authors}


class RegistryThirdPartyTest(_PilotRange):

    def test_a_range_of_only_pilot_commits_names_the_pilot_with_its_family(self):
        # La plage après la tête relue : seul le commit du pilote.
        self.pilot_turn()
        authors, unknown, _correctors = self.reading([self.adapted])
        self.assertEqual(unknown, [])
        self.assertEqual(self.names(authors), {PILOT: "claude"})

    def test_the_whole_range_keeps_the_assignee_and_adds_the_pilot(self):
        self.pilot_turn()
        authors, unknown, _correctors = self.reading([self.adapted, self.reviewed])
        self.assertEqual(unknown, [])
        self.assertEqual(self.names(authors), {AUTHOR: "deepseek", PILOT: "claude"})

    def test_without_a_turn_the_registry_third_party_stays_unknown(self):
        # #2721 : le registre courant ne prouve aucune famille historique.
        _authors, unknown, _correctors = self.reading([self.adapted])
        self.assertEqual(unknown, [self.adapted[:12]])

    def test_the_gate_excludes_the_pilot_family(self):
        self.pilot_turn()
        authors, _unknown, _correctors = self.reading([self.adapted, self.reviewed])
        marks = [{"kind": "verdict", "sha": self.adapted, "verdict": "ok",
                  "actor": actor, "id": index, "at_ts": float(index)}
                 for index, actor in enumerate(("r_claude", "r_codex"), 1)]
        gate = merge_queue.review_gate(
            None, marks, self.adapted, required=1,
            klass={"class": "sensitive", "doubt": False, "files": []},
            item={"assignee": AUTHOR}, authors=authors, authors_complete=True,
            family_of=lambda name: {AUTHOR: "deepseek", PILOT: "claude",
                                    "r_claude": "claude",
                                    "r_codex": "codex"}.get(str(name), ""))
        self.assertTrue(gate["ok"], gate)
        self.assertEqual([row["actor"] for row in gate["accepted"]], ["r_codex"])


class EmptyReadingWhyTest(PgTestCase):
    """Une plage lue sans auteur ne dit plus « famille inconnue » de l'assigné."""

    def test_the_refusal_names_the_empty_reading(self):
        marks = [{"kind": "verdict", "sha": "a" * 40, "verdict": "ok",
                  "actor": "r_codex", "id": 1, "at_ts": 1.0}]
        with mock.patch.object(merge_queue, "_attribution_on_freeze",
                               return_value=([], [])), \
                mock.patch.object(provenance_scope, "production", return_value=None):
            gate = merge_queue.review_gate(
                None, marks, "a" * 40, required=1, repo=self.tmp,
                klass={"class": "sensitive", "doubt": False, "files": []},
                item={"assignee": "claude2", "id": 5852},
                family_of=lambda name: {"claude2": "claude",
                                        "r_codex": "codex"}.get(str(name), ""))
        self.assertFalse(gate["ok"])
        self.assertIn("names no author", gate["why"])
        self.assertNotIn("no known model family", gate["why"])


class CarriedPendingWhyTest(_Reviewed):
    """#5880 (2) : la note de reprise dit la raison d'une porte en `error`."""

    def test_the_pending_note_carries_the_gate_error(self):
        self.reviewed_freeze()
        freeze = self.freeze_latest()
        qa_row = dict(work.get(self.db, self.lot), state="qa")
        gate = {"ok": False, "error": "author provenance unknown for frozen "
                                      "commit(s): abf5dc36acb8",
                "why": "no review verdict ok on the freeze abf5dc36acb8"}
        with mock.patch.object(verdict_carry.work, "get", return_value=qa_row), \
                mock.patch.object(verdict_carry, "_carried_ok_on_freeze",
                                  return_value=[{"id": 1}]), \
                mock.patch.object(merge_queue, "review_gate_for_item",
                                  return_value=gate), \
                mock.patch.object(lot_class, "repo_for_freeze",
                                  return_value=(self.repo.root, freeze["sha"])):
            result = verdict_carry.promote_carried(self.db, self.lot, freeze=freeze)
        self.assertEqual(result["result"], "pending", result)
        notes = self.notes(verdict_carry.PENDING)
        self.assertEqual(len(notes), 1, notes)
        self.assertIn("author provenance unknown for frozen commit(s): abf5dc36acb8",
                      notes[0])
        self.assertNotIn("unreadable", notes[0])
