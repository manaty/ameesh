# SPDX-License-Identifier: AGPL-3.0-only
"""#6365 — la porte lit le delta que la désignation a lu.

Constat (orchestrateur Nexlink #201378, lot nexlink #6349, gel #35624) : la
tête 73832bb2e n'a qu'un commit, de mty-nxl-impl-02 (deepseek), et la
désignation n'a nommé que lui. La porte a pourtant refusé le quorum sensible
avec « author families of this head: codex, deepseek; not counted: codex3
(codex): author of the head ». Elle lisait `merge-base(base_sha, tête)..tête`
avec `base_sha` = la ref `origin/develop` du clone qui lisait, fraîche de son
seul dernier fetch ; sur une forge muette, le filtre « déjà dans la cible »
(#3116/#4673) retombait sur la même ref. Les commits entrés dans `develop`
depuis (dont un de codex3, relecteur désigné, donc compté correcteur)
devenaient des auteurs : trois familles, quorum impossible.

Attendu : la désignation garde dans sa marque le début du delta qu'elle a lu
(`merge-base` de la cible rafraîchie et de la tête) ; la porte relit cette
plage, sans dépendre de la fraîcheur de son clone ni de la forge. Seule une
marque écrite par le système compte ; une base locale plus récente reste.
"""
from __future__ import annotations

import json
import os
from unittest import mock

from ameesh import merge_queue, registry, review_designation as rd
from ameesh import review_designation_activation as act
from ameesh import review_designation_pg as pg
from ameesh import storage, work

from .support import PgTestCase
from .test_2447_review_designation_activation import _Repo

FAMILIES = ("claude", "codex", "deepseek")


class StaleCloneMuteForge(PgTestCase):
    """Un clone en retard sur la cible, une forge injoignable."""

    def setUp(self):
        super().setUp()
        for table in ("turn_costs", "provenance_receipts", "canon_state",
                      "quota_gauge_readings"):
            self.db.execute("DELETE FROM %s" % table)
        for name, harness in (("impl", "deepseek"), ("third", "codex"),
                              ("fixer", "claude")):
            registry.upsert(self.db, name, harness=harness, host=self.cfg.host,
                            cwd=os.path.join(self.tmp, "writer"), status="idle")
            self.db.execute("UPDATE agent_registry SET team = 'ameesh'"
                            " WHERE name = %s", (name,))
        self.writer = _Repo(os.path.join(self.tmp, "writer"))
        self.writer.write("base.txt", "base\n")
        self.base = self.writer.commit("base")
        self.writer.git("branch", "-M", "main")
        origin = os.path.join(self.tmp, "origin.git")
        self.writer.git("init", "--bare", "-q", origin)
        self.writer.git("remote", "add", "origin", origin)
        self.writer.git("push", "-q", "origin", "main")
        self.reader = _Repo(os.path.join(self.tmp, "reader"))
        self.reader.git("remote", "add", "origin", origin)
        self.reader.git("fetch", "-q", "origin", "main:refs/remotes/origin/main")
        # Un tiers entre dans la cible après le dernier fetch du clone.
        self.writer.write("deploy-api.yml", "third party\n")
        self.third = self.writer.commit("third: delivered by another lot",
                                        agent="third")
        self.writer.git("push", "-q", "origin", "main")
        self.writer.git("checkout", "-q", "-b", "lot/6365")
        self.writer.write("own.txt", "own change\n")
        self.head = self.writer.commit("impl: own change", agent="impl")
        self.writer.git("push", "-q", "origin", "HEAD")
        self.reader.git("fetch", "-q", "origin", self.head)
        # La forge se tait : ni `ls-remote` ni fetch ne rafraîchissent la cible.
        self.reader.git("remote", "set-url", "origin",
                        os.path.join(self.tmp, "missing.git"))
        self.assertEqual(self.reader.git("rev-parse", "refs/remotes/origin/main"),
                         self.base)
        self.item = work.add(self.db, title="porte #6365", app="ameesh",
                             assignee="impl", branch="lot/6365")
        self.lot = int(self.item["id"])
        self.receipt(self.head, "impl", "deepseek")
        self.receipt(self.third, "third", "codex")
        self.marks = [{"id": 1, "kind": "frozen", "sha": self.head,
                       "actor": "impl", "at_ts": 0.0}]

    def receipt(self, sha, actor, harness):
        self.db.execute(
            "INSERT INTO provenance_receipts"
            " (subject, sha, actor, session, turn, harness, work_item_id)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (subject) DO NOTHING",
            ("commit:" + sha, sha, actor, "s-" + harness, "t", harness, self.lot))

    def mark(self, base, *, actor=pg.SENDER, freeze_id=1, evidence=True):
        """Une marque ; `evidence=True` : la preuve que pose le port."""
        payload = {"state": rd.DESIGNATED, "required": 2, "reviewers": [],
                   "missing": 0, "why": "", "authors": [],
                   "freeze": {"id": freeze_id, "sha": self.head},
                   "unknown_authors": [], "policy_ref": "", "sent": [],
                   "base": base}
        storage.of(self.db).work.note(
            self.lot, "qa", "%s %s" % (pg.mark_prefix(self.lot, freeze_id),
                                       json.dumps(payload, sort_keys=True)),
            actor,
            evidence=(pg.base_evidence(self.lot, freeze_id, self.head, base)
                      if evidence is True else evidence or None))

    def authors(self):
        authors, _correctors = merge_queue._attribution_on_freeze(
            self.db, self.marks, self.head, repo=self.reader.root,
            klass={"base_sha": self.base, "class": "sensitive",
                   "target": "origin/main"},
            item=work.get(self.db, self.lot), families=FAMILIES, strict=True,
            target="origin/main")
        return {row["name"] for row in authors}


class GateReadsTheDesignatedDeltaTest(StaleCloneMuteForge):
    def test_without_a_designated_base_the_stale_range_counts_the_third(self):
        # Le témoin du défaut : sans marque, le doute garde le tiers.
        self.assertEqual(self.authors(), {"impl", "third"})

    def test_the_designated_base_leaves_the_third_party_out(self):
        self.mark(self.third)
        self.assertEqual(self.authors(), {"impl"})

    def test_a_note_claiming_the_system_actor_is_not_a_base(self):
        # B/P1 impl-03 : `work note --actor ameesh` copie le texte et
        # l'acteur, jamais la preuve structurée.
        self.mark(self.third, evidence=False)
        self.assertEqual(self.authors(), {"impl", "third"})

    def test_a_forged_mark_after_the_system_one_does_not_replace_its_base(self):
        # La marque forgée nomme un autre ancêtre valable de la tête : seule
        # l'absence de preuve l'écarte.
        self.mark(self.third)
        self.mark(self.base, evidence=False)
        self.assertEqual(merge_queue._designated_base(
            self.db, work.get(self.db, self.lot), self.marks[0],
            self.reader.root, self.head), self.third)

    def test_an_evidence_naming_another_base_is_not_a_proof(self):
        self.mark(self.third, evidence=pg.base_evidence(
            self.lot, 1, self.head, self.base))
        self.assertEqual(self.authors(), {"impl", "third"})

    def test_an_evidence_of_another_head_is_not_a_proof(self):
        self.mark(self.third, evidence=pg.base_evidence(
            self.lot, 1, self.third, self.third))
        self.assertEqual(self.authors(), {"impl", "third"})

    def test_a_mark_of_another_freeze_is_not_a_base(self):
        self.mark(self.third, freeze_id=2)
        self.assertEqual(self.authors(), {"impl", "third"})

    def test_a_base_that_is_not_an_ancestor_of_the_head_is_ignored(self):
        self.writer.git("checkout", "-q", "main")
        self.writer.write("elsewhere.txt", "elsewhere\n")
        stray = self.writer.commit("stray", agent="third")
        self.writer.git("push", "-q", "origin", "main")
        self.reader.git("fetch", "-q", os.path.join(self.tmp, "origin.git"), stray)
        self.mark(stray)
        self.assertEqual(self.authors(), {"impl", "third"})

    def test_the_head_itself_is_never_a_base(self):
        self.mark(self.head)
        self.assertEqual(self.authors(), {"impl", "third"})

    def test_the_real_delta_still_counts_its_authors(self):
        self.writer.git("checkout", "-q", "lot/6365")
        self.writer.write("fix.txt", "a fix by another family\n")
        fixed = self.writer.commit("fixer: correction", agent="fixer")
        self.writer.git("push", "-q", "origin", "HEAD")
        self.reader.git("fetch", "-q", os.path.join(self.tmp, "origin.git"), fixed)
        self.receipt(fixed, "fixer", "claude")
        self.head = fixed
        self.marks = [{"id": 1, "kind": "frozen", "sha": fixed,
                       "actor": "impl", "at_ts": 0.0}]
        self.mark(self.third)
        self.assertEqual(self.authors(), {"impl", "fixer"})

    def test_a_newer_local_base_is_kept(self):
        # La marque nomme une base plus ancienne que la ref du clone : la
        # lecture garde la plus récente, jamais une plage plus large.
        self.reader.git("update-ref", "refs/remotes/origin/main", self.third)
        self.mark(self.base)
        seen = {}
        real = act.attribution_of_head

        def spy(db, item, milestone, repo, base, head, **kwargs):
            seen["base"] = base
            return real(db, item, milestone, repo, base, head, **kwargs)

        with mock.patch.object(act, "attribution_of_head", side_effect=spy):
            authors, _correctors = merge_queue._attribution_on_freeze(
                self.db, self.marks, self.head, repo=self.reader.root,
                klass={"base_sha": self.third, "class": "sensitive",
                       "target": "origin/main"},
                item=work.get(self.db, self.lot), families=FAMILIES,
                strict=True, target="origin/main")
        self.assertEqual(seen["base"], self.third)
        self.assertEqual({row["name"] for row in authors}, {"impl"})


class DesignationWritesItsBaseTest(StaleCloneMuteForge):
    """Parité : la base écrite par la désignation est celle que la porte lit."""

    def test_the_port_writes_the_base_and_its_proof(self):
        # Câblage réel : `PgPort.commit` → marque + preuve → porte.
        rows = self.db.query(
            "INSERT INTO work_item_milestones (work_item_id, kind, sha, actor)"
            " VALUES (%s, 'frozen', %s, 'impl') RETURNING id",
            (self.lot, self.head))
        freeze_id = int(rows[0]["id"])
        self.marks = [{"id": freeze_id, "kind": "frozen", "sha": self.head,
                       "actor": "impl", "at_ts": 0.0}]
        pg.PgPort(self.db).commit(
            self.lot, freeze={"id": freeze_id, "sha": self.head,
                              "base": self.third},
            required=1, state=rd.DESIGNATED,
            reviewers=[{"name": "fixer", "family": "claude"}], missing=0,
            why="", authors=[{"name": "impl", "family": "deepseek"}])
        events = storage.of(self.db).work.events_noted(
            pg.mark_prefix(self.lot, freeze_id), 0.0, ids=[self.lot])
        self.assertEqual(pg.designation_of(events[-1]["note"])["base"], self.third)
        self.assertEqual(merge_queue._designated_base(
            self.db, work.get(self.db, self.lot), self.marks[0],
            self.reader.root, self.head), self.third)
        self.assertEqual(self.authors(), {"impl"})

    def test_apply_hands_the_port_its_full_commit_base(self):
        seen = []

        class Port:
            def latest_freeze(self, lot):
                return {"id": 1, "sha": self.head}

            def recorded(self, lot, freeze_id):
                return None

            def commit(self, lot, **kwargs):
                seen.append(dict(kwargs["freeze"]))
                return {"state": kwargs["state"], "freeze": kwargs["freeze"]}

        port = Port()
        port.head = self.head
        candidates = [rd.Candidate(name="fixer", family="claude",
                                   available=True, load=0)]
        for base, expected in ((self.third, self.third), ("origin/main", None)):
            rd.apply({"id": self.lot, "assignee": "impl"},
                     policy={"rules": [{"paths": ["**"], "reviews": 1}]},
                     paths=["own.txt"], port=port,
                     freeze_sha=self.head, candidates=candidates,
                     base=base, families=FAMILIES, required=1)
            self.assertEqual(seen[-1].get("base"), expected)
