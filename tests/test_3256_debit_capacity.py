# SPDX-License-Identifier: AGPL-3.0-only
"""#3256 B : la section `capacity` du débit et `--check` à 20 % au repos.

Témoin du 28/10 à 14:52Z : 32 agents sur une quarantaine au repos, aucun lot
démarrable, et `debit --check` ne sortait pas 2 (`idle_agents_with_work`
vide). Acceptation (spec `docs/design/3256-debit-file.md` §3.3) : avec 20 %
d'agents au repos depuis 30 min sans lot de découpage récent, `--check` sort
2 et nomme le projet ; la section donne les heures-agent au repos et la perte
au reset par compte.
"""
from __future__ import annotations

import contextlib
import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

from ameesh import cost, debit

T = 1793195520.0            # 2026-10-28 14:52:00Z
H = 3600.0
PART, REPOS = 0.20, 1800
#: l'identité du lot de découpage d'ameesh (#4273, `file_decoupage.source_of`)
SPLIT = "file-decoupage:ameesh"


def agent(name, project="ameesh", **extra):
    row = {"name": name, "mode": "execute", "status": "idle", "lease_live": True,
           "lease_owner": "runner:%s" % name, "unread": 0, "assigned_lot_id": None,
           "active_lots": 0, "actionable_lots": 0, "projects": [project],
           # #4779 : profil de relecteur prouvé (capacité `propose`, un rôle).
           "capabilities": ["read", "report-drift", "propose"],
           "roles": ["implementation"]}
    row.update(extra)
    return row


def busy(name, project="ameesh"):
    """Un agent qui a un lot actionnable : pas au repos."""
    return agent(name, project, actionable_lots=1, assigned_lot_id=1)


def turn(name, start, end, lot=7):
    return {"agent": name, "at_ts": start, "end_ts": end, "work_item_id": lot}


def measure(agents, **kwargs):
    kwargs.setdefault("repos_part", PART)
    kwargs.setdefault("repos_s", REPOS)
    return debit.capacity(agents=agents, now=T, window_s=6 * H, **kwargs)


def team(idle, working, project="ameesh"):
    return ([agent("%s-idle-%d" % (project, i), project) for i in range(idle)]
            + [busy("%s-busy-%d" % (project, i), project) for i in range(working)])


def mechanics_doc(*, families=("claude", "codex", "deepseek"), excluded=()):
    """Le document `mechanics.configured` réduit à ce que `read_capacity` lit."""
    from types import SimpleNamespace
    return SimpleNamespace(
        queue_fill=SimpleNamespace(repos_part=PART, repos_s=REPOS),
        families=tuple(families),
        auto_take_excluded_families=lambda now: tuple(excluded))


class TriggerTest(unittest.TestCase):
    def test_twenty_percent_idle_without_decoupage_names_the_project(self):
        cap = measure(team(2, 8))
        self.assertEqual([a["project"] for a in cap["alerts"]], ["ameesh"])
        self.assertEqual(cap["alerts"][0]["idle"], 2)
        self.assertEqual(cap["projects"][0]["idle_share"], 0.2)
        self.assertIn("découpage", cap["alerts"][0]["gesture"])

    def test_under_twenty_percent_is_quiet(self):
        self.assertEqual(measure(team(1, 9))["alerts"], [])

    def test_a_lot_turn_within_thirty_minutes_is_not_rest(self):
        agents = team(2, 8)
        turns = [turn("ameesh-idle-0", T - 20 * 60, T - 10 * 60)]
        self.assertEqual(measure(agents, turns=turns)["alerts"], [])

    def test_a_mail_turn_does_not_end_the_rest(self):
        agents = team(2, 8)
        turns = [turn("ameesh-idle-0", T - 5 * 60, T - 4 * 60, lot=None)]
        self.assertEqual(len(measure(agents, turns=turns)["alerts"]), 1)

    def test_a_fresh_decoupage_lot_holds_the_check(self):
        lots = [{"id": 90, "source": SPLIT, "title": "Découper la feuille de route de ameesh : des lots "
                 "démarrables pour 2 agents", "app": "ameesh", "state": "intake",
                 "created_ts": T - 20 * 60}]
        cap = measure(team(2, 8), open_lots=lots)
        self.assertEqual(cap["alerts"], [])
        self.assertEqual(cap["projects"][0]["decoupage"]["lot"], 90)

    def test_a_decoupage_lot_older_than_an_hour_calls_the_auditor(self):
        lots = [{"id": 90, "source": SPLIT, "title": "Découper la feuille de route de ameesh",
                 "app": "ameesh", "state": "build", "created_ts": T - 2 * H}]
        cap = measure(team(2, 8), open_lots=lots)
        self.assertEqual(len(cap["alerts"]), 1)
        self.assertIn("#90", cap["alerts"][0]["gesture"])
        self.assertIn("l'auditeur crée les lots", cap["alerts"][0]["gesture"])

    def test_startable_lots_name_the_dispatch(self):
        cap = measure(team(2, 8), startable=[{"id": 12, "app": "ameesh"}])
        self.assertEqual(cap["projects"][0]["startable_lots"], [12])
        self.assertIn("ameesh work assign", cap["alerts"][0]["gesture"])

    def test_an_exhausted_roadmap_is_quiet_and_counts_no_hours(self):
        notes = [{"work_item_id": 90, "source": SPLIT, "note": "roadmap: épuisée ameesh (feuille "
                  "docs/roadmap.md, décision owner)", "at_ts": T - H}]
        cap = measure(team(2, 8), roadmap_notes=notes)
        self.assertEqual(cap["alerts"], [])
        self.assertEqual(cap["projects"][0]["idle_agent_hours"], 0.0)
        self.assertEqual(cap["projects"][0]["roadmap_exhausted"]["lot"], 90)

    def test_a_startable_lot_reopens_an_exhausted_roadmap(self):
        notes = [{"work_item_id": 90, "source": SPLIT, "note": "roadmap: épuisée ameesh", "at_ts": T - H}]
        cap = measure(team(2, 8), roadmap_notes=notes,
                      startable=[{"id": 12, "app": "ameesh"}])
        self.assertIsNone(cap["projects"][0]["roadmap_exhausted"])
        self.assertEqual(len(cap["alerts"]), 1)

    def test_a_startable_lot_arrived_after_the_note_reopens_for_good(self):
        # B de cx3 sur b6f1c85 : le lot 91 arrive démarrable après la note,
        # puis il est pris (`reopened`, lu à l'historique des prises) ; plus
        # aucun lot démarrable, mais la route reste rouverte
        notes = [{"work_item_id": 90, "source": SPLIT, "note": "roadmap: épuisée ameesh", "at_ts": T - 2 * H}]
        cap = measure(team(2, 8), roadmap_notes=notes,
                      reopened={"ameesh": T - H})
        self.assertIsNone(cap["projects"][0]["roadmap_exhausted"])
        self.assertEqual(len(cap["alerts"]), 1)
        self.assertGreater(cap["projects"][0]["idle_agent_hours"], 0)

    def test_a_lot_created_before_the_note_keeps_it(self):
        notes = [{"work_item_id": 90, "source": SPLIT, "note": "roadmap: épuisée ameesh", "at_ts": T - H}]
        cap = measure(team(2, 8), roadmap_notes=notes,
                      reopened={"ameesh": T - 2 * H})
        self.assertIsNotNone(cap["projects"][0]["roadmap_exhausted"])
        self.assertEqual(cap["alerts"], [])

    def test_a_lot_created_already_assigned_does_not_reopen(self):
        # B5 (impl-03) : un lot doc créé assigné après la note n'arrive pas
        # démarrable ; `reopened` ne le porte pas, la route reste épuisée
        notes = [{"work_item_id": 90, "source": SPLIT, "note": "roadmap: épuisée ameesh",
                  "at_ts": T - 2 * H}]
        cap = measure(team(2, 8), roadmap_notes=notes, reopened={})
        self.assertIsNotNone(cap["projects"][0]["roadmap_exhausted"])
        self.assertEqual(cap["alerts"], [])

    def test_a_note_outside_the_split_lot_does_not_count(self):
        # B4 (impl-03) : la note se pose sur le lot de découpage du projet
        for source in ("", "github:issue", "file-decoupage:nexlink"):
            with self.subTest(source=source):
                notes = [{"work_item_id": 999, "source": source,
                          "note": "roadmap: épuisée ameesh", "at_ts": T - H}]
                cap = measure(team(2, 8), roadmap_notes=notes)
                self.assertIsNone(cap["projects"][0]["roadmap_exhausted"])
                self.assertEqual(len(cap["alerts"]), 1)

    def test_old_split_and_startable_lots_ask_to_assign_first(self):
        # B2 (impl-03) : confier d'abord, le repli ensuite (docs/AUDITEUR.md)
        lots = [{"id": 90, "source": SPLIT, "app": "ameesh", "state": "build",
                 "created_ts": T - 2 * H}]
        cap = measure(team(2, 8), open_lots=lots, startable=[{"id": 12, "app": "ameesh"}])
        self.assertIn("ameesh work assign", cap["alerts"][0]["gesture"])
        self.assertNotIn("l'auditeur crée", cap["alerts"][0]["gesture"])

    def test_an_incomplete_read_raises_no_alert(self):
        # B3 (impl-03) : la mesure reste, l'alerte ne part pas
        cap = measure(team(2, 8), gating_ok=False)
        self.assertEqual(cap["alerts"], [])
        self.assertEqual(cap["mesh"]["idle"], 2)
        self.assertFalse(cap["gating_ok"])

    def test_the_mesh_does_not_count_an_exhausted_project_hours(self):
        notes = [{"work_item_id": 90, "source": SPLIT, "note": "roadmap: épuisée ameesh", "at_ts": T - H}]
        cap = measure(team(2, 0, "ameesh") + team(1, 0, "nexlink"), roadmap_notes=notes)
        nexlink = [row for row in cap["projects"] if row["project"] == "nexlink"][0]
        self.assertEqual(nexlink["idle_agent_hours"], 6.0)
        self.assertEqual(cap["mesh"]["idle_agent_hours"], 6.0)

    def test_without_the_canon_block_no_alert_is_raised(self):
        cap = measure(team(5, 5), repos_part=None, repos_s=None)
        self.assertIsNone(cap["rule"])
        self.assertEqual(cap["alerts"], [])
        self.assertEqual(cap["mesh"]["idle"], 5)

    def test_projects_are_judged_apart(self):
        agents = team(2, 8, "ameesh") + team(0, 5, "nexlink")
        cap = measure(agents)
        self.assertEqual([a["project"] for a in cap["alerts"]], ["ameesh"])
        self.assertEqual(cap["mesh"]["wakeable"], 15)


class WakeableTest(unittest.TestCase):
    def test_critical_held_paused_stopped_and_ephemeral_are_not_counted(self):
        agents = team(1, 4) + [
            agent("auditeur"), agent("held"), agent("eph", ephemeral=True),
            agent("paused", operator_paused=True), agent("stopped", status="stopped"),
            agent("chat", mode="external")]
        cap = measure(agents, held={"held": "budget"})
        self.assertEqual(cap["projects"][0]["wakeable"], 5)
        self.assertEqual(cap["projects"][0]["idle_agents"], ["ameesh-idle-0"])
        self.assertEqual(cap["held"], [{"agent": "held", "reason": "budget"}])

    def test_retired_and_runnerless_agents_are_not_counted(self):
        # B1 (impl-03) : 2 au repos + 8 occupés = 20 % ; un retraité resté
        # `idle` ou un agent sans exécuteur vivant ne dilue pas la part
        agents = team(2, 8) + [agent("retired", retired_ts=T - H),
                               agent("norunner", lease_live=False)]
        cap = measure(agents)
        self.assertEqual(cap["projects"][0]["wakeable"], 10)
        self.assertEqual(len(cap["alerts"]), 1)


class IdleHoursTest(unittest.TestCase):
    def test_gaps_of_thirty_minutes_or_more_count(self):
        # fenêtre de 6 h ; un tour de 1 h au milieu, un trou de 10 min ignoré
        turns = [turn("a", T - 4 * H, T - 3 * H), turn("a", T - 3 * H + 600, T - 2 * H)]
        cap = measure([agent("a")], turns=turns)
        # trous : 2 h avant, 10 min (ignoré), 2 h après
        self.assertEqual(cap["mesh"]["idle_agent_hours"], 4.0)

    def test_a_turn_across_the_window_start_is_clipped(self):
        turns = [turn("a", T - 7 * H, T - 5 * H)]
        cap = measure([agent("a")], turns=turns)
        self.assertEqual(cap["mesh"]["idle_agent_hours"], 5.0)

    def test_a_busy_agents_gaps_are_not_idle_hours(self):
        # #5043 : l'attente d'un agent sur son propre lot (CI, avis) n'est pas
        # de la capacité disponible — aucun agent au repos, aucune heure
        turns = [turn("busy", T - 5 * H, T - 4 * H)]
        cap = measure([busy("busy")], turns=turns)
        self.assertEqual(cap["mesh"]["idle_agent_hours"], 0.0)
        self.assertEqual(cap["mesh"]["idle"], 0)

    def test_only_the_resting_agents_count_hours(self):
        # #5043 : le total est la somme des seuls agents au repos, pas celle
        # de tous les réveillables du projet
        turns = [turn("busy", T - 5 * H, T - 4 * H)]
        cap = measure([agent("resting"), busy("busy")], turns=turns)
        self.assertEqual(cap["mesh"]["idle"], 1)
        self.assertEqual(cap["mesh"]["idle_agent_hours"], 6.0)

    def test_a_budget_hold_counts_no_idle_hours(self):
        # #5043 : la pause budget n'est pas une perte de capacité
        cap = measure([agent("a")], held={"a": "budget"})
        self.assertEqual(cap["mesh"]["idle_agent_hours"], 0.0)

    def test_a_critical_role_counts_no_idle_hours(self):
        # #5043 : un critique ou un relecteur n'est pas réveillable ; ses
        # trous ne gonflent pas la perte affichée
        cap = measure([agent("a"), agent("auditeur")])
        self.assertEqual(cap["projects"][0]["wakeable"], 1)
        self.assertEqual(cap["mesh"]["idle_agent_hours"], 6.0)

    def test_an_excluded_family_counts_no_idle_hours(self):
        # #5043 : une famille exclue des prises (#5005) est une capacité
        # volontairement écartée, pas une perte : elle ne compte ni dans les
        # réveillables ni dans les heures-agent
        families = ("claude", "codex", "deepseek")
        cap = measure([agent("a", harness="claude-opus"),
                       agent("ds", harness="deepseek-flash")],
                      families=families, excluded_families=("deepseek",))
        self.assertEqual(cap["projects"][0]["wakeable"], 1)
        self.assertEqual(cap["mesh"]["idle"], 1)
        self.assertEqual(cap["mesh"]["idle_agent_hours"], 6.0)


class PlanLossTest(unittest.TestCase):
    def test_loss_per_account_as_accounts_computes_it(self):
        five = cost.Gauge("claude", "five_hour", 0.22, T + H, 5 * H)
        week = cost.Gauge("claude", "seven_day", 0.40, T + 3 * 86400, 7 * 86400)
        losses = debit.plan_losses({("claude", "primaire"): [(five, T), (week, T)]}, T)
        self.assertEqual(losses, [{"harness": "claude", "account": "primaire",
                                   "stale": False, "lost": 0.78, "window": "five_hour",
                                   "resets_in_s": H}])
        cap = measure(team(1, 1), losses=losses)
        self.assertEqual(cap["plan_loss_at_reset"][0]["lost"], 0.78)
        text = "\n".join(debit._capacity_text(cap))
        self.assertIn("claude/primaire 78 % de five_hour", text)


class CheckTest(unittest.TestCase):
    def _main(self, cap):
        report = {"totals": {"slow_waits": 0, "capacity_alerts": len(cap["alerts"])},
                  "capacity": cap}
        db = mock.Mock()
        with mock.patch.object(debit, "read", return_value=report), \
                mock.patch("ameesh.config.load"), \
                mock.patch("ameesh.db.connect", return_value=db), \
                mock.patch("ameesh.db.require_schema"), \
                mock.patch.object(debit, "format_text", return_value=""), \
                redirect_stdout(io.StringIO()):
            return debit.main(["--check"])

    def test_check_exits_two_on_a_capacity_alert(self):
        self.assertEqual(self._main(measure(team(2, 8))), 2)

    def test_check_exits_zero_without_one(self):
        self.assertEqual(self._main(measure(team(1, 9))), 0)


class ReadCapacityTest(unittest.TestCase):
    def test_open_lots_unreadable_raises_no_alert(self):
        # B3 (impl-03) : open_lots_activity lève ; 2/10 au repos mais aucune
        # alerte, la lecture manquante est nommée
        from types import SimpleNamespace
        store = SimpleNamespace(operations=SimpleNamespace(
            listing=lambda: team(2, 8),
            open_lots_activity=mock.Mock(side_effect=RuntimeError("boom"))))
        rule = mechanics_doc()
        db = mock.Mock()
        db.query.return_value = []
        unreadable: list = []
        with mock.patch("ameesh.storage.of", return_value=store), \
                mock.patch("ameesh.mechanics.configured", return_value=rule), \
                mock.patch("ameesh.account_turn.turn_hold", return_value=""), \
                mock.patch("ameesh.sous_utilisation.critical_verdict",
                           return_value=(False, False)), \
                mock.patch("ameesh.sous_utilisation.latest_account_gauges", return_value={}):
            cap = debit.read_capacity(db, cfg=object(), now=T, hours=6, turns=[],
                                      canons=[], unreadable=unreadable)
        self.assertEqual(cap["mesh"]["idle"], 2)
        self.assertEqual(cap["alerts"], [])
        self.assertEqual(cap["gating_unreadable"], ["open lots"])
        self.assertTrue(any("open lots" in line for line in unreadable))


    def test_the_split_lot_is_read_from_the_real_row_shape(self):
        # B6 (impl-04 sur ac79c550) : `open_lots_activity` ne rend pas
        # `source` ; un lot de découpage de 20 min doit pourtant retenir
        # `--check`, et le débit le nommer.
        from types import SimpleNamespace
        row = {"id": 90, "title": "Découper la feuille de route de ameesh : des lots "
               "démarrables pour 2 agent(s)", "state": "intake", "assignee": "pilote",
               "app": "ameesh", "type": "evolution", "created_ts": T - 20 * 60}
        store = SimpleNamespace(operations=SimpleNamespace(
            listing=lambda: team(2, 8), open_lots_activity=lambda limit: [row]))
        rule = mechanics_doc()

        def query(sql, params=()):
            if "file-decoupage:%%'" in sql and "SELECT id, source" in sql:
                return [{"id": 90, "source": SPLIT}]
            return []
        db = mock.Mock()
        db.query.side_effect = query
        with mock.patch("ameesh.storage.of", return_value=store), \
                mock.patch("ameesh.mechanics.configured", return_value=rule), \
                mock.patch("ameesh.account_turn.turn_hold", return_value=""), \
                mock.patch("ameesh.sous_utilisation.waiting_project_lots", return_value=[]), \
                mock.patch("ameesh.sous_utilisation.critical_verdict",
                           return_value=(False, False)), \
                mock.patch("ameesh.sous_utilisation.latest_account_gauges", return_value={}):
            cap = debit.read_capacity(db, cfg=object(), now=T, hours=6, turns=[],
                                      canons=[], unreadable=[])
        project = [p for p in cap["projects"] if p["project"] == "ameesh"][0]
        self.assertEqual(project["decoupage"]["lot"], 90)
        self.assertTrue(cap["gating_ok"])
        self.assertEqual(cap["alerts"], [])


    def _read_with(self, patches, *, listing=None, document=None):
        """`read_capacity` sur 2/10 au repos, un lot ouvert démarrable, et des
        lecteurs internes remplacés par `patches`."""
        from types import SimpleNamespace
        lot = {"id": 12, "title": "lot", "state": "intake", "assignee": None,
               "app": "ameesh", "type": "evolution", "created_ts": T - H}
        store = SimpleNamespace(
            operations=SimpleNamespace(
                listing=lambda: listing if listing is not None else team(2, 8),
                open_lots_activity=lambda limit: [lot]),
            # #4561 : la garde d'empreinte est lue pour la part démarrable.
            work_footprints=SimpleNamespace(for_items=lambda ids: []))
        rule = document if document is not None else mechanics_doc()
        db = mock.Mock()
        db.query.return_value = []
        unreadable: list = []
        base = {"ameesh.storage.of": mock.Mock(return_value=store),
                "ameesh.mechanics.configured": mock.Mock(return_value=rule),
                "ameesh.sous_utilisation.critical_verdict":
                    mock.Mock(return_value=(False, False)),
                "ameesh.sous_utilisation.latest_account_gauges": mock.Mock(return_value={}),
                "ameesh.planned_waits.read": mock.Mock(return_value={}),
                "ameesh.account_turn.turn_hold": mock.Mock(return_value="")}
        base.update(patches)
        with contextlib.ExitStack() as stack:
            for target, value in base.items():
                stack.enter_context(mock.patch(target, value))
            cap = debit.read_capacity(db, cfg=object(), now=T, hours=6, turns=[],
                                      canons=[], unreadable=unreadable)
        return cap, unreadable

    def test_internal_readers_succeeding_raise_the_alert(self):
        cap, unreadable = self._read_with({})
        self.assertEqual(len(cap["alerts"]), 1)
        self.assertEqual(unreadable, [])

    def test_planned_waits_failing_raises_no_alert(self):
        # B de cx1 : `waiting_project_lots(db=…)` absorbait cet échec
        cap, unreadable = self._read_with(
            {"ameesh.planned_waits.read": mock.Mock(side_effect=RuntimeError("waits"))})
        self.assertEqual(cap["alerts"], [])
        self.assertEqual(cap["gating_unreadable"], ["startable lots"])
        self.assertTrue(any("startable lots" in line for line in unreadable))

    def test_turn_hold_failing_raises_no_alert(self):
        # B de cx1 : `_budget_held` absorbait cet échec
        cap, unreadable = self._read_with(
            {"ameesh.account_turn.turn_hold": mock.Mock(side_effect=RuntimeError("hold"))})
        self.assertEqual(cap["alerts"], [])
        self.assertEqual(cap["gating_unreadable"], ["budget holds"])

    def test_an_excluded_family_is_left_out_of_capacity(self):
        # #5043 : le lecteur passe au calcul les familles du canon et
        # l'exclusion temporaire des prises (#5005)
        cap, _ = self._read_with(
            {}, listing=team(2, 8) + [agent("ds", harness="deepseek-flash")],
            document=mechanics_doc(excluded=("deepseek",)))
        self.assertEqual(cap["mesh"]["wakeable"], 10)
        self.assertEqual(cap["mesh"]["idle"], 2)
        self.assertEqual(cap["mesh"]["idle_agent_hours"], 12.0)


if __name__ == "__main__":
    unittest.main()
