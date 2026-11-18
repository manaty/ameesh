# SPDX-License-Identifier: AGPL-3.0-only
"""#6777 — la mesure de la carte part dans sa fenêtre, sans le cron de GitHub.

Constat : le cron `30 3 * * *` de carte.yml est démarré par GitHub vers 10-11Z
chaque jour ; la garde de fenêtre de #6157 le saute donc tous les jours et
l'alerte `map_stale` reste ouverte. Témoins :

* `scripts/carte-fenetre.py` : une carte de plus de 48 h (le seuil de
  `map_stale`) fait mesurer même hors de [00:00, 07:00)Z, au premier créneau
  sans paquet en cours — c'est `tests/test_6157_carte_fenetre.py` ;
* la minuterie de rôle `ameesh-carte` (01:00Z, `ameesh role timers`) dispatche
  `carte.yml` sur l'hôte qui détient le bail, et remplace la copie manuelle de
  #6157 au lieu de la doubler ;
* `ameesh carte dispatch` envoie `planifie=true` aux dépôts de
  `github.projects` qui portent le workflow, sans décider lui-même de mesurer.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
from unittest import mock

import contextlib
import io
import json
import pathlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from ameesh import carte, plan_github, role_timers

REPO = pathlib.Path(__file__).resolve().parent.parent


def cfg(projects=None):
    return SimpleNamespace(github={"projects": projects or {"ameesh": "manaty/ameesh"}})


def settings(projects=None):
    return plan_github.settings_of(cfg(projects))


class FakeGh:
    """Un `gh` en mémoire : deux lectures de workflow et le dispatch."""

    def __init__(self, *, workflows=(), fail_read=(), fail_dispatch=()):
        self.workflows = set(workflows)
        self.fail_read = set(fail_read)
        self.fail_dispatch = set(fail_dispatch)
        self.calls = []

    def run(self, argv, stdin=None):
        self.calls.append((list(argv), stdin))
        path = next((arg for arg in argv if arg.startswith("repos/")), "")
        depot = "/".join(path.split("/")[:3])
        depot = depot[len("repos/"):] if depot.startswith("repos/") else depot
        if "/contents/" in path:
            ref = path.split("?ref=", 1)[-1]
            if depot in self.fail_read:
                return 1, "", "gh: Server Error (HTTP 500)"
            if (depot, ref) in self.workflows:
                return 0, "{}", ""
            return 1, "", "gh: Not Found (HTTP 404)"
        if "/dispatches" in path:
            if depot in self.fail_dispatch:
                return 1, "", "gh: Bad Request (HTTP 400)"
            return 0, "", ""
        raise AssertionError("appel gh inattendu : %r" % (argv,))

    def dispatches(self):
        return [json.loads(stdin) for argv, stdin in self.calls
                if any("/dispatches" in arg for arg in argv)]


class CiblesTest(unittest.TestCase):

    def test_la_base_declaree_du_depot_est_suivie(self):
        visees = carte.cibles(settings({
            "ameesh": "manaty/ameesh",
            "nexlink": {"repo": "manaty/nexlink", "base": ["develop"]}}))
        self.assertEqual(visees, [
            {"projet": "ameesh", "depot": "manaty/ameesh", "branche": "main"},
            {"projet": "nexlink", "depot": "manaty/nexlink", "branche": "develop"},
        ])

    def test_un_projet_inconnu_est_refuse(self):
        with self.assertRaises(carte.CarteError):
            carte.cibles(settings(), projets=["absent"])

    def test_un_depot_explicite_hors_configuration_reste_possible(self):
        self.assertEqual(
            carte.cibles(settings(), depots=["acme/outil"]),
            [{"projet": "", "depot": "acme/outil", "branche": "main"}])

    def test_un_depot_cite_deux_fois_n_est_vise_qu_une_fois(self):
        self.assertEqual(len(carte.cibles(settings(), depots=["manaty/ameesh",
                                                             "MANATY/AMEESH"])), 1)


class DispatchTest(unittest.TestCase):

    def lancer(self, *argv, gh=None):
        gh = gh or FakeGh(workflows={("manaty/ameesh", "main")})
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = carte.main(["dispatch", *argv], gh=gh, cfg=cfg())
        return code, out.getvalue(), err.getvalue(), gh

    def test_un_dispatch_planifie_dans_la_fenetre(self):
        code, out, _, gh = self.lancer()
        self.assertEqual(code, 0, out)
        self.assertEqual(gh.dispatches(),
                         [{"ref": "main", "inputs": {"planifie": "true"}}])
        self.assertIn("manaty/ameesh@main", out)

    def test_un_depot_sans_workflow_est_ignore_sans_echec(self):
        gh = FakeGh()
        code, out, err, gh = self.lancer(gh=gh)
        self.assertEqual(code, 0)
        self.assertEqual(gh.dispatches(), [])
        self.assertIn("workflow carte.yml absent", out)
        self.assertEqual(err, "")

    def test_dry_run_n_envoie_rien(self):
        code, out, _, gh = self.lancer("--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(gh.dispatches(), [])
        self.assertIn("dry-run", out)

    def test_manuel_envoie_planifie_false(self):
        code, _, _, gh = self.lancer("--manuel")
        self.assertEqual(code, 0)
        self.assertEqual(gh.dispatches(),
                         [{"ref": "main", "inputs": {"planifie": "false"}}])

    def test_un_echec_de_dispatch_sort_en_1(self):
        gh = FakeGh(workflows={("manaty/ameesh", "main")},
                    fail_dispatch={"manaty/ameesh"})
        code, _, err, gh = self.lancer(gh=gh)
        self.assertEqual(code, 1)
        self.assertIn("dispatch de carte.yml sur manaty/ameesh refusé", err)

    def test_une_lecture_autre_qu_un_404_est_une_erreur(self):
        gh = FakeGh(workflows={("manaty/ameesh", "main")},
                    fail_read={"manaty/ameesh"})
        # la lecture de contenu échoue en 500 : ce n'est pas « workflow absent »
        code, _, err, _ = self.lancer(gh=gh)
        self.assertEqual(code, 1)
        self.assertIn("lecture de carte.yml sur manaty/ameesh impossible", err)

    def test_la_sortie_json_n_est_qu_un_objet(self):
        code, out, _, _ = self.lancer("--json")
        self.assertEqual(code, 0)
        document = json.loads(out)
        self.assertEqual(document["schema"], carte.SCHEMA)
        self.assertEqual(document["results"][0]["action"], "dispatch")


class HourlyCatchupTest(unittest.TestCase):
    def test_hourly_dispatch_leaves_packet_arbitration_to_guard(self):
        spec = importlib.util.spec_from_file_location("carte_fenetre", REPO / "scripts/carte-fenetre.py")
        guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guard)
        now = dt.datetime(2026, 11, 15, 12, tzinfo=dt.timezone.utc)
        published = now - dt.timedelta(hours=49)
        rows = [{"repo": "manaty/ameesh", "branch": "main",
                 "published_ts": published.timestamp()}]
        gh = FakeGh(workflows={("manaty/ameesh", "main")})
        for offset, packet in ((0, True), (1, False)):
            tick = now + dt.timedelta(hours=offset)
            with mock.patch.object(carte, "cartes_connues", return_value=rows):
                self.assertEqual(carte.main(["dispatch", "--horaire"],
                                           gh=gh, cfg=cfg(), maintenant=tick), 0)
            measure, _ = guard.decider(evenement="workflow_dispatch", planifie=True,
                                      maintenant=tick, derniere_carte=published,
                                      paquet_actif=packet)
            self.assertEqual(measure, not packet)
        self.assertEqual(len(gh.dispatches()), 2)
        self.assertTrue(all(d["inputs"] == {"planifie": "true"} for d in gh.dispatches()))

    def test_fresh_unknown_or_other_branch_never_dispatch_hourly(self):
        now = dt.datetime(2026, 11, 15, 12, tzinfo=dt.timezone.utc)
        for age, branch in ((47, "main"), (48, "main"), (None, "main"), (49, "other")):
            with self.subTest(age=age, branch=branch):
                rows = [{"repo": "manaty/ameesh", "branch": branch,
                         "published_ts": None if age is None else
                         (now - dt.timedelta(hours=age)).timestamp()}]
                gh = FakeGh(workflows={("manaty/ameesh", "main")})
                with mock.patch.object(carte, "cartes_connues", return_value=rows):
                    self.assertEqual(carte.main(["dispatch", "--horaire"],
                                               gh=gh, cfg=cfg(), maintenant=now), 0)
                self.assertEqual(gh.calls, [])

    def test_daily_request_does_not_require_known_age(self):
        now = dt.datetime(2026, 11, 15, 1, tzinfo=dt.timezone.utc)
        gh = FakeGh(workflows={("manaty/ameesh", "main")})
        with mock.patch.object(carte, "cartes_connues", side_effect=AssertionError):
            self.assertEqual(carte.main(["dispatch", "--horaire"], gh=gh,
                                       cfg=cfg(), maintenant=now), 0)
        self.assertEqual(len(gh.dispatches()), 1)

    def test_database_unavailable_is_unknown_age(self):
        now = dt.datetime(2026, 11, 15, 12, tzinfo=dt.timezone.utc)
        gh = FakeGh(workflows={("manaty/ameesh", "main")})
        with mock.patch.object(carte, "cartes_connues", side_effect=RuntimeError("offline")):
            self.assertEqual(carte.main(["dispatch", "--horaire"], gh=gh,
                                       cfg=cfg(), maintenant=now), 0)
        self.assertEqual(gh.calls, [])


class PacketCatchupTest(unittest.TestCase):
    def test_tick_checks_staleness_before_following_a_reserved_packet(self):
        from tests.test_3845_paquet_horaire import ph
        ctx = SimpleNamespace(now=dt.datetime(2026, 11, 15, 12,
                              tzinfo=dt.timezone.utc).timestamp(),
                              project="demo", target="develop", dry_run=False, log=[])
        with mock.patch.object(ph, "paused", return_value=None), \
             mock.patch.object(ph, "check_fresh"), \
             mock.patch.object(ph, "align_schema"), \
             mock.patch.object(ph, "tick", return_value="reserved") as tick, \
             mock.patch.object(carte, "main", return_value=1) as dispatch:
            self.assertEqual(ph.run(ctx), "reserved")
        self.assertEqual(dispatch.call_args.args[0],
                         ["dispatch", "--horaire", "--project", "demo",
                          "--branch", "develop"])
        tick.assert_called_once_with(ctx)
        self.assertTrue(ctx.log)

    def test_ten_minute_ticks_dispatch_once_per_hour_except_daily_slot(self):
        from tests.test_3845_paquet_horaire import ph
        start = dt.datetime(2026, 11, 15, tzinfo=dt.timezone.utc)
        ctx = SimpleNamespace(project="demo", target="main", dry_run=False, log=[])
        with mock.patch.object(carte, "main", return_value=0) as dispatch:
            for minute in range(0, 24 * 60, 10):
                ctx.now = (start + dt.timedelta(minutes=minute, seconds=30)).timestamp()
                ph.catch_up_map(ctx)
            self.assertEqual(dispatch.call_count, 23)
            attempted = [call.kwargs["maintenant"] for call in dispatch.call_args_list]
            self.assertEqual([when.hour for when in attempted],
                             [hour for hour in range(24) if hour != 1])
            self.assertTrue(all(when.minute == 0 for when in attempted))

    def test_daily_timer_and_dry_run_are_respected(self):
        from tests.test_3845_paquet_horaire import ph
        ctx = SimpleNamespace(now=dt.datetime(2026, 11, 15, 1,
                              tzinfo=dt.timezone.utc).timestamp(),
                              project="demo", target="main", dry_run=True, log=[])
        with mock.patch.object(carte, "main", return_value=0) as dispatch:
            ph.catch_up_map(ctx)
            dispatch.assert_not_called()
            ctx.now += 3600
            ph.catch_up_map(ctx)
            self.assertIn("--dry-run", dispatch.call_args.args[0])


def row(candidates=("agents-a", "agents-b")):
    return {"name": "mesh-design", "roles": ["conception"], "releve": "election",
            "host": "agents-a",
            "candidats": list(candidates)}


def plan(*, host="agents-a", role="mesh-design", projects=None,
         paquet=("", "/clone"), row_value=None):
    return role_timers.plan(
        host=host, role=role, row=row_value or row(),
        settings=SimpleNamespace(projects=projects if projects is not None
                                 else {"ameesh": "manaty/ameesh"}, interval=900),
        watch_intervals=[900], notify_humans=["human:owner"], paquet=paquet)


def carte_item(report):
    return next(item for item in report["timers"] if item["name"] == "carte")


class RoleTimerTest(unittest.TestCase):

    def test_un_candidat_recoit_le_dispatch_a_01h(self):
        item = carte_item(plan())
        self.assertEqual(item["unit"], "ameesh-carte")
        self.assertEqual(item["interval_s"], 24 * 3600)
        self.assertEqual(item["blocked"], "")

    def test_un_non_candidat_ne_recoit_rien(self):
        self.assertEqual(plan(host="agents-c")["timers"], [])

    def test_l_auditeur_ne_porte_pas_la_carte(self):
        names = [item["name"] for item in plan(
            role="auditeur", row_value=dict(row(), roles=["auditeur"]))["timers"]]
        self.assertNotIn("carte", names)

    def test_sans_projets_la_passe_n_est_pas_posee(self):
        names = [item["name"] for item in plan(projects={})["timers"]]
        self.assertNotIn("carte", names)

    def test_le_paquet_reste_la_derniere_passe(self):
        # le tick du paquet est cherché en fin de plan par les tests de #3854
        self.assertEqual(plan()["timers"][-1]["name"], "paquet-horaire")

    def test_les_unites_sont_rendues_sous_le_nom_de_la_copie_manuelle(self):
        with tempfile.TemporaryDirectory(prefix="6777-units-") as tmp:
            directory = Path(tmp)
            report = plan()
            role_timers.install(report, directory=directory,
                                bin_path="/opt/ameesh/bin/ameesh", apply=False)
            service = (directory / "ameesh-carte.service").read_text(encoding="utf-8")
            timer = (directory / "ameesh-carte.timer").read_text(encoding="utf-8")
            self.assertIn("ExecStart=/opt/ameesh/bin/ameesh role run mesh-design -- "
                          "/opt/ameesh/bin/ameesh carte dispatch", service)
            self.assertIn("EnvironmentFile=-%h/.config/ameesh/notify.env", service)
            self.assertIn("OnCalendar=*-*-* 01:00:00 UTC", timer)
            self.assertIn("Persistent=true", timer)
            self.assertEqual(role_timers.check(
                report, directory=directory, apply=False,
                bin_path="/opt/ameesh/bin/ameesh"), [])

    def test_une_copie_manuelle_est_remplacee_pas_doublee(self):
        with tempfile.TemporaryDirectory(prefix="6777-copy-") as tmp:
            directory = Path(tmp)
            (directory / "ameesh-carte.service").write_text(
                "[Service]\nExecStart=gh workflow run\n", encoding="utf-8")
            (directory / "ameesh-carte.timer").write_text(
                "[Timer]\nOnCalendar=03:30\n", encoding="utf-8")
            role_timers.install(plan(), directory=directory, apply=False)
            self.assertEqual(sorted(p.name for p in directory.glob("*carte*")),
                             ["ameesh-carte.service", "ameesh-carte.timer"])
            self.assertIn("carte dispatch",
                          (directory / "ameesh-carte.service").read_text(encoding="utf-8"))

    def test_les_unites_versionnees_portent_la_meme_cadence(self):
        timer = (REPO / "deploy/systemd/ameesh-carte.timer").read_text(encoding="utf-8")
        service = (REPO / "deploy/systemd/ameesh-carte.service").read_text(encoding="utf-8")
        self.assertIn("OnCalendar=*-*-* 01:00:00 UTC", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("role run mesh-design", service)
        self.assertIn("carte dispatch", service)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
