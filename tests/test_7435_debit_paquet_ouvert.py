# SPDX-License-Identifier: AGPL-3.0-only
"""#7435 : un membre d'un paquet ouvert à la même tête n'est pas `qa_merge`.

Témoin du 14/11 à 23:15Z : #7374, #7063, #7150 et #7375 étaient membres du
paquet 634 (PR #4600) en CI ; `debit --check` sortait 2 en les classant
`qa_merge` (attente du paquet). Un lot composé dans un porteur **non terminé**
à sa tête gelée a le paquet pour voie d'atterrissage : le rapport le classe
`in_packet` (niveau 0), hors `slow_waits`. Un porteur terminé, une composition
sur une autre tête ou un état de porteur absent laissent la ligne revenir.

#7445 : un porteur en retenue (`blocked`, `waiting_human`) ne porte plus ses
membres. Son attente propre peut être invisible (un porteur bloqué sans
`hold:` n'a aucune ligne à lui) : classer ses membres `in_packet` masquerait
l'attente jusqu'à recomposition ou fermeture. Le membre redevient donc une
attente `qa_merge` liée au porteur ; elle reste hors `--check` quand le
porteur attend une décision humaine (`waiting_human`).
"""
from __future__ import annotations

import json
import unittest

from ameesh import debit, registry, work

from .support import PgTestCase

T = 1794186000.0            # 2026-11-09 01:00:00Z
H = 3600.0
SHA = "a" * 40


def item(ident, *, app="ameesh", assignee="deepseek6"):
    return {"id": ident, "title": "lot %d" % ident, "app": app,
            "assignee": assignee, "priority": 2,
            "branch": "lot/%d-test" % ident, "branch_target": "main"}


def frozen(ident, mid, at, sha=SHA):
    return {"work_item_id": ident, "id": mid, "kind": "frozen", "at_ts": at,
            "sha": sha, "actor": "auteur", "verdict": None, "note": ""}


def verdict(ident, mid, at, actor="orchestrateur", kind="ok", sha=SHA):
    return {"work_item_id": ident, "id": mid, "kind": "verdict", "at_ts": at,
            "sha": sha, "actor": actor, "verdict": kind, "note": ""}


def event(ident, eid, at, note, actor="pilote", state="qa"):
    return {"work_item_id": ident, "id": eid, "state": state, "at_ts": at,
            "actor": actor, "note": note}


def mark(ident, freeze_id, names):
    return "review-designation:%d:%d: %s" % (
        ident, freeze_id,
        json.dumps({"reviewers": [{"name": n} for n in names]}))


def reviewed(ident, freeze_id, at, reviewer="orchestrateur"):
    """Un lot relu : gel, désignation, verdict ok sur la même tête."""
    return ([frozen(ident, freeze_id, at),
             verdict(ident, freeze_id + 1, at + H)],
            [event(ident, freeze_id, at, mark(ident, freeze_id, [reviewer]))])


def run(**extra):
    values = dict(items=[], milestones=[], events=[], agents=[], turns=[],
                  merges=[], ci_runs=[], jobs=[], packets=[], now=T,
                  installed="1.55.0",
                  devices={"names": [], "phrases": [], "sources": [],
                           "unreadable": []},
                  hours=6, wait_s=2 * H, ci_hours=24, ci_slow_s=30 * 60)
    values.update(extra)
    return debit.build(**values)


def kinds(report):
    return [(row["lot"], row["kind"]) for row in report["slow_waits"]]


def qa_row(report, lot):
    return next(row for row in report["qa"] if row["lot"] == lot)


class PacketMemberTest(unittest.TestCase):
    """Le membre d'un porteur non terminé à sa tête gelée reste niveau 0."""

    def packet(self, *, state="qa", sha=SHA, include_state=True):
        row = {"package_id": 7427, "work_item_id": 7101,
               "frozen_sha": sha, "composed_ts": T - 5 * H}
        if include_state:
            row["package_state"] = state
        return row

    def report(self, **kw):
        milestones, events = reviewed(7101, 100, T - 6 * H)
        return run(items=[item(7101)], milestones=milestones, events=events,
                   packets=[self.packet(**kw)])

    def test_membre_du_paquet_a_la_meme_tete_n_est_pas_qa_merge(self):
        report = self.report()
        self.assertNotIn((7101, "qa_merge"), kinds(report))
        self.assertEqual(qa_row(report, 7101)["in_packet"], 7427)
        self.assertEqual(report["totals"]["in_packet"], 1)

    def test_porteur_termine_rend_la_ligne(self):
        for state in ("delivered", "merged", "promoted", "closed"):
            with self.subTest(state=state):
                report = self.report(state=state)
                self.assertIn((7101, "qa_merge"), kinds(report))
                self.assertNotIn("in_packet", qa_row(report, 7101))
                self.assertEqual(report["totals"]["in_packet"], 0)

    def test_porteur_actif_ou_en_revue_porte_toujours(self):
        # La frontière de #7445 : seules les retenues retirent la qualité de
        # porteur ; un porteur en construction ou en revue porte son membre.
        for state in ("intake", "build", "qa", "validated"):
            with self.subTest(state=state):
                report = self.report(state=state)
                self.assertNotIn((7101, "qa_merge"), kinds(report))
                self.assertEqual(qa_row(report, 7101)["in_packet"], 7427)

    def test_porteur_bloque_rend_une_attente_d_agent(self):
        # #7445 : un porteur bloqué ne porte plus son membre ; l'attente
        # redevient visible, liée au porteur, et reste une attente d'agent
        # (le pilote débloque le porteur ou recompose le membre).
        report = self.report(state="blocked")
        self.assertIn((7101, "qa_merge"), kinds(report))
        wait = next(row for row in report["slow_waits"] if row["lot"] == 7101)
        self.assertEqual(wait["packet"], 7427)
        self.assertEqual(wait["packet_state"], "blocked")
        self.assertNotIn("in_packet", qa_row(report, 7101))
        self.assertEqual(report["totals"]["in_packet"], 0)

    def test_porteur_en_attente_humaine_rend_une_attente_humaine(self):
        # #7445 : un porteur `waiting_human` attend une décision owner ; le
        # membre est visible dans les attentes humaines, hors `--check`.
        report = self.report(state="waiting_human")
        self.assertNotIn((7101, "qa_merge"), kinds(report))
        self.assertIn((7101, "qa_merge"),
                      [(row["lot"], row["kind"])
                       for row in report["human_waits"]])
        wait = next(row for row in report["human_waits"] if row["lot"] == 7101)
        self.assertEqual(wait["packet"], 7427)
        self.assertEqual(wait["packet_state"], "waiting_human")
        self.assertNotIn("in_packet", qa_row(report, 7101))
        self.assertEqual(report["totals"]["in_packet"], 0)

    def test_porteur_compose_sur_une_autre_tete_rend_la_ligne(self):
        report = self.report(sha="b" * 40)
        self.assertIn((7101, "qa_merge"), kinds(report))
        self.assertNotIn("in_packet", qa_row(report, 7101))

    def test_etat_de_porteur_absent_ne_prouve_rien(self):
        # Une lecture incomplète (appel sans `package_state`) ne doit jamais
        # taire une attente : le doute laisse la ligne.
        report = self.report(include_state=False)
        self.assertIn((7101, "qa_merge"), kinds(report))
        self.assertNotIn("in_packet", qa_row(report, 7101))

    def test_porteur_ouvert_a_la_meme_tete_mais_lot_non_relu_garde_sa_ligne(self):
        # La classification ne couvre que le lot réellement relu : un avis dû
        # reste un avis dû, membre ou non du paquet.
        milestones = [frozen(7101, 100, T - 6 * H)]
        events = [event(7101, 100, T - 6 * H,
                        mark(7101, 100, ["orchestrateur"]))]
        report = run(items=[item(7101)], milestones=milestones, events=events,
                     packets=[self.packet()])
        self.assertIn((7101, "qa"), kinds(report))
        self.assertNotIn((7101, "qa_merge"), kinds(report))
        self.assertNotIn("in_packet", qa_row(report, 7101))


class ReadPacketTest(PgTestCase):
    """Le SQL de `read` porte bien l'état du porteur jusqu'à `build`."""

    def setUp(self):
        super().setUp()
        self.db.execute(
            "TRUNCATE work_packet_members, work_item_milestones,"
            " work_item_events, work_items CASCADE")
        for name, harness in (("deepseek6", "deepseek"),
                              ("orchestrateur", "claude")):
            registry.upsert(self.db, name, harness=harness, host=self.cfg.host)
        self.lot = work.add(self.db, title="membre de paquet",
                            assignee="deepseek6", app="ameesh",
                            branch="lot/7435-membre", branch_target="main",
                            actor="deepseek6")
        self.lot_id = int(self.lot["id"])
        work.move(self.db, self.lot_id, "build", actor="deepseek6")
        work.move(self.db, self.lot_id, "qa", actor="deepseek6")
        self.db.execute(
            "INSERT INTO work_item_milestones"
            " (id, work_item_id, kind, at, sha, actor, verdict, note)"
            " VALUES (7435001, %s, 'frozen', to_timestamp(%s), %s, 'auteur',"
            " NULL, '')", (self.lot_id, T - 6 * H, SHA))
        self.db.execute(
            "INSERT INTO work_item_milestones"
            " (id, work_item_id, kind, at, sha, actor, verdict, note)"
            " VALUES (7435002, %s, 'verdict', to_timestamp(%s), %s,"
            " 'orchestrateur', 'ok', '')", (self.lot_id, T - 5 * H, SHA))
        self.db.execute(
            "INSERT INTO work_item_events"
            " (work_item_id, state, note, actor, created_at)"
            " VALUES (%s, 'qa', %s, 'ameesh', to_timestamp(%s))",
            (self.lot_id, mark(self.lot_id, 7435001, ["orchestrateur"]),
             T - 6 * H))
        self.db.execute(
            "INSERT INTO work_items"
            " (id, type, source, title, app, state, assignee, branch,"
            "  branch_target, created_at)"
            " VALUES (7427, 'evolution', 'test', 'paquet 634', 'ameesh', 'qa',"
            " 'orchestrateur', 'integration/paquet-634-test', 'main',"
            " to_timestamp(%s))", (T - 2 * H,))
        self.db.execute(
            "INSERT INTO work_packet_members"
            " (package_id, work_item_id, package_branch, base_sha, head_sha,"
            "  unit_sha, source_sha, frozen_sha, recorded_by, recorded_at)"
            " VALUES (7427, %s, 'integration/paquet-634-test', %s, %s, %s, %s,"
            " %s, 'test', to_timestamp(%s))",
            (self.lot_id, "b" * 40, "c" * 40, "d" * 40, SHA, SHA, T - 1 * H))

    def read(self):
        return debit.read(self.db, cfg=self.cfg, now=T, wait_s=2 * H,
                          jobs=False, canons=[])

    def test_membre_d_un_porteur_en_qa_ne_sort_pas_en_qa_merge(self):
        report = self.read()
        self.assertNotIn((self.lot_id, "qa_merge"),
                         [(row["lot"], row["kind"])
                          for row in report["slow_waits"]])
        self.assertEqual(qa_row(report, self.lot_id)["in_packet"], 7427)
        self.assertEqual(report["totals"]["in_packet"], 1)

    def test_porteur_ferme_rend_la_ligne(self):
        self.db.execute(
            "UPDATE work_items SET state = 'closed', legacy_state = 'closed',"
            " close_reason = 'abandoned'"
            " WHERE id = 7427")
        report = self.read()
        self.assertIn((self.lot_id, "qa_merge"),
                      [(row["lot"], row["kind"])
                       for row in report["slow_waits"]])
        self.assertNotIn("in_packet", qa_row(report, self.lot_id))

    def test_porteur_en_retenue_rend_la_ligne(self):
        # #7445 : `read` porte bien `blocked`/`waiting_human` jusqu'à `build`,
        # où le membre n'est plus classé `in_packet` ; la ligne est un agent
        # pour un porteur bloqué, une attente humaine pour `waiting_human`.
        for state, bucket in (("blocked", "slow_waits"),
                              ("waiting_human", "human_waits")):
            with self.subTest(state=state):
                self.db.execute(
                    "UPDATE work_items SET state = %s WHERE id = 7427",
                    (state,))
                report = self.read()
                self.assertIn((self.lot_id, "qa_merge"),
                              [(row["lot"], row["kind"])
                               for row in report[bucket]])
                self.assertNotIn("in_packet", qa_row(report, self.lot_id))
                self.assertEqual(report["totals"]["in_packet"], 0)


if __name__ == "__main__":
    unittest.main()
