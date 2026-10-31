# SPDX-License-Identifier: AGPL-3.0-only
"""#3845 : paquet horaire déclenché par minuterie (scripts/paquet-horaire.py).

Le tic est piloté par une fausse coquille : aucune base, aucune forge. Les
témoins : rien n'est créé sans lot prêt ni pendant une réservation ; une
composition propre pose « verdict non requis » et demande la ci-full ; une
résolution laisse ce geste au pilote ; un rouge attribué à une seule unité la
retire et recompose une fois, en écrivant à son auteur ; un rouge non concluant
ne retire rien ; le porteur est créé avec son estimation (garde L157 de la
vraie CLI) ; une tentative morte (arrêt, échec) est reprise sur le même
porteur, bornée, puis signalée une fois.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import unittest
from unittest import mock
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "paquet_horaire", ROOT / "scripts" / "paquet-horaire.py")
ph = importlib.util.module_from_spec(SPEC)
sys.modules["paquet_horaire"] = ph
SPEC.loader.exec_module(ph)

HEAD = "a" * 40
HEAD2 = "b" * 40
ZONE = ph.ZoneInfo("Europe/Paris")
NOW = datetime(2026, 10, 27, 10, 25, tzinfo=ZONE).timestamp()

MANIFEST = """# Paquet

## Composition (2 unités)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #3801 | `035249f490d2` | `035249f49` | composition |
| 2 | #3807 | `6f5d44e941c4` | `6f5d44e94` | composition |
"""
RESOLVED = MANIFEST.replace("| 1 | #3801 | `035249f490d2` | `035249f49` | composition |",
                            "| 1 | #3779, #3755 | `404177ae7c83` | `51762f6fa8b1` | résolution |")


class FakeShell:
    def __init__(self):
        self.calls: list[list[str]] = []
        self.mails: list[tuple[str, str]] = []
        self.lists: dict[str, list] = {}
        self.items: dict[int, dict] = {}
        self.ci: dict[int, dict] = {}
        self.agents: list[dict] = []
        self.auto_out = "manifeste reviews/x.md : tête %s\n" % HEAD
        self.auto_rc = 0
        self.added: list[list[str]] = []
        self.clock = NOW
        #: #4506 : la tête gelée est-elle un ancêtre de origin/<cible> ?
        self.ancestor = False

    def run(self, cmd, *, cwd=None, input_text=None, timeout=0):
        self.calls.append(list(cmd))
        if cmd[0] == "agent-mail":
            self.mails.append((cmd[2], input_text))
            return ph.Result(0)
        if cmd[0] == "git":
            if "ls-remote" in cmd:
                return ph.Result(0, "%s\trefs/heads/integration/paquet-h-20261027-03\n" % HEAD)
            if "merge-base" in cmd and "--is-ancestor" in cmd:
                return ph.Result(0 if self.ancestor else 1)
            return ph.Result(0)
        if cmd[0] == "ameesh":
            args = cmd[1:]
            if args[:1] == ["list"]:
                return ph.Result(0, json.dumps(self.agents))
            if args[:2] == ["work", "list"]:
                state = args[args.index("--state") + 1] if "--state" in args else ""
                return ph.Result(0, json.dumps(self.lists.get(state, [])))
            if args[:2] == ["work", "add"]:
                self.added.append(args)
                return ph.Result(0, "lot #4000 created in intake: x\n")
            if args[:2] == ["work", "show"]:
                return ph.Result(0, json.dumps(self.items[int(args[2])]))
            if args[:2] == ["ci", "status"]:
                return ph.Result(0, json.dumps(self.ci.get(int(args[2]), {"runs": []})))
            if args[:2] == ["work", "note"]:
                item = self.items.get(int(args[2]))
                if item is not None:
                    self.clock += 1
                    item.setdefault("events", []).append(
                        {"note": args[3], "created_ts": self.clock})
            return ph.Result(0, "ok\n")
        if str(cmd[1]).endswith("paquet-auto.py"):
            return ph.Result(self.auto_rc, self.auto_out)
        raise AssertionError("commande inattendue : %r" % cmd)

    def ran(self, *prefix) -> list[list[str]]:
        return [c for c in self.calls if c[:len(prefix)] == list(prefix)
                or (len(c) > 1 and c[1:1 + len(prefix)] == list(prefix))]


def carrier(lot=4000, *, sha=HEAD, notes=(), title="Paquet horaire h-20261027-04 — x",
            source="paquet-horaire (#3845), minuterie", created=NOW - 600):
    return {"id": lot, "app": "ameesh", "title": title, "source": source,
            "state": "qa", "branch": "integration/paquet-h-20261027-04",
            "branch_target": "main", "created_ts": created, "assignee": "mesh-design",
            "freeze": {"declared": {"sha": sha}} if sha else {},
            "events": [{"note": n} for n in notes]}


def red_run(*, attribution=True, best=(3801,), status="failure", part="psycopg 1/8"):
    test = "tests.test_x.T.test_a"
    run = {"run_id": "77", "head_sha": HEAD, "status": status, "url": "u",
           "failures": [{"test": test, "part": part, "inherited": False}]}
    if attribution:
        run["attribution"] = {"status": "ok", "tests": [{
            "test": test, "status": "probable",
            "units": [{"lot": lot, "best": 4} for lot in best]
            + [{"lot": 3807, "best": 1}]}]}
    return run


class Base(unittest.TestCase):
    def setUp(self):
        self.shell = FakeShell()
        self.ctx = ph.Context(repo=ROOT, target="main", project="ameesh",
                              pilot="mesh-design", actor="mesh-design",
                              shell=self.shell, python="python3", now=NOW,
                              zone=ZONE)
        self._saved = (ph.ready_lots, ph.manifest_at, ph.replay_locally,
                       ph.auto.composition_holder)
        self.ready = [3801, 3807]
        self.manifest = MANIFEST
        self.replay = ""
        ph.ready_lots = lambda ctx: list(self.ready)
        ph.manifest_at = lambda ctx, item, sha: self.manifest
        ph.replay_locally = lambda ctx, sha, tests: self.replay
        # #4329 : le verrou de composition est lu dans le `.git` du checkout ;
        # les témoins du tic le remplacent pour ne pas dépendre du dépôt réel
        # (guest de prévalidation sans histoire, safe.directory, etc.). Les
        # témoins du verrou le patchent explicitement dans leur with.
        ph.auto.composition_holder = lambda repo, name: None

    def tearDown(self):
        (ph.ready_lots, ph.manifest_at, ph.replay_locally,
         ph.auto.composition_holder) = self._saved


class RecomposeCloneTest(Base):
    """#4562 : la recomposition n'écrit pas dans le clone d'un agent."""

    def test_recomposition_refuse_le_dossier_de_travail_d_un_agent(self):
        self.shell.items[4000] = carrier()
        self.shell.agents = [{"name": "mty-amh-impl-02", "host": self.ctx.host,
                              "cwd": str(ROOT)}]
        with self.assertRaisesRegex(ph.InputError, "dossier de travail"):
            ph.pilot_recompose(self.ctx, 4000, [], [])
        self.assertFalse([c for c in self.shell.calls
                          if "paquet-auto.py" in " ".join(c)])


class AliveTest(unittest.TestCase):
    """#4501 : la vie d'une tentative se lit au pid, jamais à la commande."""

    def test_pid_vivant_sous_un_autre_nom_que_le_script(self):
        # h-12 du 29/10 : la recomposition tournait sous `scripts/.h12-tick.py`,
        # donc sans « paquet-horaire » dans sa ligne de commande ; le tic l'a
        # jugée morte et a recomposé le même porteur en parallèle.
        proc = subprocess.Popen(["sleep", "60"])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.terminate)
        ctx = ph.Context(repo=ROOT, target="main", project="ameesh",
                         pilot="mesh-design", actor="mesh-design",
                         shell=FakeShell(), python="python3")
        self.assertTrue(ctx.alive(proc.pid))
        self.assertFalse(ctx.alive(999999999))

    def test_zombie_ou_pid_disparu_n_est_pas_vivant(self):
        proc = subprocess.Popen(["true"])
        pid = proc.pid
        proc.wait()
        self.assertFalse(ph.Context(repo=ROOT, target="main", project="ameesh",
                                    pilot="mesh-design", actor="mesh-design",
                                    shell=FakeShell(), python="python3").alive(pid))


class ComposeTest(Base):
    def test_rien_sans_lot_pret(self):
        self.ready = []
        self.assertIn("aucun lot prêt", ph.tick(self.ctx))
        self.assertEqual(self.shell.ran("work", "add"), [])

    def test_heure_deja_servie(self):
        # #4171 : un porteur clos sans fusion garde le créneau (pas de
        # recomposition à chaque tic).
        for reason in ("abandoned", "superseded"):
            self.shell.lists[""] = [dict(carrier(), state="closed", close_reason=reason,
                                         created_ts=NOW - 60)]
            self.assertIn("déjà créé", ph.tick(self.ctx))
        self.assertEqual(self.shell.ran("work", "add"), [])

    def test_nouveau_nom_de_paquet_deja_verrouille(self):
        # #4329 : un paquet-auto lancé à la main tient le verrou du nom que le
        # tic allait prendre : rien n'est créé.
        with mock.patch.object(ph.auto, "composition_holder",
                               return_value=(self.ctx.host, 4321)):
            self.assertIn("composition en cours (%s:4321)" % self.ctx.host,
                          ph.tick(self.ctx))
        self.assertEqual(self.shell.ran("work", "add"), [])

    def test_porteur_fusionne_dans_l_heure_libere_le_creneau(self):
        # #4171 (décision #154026 a) : fusion vue → le suivant se compose
        # sans attendre l'heure suivante.
        for state, reason in (("merged", ""), ("promoted", ""), ("delivered", ""), ("closed", "delivered")):
            row = dict(carrier(lot=3990), state=state, close_reason=reason,
                       created_ts=NOW - 60)
            self.assertFalse(ph.slot_served([row], NOW, ZONE, "ameesh", "main"), state)
        self.shell.lists[""] = [dict(carrier(lot=3990), state="delivered", created_ts=NOW - 60)]
        self.shell.items[4000] = carrier()
        self.assertIn("ci-full demandée", ph.tick(self.ctx))
        self.assertEqual(len(self.shell.ran("work", "add")), 1)

    def test_sans_fusion_vue_le_creneau_reste_consomme(self):
        # #4171 (b) : l'heure reste le rythme minimum ; seul un porteur dont la
        # fusion est vue libère le créneau, tout autre état le garde.
        for state, reason in (("intake", ""), ("build", ""), ("qa", ""), ("blocked", ""),
                              ("waiting_human", ""), ("closed", "abandoned"),
                              ("closed", "superseded"), ("closed", "")):
            row = dict(carrier(lot=3990), state=state, close_reason=reason,
                       created_ts=NOW - 60)
            self.assertTrue(ph.slot_served([row], NOW, ZONE, "ameesh", "main"),
                            (state, reason))
        # un porteur de l'heure précédente ne consomme pas le créneau courant
        old = dict(carrier(lot=3990), state="closed", close_reason="abandoned",
                   created_ts=ph.hour_start(NOW, ZONE) - 1)
        self.assertFalse(ph.slot_served([old], NOW, ZONE, "ameesh", "main"))

    def test_enchainement_epoque_perimee_ne_compose_pas(self):
        # #4171 (a) : le créneau libéré par une fusion vue ne lève pas la
        # garde d'époque de #4113 (paquet-auto.check_epoch) : bail périmé →
        # rien de poussé, ni gel ni ci-full, échec signalé au pilote.
        self.shell.lists[""] = [dict(carrier(lot=3990), state="delivered", created_ts=NOW - 60)]
        self.ctx.epoch = 279
        self.shell.auto_rc = 1
        self.shell.auto_out = ("paquet-auto : composition interrompue avant la prochaine "
                               "opération : époque du bail 279 périmée\n")
        with self.assertRaises(ph.HoraireError):
            ph.tick(self.ctx)
        self.assertEqual(self.shell.ran("ci", "run"), [])
        self.assertEqual(self.shell.ran("work", "reviewer"), [])
        notes = [c[c.index("note") + 2] for c in self.shell.ran("work", "note")]
        self.assertTrue(any(n.startswith(ph.MARK_ATTEMPT) and "epoque=279" in n
                            for n in notes), notes)
        self.assertTrue(any(n.startswith(ph.MARK_FAILED) for n in notes), notes)
        self.assertIn("époque", self.shell.mails[-1][1])

    def test_porteur_ouvert_dans_l_heure_reserve_la_cible(self):
        self.shell.lists["qa"] = [carrier(lot=3990, created=NOW - 60)]
        self.shell.lists[""] = [carrier(lot=3990, created=NOW - 60)]
        self.shell.items[3990] = carrier(lot=3990, created=NOW - 60)
        ph.tick(self.ctx)
        self.assertTrue(ph.slot_served(self.shell.lists[""], NOW, ZONE, "ameesh", "main"))
        self.assertEqual(self.shell.ran("work", "add"), [])

    def test_reservation_par_un_autre_porteur(self):
        manual = carrier(lot=3999, title="h-33 manuel", source="mesh-design")
        self.shell.lists["qa"] = [manual]
        self.assertIn("réservée par #3999", ph.tick(self.ctx))
        self.assertEqual(self.shell.ran("work", "add"), [])
        self.assertFalse([c for c in self.shell.calls if "paquet-auto.py" in " ".join(c)])

    def test_composition_propre_verdict_non_requis_et_ci_full(self):
        self.shell.items[4000] = carrier()
        self.assertIn("ci-full demandée", ph.tick(self.ctx))
        add = self.shell.ran("work", "add")[0]
        self.assertIn("integration/paquet-h-20261027-04", add)
        self.assertEqual(add[add.index("--title") + 1][:31], "Paquet horaire h-20261027-04 — ")
        # #4667 : le corps du porteur ne cite pas le script par son chemin —
        # l'empreinte d'intake inférée de cette citation le faisait naître libre.
        self.assertNotIn("scripts/paquet-horaire.py", add[add.index("--body") + 1])
        auto = [c for c in self.shell.calls if "paquet-auto.py" in " ".join(c)][0]
        self.assertNotIn("--force", auto)
        # #4581 : une composition neuve n'est pas une recomposition ; elle ne
        # reprend pas une composition précédente.
        self.assertNotIn("--keep-composition", auto)
        self.assertTrue(self.shell.ran("work", "reviewer", "4000", "--no-verdict"))
        ci = self.shell.ran("ci", "run", "4000", "--full")[0]
        self.assertEqual(ci[ci.index("--sha") + 1], HEAD)
        self.assertIn(ph.MARK_CI + HEAD, ph.marks(self.shell.items[4000]))
        self.assertEqual(self.shell.mails[-1][0], "mesh-design")

    def test_courriers_nomment_l_expediteur(self):
        # revue mty-amh-impl-04 : `role run` retire AGENT_MAIL_NAME ; sans
        # --from, agent-mail refuse (« unbound sender »).
        self.shell.items[4000] = carrier()
        ph.tick(self.ctx)
        mails = [c for c in self.shell.calls if c[0] == "agent-mail"]
        self.assertTrue(mails)
        for cmd in mails:
            self.assertEqual(cmd[cmd.index("--from") + 1], "mesh-design")
        auto = [c for c in self.shell.calls if "paquet-auto.py" in " ".join(c)][0]
        self.assertEqual(auto[auto.index("--from") + 1], "mesh-design")

    def test_resolution_laisse_le_verdict_au_pilote(self):
        self.manifest = RESOLVED
        self.shell.items[4000] = carrier()
        ph.tick(self.ctx)
        self.assertEqual(self.shell.ran("work", "reviewer"), [])
        self.assertTrue(self.shell.ran("ci", "run", "4000", "--full"))
        self.assertIn("#1979", self.shell.mails[-1][1])

    def test_composition_vide_clot_le_porteur(self):
        self.shell.auto_out = "aucune unité composée : #3801 (conflit)\n"
        self.shell.items[4000] = carrier()
        self.assertIn("rien composé", ph.tick(self.ctx))
        self.assertTrue(self.shell.ran("work", "close", "4000", "--abandoned"))
        self.assertEqual(self.shell.ran("ci", "run"), [])


class FollowTest(Base):
    def open_carrier(self, **kw):
        item = carrier(notes=(ph.MARK_CI + HEAD,), **kw)
        self.shell.items[4000] = item
        self.shell.lists["qa"] = [item]
        return item

    def test_validated_carrier_with_ci_running_prevents_another_composition(self):
        item = self.open_carrier()
        item["state"] = "validated"
        self.shell.lists["qa"] = []
        self.shell.lists["validated"] = [item]
        self.shell.ci[4000] = {"runs": [dict(red_run(), status="running")]}
        self.assertIn("en cours", ph.tick(self.ctx))
        self.assertFalse(self.shell.ran("work", "add"))

    def test_ci_en_cours_rien(self):
        self.open_carrier()
        self.shell.ci[4000] = {"runs": [dict(red_run(), status="running")]}
        self.assertIn("en cours", ph.tick(self.ctx))

    def test_composition_vivante_compte_comme_en_cours(self):
        # #4329 : le tick suit un porteur dont une composition lancée hors du
        # tic tient le verrou : ni publication, ni ci-full, ni recomposition.
        self.open_carrier()
        with mock.patch.object(ph.auto, "composition_holder",
                               return_value=(self.ctx.host, 4321)):
            out = ph.tick(self.ctx)
        self.assertIn("composition en cours (%s:4321)" % self.ctx.host, out)
        self.assertEqual(self.shell.ran("ci", "run"), [])
        self.assertEqual(self.shell.ran("work", "reviewer"), [])

    def test_rouge_deterministe_retire_et_recompose_une_fois(self):
        self.open_carrier()
        self.shell.items[3801] = {"id": 3801, "assignee": "mty-amh-impl-02"}
        self.shell.ci[4000] = {"runs": [red_run()]}
        ph.tick(self.ctx)
        auto = [c for c in self.shell.calls if "paquet-auto.py" in " ".join(c)][0]
        self.assertIn("--force", auto)
        self.assertEqual(auto[auto.index("--exclude") + 1], "3801")
        # #4581 : la recomposition reprend les membres de la composition
        # précédente (la tête gelée rouge), moins l'unité exclue.
        self.assertEqual(auto[auto.index("--keep-composition") + 1], HEAD)
        # #4201 : la recomposition automatique dit son vrai motif au manifeste.
        self.assertEqual(auto[auto.index("--exclude-reason") + 1],
                         ph.EXCLUDE_CI_FULL_REASON)
        self.assertIn("ci-full rouge attribuée", ph.EXCLUDE_CI_FULL_REASON)
        self.assertIn("mty-amh-impl-02", [to for to, _ in self.shell.mails])
        notes = ph.marks(self.shell.items[4000])
        self.assertTrue(any(n.startswith(ph.MARK_ATTEMPT + "recomposition n=1 base=%s"
                                         % HEAD) for n in notes))
        # la recomposition est republiée : ci-full sur la nouvelle tête
        self.assertTrue(self.shell.ran("ci", "run", "4000", "--full"))

    def test_lot_retire_repasse_en_build_avec_sa_trace(self):
        # #4028 : le lot retiré porte sa trace (paquet, porteur, run, tests) et
        # repasse en build : correctif dû par l'assigné jusqu'au regel.
        self.open_carrier()
        self.shell.items[3801] = {"id": 3801, "assignee": "mty-amh-impl-02"}
        self.shell.ci[4000] = {"runs": [red_run()]}
        ph.tick(self.ctx)
        move = self.shell.ran("work", "move", "3801", "build")
        self.assertEqual(len(move), 1)
        trace = move[0][move[0].index("--note") + 1]
        self.assertTrue(trace.startswith(ph.MARK_EXCLUDED + "h-20261027-04 porteur=#4000 "
                                         "run=77 tests=tests.test_x.T.test_a"))
        self.assertEqual(move[0][move[0].index("--actor") + 1], "mesh-design")
        # #4043 : le retrait ne consomme pas de boucle d'auteur
        self.assertIn("--package-exclusion", move[0])
        self.assertEqual(self.shell.ran("work", "move", "3807"), [])

    def test_transition_refusee_laisse_la_trace_en_note(self):
        self.open_carrier()
        self.shell.items[3801] = {"id": 3801, "assignee": "mty-amh-impl-02"}
        self.shell.ci[4000] = {"runs": [red_run()]}
        run = self.shell.run

        def refuse(cmd, **kw):
            if cmd[:3] == ["ameesh", "work", "move"]:
                self.shell.calls.append(list(cmd))
                return ph.Result(1, "qa → build loop exhausted (3)")
            return run(cmd, **kw)
        self.shell.run = refuse
        ph.tick(self.ctx)
        self.assertTrue(any(n.startswith(ph.MARK_EXCLUDED + "h-20261027-04 porteur=#4000")
                            for n in ph.marks(self.shell.items[3801])))
        # la recomposition n'est pas retenue par le refus
        self.assertTrue([c for c in self.shell.calls if "paquet-auto.py" in " ".join(c)])

    def test_rouge_apres_recomposition_ne_retire_plus(self):
        item = self.open_carrier()
        item["events"].append({"note": ph.MARK_ATTEMPT + "recomposition n=1 base=%s "
                               "par=autre:1 run=70 sans=3755" % HEAD2})
        self.shell.ci[4000] = {"runs": [red_run()]}
        self.assertIn("épuisé", ph.tick(self.ctx))
        self.assertFalse([c for c in self.shell.calls if "paquet-auto.py" in " ".join(c)])
        self.assertTrue(any(n.startswith(ph.MARK_RED + "77") for n in ph.marks(item)))
        # consigné une fois : le tic suivant ne réécrit pas au pilote
        mails = len(self.shell.mails)
        ph.tick(self.ctx)
        self.assertEqual(len(self.shell.mails), mails)

    def test_rouge_ambigu_non_concluant(self):
        self.open_carrier()
        self.shell.ci[4000] = {"runs": [red_run(best=(3801, 3807))]}
        self.assertIn("rouge consigné", ph.tick(self.ctx))
        self.assertFalse([c for c in self.shell.calls if "paquet-auto.py" in " ".join(c)])

    def test_vert_en_local_non_concluant(self):
        self.open_carrier()
        self.replay = "vert en local (1 test(s)) : rouge non reproduit"
        self.shell.ci[4000] = {"runs": [red_run()]}
        self.assertIn("vert en local", ph.tick(self.ctx))
        self.assertFalse([c for c in self.shell.calls if "paquet-auto.py" in " ".join(c)])


class EstimateGuardTest(Base):
    """B1 : la commande de création passe la vraie garde de `ameesh work add`."""

    def test_creation_passe_la_garde_l157_pour_un_agent(self):
        from ameesh import estimates, mesh_cli
        self.shell.items[4000] = carrier()
        ph.tick(self.ctx)
        args = mesh_cli.build_parser().parse_args(self.shell.added[0])
        self.assertEqual(args.actor, "mesh-design")
        saved = estimates.is_agent
        estimates.is_agent = lambda db, actor: True  # mesh-design est au registre
        try:
            minutes = estimates.parse(args.estimate) if args.estimate else None
            self.assertFalse(estimates.check(None, args.actor, minutes))
            with self.assertRaises(estimates.EstimateError):  # le témoin mord
                estimates.check(None, args.actor, None)
        finally:
            estimates.is_agent = saved
        self.assertEqual(minutes, 165)
        self.assertIn("#153989", args.estimate_source)


class ResumeTest(Base):
    """B2 : arrêt ou échec entre deux gestes, reprise sur le même porteur."""

    def unfrozen(self, *notes, created=NOW - 3600):
        item = carrier(sha="", created=created)
        item["events"] = [{"note": n, "created_ts": NOW - 3000 + i}
                          for i, n in enumerate(notes)]
        self.shell.items[4000] = item
        self.shell.lists["qa"] = [item]
        return item

    def autos(self):
        return [c for c in self.shell.calls if "paquet-auto.py" in " ".join(c)]

    def test_porteur_sans_tentative_repris(self):
        self.unfrozen()
        def frozen(cmd, _orig=self.shell.run, **kw):
            out = _orig(cmd, **kw)
            if "paquet-auto.py" in " ".join(cmd):
                self.shell.items[4000]["freeze"] = {"declared": {"sha": HEAD}}
            return out
        self.shell.run = frozen
        self.assertIn("ci-full demandée", ph.tick(self.ctx))
        # aucune marque : paquet-auto n'a jamais tourné, tentative 1 sans --force
        self.assertNotIn("--force", self.autos()[0])
        self.assertIn(ph.MARK_ATTEMPT + "composition n=1 base=-",
                      " ".join(ph.marks(self.shell.items[4000])))
        self.assertEqual(self.shell.ran("work", "add"), [])

    def test_porteur_tout_juste_cree_attend(self):
        self.unfrozen(created=NOW - 30)
        self.assertIn("en cours", ph.tick(self.ctx))
        self.assertEqual(self.autos(), [])

    def test_paquet_auto_refuse_une_composition_vivante_sans_echec(self):
        # #4329 : le refus « composition en cours » (rc 3) n'est pas un échec
        # de composition : aucune marque d'échec, aucun courrier.
        item = self.unfrozen()
        self.shell.auto_rc = ph.auto.IN_PROGRESS_RC
        self.shell.auto_out = "paquet-auto : composition en cours (hote:1)\n"
        out = ph.tick(self.ctx)
        self.assertIn("composition en cours", out)
        notes = ph.marks(item)
        self.assertTrue(notes)
        self.assertFalse([n for n in notes if n.startswith(ph.MARK_FAILED)])
        self.assertEqual(self.shell.mails, [])

    def test_tentative_vivante_sur_cet_hote_attend(self):
        self.ctx.alive = lambda pid: True
        self.unfrozen(ph.MARK_ATTEMPT + "composition n=1 base=- par=%s:99" % self.ctx.host)
        self.assertIn("en cours", ph.tick(self.ctx))
        self.assertEqual(self.autos(), [])

    def test_tentative_vivante_sous_un_autre_nom_de_script_attend(self):
        # #4501 : le pid de la tentative est vivant mais sa ligne de commande
        # ne nomme pas `paquet-horaire` (h-12 : `scripts/.h12-tick.py`) ; le
        # tic ne recompose pas le même porteur.
        proc = subprocess.Popen(["sleep", "60"])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.terminate)
        item = carrier(notes=(ph.MARK_ATTEMPT
                              + "composition n=1 base=%s par=%s:%d"
                              % (HEAD, self.ctx.host, proc.pid),))
        self.shell.items[4000] = item
        self.shell.lists["qa"] = [item]
        self.assertIn("en cours", ph.tick(self.ctx))
        self.assertEqual(self.autos(), [])

    def test_ci_full_en_cours_sur_la_tete_gelee_suspend_la_reprise(self):
        # #4501 : la tentative est morte, mais la tête poussée (gelée) est sous
        # ci-full ; un second composeur ne recompose pas par-dessus.
        self.ctx.alive = lambda pid: False
        item = carrier(notes=(ph.MARK_ATTEMPT + "composition n=1 base=%s par=%s:99"
                              % (HEAD, self.ctx.host),))
        self.shell.items[4000] = item
        self.shell.lists["qa"] = [item]
        self.shell.ci[4000] = {"runs": [dict(red_run(), status="running")]}
        out = ph.tick(self.ctx)
        self.assertIn("ci-full en cours", out)
        self.assertEqual(self.autos(), [])
        self.assertEqual(self.shell.ran("ci", "run"), [])

    def test_ci_full_en_cours_sur_la_tete_de_branche_vue_suspend_la_reprise(self):
        # #4501 : une poussée plus récente, pas encore gelée (fenêtre entre
        # `push` et `frozen`), est déjà sous CI ; la reprise attend.
        self.ctx.alive = lambda pid: False
        item = carrier(notes=(ph.MARK_ATTEMPT + "composition n=1 base=%s par=%s:99"
                              % (HEAD, self.ctx.host),))
        item["branch_head"] = HEAD2
        self.shell.items[4000] = item
        self.shell.lists["qa"] = [item]
        self.shell.ci[4000] = {"runs": [dict(red_run(), head_sha=HEAD2,
                                             status="running")]}
        out = ph.tick(self.ctx)
        self.assertIn("ci-full en cours", out)
        self.assertEqual(self.autos(), [])

    def test_recomposition_du_pilote_refusee_pendant_une_ci_full(self):
        self.shell.items[4000] = carrier()
        self.shell.ci[4000] = {"runs": [dict(red_run(), status="running")]}
        with self.assertRaisesRegex(ph.InputError, "ci-full en cours"):
            ph.pilot_recompose(self.ctx, 4000, [], [])
        self.assertEqual(self.autos(), [])

    def test_tentative_morte_sur_cet_hote_reprise(self):
        self.ctx.alive = lambda pid: False
        item = self.unfrozen(
            ph.MARK_ATTEMPT + "composition n=1 base=- par=%s:99" % self.ctx.host)
        self.shell.auto_rc = 1
        with self.assertRaises(ph.HoraireError):
            ph.tick(self.ctx)
        self.assertIn("--force", self.autos()[0])
        notes = ph.marks(item)
        self.assertIn(ph.MARK_FAILED + "composition n=2 base=- rc=1", notes)
        # l'échec consigné est repris au tic suivant, sans attendre de délai
        self.shell.auto_rc = 0
        ph.tick(self.ctx)
        tried = [n for n in ph.marks(item) if n.startswith(ph.MARK_ATTEMPT)]
        self.assertIn("composition n=3", tried[-1])

    def test_autre_hote_attend_le_delai(self):
        item = self.unfrozen(ph.MARK_ATTEMPT + "composition n=1 base=- par=autre:9")
        self.assertIn("en cours", ph.tick(self.ctx))
        item["events"][0]["created_ts"] = NOW - ph.ATTEMPT_STALE - 1
        self.shell.auto_out = "aucune unité composée\n"
        self.assertIn("rien composé", ph.tick(self.ctx))
        self.assertEqual(len(self.autos()), 1)

    def test_autre_hote_epoque_ancienne_reprise_au_tic_suivant(self):
        # #4087 : h-46, tentative d'agents-2 (époque 277), bail passé à 279
        self.ctx.epoch = 279
        self.unfrozen(ph.MARK_ATTEMPT + "composition n=1 base=- par=autre:9 epoque=277")
        self.shell.auto_out = "aucune unité composée\n"
        self.assertIn("rien composé", ph.tick(self.ctx))
        auto = self.autos()[0]
        self.assertIn("--force", auto)
        tried = [n for n in ph.marks(self.shell.items[4000])
                 if n.startswith(ph.MARK_ATTEMPT)]
        self.assertTrue(tried[-1].startswith(ph.MARK_ATTEMPT + "composition n=2 "))
        self.assertTrue(tried[-1].endswith(" epoque=279"))

    def test_autre_hote_meme_epoque_reste_en_cours(self):
        self.ctx.epoch = 279
        item = self.unfrozen(
            ph.MARK_ATTEMPT + "composition n=1 base=- par=autre:9 epoque=279")
        item["events"][0]["created_ts"] = NOW - ph.ATTEMPT_STALE - 1
        self.assertIn("en cours", ph.tick(self.ctx))
        self.assertEqual(self.autos(), [])

    def test_autre_hote_sans_epoque_du_tic_attend_le_delai(self):
        self.unfrozen(ph.MARK_ATTEMPT + "composition n=1 base=- par=autre:9 epoque=277")
        self.assertIn("en cours", ph.tick(self.ctx))
        self.assertEqual(self.autos(), [])

    def test_meme_hote_pid_vivant_malgre_l_epoque(self):
        self.ctx.epoch = 279
        self.ctx.alive = lambda pid: True
        self.unfrozen(ph.MARK_ATTEMPT + "composition n=1 base=- par=%s:99 epoque=277"
                      % self.ctx.host)
        self.assertIn("en cours", ph.tick(self.ctx))
        self.assertEqual(self.autos(), [])

    def test_marque_recomposition_avec_epoque_relue(self):
        found = ph.attempts({"events": [{"note": ph.MARK_ATTEMPT + "recomposition n=1 "
                                         "base=%s par=autre:9 run=77 sans=3801,3807 "
                                         "epoque=12" % HEAD}]})
        self.assertEqual((found[0].run_id, found[0].exclude, found[0].epoch),
                         ("77", [3801, 3807], 12))

    def test_epoque_du_bail_lue_a_l_environnement(self):
        with mock.patch.dict(os.environ, {"AMEESH_LEASE_EPOCH": "279"}):
            self.assertEqual(ph.lease_epoch(), 279)
        with mock.patch.dict(os.environ, {"AMEESH_LEASE_EPOCH": "x"}):
            self.assertEqual(ph.lease_epoch(), 0)

    def test_reprise_epuisee_signalee_une_fois(self):
        notes = []
        for n in range(1, ph.MAX_ATTEMPTS + 1):
            notes += [ph.MARK_ATTEMPT + "composition n=%d base=- par=autre:9" % n,
                      ph.MARK_FAILED + "composition n=%d base=- rc=1" % n]
        self.unfrozen(*notes)
        self.assertIn("épuisée", ph.tick(self.ctx))
        self.assertEqual(self.autos(), [])
        self.assertEqual(len(self.shell.mails), 1)
        self.assertIn("épuisée, déjà signalée", ph.tick(self.ctx))
        self.assertEqual(len(self.shell.mails), 1)

    def test_recomposition_interrompue_reprise_sans_reecrire_aux_auteurs(self):
        item = carrier(notes=(ph.MARK_CI + HEAD,))
        item["events"] = [{"note": ph.MARK_CI + HEAD, "created_ts": NOW - 900},
                          {"note": ph.MARK_ATTEMPT + "recomposition n=1 base=%s par=autre:9 "
                           "run=77 sans=3801" % HEAD, "created_ts": NOW - 800},
                          {"note": ph.MARK_FAILED + "recomposition n=1 base=%s rc=1" % HEAD,
                           "created_ts": NOW - 700}]
        self.shell.items[4000] = item
        self.shell.lists["qa"] = [item]
        self.shell.ci[4000] = {"runs": [red_run()]}
        ph.tick(self.ctx)
        auto = self.autos()[0]
        self.assertIn("--force", auto)
        self.assertEqual(auto[auto.index("--exclude") + 1], "3801")
        # #4581 : une reprise de recomposition garde la même composition de
        # départ, sans réadmettre les lots prêts entre-temps.
        self.assertEqual(auto[auto.index("--keep-composition") + 1], HEAD)
        self.assertIn(ph.MARK_ATTEMPT + "recomposition n=2 base=%s" % HEAD,
                      " ".join(ph.marks(item)))
        self.assertNotIn("mty-amh-impl-02", [to for to, _ in self.shell.mails])
        self.assertFalse(any(n.startswith(ph.MARK_RED) for n in ph.marks(item)))


class ClassifyTest(unittest.TestCase):
    def test_cas(self):
        self.assertTrue(ph.classify(red_run()).deterministic)
        self.assertEqual(ph.classify(red_run()).lots, [3801])
        self.assertFalse(ph.classify(red_run(status="cancelled")).deterministic)
        self.assertFalse(ph.classify(red_run(part="job")).deterministic)
        self.assertFalse(ph.classify(red_run(attribution=False)).deterministic)
        inherited = red_run()
        inherited["failures"][0]["inherited"] = True
        self.assertFalse(ph.classify(inherited).deterministic)

    def test_unites_du_manifeste(self):
        units = ph.composition_units(RESOLVED)
        self.assertEqual([u.resolution for u in units], [True, False])
        self.assertEqual(units[0].lots, [3779, 3755])

    def test_heure_locale(self):
        start = ph.hour_start(NOW, ZONE)
        self.assertEqual(datetime.fromtimestamp(start, ZONE).minute, 0)
        self.assertLessEqual(NOW - start, 3600)


class BestWeightTest(unittest.TestCase):
    """`best` est un poids (#3801), pas un booléen."""

    def run_with(self, *weights):
        run = red_run()
        run["attribution"]["tests"][0]["units"] = [
            {"lot": 3801 + i, "best": w} for i, w in enumerate(weights)]
        return ph.classify(run)

    def test_poids_distincts_un_seul_meilleur(self):
        verdict = self.run_with(4, 2, 1)
        self.assertTrue(verdict.deterministic)
        self.assertEqual(verdict.lots, [3801])

    def test_egalite_et_preuve_faible(self):
        self.assertFalse(self.run_with(4, 4).deterministic)
        self.assertIn("faible", self.run_with(2, 1).why)


class FreshTest(unittest.TestCase):
    """Revue cx2 : un clone qui n'est pas à la tête exacte de la cible n'écrit rien."""

    def ctx(self, local, remote, *, fetch_rc=0, dirty=""):
        calls = []

        class Git:
            def run(self, cmd, **kw):
                calls.append(cmd)
                if "fetch" in cmd:
                    return ph.Result(fetch_rc, "", "réseau" if fetch_rc else "")
                if "rev-parse" in cmd:
                    return ph.Result(0, (local if cmd[-1] == "HEAD^{commit}" else remote) + "\n")
                if "status" in cmd:
                    return ph.Result(0, dirty)
                raise AssertionError(cmd)
        return ph.Context(repo=ROOT, target="main", project="ameesh", pilot="p",
                          actor="p", shell=Git(), zone=ZONE), calls

    def test_tete_exacte(self):
        ctx, _ = self.ctx(HEAD, HEAD)
        ph.check_fresh(ctx)

    def test_refus(self):
        for kw in ({"local": HEAD, "remote": HEAD2}, {"local": HEAD, "remote": HEAD, "fetch_rc": 1},
                   {"local": HEAD, "remote": HEAD, "dirty": " M scripts/paquet-horaire.py"}):
            ctx, _ = self.ctx(**kw)
            with self.assertRaises(ph.HoraireError):
                ph.check_fresh(ctx)

    def test_main_refuse_sans_aucun_geste(self):
        saved = (ph.check_fresh, ph.tick, ph.align_schema, ph.read_pause)
        ph.read_pause = lambda project, target, **kw: None
        ticked = []
        def stale(ctx):
            raise ph.HoraireError("code périmé")
        ph.check_fresh, ph.tick = stale, lambda ctx: ticked.append(ctx) or "x"
        ph.align_schema = lambda ctx: ticked.append("schéma")
        try:
            with mock.patch.dict(os.environ, {}, clear=False):
                self.assertEqual(ph.main(["tick", "--repo", str(ROOT)]), 1)
        finally:
            ph.check_fresh, ph.tick, ph.align_schema, ph.read_pause = saved
        self.assertEqual(ticked, [])


class SchemaTest(unittest.TestCase):
    """#4075 : le tic pose les migrations additives de la tête avant tout geste ;
    un refus nomme les migrations, ouvre un lot d'empreinte et écrit au pilote
    et au bureau, une fois par empreinte."""

    ADDITIVE = "0499_operator_pause_until: additive\n"

    def ctx(self, read=(0, ""), apply=(0, "1 migration(s) applied\n"), *,
            dry_run=False, created=True):
        calls, mails = [], []

        class Sh:
            def run(self, cmd, input_text=None, **kw):
                calls.append(cmd)
                if cmd[0] == "agent-mail":
                    mails.append((cmd[2], cmd, input_text))
                    return ph.Result(0)
                if cmd[0] == "ameesh":
                    return ph.Result(0, json.dumps({"created": created, "lot": 4100}))
                rc, out = read if cmd[-1] == "--dry-run" else apply
                return ph.Result(rc, out, "")
        ctx = ph.Context(repo=ROOT, target="main", project="ameesh", pilot="mesh-design",
                         actor="mesh-design", shell=Sh(), python="python3", zone=ZONE,
                         dry_run=dry_run)
        return ctx, calls, mails

    def test_a_jour_une_seule_lecture(self):
        ctx, calls, mails = self.ctx(read=(0, "schema up to date: no migration to apply\n"))
        ph.align_schema(ctx)
        self.assertEqual([c[-1] for c in calls], ["--dry-run"])
        self.assertEqual((ctx.log, mails), ([], []))

    def test_pose_additive_depuis_le_src_du_clone(self):
        ctx, calls, mails = self.ctx(read=(0, self.ADDITIVE))
        ph.align_schema(ctx)
        self.assertEqual(calls[1][:2], ["python3", "-c"])
        self.assertEqual(calls[1][3:], [str(ROOT / "src"), "--additive-only"])
        self.assertTrue(any("0499_operator_pause_until" in line for line in ctx.log))
        self.assertEqual(mails, [])

    def test_non_additive_jamais_posee(self):
        ctx, calls, mails = self.ctx(read=(0, self.ADDITIVE + "0500_x: NOT additive — drop column\n"))
        with self.assertRaises(ph.HoraireError) as caught:
            ph.align_schema(ctx)
        self.assertNotIn("--additive-only", [c[-1] for c in calls])
        self.assertIn("0500_x", str(caught.exception))
        self.assertEqual([m[0] for m in mails], ["mesh-design", "bureau"])

    def test_droit_ddl_absent_nomme_et_alerte(self):
        ctx, calls, mails = self.ctx(
            read=(0, self.ADDITIVE),
            apply=(1, "migration failed: permission denied for table work_items\n"))
        with self.assertRaises(ph.HoraireError) as caught:
            ph.align_schema(ctx)
        text = str(caught.exception)
        self.assertIn("0499_operator_pause_until", text)
        self.assertIn("droit DDL", text)
        fingerprint = [c for c in calls if c[0] == "ameesh"][0]
        self.assertEqual(fingerprint[1:4], ["work", "fingerprint", "open"])
        self.assertIn("paquet-horaire:schema:droit-ddl:0499_operator_pause_until", fingerprint)
        self.assertEqual([m[0] for m in mails], ["mesh-design", "bureau"])
        self.assertIn("4100", mails[0][1])
        self.assertIn("droit DDL", mails[0][2])

    def test_empreinte_deja_ouverte_aucun_courrier(self):
        ctx, calls, mails = self.ctx(read=(0, self.ADDITIVE), apply=(1, "permission denied"),
                                     created=False)
        with self.assertRaises(ph.HoraireError):
            ph.align_schema(ctx)
        self.assertEqual(mails, [])
        notes = [c for c in calls if c[:3] == ["ameesh", "work", "note"]]
        self.assertEqual(len(notes), 1)
        self.assertIn("permission denied", notes[0][-1])

    def test_verrou_reessaye_une_seule_fois_avant_alerte(self):
        for second in (ph.Result(0, "applied"), ph.Result(1, "[SQLSTATE 55P03] lock")):
            with self.subTest(second=second.returncode):
                ctx, calls, mails = self.ctx(read=(0, self.ADDITIVE))
                results = iter((ph.Result(0, self.ADDITIVE),
                                ph.Result(1, "migration failed [SQLSTATE 55P03]: lock"), second))
                with mock.patch.object(ph, "_migrate", side_effect=lambda *a: next(results)) as migrate:
                    if second.returncode:
                        with self.assertRaises(ph.HoraireError):
                            ph.align_schema(ctx)
                    else:
                        ph.align_schema(ctx)
                self.assertEqual(migrate.call_count, 3)
                self.assertEqual(len(mails), 2 if second.returncode else 0)

    def test_autre_erreur_aucun_reessai_et_detail_dans_lot(self):
        ctx, calls, mails = self.ctx(read=(0, self.ADDITIVE),
                                     apply=(1, "migration failed [SQLSTATE 42501]: permission denied"))
        with self.assertRaises(ph.HoraireError):
            ph.align_schema(ctx)
        self.assertEqual(sum(c[-1] == "--additive-only" for c in calls), 1)
        notes = [c for c in calls if c[:3] == ["ameesh", "work", "note"]]
        self.assertEqual(len(notes), 1)
        self.assertIn("SQLSTATE 42501", notes[0][-1])
        self.assertEqual(notes[0][-1], mails[0][2])

    def test_essai_lit_seulement(self):
        ctx, calls, mails = self.ctx(read=(0, self.ADDITIVE), dry_run=True)
        ph.align_schema(ctx)
        self.assertEqual([c[-1] for c in calls], ["--dry-run"])
        self.assertEqual(mails, [])

    def test_code_reel_charge_le_src_donne(self):
        # Le code passé à `-c` importe `ameesh.cli` depuis argv[1] ; `--help`
        # n'exécute jamais `migrate` (cli.main), donc aucune base n'est lue.
        proc = subprocess.run([sys.executable, "-I", "-c", ph.MIGRATE_CODE,
                               str(ROOT / "src"), "--help"],
                              capture_output=True, text=True, timeout=120, cwd=str(ROOT))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("agent-mail migrate", proc.stdout)


class GitIdentityTest(unittest.TestCase):
    """#3916 : un HOME sans .gitconfig, la composition commite quand même."""

    def test_main_pose_l_identite_du_pilote(self):
        saved = (ph.check_fresh, ph.tick, ph.align_schema, ph.read_pause)
        ph.read_pause = lambda project, target, **kw: None
        seen = []
        ph.check_fresh = ph.align_schema = lambda ctx: None
        ph.tick = lambda ctx: seen.append(
            (os.environ.get("GIT_AUTHOR_NAME"),
             os.environ.get("GIT_COMMITTER_EMAIL"))) or "x"
        # La CI n'a pas de canon compilé (config vide) : le fuseau est simulé,
        # comme check_fresh et tick, pour ne tester que l'identité posée.
        from ameesh import config as config_mod, mechanics
        zone = mock.Mock(timezone="Europe/Paris")
        try:
            with mock.patch.dict(os.environ, {}, clear=False), \
                    mock.patch.object(config_mod, "load", lambda *a, **k: {}), \
                    mock.patch.object(mechanics, "configured", lambda *a, **k: zone):
                for key in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                            "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
                    os.environ.pop(key, None)
                self.assertEqual(ph.main(["tick", "--repo", str(ROOT),
                                          "--pilot", "pilote-x"]), 0)
        finally:
            ph.check_fresh, ph.tick, ph.align_schema, ph.read_pause = saved
        self.assertEqual(seen, [("pilote-x", "pilote-x@ameesh.invalid")])

    def test_commit_sans_gitconfig(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {}, clear=False):
            home = Path(tmp) / "home"
            home.mkdir()
            repo = Path(tmp) / "repo"
            for key in list(os.environ):
                if key.startswith("GIT_"):
                    os.environ.pop(key)
            os.environ.update({"HOME": str(home), "XDG_CONFIG_HOME": str(home),
                               "GIT_CONFIG_NOSYSTEM": "1",
                               "EMAIL": "", "GIT_CONFIG_GLOBAL": os.devnull})
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            env = ph.pose_git_identity("pilote-x")
            done = subprocess.run(
                ["git", "-C", str(repo), "-c", "user.useConfigOnly=true",
                 "commit", "-q", "--allow-empty", "-m", "témoin"],
                capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            who = subprocess.run(
                ["git", "-C", str(repo), "log", "-1", "--format=%an <%ae> / %cn <%ce>"],
                capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(env["GIT_COMMITTER_NAME"], "pilote-x")
        self.assertEqual(who, "pilote-x <pilote-x@ameesh.invalid> / "
                              "pilote-x <pilote-x@ameesh.invalid>")


class ExcludeOptionTest(unittest.TestCase):
    def test_paquet_auto_accepte_exclude(self):
        parsed = []
        original = ph.auto.compose_packet
        ph.auto.compose_packet = lambda args: parsed.append(args.exclude) or 0
        try:
            rc = ph.auto.main(["--name", "h-x", "--carrier", "9",
                               "--exclude", "3801", "--exclude", "3755"])
        finally:
            ph.auto.compose_packet = original
        self.assertEqual(rc, 0)
        self.assertEqual(parsed, [[3801, 3755]])

    def test_paquet_auto_accepte_exclude_reason_apparie(self):
        # #4201 : --exclude-reason est répétable et suit l'ordre des --exclude.
        parsed = []
        original = ph.auto.compose_packet
        ph.auto.compose_packet = lambda args: parsed.append(
            (args.exclude, args.exclude_reason)) or 0
        try:
            rc = ph.auto.main([
                "--name", "h-x", "--carrier", "9",
                "--exclude", "3801", "--exclude-reason", "B de relecture",
                "--exclude", "3755", "--exclude-reason", "rouge local"])
        finally:
            ph.auto.compose_packet = original
        self.assertEqual(rc, 0)
        self.assertEqual(parsed, [([3801, 3755],
                                   ["B de relecture", "rouge local"])])

    def test_paquet_auto_courrier_avec_expediteur(self):
        seen = []
        original = ph.auto.run_command
        ph.auto.run_command = lambda cmd, **kw: seen.append(cmd) or subprocess.CompletedProcess(cmd, 0, b"", b"")
        try:
            ph.auto.send_mail("mesh-design", 9, "corps", "mesh-design")
            ph.auto.send_mail("mesh-design", 9, "corps")
        finally:
            ph.auto.run_command = original
        self.assertEqual(seen[0][-2:], ["--from", "mesh-design"])
        self.assertNotIn("--from", seen[1])

    def test_paquet_auto_expediteur_par_defaut_le_role(self):
        parsed = []
        original = ph.auto.compose_packet
        ph.auto.compose_packet = lambda args: parsed.append(args.sender) or 0
        env = {k: v for k, v in os.environ.items() if k != "AGENT_MAIL_NAME"}
        env["AMEESH_ROLE"] = "mesh-design"
        try:
            with mock.patch.dict(os.environ, env, clear=True):
                ph.auto.main(["--name", "h-x", "--carrier", "9"])
            ph.auto.main(["--name", "h-x", "--carrier", "9", "--from", "x"])
        finally:
            ph.auto.compose_packet = original
        self.assertEqual(parsed, ["mesh-design", "x"])


if __name__ == "__main__":
    unittest.main()
