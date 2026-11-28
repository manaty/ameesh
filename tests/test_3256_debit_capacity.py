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
import types
import unittest
from contextlib import redirect_stdout
from unittest import mock

from ameesh import cost, debit, sous_utilisation

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


def split_lot(ident=90, project="ameesh", state="intake", **extra):
    """Le lot de découpage du projet : seule une note posée sur lui vaut
    (#6215, comme `file_decoupage._open_split`)."""
    row = {"id": ident, "source": "file-decoupage:%s" % project,
           "app": project, "state": state, "created_ts": T - 2 * H,
           "title": "Découper la feuille de route de %s" % project}
    row.update(extra)
    return row


class _Canon:
    """Canon minimal (#7661) : les fiches des agents, rôles déclarés."""

    readable = True
    federation: dict = {}

    def __init__(self, cards):
        self.agents = [
            types.SimpleNamespace(
                title=name, fiche=types.SimpleNamespace(data={"roles": roles}))
            for name, roles in cards.items()]


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

    def test_a_split_lot_moved_within_the_hour_holds_the_check(self):
        # #7848 : #7711 avait 48 h, mais son pilote y notait un point chaque
        # heure ; le repli court depuis le dernier mouvement, pas la création.
        lots = [split_lot(created_ts=T - 48 * H, moved_ts=T - 20 * 60)]
        cap = measure(team(2, 8), open_lots=lots)
        self.assertEqual(cap["alerts"], [])
        entry = cap["projects"][0]["decoupage"]
        self.assertEqual(entry["age_s"], 48 * H)
        self.assertEqual(entry["quiet_s"], 20 * 60)

    def test_a_split_lot_quiet_for_an_hour_calls_the_auditor(self):
        lots = [split_lot(created_ts=T - 48 * H, moved_ts=T - 2 * H)]
        cap = measure(team(2, 8), open_lots=lots)
        self.assertEqual(len(cap["alerts"]), 1)
        self.assertEqual(cap["alerts"][0]["decoupage"]["quiet_s"], 2 * H)
        self.assertIn("sans mouvement", cap["alerts"][0]["gesture"])
        # #8388 : l'alerte porte le déclencheur du courrier de repli.
        self.assertTrue(cap["alerts"][0]["fallback"])

    def test_a_startable_lot_is_not_a_fallback_even_on_a_quiet_split(self):
        # #8388 : confier d'abord ; le repli ne part pas quand un lot
        # démarrable attend un preneur.
        lots = [split_lot(created_ts=T - 2 * H)]
        cap = measure(team(2, 8), open_lots=lots,
                      startable=[{"id": 12, "app": "ameesh"}])
        self.assertEqual(len(cap["alerts"]), 1)
        self.assertFalse(cap["alerts"][0]["fallback"])

    def test_footprint_lots_do_not_hold_the_fallback(self):
        # #8388, témoin #8035 : des lots retenus par l'empreinte ne sont pas
        # du découpage produit ; l'alerte garde le repli dû et nomme les
        # deux gestes.
        lots = [split_lot(created_ts=T - 48 * H, moved_ts=T - 2 * H),
                {"id": 12, "app": "ameesh", "state": "intake", "title": "lot",
                 "created_ts": T - H}]
        cap = measure(team(2, 8), open_lots=lots,
                      footprint={12: [{"id": 9, "state": "build"}]})
        alert = cap["alerts"][0]
        self.assertEqual(alert["footprint_lots"][0]["lot"], 12)
        self.assertTrue(alert["fallback"])
        self.assertIn("repli", alert["gesture"])
        self.assertIn("empreinte", alert["gesture"])

    def test_an_observation_window_holds_the_fallback(self):
        # #8199 : une fenêtre d'observation non échue tient le repli, même
        # quand un lot retenu par empreinte fait lever l'alerte.
        lots = [split_lot(created_ts=T - 48 * H, moved_ts=T - 2 * H),
                {"id": 12, "app": "ameesh", "state": "intake", "title": "lot",
                 "created_ts": T - H}]
        cap = measure(team(2, 8), open_lots=lots,
                      footprint={12: [{"id": 9, "state": "build"}]},
                      window_waits=[{"lot": 90, "due_ts": T + H,
                                     "what": "window"}])
        alert = cap["alerts"][0]
        self.assertEqual(alert["footprint_lots"][0]["lot"], 12)
        self.assertFalse(alert["fallback"])
        self.assertNotIn("repli", alert["gesture"])

    def test_an_exhausted_roadmap_is_not_a_fallback(self):
        # #4623/#8388 : une feuille de route épuisée n'appelle pas le repli,
        # même quand un lot retenu par l'empreinte fait lever l'alerte.
        lots = [split_lot(created_ts=T - 48 * H, moved_ts=T - 2 * H),
                {"id": 12, "app": "ameesh", "state": "intake", "title": "lot",
                 "created_ts": T - H}]
        notes = [{"work_item_id": 90, "source": SPLIT,
                  "note": "roadmap: épuisée ameesh (feuille)", "at_ts": T - H}]
        cap = measure(team(2, 8), open_lots=lots, roadmap_notes=notes,
                      footprint={12: [{"id": 9, "state": "build"}]})
        alert = cap["alerts"][0]
        self.assertFalse(alert["fallback"])
        self.assertNotIn("repli", alert["gesture"])

    def test_a_move_before_creation_never_rejuvenates_the_lot(self):
        lots = [split_lot(created_ts=T - 2 * H, moved_ts=T - 3 * H)]
        entry = measure(team(2, 8), open_lots=lots)["projects"][0]["decoupage"]
        self.assertEqual(entry["quiet_s"], 2 * H)

    def test_the_text_names_the_last_move(self):
        lots = [split_lot(created_ts=T - 48 * H, moved_ts=T - 20 * 60)]
        lines = debit._capacity_text(measure(team(2, 8), open_lots=lots))
        self.assertTrue(any("découpage #90 (48 h 00, dernier mouvement il y a 20 min)"
                            in line for line in lines), lines)

    def test_startable_lots_name_the_dispatch(self):
        cap = measure(team(2, 8), startable=[{"id": 12, "app": "ameesh"}])
        self.assertEqual(cap["projects"][0]["startable_lots"], [12])
        self.assertIn("ameesh work assign", cap["alerts"][0]["gesture"])

    def test_an_exhausted_roadmap_is_quiet_and_marks_the_hours_excluded(self):
        notes = [{"work_item_id": 90, "source": SPLIT, "note": "roadmap: épuisée ameesh (feuille "
                  "docs/roadmap.md, décision owner)", "at_ts": T - H}]
        cap = measure(team(2, 8), roadmap_notes=notes, open_lots=[split_lot()])
        self.assertEqual(cap["alerts"], [])
        project = cap["projects"][0]
        self.assertEqual(project["idle_agent_hours"], 0.0)
        self.assertEqual(project["idle_agent_hours_excluded"],
                         "feuille de route épuisée")
        self.assertEqual(project["roadmap_exhausted"]["lot"], 90)
        # #7849 : le 0,0 d'exclusion n'est pas affiché comme une mesure, et le
        # mesh nomme les agents hors mesure.
        self.assertEqual(cap["mesh"]["idle_agents_excluded"], 2)
        self.assertEqual(cap["mesh"]["idle_hours_excluded_reasons"],
                         ["feuille de route épuisée"])
        text = "\n".join(debit._capacity_text(cap))
        self.assertIn("exclu (feuille de route épuisée)", text)
        self.assertIn("heures-agent exclues (2 agent(s) : feuille de route épuisée)", text)
        self.assertNotIn("0.0 heure(s)-agent", text)

    def test_a_dated_startable_lot_counts_the_hours_and_lifts_the_exclusion(self):
        # #7849/#7249 : un lot ponctuel daté borne la reprise ; les heures
        # depuis sa création sont mesurées, la ligne n'est plus « exclue ».
        notes = [{"work_item_id": 90, "source": SPLIT, "note": "roadmap: épuisée ameesh",
                  "at_ts": T - H}]
        cap = measure(team(1, 1), roadmap_notes=notes, open_lots=[split_lot()],
                      startable=[{"id": 12, "app": "ameesh", "created_ts": T - H / 2}])
        project = cap["projects"][0]
        self.assertEqual(project["idle_agent_hours"], 0.5)
        self.assertNotIn("idle_agent_hours_excluded", project)
        self.assertEqual(cap["mesh"]["idle_agents_excluded"], 0)
        text = "\n".join(debit._capacity_text(cap))
        self.assertIn("0.5 h-agent", text)
        self.assertIn("feuille de route épuisée", text)
        self.assertNotIn("exclu (", text)

    def test_a_startable_lot_keeps_the_exhaustion_note(self):
        notes = [{"work_item_id": 90, "source": SPLIT, "note": "roadmap: épuisée ameesh", "at_ts": T - H}]
        cap = measure(team(2, 8), roadmap_notes=notes, open_lots=[split_lot()],
                      startable=[{"id": 12, "app": "ameesh"}])
        project = cap["projects"][0]
        self.assertEqual(project["roadmap_exhausted"]["lot"], 90)
        self.assertEqual(project["idle_agent_hours"], 0.0)
        # #7849 : sans date de création, aucune heure n'est inventée, mais la
        # mesure est dite exclue au lieu de passer pour un 0,0 mesuré.
        self.assertEqual(project["idle_agent_hours_excluded"],
                         "date de création du lot démarrable inconnue")
        self.assertEqual(len(cap["alerts"]), 1)
        text = "\n".join(debit._capacity_text(cap))
        self.assertIn("exclu (date de création du lot démarrable inconnue)", text)
        self.assertIn("feuille de route épuisée", text)

    def test_the_mesh_counts_measured_hours_and_sets_the_excluded_apart(self):
        # #7849 : un projet épuisé exclu ne masque pas les heures mesurées
        # d'un autre projet ; le mesh les distingue.
        notes = [{"work_item_id": 90, "source": SPLIT, "note": "roadmap: épuisée ameesh",
                  "at_ts": T - H}]
        cap = measure(team(2, 8, "ameesh") + team(1, 1, "nexlink"),
                      roadmap_notes=notes, open_lots=[split_lot()])
        nexlink = [row for row in cap["projects"]
                   if row["project"] == "nexlink"][0]
        self.assertNotIn("idle_agent_hours_excluded", nexlink)
        self.assertEqual(cap["mesh"]["idle_agent_hours"], 6.0)
        self.assertEqual(cap["mesh"]["idle_agents_excluded"], 2)
        text = "\n".join(debit._capacity_text(cap))
        self.assertIn("6.0 heure(s)-agent mesurées "
                      "(2 agent(s) exclus : feuille de route épuisée)", text)

    def test_footprint_lots_on_exhausted_roadmaps_count_on_their_own_field(self):
        # #7890 : témoin du 19/11 — routes épuisées, 0 démarrable, des lots
        # retenus par la garde d'empreinte ; le débit rendait 0,0 h. Les heures
        # se lisent sur `idle_agent_hours_footprint`, sans rouvrir
        # `idle_agent_hours` (#7249).
        notes = [{"work_item_id": 90, "source": SPLIT, "note": "roadmap: épuisée ameesh",
                  "at_ts": T - 3 * H},
                 {"work_item_id": 91, "source": "file-decoupage:nexlink",
                  "note": "roadmap: épuisée nexlink", "at_ts": T - 3 * H}]
        blocker = {"id": 50, "state": "build", "assignee": "x"}
        held = [{"id": 61, "app": "ameesh", "state": "intake", "created_ts": T - 2 * H},
                {"id": 62, "app": "ameesh", "state": "intake", "created_ts": T - H},
                {"id": 63, "app": "nexlink", "state": "intake", "created_ts": T - H}]
        cap = measure(team(2, 0, "ameesh") + team(1, 0, "nexlink"), roadmap_notes=notes,
                      open_lots=[split_lot(), split_lot(91, "nexlink")] + held,
                      footprint={61: [blocker], 62: [blocker], 63: [blocker]})
        rows = {row["project"]: row for row in cap["projects"]}
        self.assertEqual(rows["ameesh"]["idle_agent_hours"], 0.0)
        self.assertEqual(rows["ameesh"]["idle_agent_hours_excluded"],
                         "feuille de route épuisée")
        self.assertEqual(rows["ameesh"]["idle_agent_hours_footprint"], 4.0)
        self.assertEqual(rows["nexlink"]["idle_agent_hours_footprint"], 1.0)
        self.assertEqual(cap["mesh"]["startable_lots"], 0)
        self.assertEqual(cap["mesh"]["footprint_lots"], 3)
        self.assertEqual(cap["mesh"]["idle_agent_hours"], 0.0)
        self.assertEqual(cap["mesh"]["idle_agent_hours_footprint"], 5.0)
        text = "\n".join(debit._capacity_text(cap))
        self.assertIn("5.0 heure(s)-agent au repos retenues par empreinte", text)
        self.assertIn("4.0 h-agent retenues par empreinte", text)

    def test_an_undated_footprint_lot_invents_no_hours(self):
        notes = [{"work_item_id": 90, "source": SPLIT, "note": "roadmap: épuisée ameesh",
                  "at_ts": T - H}]
        cap = measure(team(2, 0), roadmap_notes=notes,
                      open_lots=[split_lot(), {"id": 61, "app": "ameesh", "state": "intake"}],
                      footprint={61: [{"id": 50, "state": "build"}]})
        self.assertEqual(cap["projects"][0]["idle_agent_hours_footprint"], 0.0)
        self.assertNotIn("retenues par empreinte", "\n".join(debit._capacity_text(cap)))

    def test_a_reopening_note_after_the_exhaustion_reopens(self):
        # #5149 : la dernière note fait foi, comme pour le sommeil et
        # `ameesh alerts` (#4623)
        notes = [{"work_item_id": 90, "source": SPLIT, "note": "roadmap: épuisée ameesh", "at_ts": T - 2 * H},
                 {"work_item_id": 90, "source": SPLIT, "note": "roadmap: rouverte ameesh", "at_ts": T - H}]
        cap = measure(team(2, 8), roadmap_notes=notes, open_lots=[split_lot()])
        self.assertIsNone(cap["projects"][0]["roadmap_exhausted"])
        self.assertEqual(len(cap["alerts"]), 1)
        self.assertGreater(cap["projects"][0]["idle_agent_hours"], 0)

    def test_an_exhaustion_after_a_reopening_holds(self):
        notes = [{"work_item_id": 90, "source": SPLIT, "note": "roadmap: rouverte ameesh", "at_ts": T - 2 * H},
                 {"work_item_id": 90, "source": SPLIT, "note": "roadmap: épuisée ameesh", "at_ts": T - H}]
        cap = measure(team(2, 8), roadmap_notes=notes, open_lots=[split_lot()])
        self.assertEqual(cap["projects"][0]["roadmap_exhausted"]["lot"], 90)
        self.assertEqual(cap["alerts"], [])

    def test_a_lot_taken_after_the_note_does_not_reopen(self):
        # #5149 (audit 31/10 21:15, #5172) : un lot créé puis pris après la
        # note n'est plus démarrable ; la route reste épuisée, comme
        # `ameesh alerts` le dit
        notes = [{"work_item_id": 90, "source": SPLIT, "note": "roadmap: épuisée ameesh",
                  "at_ts": T - 2 * H}]
        lots = [split_lot(),
                {"id": 91, "app": "ameesh", "state": "qa", "assignee": "bureau",
                 "created_ts": T - H}]
        cap = measure(team(2, 8), roadmap_notes=notes, open_lots=lots)
        self.assertIsNotNone(cap["projects"][0]["roadmap_exhausted"])
        self.assertEqual(cap["alerts"], [])

    def test_a_review_seat_exempted_from_sleep_is_not_idle(self):
        # #5149 : le seul relecteur d'une famille, épargné du sommeil pour un
        # gel, tient un siège ; il ne compte pas au repos
        notes = [{"work_item_id": 90, "source": SPLIT, "note": "roadmap: épuisée ameesh", "at_ts": T - 2 * H},
                 {"work_item_id": 90, "source": SPLIT, "at_ts": T - H,
                  "note": "sleep exempted: ameesh-idle-0: only eligible reviewer of "
                          "family deepseek for pending freeze #130"},
                 {"work_item_id": 90, "source": SPLIT, "at_ts": T - H,
                  "note": "sleep exempted: ameesh-idle-1: unread mail from pilote (5 min)"}]
        # #6010 : le siège tient tant que le lot cité attend son avis
        lots = [split_lot(),
                {"id": 130, "app": "ameesh", "state": "qa", "assignee": "ameesh-busy-0"}]
        cap = measure(team(2, 8), roadmap_notes=notes, open_lots=lots)
        self.assertEqual(cap["projects"][0]["idle_agents"], ["ameesh-idle-1"])
        self.assertEqual(cap["projects"][0]["wakeable"], 9)
        self.assertIn({"agent": "ameesh-idle-0", "reason": "review seat: only eligible "
                       "reviewer of family deepseek for pending freeze #130"}, cap["held"])

    def test_an_exemption_older_than_the_exhaustion_holds_no_seat(self):
        notes = [{"work_item_id": 90, "source": SPLIT, "at_ts": T - 3 * H,
                  "note": "sleep exempted: ameesh-idle-0: designated review pending on #130"},
                 {"work_item_id": 90, "source": SPLIT, "note": "roadmap: épuisée ameesh", "at_ts": T - 2 * H}]
        cap = measure(team(2, 8), roadmap_notes=notes, open_lots=[split_lot()])
        self.assertEqual(cap["projects"][0]["idle"], 2)
        self.assertEqual(cap["held"], [])

    def test_a_note_outside_the_open_split_lot_does_not_count(self):
        # B4 (impl-03), #6215 : la note se pose sur le lot de découpage ouvert
        # du projet ; une note sur un autre lot (ordinaire, fermé, ou d'un
        # autre projet) ne vaut pas, comme pour le sommeil.
        for source, work_item_id in (
                ("", 999), ("github:issue", 999),
                ("file-decoupage:nexlink", 999),
                ("file-decoupage:ameesh", 91)):
            with self.subTest(source=source, work_item_id=work_item_id):
                notes = [{"work_item_id": work_item_id, "source": source,
                          "note": "roadmap: épuisée ameesh", "at_ts": T - H}]
                closed = split_lot(91, state="closed")
                cap = measure(team(2, 8), roadmap_notes=notes,
                              open_lots=[split_lot(), closed])
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
        cap = measure(team(2, 0, "ameesh") + team(1, 0, "nexlink"), roadmap_notes=notes,
                      open_lots=[split_lot()])
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
        cap = measure(agents, held={"held": "budget"},
                      canons=[_Canon({"auditeur": ["auditeur"]})], configured=True)
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
        cap = measure([agent("a"), agent("auditeur")],
                      canons=[_Canon({"auditeur": ["auditeur"]})], configured=True)
        self.assertEqual(cap["projects"][0]["wakeable"], 1)
        self.assertEqual(cap["mesh"]["idle_agent_hours"], 6.0)

    def test_a_reviewer_role_counts_no_idle_hours(self):
        # #7174 : un agent dont la fiche porte le rôle `relecture` n'est pas
        # une capacité de feuille de route (AUDITEUR.md, « Capacité au
        # repos ») : il ne compte ni dans les réveillables ni dans les
        # heures-agent, même sans avis dû à cet instant.
        cap = measure([agent("a"), agent("rev", roles=["implementation", "relecture"])])
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

    def test_an_expired_window_does_not_hide_the_open_window_loss(self):
        # #7522 : la five_hour du dernier relevé complet a expiré, mais la
        # seven_day a été republiée depuis (primaire Claude) ; l'ancienne
        # fenêtre n'annonce aucune perte et ne périme pas la nouvelle.
        five = cost.Gauge("claude", "five_hour", 0.03, T - 2 * 86400, 5 * H,
                          produced_at=T - 3 * 86400)
        week = cost.Gauge("claude", "seven_day", 0.68, T + 2 * 86400, 7 * 86400,
                          produced_at=T - 120)
        losses = debit.plan_losses({("claude", "primaire"): [
            (five, T - 3 * 86400), (week, T - 120)]}, T)
        self.assertEqual(losses, [{"harness": "claude", "account": "primaire",
                                   "stale": False, "lost": 0.32, "window": "seven_day",
                                   "resets_in_s": 2 * 86400}])
        text = "\n".join(debit._capacity_text(measure(team(1, 1), losses=losses)))
        self.assertIn("claude/primaire 32 % de seven_day", text)

    def test_a_partial_reading_keeps_the_open_window_it_can_still_bound(self):
        # #7522 : relevé partiel (five_hour échue) encore informateur pour la
        # seven_day : son âge (16 h) ne dépasse pas ce qui lui reste (3 j).
        five = cost.Gauge("claude", "five_hour", 0.01, T - H, 5 * H,
                          produced_at=T - 16 * H)
        week = cost.Gauge("claude", "seven_day", 0.59, T + 3 * 86400, 7 * 86400,
                          produced_at=T - 16 * H)
        losses = debit.plan_losses({("claude", "secondaire"): [
            (five, T - 16 * H), (week, T - 16 * H)]}, T)
        self.assertEqual(losses, [{"harness": "claude", "account": "secondaire",
                                   "stale": False, "lost": 0.41,
                                   "window": "seven_day",
                                   "resets_in_s": 3 * 86400}])

    def test_a_current_reading_keeps_the_soonest_open_window(self):
        # #7522 : sans fenêtre échue, le calcul d'`accounts` ne bouge pas ; la
        # borne d'âge ne s'applique qu'à un relevé partiel. Le codex-300min
        # d'un relevé de 78 min (reset dans 65 min) reste annoncé.
        short = cost.Gauge("codex", "codex-300min", 0.32, T + 3899, 5 * H,
                           produced_at=T - 4733)
        long = cost.Gauge("codex", "codex-10080min", 0.60, T + 307052,
                          7 * 86400, produced_at=T - 4733)
        losses = debit.plan_losses({("codex", "secondaire"): [
            (short, T - 2151), (long, T - 2151)]}, T)
        self.assertEqual(losses, [{"harness": "codex", "account": "secondaire",
                                   "stale": False, "lost": 0.68,
                                   "window": "codex-300min",
                                   "resets_in_s": 3899}])

    def test_an_open_window_older_than_what_remains_stays_stale(self):
        # #1561/#1672 : un relevé de quatre jours ne borne plus une fenêtre à
        # neuf heures de sa remise à zéro — aucune perte n'est annoncée.
        five = cost.Gauge("claude", "five_hour", 0.30, T - 4 * 86400, 5 * H,
                          produced_at=T - 4 * 86400)
        week = cost.Gauge("claude", "seven_day", 0.30, T + 9 * H, 7 * 86400,
                          produced_at=T - 4 * 86400)
        losses = debit.plan_losses({("claude", "secondaire"): [
            (five, T - 4 * 86400), (week, T - 4 * 86400)]}, T)
        self.assertEqual(losses, [{"harness": "claude", "account": "secondaire",
                                   "stale": True, "lost": None, "window": None,
                                   "resets_in_s": None}])

    def test_all_windows_expired_announce_no_loss(self):
        five = cost.Gauge("claude", "five_hour", 0.03, T - H, 5 * H,
                          produced_at=T - 6 * H)
        week = cost.Gauge("claude", "seven_day", 0.54, T - 2 * H, 7 * 86400,
                          produced_at=T - 6 * H)
        losses = debit.plan_losses({("claude", "tertiaire"): [
            (five, T - 6 * H), (week, T - 6 * H)]}, T)
        self.assertEqual(losses, [{"harness": "claude", "account": "tertiaire",
                                   "stale": False, "lost": None, "window": None,
                                   "resets_in_s": None}])

    def test_an_undated_window_keeps_an_expired_reading_uncertain(self):
        # #1566 N2 : une fenêtre non datée ne prouve pas sa remise à zéro ;
        # elle n'annonce aucune perte, mais le relevé n'est pas courant.
        five = cost.Gauge("claude", "five_hour", 0.03, T - H, 5 * H,
                          produced_at=T - 6 * H)
        undated = cost.Gauge("claude", "seven_day", 0.40, None, 7 * 86400,
                             produced_at=T - 6 * H)
        losses = debit.plan_losses({("claude", "tertiaire"): [
            (five, T - 6 * H), (undated, T - 6 * H)]}, T)
        self.assertEqual(losses, [{"harness": "claude", "account": "tertiaire",
                                   "stale": True, "lost": None, "window": None,
                                   "resets_in_s": None}])


class GaugeProvenanceTest(unittest.TestCase):
    def test_the_latest_gauge_keeps_its_production_date(self):
        # #7522 : sans `produced_at`, le débit ne peut pas distinguer une
        # fenêtre échue d'un ancien relevé d'une fenêtre fraîche.
        gauge = sous_utilisation._gauge({
            "harness": "claude", "key": "seven_day@fable", "used": 0.4,
            "resets_at_ts": T + 3 * 86400, "window_s": 7 * 86400,
            "produced_ts": T - 120, "observed_ts": T - 60, "source": "mesh-design"})
        self.assertEqual(gauge.produced_at, T - 120)
        self.assertEqual(gauge.read_at, T - 120)     # production, pas insertion
        self.assertEqual(gauge.source, "mesh-design")
        self.assertEqual(gauge.scope, "fable")

    def test_a_reading_without_production_date_keeps_the_insertion_date(self):
        gauge = sous_utilisation._gauge({
            "harness": "codex", "key": "codex-300min", "used": 0.2,
            "resets_at_ts": T + H, "window_s": 5 * H,
            "produced_ts": None, "observed_ts": T - 60, "source": ""})
        self.assertIsNone(gauge.produced_at)
        self.assertEqual(gauge.read_at, T - 60)


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
                mock.patch("ameesh.host_lifecycle.closed_hosts", return_value={}), \
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
                mock.patch("ameesh.host_lifecycle.closed_hosts", return_value={}), \
                mock.patch("ameesh.sous_utilisation.latest_account_gauges", return_value={}):
            cap = debit.read_capacity(db, cfg=object(), now=T, hours=6, turns=[],
                                      canons=[], unreadable=[])
        project = [p for p in cap["projects"] if p["project"] == "ameesh"][0]
        self.assertEqual(project["decoupage"]["lot"], 90)
        self.assertTrue(cap["gating_ok"])
        self.assertEqual(cap["alerts"], [])


    def test_the_last_split_move_is_read_and_holds_the_check(self):
        # #7848 : `read_capacity` lit le dernier mouvement du lot de découpage
        # (note du pilote, lot créé qui le cite) ; un lot de 48 h noté il y a
        # 20 min ne lève pas l'alerte. La requête vise le seul lot de découpage.
        from types import SimpleNamespace
        row = {"id": 90, "title": "Découper la feuille de route de ameesh",
               "state": "intake", "assignee": "pilote", "app": "ameesh",
               "type": "evolution", "created_ts": T - 48 * H}
        other = {"id": 91, "title": "autre", "state": "intake", "app": "ameesh",
                 "type": "bug", "created_ts": T - H}
        store = SimpleNamespace(operations=SimpleNamespace(
            listing=lambda: team(2, 8), open_lots_activity=lambda limit: [row, other]))
        asked = []

        def query(sql, params=()):
            if "file-decoupage:%%'" in sql and "SELECT id, source" in sql:
                return [{"id": 90, "source": SPLIT}]
            if sql is debit._SPLIT_MOVES_SQL:
                asked.append(params)
                return [{"id": 90, "moved_ts": T - 20 * 60}]
            return []
        db = mock.Mock()
        db.query.side_effect = query
        with mock.patch("ameesh.storage.of", return_value=store), \
                mock.patch("ameesh.mechanics.configured", return_value=mechanics_doc()), \
                mock.patch("ameesh.account_turn.turn_hold", return_value=""), \
                mock.patch("ameesh.sous_utilisation.waiting_project_lots", return_value=[]), \
                mock.patch("ameesh.sous_utilisation.critical_verdict",
                           return_value=(False, False)), \
                mock.patch("ameesh.host_lifecycle.closed_hosts", return_value={}), \
                mock.patch("ameesh.sous_utilisation.latest_account_gauges", return_value={}):
            cap = debit.read_capacity(db, cfg=object(), now=T, hours=6, turns=[],
                                      canons=[], unreadable=[])
        self.assertEqual(asked, [("90",)])
        project = [p for p in cap["projects"] if p["project"] == "ameesh"][0]
        self.assertEqual(project["decoupage"]["quiet_s"], 20 * 60)
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
                "ameesh.account_turn.turn_hold": mock.Mock(return_value=""),
                "ameesh.host_lifecycle.closed_hosts": mock.Mock(return_value={})}
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
