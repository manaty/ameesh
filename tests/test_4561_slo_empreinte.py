# SPDX-License-Identifier: AGPL-3.0-only
"""#4561 — l'attente de la garde d'empreinte (#3185) ne compte pas dans les
pas SLO « signalé → assigné » et « prêt → démarré ».

Constat du 29/10 : #4515 (empreinte `runner.py`) et #4521
(`tests/test_generic_core.py`) restent en intake parce que `work assign` les
refuse — un lot occupant du même dépôt chevauche leur empreinte —, et l'horloge
SLO escalade pourtant jusqu'au niveau 3 (« bureau or auditor: reassign it now »)
un lot que l'outil ne laisse pas attribuer. `ameesh debit` les range aussi en
« démarrables ».

Témoin : lot A en qa sur `f.py`, lot B en intake sur `f.py` → aucune escalade
SLO sur B, B hors des démarrables (rangé « en attente d'empreinte » avec A et
son état) ; à la fusion de A, l'horloge de B repart de cette fusion, pas de sa
création.
"""
from __future__ import annotations

import time
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import debit, exploitation, registry, slo, storage, work

from .support import PgTestCase
from .test_3256_debit_capacity import H, PART, REPOS, T, team
from .test_789_slo_alerts import _seuils


T0 = 1_792_950_000.0        # 2026-10-26T12:00:00Z, un instant de référence


def _item(**kw):
    item = {"id": 2, "state": "intake", "priority": 2, "assignee": None,
            "app": "ameesh", "created_ts": T0}
    item.update(kw)
    return item


def _note(at, ids=(1,), ident=1, *, stamped=True):
    return {"id": ident, "state": "intake", "created_ts": at,
            "note": slo.footprint_wait_note([{"id": i} for i in ids],
                                            at=at if stamped else None)}


def _release(at, ids=(1,), ident=9):
    return {"id": ident, "state": "intake", "created_ts": at,
            "note": slo.footprint_release_note([{"id": i} for i in ids], at=at)}


class EngineTest(unittest.TestCase):
    """Le moteur : suspension, redémarrage, épisodes, garde qui ne s'applique pas."""

    def test_les_deux_pas_sont_suspendus_pendant_l_attente(self):
        blockers = [{"id": 1, "state": "qa", "assignee": "dev1"}]
        wait = slo.footprint_wait([], blockers)
        rows = slo.lot_steps(_item(), {"requested": T0}, [],
                             now=T0 + 3 * 3600, footprint=wait)
        self.assertEqual(rows, [])

    def test_l_horloge_repart_de_la_fusion_pas_de_la_creation(self):
        release = T0 + 7200.0
        events = [_note(T0 + 60, ids=(1,))]
        wait = slo.footprint_wait(events, [], satisfied={1: release})
        rows = slo.lot_steps(_item(), {"requested": T0}, events,
                             now=release + 1800, footprint=wait)
        by_step = {row["step"]: row for row in rows}
        for step in ("signal_assigned", "intake_started"):
            self.assertIn(step, by_step)
            self.assertEqual(by_step[step]["started_ts"], release, step)
            self.assertEqual(by_step[step]["elapsed_s"], 1800, step)
            self.assertEqual(by_step[step]["level"], 0, step)
            self.assertEqual(by_step[step]["footprint_released_ts"], release, step)
            self.assertEqual(by_step[step]["footprint_lots"], [1], step)
        self.assertEqual(slo.breaches(rows), [])

    def test_apres_la_levée_le_retard_ne_part_plus_de_la_creation(self):
        # Sans la levée datée, #4515 aurait 13 h de retard : niveau 3.
        release = T0 + 4 * 3600.0
        events = [_note(T0 + 60)]
        wait = slo.footprint_wait(events, [], satisfied={1: release})
        rows = slo.lot_steps(_item(), {"requested": T0}, events,
                             now=release + 1.5 * 3600, footprint=wait)
        levels = {row["step"]: row["level"] for row in rows}
        self.assertEqual(levels, {"signal_assigned": 1, "intake_started": 1})
        self.assertEqual({row["started_ts"] for row in rows}, {release})

    def test_un_lot_attribue_ne_porte_pas_l_attente(self):
        # La garde ne refuse pas la réassignation d'un lot en cours : seule
        # l'attribution d'un lot intake sans porteur est retenue.
        assigned = _item(assignee="dev1", state="intake")
        lots = [_item(id=1, state="qa", assignee="dev1"),
                {"id": 2, "state": "intake", "assignee": "dev1", "app": "ameesh"}]
        paths = {1: ["f.py"], 2: ["f.py"]}
        self.assertEqual(slo.footprint_blockers(assigned, lots, paths), [])

    def test_meme_depot_et_chemin_seulement(self):
        other = {"id": 1, "state": "qa", "assignee": "dev1", "app": "ameesh"}
        paths = {2: ["f.py"], 1: ["f.py"]}
        self.assertEqual([b["id"] for b in
                          slo.footprint_blockers(_item(), [other], paths)], [1])
        # autre dépôt
        elsewhere = dict(other, app="nexlink")
        self.assertEqual(slo.footprint_blockers(_item(), [elsewhere], paths), [])
        # un lot intake sans porteur n'occupe rien
        free = dict(other, state="intake", assignee=None)
        self.assertEqual(slo.footprint_blockers(_item(), [free], paths), [])
        # un lot fusionné ne retient plus
        done = dict(other, state="merged")
        self.assertEqual(slo.footprint_blockers(_item(), [done], paths), [])

    def test_l_episode_nomme_tous_les_bloqueurs_et_l_union_fait_foi(self):
        first = _note(T0, ids=(1, 5), ident=1)
        second = _note(T0 + 60, ids=(7,), ident=2)
        self.assertEqual(slo.footprint_wait_note([{"id": 5}, {"id": 1}]),
                         "en attente d'empreinte : #1, #5")
        # B1 de cx2 : l'union des bloqueurs de l'épisode, pas la dernière
        # note seule — un ensemble qui revient serait dédoublonné au journal.
        episode = slo.footprint_episode([second, first])
        self.assertEqual((episode["open"], episode["ids"]), (True, [1, 5, 7]))
        self.assertEqual(slo.footprint_episode([{"note": "autre"}])["ids"], [])

    def test_un_ensemble_de_bloqueurs_qui_revient_garde_la_vraie_levee(self):
        # A/B/A : {A, B} -> {B} -> {A, B} ; l'union garde A, la levée est sa
        # fusion, la plus tardive (B1 de cx2 sur 1f182041f).
        both = _note(T0, ids=(1, 5), ident=1)
        only_b = _note(T0 + 60, ids=(5,), ident=2)
        wait = slo.footprint_wait([only_b, both], [],
                                  satisfied={1: T0 + 900, 5: T0 + 300})
        self.assertEqual(wait["released_ts"], T0 + 900)
        self.assertFalse(wait["undated"])

    def test_une_levee_sans_date_reste_suspendue_deux_passes(self):
        # B2 de cx2 : sans date de levée, l'horloge ne repart pas de chaque
        # passe (elle ne vieillirait jamais) ; l'attente reste suspendue.
        events = [_note(T0, ids=(1,))]
        first = slo.footprint_wait(events, [], satisfied={})
        self.assertTrue(first["undated"])
        self.assertIsNone(first["released_ts"])
        second = slo.footprint_wait(events, [], satisfied={})
        self.assertEqual(first, second)
        for now in (T0 + 2 * 3600, T0 + 4 * 3600):
            self.assertEqual(slo.lot_steps(_item(), {"requested": T0}, events,
                                           now=now, footprint=first), [])

    def test_la_fermeture_datee_ne_bouge_plus_avec_l_activite_du_bloqueur(self):
        # P1 de cx1 sur b8d7a9901 : la levée est datée une fois (note de
        # clôture) ; une activité ultérieure du bloqueur ne la déplace pas.
        events = [_note(T0, ids=(1,)), _release(T0 + 1000, ids=(1,))]
        wait = slo.footprint_wait(events, [], satisfied={})
        self.assertEqual(wait["released_ts"], T0 + 1000)
        first = slo.lot_steps(_item(), {"requested": T0}, events,
                              now=T0 + 10000, footprint=wait)
        self.assertEqual({row["elapsed_s"] for row in first}, {9000})
        # P1 de cx2 sur 1b7e58d00 : la fusion POSTÉRIEURE du bloqueur (sorti
        # de l'empreinte avant) n'écrase pas la clôture de l'épisode.
        self.assertEqual(slo.footprint_wait(events, [], satisfied={1: T0 + 19900}),
                         wait)
        later = slo.lot_steps(_item(), {"requested": T0}, events,
                              now=T0 + 20000, footprint=wait)
        self.assertEqual({row["elapsed_s"] for row in later}, {19000})

    def test_une_fusion_avant_la_cloture_reste_la_date_exacte(self):
        # Le cas normal : le bloqueur a fusionné avant la passe de clôture ;
        # la fusion (la plus tôt) date la levée, pas l'instant de la note.
        events = [_note(T0, ids=(1,)), _release(T0 + 1300, ids=(1,))]
        wait = slo.footprint_wait(events, [], satisfied={1: T0 + 1000})
        self.assertEqual(wait["released_ts"], T0 + 1000)

    def test_sans_bloqueur_ni_note_rien_ne_change(self):
        rows = slo.lot_steps(_item(), {"requested": T0}, [], now=T0 + 3 * 3600,
                             footprint=None)
        self.assertEqual({row["step"] for row in rows},
                         {"signal_assigned", "intake_started"})


class _Base(PgTestCase):
    def setUp(self):
        super().setUp()
        for name in ("orch", "dev1", "bureau", "auditeur"):
            registry.upsert(self.db, name, harness="claude", host=self.cfg.host)
        self.db.execute("UPDATE agent_registry SET lease_owner = 'runner-' || name,"
                        " lease_expires_at = now() + interval '1 hour'")
        self.now = time.time()

    def slo(self, record_footprint=True, **kw):
        # #4579 : par défaut, la passe de livraison du notificateur (qui date
        # les épisodes) ; `record_footprint=False` est une simple lecture.
        kw.setdefault("orchestrators_declared", ["orch"])
        return [a for a in exploitation.alerts(self.cfg, self.db, now=self.now,
                                               record_footprint=record_footprint,
                                               **_seuils(**kw))
                if a["type"] == "slo_breach"]

    def footprint(self, item, *paths):
        storage.of(self.db).work_footprints.replace(
            int(item["id"]), "manual", list(paths), actor="test")

    def journal(self, item):
        return [row["note"] for row in self.db.query(
            "SELECT note FROM work_item_events WHERE work_item_id = %s ORDER BY id",
            (item["id"],))]


class TemoinAqaBintakeTest(_Base):
    def test_aucune_escalade_puis_l_horloge_repart_de_la_fusion(self):
        a = work.add(self.db, title="A occupe f.py", app="ameesh", assignee="dev1")
        b = work.add(self.db, title="B attend f.py", app="ameesh")
        self.footprint(a, "f.py")
        self.footprint(b, "f.py")
        self.db.execute("UPDATE work_items SET state = 'qa' WHERE id = %s",
                        (a["id"],))
        # B est en attente depuis 13 h : sans la garde, niveau 3.
        self.db.execute("UPDATE work_items SET created_at = now() - interval '13 hours'"
                        " WHERE id = %s", (b["id"],))

        self.assertEqual(self.slo(), [])
        self.assertTrue(any(("en attente d'empreinte : #%d" % a["id"]) in note
                            for note in self.journal(b)))

        self.db.execute("UPDATE work_items SET state = 'merged' WHERE id = %s",
                        (a["id"],))
        merged = time.time()
        self.now = merged + 60
        self.assertEqual(self.slo(), [])
        # la clôture est datée une fois, avec les bloqueurs de l'épisode
        self.assertTrue(any("empreinte libérée : #%d" % a["id"] in note
                            for note in self.journal(b)))
        self.now = merged + 1.5 * H
        alerts = self.slo()
        self.assertEqual([(alert["step"], alert["level"], alert["targets"])
                          for alert in alerts],
                         [("signal_assigned", 1, ["assignee"]),
                          ("intake_started", 1, ["assignee"])])
        for alert in alerts:
            self.assertAlmostEqual(alert["since"], merged, delta=5)
            # 1 h 30 depuis la fusion : 30 min au-delà de la cible d'une heure
            self.assertAlmostEqual(alert["value"], 0.5 * H, delta=5)

    def test_une_lecture_n_ecrit_aucune_note_d_episode(self):
        # #4579 : `ameesh alerts` (lecture, tout agent) n'écrit pas au journal
        # du lot ; la passe de livraison seule date l'ouverture et la levée.
        a = work.add(self.db, title="A occupe f.py", app="ameesh", assignee="dev1")
        b = work.add(self.db, title="B attend f.py", app="ameesh")
        self.footprint(a, "f.py")
        self.footprint(b, "f.py")
        self.db.execute("UPDATE work_items SET state = 'qa' WHERE id = %s",
                        (a["id"],))
        self.db.execute("UPDATE work_items SET created_at = now() - interval '13 hours'"
                        " WHERE id = %s", (b["id"],))
        before = self.journal(b)

        self.assertEqual(self.slo(record_footprint=False), [])
        self.assertEqual(self.journal(b), before)

        self.assertEqual(self.slo(), [])
        opened = self.journal(b)
        self.assertTrue(any(("en attente d'empreinte : #%d" % a["id"]) in note
                            for note in opened))
        self.db.execute("UPDATE work_items SET state = 'merged' WHERE id = %s",
                        (a["id"],))
        merged = time.time()
        self.now = merged + 2 * H
        # levée non encore notée : la lecture la date de la fusion du
        # bloqueur, sans rien écrire
        alerts = self.slo(record_footprint=False)
        self.assertTrue(alerts)
        for alert in alerts:
            self.assertAlmostEqual(alert["since"], merged, delta=5)
        self.assertEqual(self.journal(b), opened)

    def test_le_lot_retenu_n_entre_pas_dans_les_demarrables(self):
        from types import SimpleNamespace
        a = work.add(self.db, title="A", app="ameesh", assignee="dev1")
        b = work.add(self.db, title="B", app="ameesh")
        self.footprint(a, "f.py")
        self.footprint(b, "f.py")
        self.db.execute("UPDATE work_items SET state = 'qa' WHERE id = %s",
                        (a["id"],))
        lots = storage.of(self.db).operations.open_lots_activity(None)
        store = storage.of(self.db)
        rule = SimpleNamespace(
            queue_fill=SimpleNamespace(repos_part=PART, repos_s=REPOS),
            families=("claude", "codex", "deepseek"),
            auto_take_excluded_families=lambda now: ())
        db = mock.Mock()
        db.query.return_value = []
        with mock.patch("ameesh.storage.of", return_value=store), \
                mock.patch("ameesh.mechanics.configured", return_value=rule), \
                mock.patch("ameesh.account_turn.turn_hold", return_value=""), \
                mock.patch("ameesh.sous_utilisation.latest_account_gauges", return_value={}):
            cap = debit.read_capacity(db, cfg=object(), now=time.time(), hours=6,
                                      turns=[], canons=[], unreadable=[])
        project = [p for p in cap["projects"] if p["project"] == "ameesh"][0]
        self.assertNotIn(b["id"], project["startable_lots"])
        self.assertNotIn(a["id"], project["startable_lots"])
        waited = [row for row in project["footprint_lots"] if row["lot"] == b["id"]]
        self.assertEqual(len(waited), 1, project["footprint_lots"])
        self.assertEqual([blocker["id"] for blocker in waited[0]["blockers"]],
                         [a["id"]])
        self.assertEqual(waited[0]["blockers"][0]["state"], "qa")


class CapacityTest(unittest.TestCase):
    """Le débit : à part, jamais démarrable, et le geste vise le bloqueur."""

    def _capacity(self, **kw):
        lots = [{"id": 12, "state": "intake", "assignee": None, "app": "ameesh"},
                {"id": 7, "state": "qa", "assignee": "dev1", "app": "ameesh",
                 "last_activity_ts": T - 3 * H}]
        kw.setdefault("startable", [{"id": 12, "app": "ameesh"}])
        kw.setdefault("footprint", {12: [{"id": 7, "state": "qa"}]})
        kw.setdefault("open_lots", lots)
        return debit.capacity(agents=team(2, 8), now=T, window_s=6 * H,
                              repos_part=PART, repos_s=REPOS, **kw)

    def test_le_lot_retenu_est_a_part_et_le_geste_vise_le_bloqueur(self):
        cap = self._capacity()
        project = cap["projects"][0]
        self.assertEqual(project["startable_lots"], [])
        self.assertEqual([row["lot"] for row in project["footprint_lots"]], [12])
        self.assertEqual(project["footprint_lots"][0]["blockers"][0]["state"], "qa")
        self.assertEqual(cap["mesh"]["startable_lots"], 0)
        self.assertEqual(cap["mesh"]["footprint_lots"], 1)
        self.assertEqual(len(cap["alerts"]), 1)
        gesture = cap["alerts"][0]["gesture"]
        self.assertIn("en attente d'empreinte", gesture)
        self.assertIn("#7", gesture)
        self.assertIn("verdict, paquet", gesture)
        # 3 h sans mouvement : le bloqueur est nommé comme tel
        self.assertIn("sans mouvement depuis 3 h", gesture)

    def test_une_feuille_de_route_epuisee_reste_ouverte_par_l_attente(self):
        split = {"id": 90, "source": "file-decoupage:ameesh", "state": "intake",
                 "app": "ameesh", "created_ts": T - 2 * H,
                 "title": "Découper la feuille de route de ameesh"}
        notes = [{"work_item_id": 90, "source": "file-decoupage:ameesh",
                  "note": "roadmap: épuisée ameesh", "at_ts": T - H}]
        cap = self._capacity(open_lots=[split,
                                        {"id": 12, "state": "intake",
                                         "assignee": None, "app": "ameesh"},
                                        {"id": 7, "state": "qa", "assignee": "dev1",
                                         "app": "ameesh", "last_activity_ts": T - H}],
                             roadmap_notes=notes)
        self.assertIsNone(cap["projects"][0]["roadmap_exhausted"])
        self.assertIn("en attente d'empreinte", cap["alerts"][0]["gesture"])


if __name__ == "__main__":
    unittest.main()
