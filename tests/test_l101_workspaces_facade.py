# SPDX-License-Identifier: AGPL-3.0-only
"""L101 T2/T3 — le ménage et les dossiers de travail derrière `ameesh.workspaces`.

Le runner ne connaît plus `menage` ni `housekeeping` : il passe par la façade
`ameesh.workspaces` (`policy`, `before_turn`, `after_turn`, `periodic_pass`,
`tmpfs_pressure`, `attach_env`). Depuis L101 T3, la façade porte aussi la
résolution du dossier d'un lot (`resolve`, `primary_workdir`), l'adoption
bornée d'un worktree déplacé (0018), le différé par lot (#811) et la règle de
courrier partagée avec le hook (`mail_hold`) : le runner garde l'état de délai
et l'appel. Ce test fige la séparation (aucun import interdit dans le runner,
pas de dépendance à l'envers) et ces règles, pour qu'une reprise du couplage
ne passe pas inaperçue.
"""
from __future__ import annotations

import ast
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from ameesh import housekeeping, lot_workdir, runner, workspaces

RACINE = pathlib.Path(__file__).resolve().parents[1]
RUNNER = RACINE / "src" / "ameesh" / "runner.py"
FACADE = RACINE / "src" / "ameesh" / "workspaces.py"

#: Modules que le runner ne doit plus importer (L101 T2).
INTERDITS = frozenset(("menage", "housekeeping"))


def _imports(chemin: pathlib.Path) -> set:
    """Noms importés par un module : module, alias et nom relatif.

    Couvre les trois formes : `import ameesh.menage`, `from .menage import x`
    et `from . import menage as menage_mod` (le nom relatif compte : c'est la
    forme utilisée par le runner avant ce lot)."""
    tree = ast.parse(chemin.read_text(encoding="utf-8"))
    noms = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            noms.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                module = "." * node.level + module
            if module:
                noms.add(module)
            for alias in node.names:
                noms.add(alias.name)
                if module:
                    noms.add(module + "." + alias.name)
    return noms


def _touche(nom: str, cibles: frozenset) -> bool:
    """Un nom importé désigne-t-il un de ces modules (ou un de ses membres) ?"""
    return any(partie in cibles for partie in nom.lstrip(".").split("."))


class RunnerImportsTest(unittest.TestCase):
    def test_runner_n_importe_ni_menage_ni_housekeeping(self):
        fautifs = sorted(nom for nom in _imports(RUNNER) if _touche(nom, INTERDITS))
        self.assertEqual(
            [], fautifs,
            "runner.py importe encore le ménage directement (%s) : passer par "
            "ameesh.workspaces (L101 T2)" % ", ".join(fautifs))

    def test_runner_importe_la_facade(self):
        self.assertTrue(
            any(_touche(nom, frozenset(("workspaces",))) for nom in _imports(RUNNER)),
            "runner.py ne passe pas par la façade ameesh.workspaces")

    def test_la_facade_ne_depend_pas_du_runner(self):
        """Pas de cycle : le runner importe la façade, jamais l'inverse."""
        fautifs = sorted(nom for nom in _imports(FACADE)
                         if _touche(nom, frozenset(("runner",))))
        self.assertEqual([], fautifs)


class DelegationTest(unittest.TestCase):
    def test_policy_delegue_a_effective(self):
        for canons, hote in (([], ""), ([], "pc")):
            with self.subTest(canons=canons, hote=hote):
                self.assertEqual(workspaces.policy(canons, hote),
                                 housekeeping.effective(canons, hote))
        self.assertEqual(workspaces.default_policy(), housekeeping.Policy())

    def test_before_turn_delegue_a_turn_begin(self):
        cfg, pol, env = object(), housekeeping.Policy(), {"TMPDIR": "/tmp/a1"}
        sentinelle = object()
        with mock.patch.object(housekeeping.Turn, "begin",
                               return_value=sentinelle) as begin:
            rendu = workspaces.before_turn(cfg, pol, "a1", "/work/a1",
                                           new_session=True, base_env=env)
        self.assertIs(rendu, sentinelle)
        begin.assert_called_once_with(cfg, pol, "a1", "/work/a1",
                                      new_session=True, base_env=env)

    def test_after_turn_conclut_puis_recolte_les_conteneurs_du_tour(self):
        tour = mock.Mock(off=False)
        tour.end.return_value = [{"kind": "tmp"}]
        with mock.patch.object(housekeeping, "reap_containers",
                               return_value={"entries": [{"kind": "container"}]}) as reap:
            entrees = workspaces.after_turn(tour, lot="4355", turn_id="t1",
                                            db="db", host="pc", runtime="rt")
        self.assertEqual(entrees, [{"kind": "tmp"}, {"kind": "container"}])
        tour.end.assert_called_once_with(lot="4355", turn_id="t1", db="db", host="pc")
        reap.assert_called_once_with("rt", "db", "pc", only_turn="t1")

    def test_after_turn_sans_conteneurs_si_menage_eteint(self):
        tour = mock.Mock(off=True)
        tour.end.return_value = []
        with mock.patch.object(housekeeping, "reap_containers") as reap:
            self.assertEqual(workspaces.after_turn(tour, lot=None, turn_id="t1",
                                                   db=None, host="pc"), [])
        reap.assert_not_called()

    def test_periodic_pass_delegue_a_run_pass(self):
        cfg, db, pol = object(), object(), housekeeping.Policy()
        with mock.patch.object(housekeeping, "run_pass",
                               return_value={"status": "ok"}) as run:
            rapport = workspaces.periodic_pass(
                cfg, db, pol, host="pc", actor="runner:1", busy=True,
                agents_in_turn={"a1"}, runtime="rt", min_interval_s=600.0,
                free_target=1234, artifact_scan=False)
        self.assertEqual(rapport, {"status": "ok"})
        run.assert_called_once_with(
            cfg, db, pol, host="pc", actor="runner:1", busy=True,
            agents_in_turn={"a1"}, runtime="rt", min_interval_s=600.0,
            free_target=1234, artifact_scan=False)

    def test_tmpfs_pressure_delegue_a_tmpfs_hint(self):
        pol = housekeeping.Policy()
        top = [{"path": "/tmp/x", "bytes": 42}]
        self.assertEqual(workspaces.tmpfs_pressure(top, pol),
                         housekeeping.tmpfs_hint(top, pol))

    def test_attach_env_verrouille_le_dossier_et_rend_l_environnement(self):
        cfg = mock.Mock(host="pc")
        base, holds = {"KEEP": "1"}, []
        verrou = object()
        with mock.patch("ameesh.canon.load_configured", return_value=[]), \
                mock.patch.object(housekeeping, "hold_tmp",
                                  return_value=verrou) as hold, \
                mock.patch.object(housekeeping, "turn_env",
                                  return_value={"TMPDIR": "/tmp/a1"}) as env:
            rendu = workspaces.attach_env(cfg, "a1", base, holds)
        self.assertEqual(rendu, {"TMPDIR": "/tmp/a1"})
        self.assertEqual(holds, [verrou])
        hold.assert_called_once_with(cfg, housekeeping.Policy(), "a1", new_session=False)
        env.assert_called_once_with(cfg, housekeeping.Policy(), "a1", base, hold=verrou)

    def test_attach_env_eteint_ne_pose_rien(self):
        cfg = mock.Mock(host="pc")
        with mock.patch("ameesh.canon.load_configured", return_value=[]):
            self.assertEqual(
                workspaces.attach_env(cfg, "a1", {"AMEESH_HOUSEKEEPING": "off"}, []), {})


# -- L101 T3 : dossier de travail, worktrees de lot, différé, mail_hold ------

class ResolutionTest(unittest.TestCase):
    """`workspaces.resolve` : la règle de `_resolve_workdir`, sortie du runner."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="l101-workdir-")
        self.addCleanup(self.tmp.cleanup)
        self.cfg, self.db = object(), object()
        self.primary = os.path.join(self.tmp.name, "primary")
        os.makedirs(self.primary)
        self.agent = {"name": "worker", "cwd": self.primary}

    def resoudre(self, *, names=("alpha", "beta"), project="", lot="7",
                 destination=None, destination_error=None, **kwargs):
        if destination_error is not None:
            destination_mock = mock.patch.object(
                workspaces.lot_workdir, "destination", side_effect=destination_error)
        else:
            destination_mock = mock.patch.object(
                workspaces.lot_workdir, "destination", return_value=destination)
        with mock.patch.object(workspaces.projects, "agent_projects",
                               return_value=list(names)), \
                mock.patch.object(workspaces.lot_workdir, "project",
                                  return_value=project) as project_mock, \
                destination_mock as destination_mock:
            rendu = workspaces.resolve(self.cfg, self.db, self.agent, lot, **kwargs)
        return rendu, project_mock, destination_mock

    def test_primaire_utilise_le_cwd_du_registre(self):
        rendu, project_mock, _ = self.resoudre(names=["alpha"])
        self.assertEqual(rendu, workspaces.Workdir(self.primary))
        project_mock.assert_not_called()  # N3 : aucun accès base en mono-projet

    def test_primaire_absent_appelle_l_adoption_bornee(self):
        self.agent["cwd"] = os.path.join(self.tmp.name, "absent")
        adopte = os.path.join(self.tmp.name, "adopte")
        adopt = mock.Mock(return_value=adopte)
        rendu, _, _ = self.resoudre(names=["alpha"], adopt_worktree=adopt)
        self.assertEqual(rendu.cwd, adopte)
        self.assertFalse(rendu.secondary)
        adopt.assert_called_once_with()

    def test_lot_secondaire_utilise_le_dossier_de_son_projet(self):
        secondaire = os.path.join(self.tmp.name, "beta")
        os.makedirs(secondaire)
        rendu, project_mock, destination_mock = self.resoudre(
            project="beta", destination=secondaire)
        self.assertEqual(rendu, workspaces.Workdir(
            secondaire, True, "project beta: %r" % secondaire, secondaire))
        project_mock.assert_called_once_with(self.db, "7")
        destination_mock.assert_called_once_with(
            self.cfg, self.agent, "beta", canons=None)

    def test_lot_secondaire_absent_differe_ce_lot_seulement(self):
        absent = os.path.join(self.tmp.name, "beta-absent")
        rendu, _, _ = self.resoudre(project="beta", destination=absent)
        self.assertIsNone(rendu.cwd)
        self.assertTrue(rendu.secondary)
        self.assertEqual(rendu.path, absent)   # dernier chemin connu du différé
        self.assertIn("project beta", rendu.error)

    def test_lot_secondaire_refuse_garde_son_erreur(self):
        rendu, _, _ = self.resoudre(
            project="beta",
            destination_error=lot_workdir.WorkdirError("Host work root absent"))
        self.assertIsNone(rendu.cwd)
        self.assertTrue(rendu.secondary)
        self.assertIsNone(rendu.path)
        self.assertEqual(rendu.error, "project beta: Host work root absent")

    def test_lot_secondaire_medie_est_refuse(self):
        rendu, _, destination_mock = self.resoudre(project="beta", mediated=True)
        self.assertEqual(rendu, workspaces.Workdir(
            None, True, "mediated executor has no per-project depot"))
        destination_mock.assert_not_called()

    def test_primaire_medie_rend_le_depot(self):
        rendu, project_mock, _ = self.resoudre(
            names=["alpha"], mediated=True, mediated_workdir=lambda: "/depot")
        self.assertEqual(rendu.cwd, "/depot")
        self.assertFalse(rendu.secondary)
        project_mock.assert_not_called()

    def test_lot_inconnu_ou_hors_perimetre_reste_au_primaire(self):
        for project in ("", "gamma"):
            with self.subTest(project=project):
                rendu, _, _ = self.resoudre(project=project)
                self.assertEqual(rendu, workspaces.Workdir(self.primary))


class AdoptionTest(unittest.TestCase):
    """`workspaces.adopt_moved_worktree` : l'adoption bornée de 0018."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="l101-adoption-")
        self.addCleanup(self.tmp.cleanup)
        self.db = object()
        self.root = os.path.join(self.tmp.name, "racine")
        self.candidat = os.path.join(self.root, "manaty", "wt")
        os.makedirs(os.path.join(self.candidat, ".git"))
        self.agent = {"name": "worker", "cwd": os.path.join(self.root, "absent")}
        self.marker = os.path.join(self.tmp.name, "worktree.json")
        self.ecrire_marker("/depot/.git", "lot/x")

    def ecrire_marker(self, common, branche, chemin=None):
        with open(chemin or self.marker, "w", encoding="utf-8") as fh:
            json.dump({"git_common_dir": common, "branch": branche}, fh)

    def adopter(self, *, used=False, marker=None, roots=None):
        rapport = []
        with mock.patch.object(
                workspaces, "worktree_marker",
                side_effect=lambda cwd: {"git_common_dir": "/depot/.git",
                                         "branch": "lot/x"}), \
                mock.patch.object(workspaces.registry, "cwd_used",
                                  return_value=used) as used_mock, \
                mock.patch.object(workspaces.registry, "upsert") as upsert:
            trouve = workspaces.adopt_moved_worktree(
                self.db, self.agent, marker_path=marker or self.marker,
                roots=roots or [self.root], report=rapport.append)
        return trouve, rapport, used_mock, upsert

    def test_candidat_unique_adopte_et_met_a_jour_le_registre(self):
        trouve, rapport, used_mock, upsert = self.adopter()
        self.assertEqual(trouve, self.candidat)
        self.assertEqual(self.agent["cwd"], self.candidat)
        used_mock.assert_called_once_with(self.db, self.candidat, "worker")
        upsert.assert_called_once_with(self.db, "worker", cwd=self.candidat)
        self.assertEqual(rapport, ["moved working directory adopted: %s" % self.candidat])

    def test_candidat_occupe_refuse(self):
        trouve, rapport, _, upsert = self.adopter(used=True)
        self.assertIsNone(trouve)
        self.assertNotEqual(self.agent["cwd"], self.candidat)
        upsert.assert_not_called()
        self.assertIn("already used by another agent", rapport[0])

    def test_deux_candidats_sont_ambigus(self):
        autre = os.path.join(self.root, "autre", "wt")
        os.makedirs(os.path.join(autre, ".git"))
        trouve, rapport, _, upsert = self.adopter()
        self.assertIsNone(trouve)
        upsert.assert_not_called()
        self.assertIn("ambiguous", rapport[0])

    def test_marqueur_absent_ou_sans_depot_refuse(self):
        self.assertIsNone(self.adopter(marker=os.path.join(self.tmp.name, "absent"))[0])
        vide = os.path.join(self.tmp.name, "vide.json")
        with open(vide, "w", encoding="utf-8") as fh:
            fh.write("{}")
        self.assertIsNone(self.adopter(marker=vide)[0])


class LotDeferralTest(unittest.TestCase):
    """#811 : le délai par lot et le retour du dossier, sortis du runner."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="l101-deferral-")
        self.addCleanup(self.tmp.cleanup)

    def test_delai_croissant_et_borne(self):
        self.assertEqual(workspaces.lot_deferral(None, now=100.0), (30.0, 130.0))
        self.assertEqual(workspaces.lot_deferral(30.0, now=100.0), (60.0, 160.0))
        self.assertEqual(workspaces.lot_deferral(400.0, now=100.0), (600.0, 700.0))
        self.assertEqual(workspaces.lot_deferral(600.0, now=100.0), (600.0, 700.0))

    def test_dossier_revenu_debloque_tout_de_suite(self):
        self.assertEqual(workspaces.lot_wait(self.tmp.name, 500.0, now=0.0),
                         workspaces.LotWait(False, True))

    def test_dossier_absent_attend_son_echeance(self):
        absent = os.path.join(self.tmp.name, "absent")
        self.assertEqual(workspaces.lot_wait(absent, 500.0, now=0.0),
                         workspaces.LotWait(True, False))
        self.assertEqual(workspaces.lot_wait(absent, 500.0, now=600.0),
                         workspaces.LotWait(False, False))


class MailHoldTest(unittest.TestCase):
    """#811 B1 : la règle partagée exécuteur/hook, sortie du runner."""

    def setUp(self):
        self.cfg, self.db = object(), object()
        self.agent = {"name": "worker"}

    def test_mono_projet_ne_lit_pas_la_base(self):
        with mock.patch.object(workspaces.projects, "agent_projects",
                               return_value=["alpha"]), \
                mock.patch.object(workspaces.lot_workdir, "project") as project:
            self.assertFalse(workspaces.mail_hold(self.cfg, self.db, self.agent, "7"))
        project.assert_not_called()

    def test_lot_secondaire_sans_dossier_retient(self):
        with mock.patch.object(workspaces.projects, "agent_projects",
                               return_value=["alpha", "beta"]), \
                mock.patch.object(workspaces.lot_workdir, "project",
                                  return_value="beta"), \
                mock.patch.object(workspaces.canon, "load_configured",
                                  return_value=[]), \
                mock.patch.object(workspaces.lot_workdir, "problem",
                                  return_value="missing") as problem:
            self.assertTrue(workspaces.mail_hold(self.cfg, self.db, self.agent, "7"))
        problem.assert_called_once_with(self.cfg, self.agent, "beta", canons=[])

    def test_lot_primaire_ou_inconnu_suit_le_flux_normal(self):
        for project in ("alpha", ""):
            with self.subTest(project=project):
                with mock.patch.object(workspaces.projects, "agent_projects",
                                       return_value=["alpha", "beta"]), \
                        mock.patch.object(workspaces.lot_workdir, "project",
                                          return_value=project), \
                        mock.patch.object(workspaces.lot_workdir,
                                          "problem") as problem:
                    self.assertFalse(
                        workspaces.mail_hold(self.cfg, self.db, self.agent, "7"))
                problem.assert_not_called()

    def test_lot_inconnu_suit_le_flux_normal(self):
        with mock.patch.object(workspaces.projects, "agent_projects",
                               return_value=["alpha", "beta"]), \
                mock.patch.object(workspaces.lot_workdir, "project",
                                  side_effect=lot_workdir.WorkdirError("unknown")):
            self.assertFalse(workspaces.mail_hold(self.cfg, self.db, self.agent, "7"))


class RunnerWorkdirDelegationTest(unittest.TestCase):
    """Le runner garde l'appel et l'état, pas la règle (L101 T3)."""

    def test_adoption_et_marqueur_vivent_dans_la_facade(self):
        self.assertFalse(hasattr(runner.AgentWorker, "_worktree_dirs"))
        self.assertIs(runner.worktree_marker, workspaces.worktree_marker)
        self.assertIs(runner.write_worktree_marker, workspaces.write_worktree_marker)

    def test_les_points_du_runner_appellent_la_facade(self):
        import inspect
        for methode, appel in (
                (runner.AgentWorker.adopt_moved_worktree,
                 "workspaces.adopt_moved_worktree"),
                (runner.AgentWorker._resolve_workdir, "workspaces.resolve"),
                (runner.AgentWorker._primary_workdir, "workspaces.primary_workdir"),
                (runner.AgentWorker._lot_workdir_deferred,
                 "workspaces.lot_deferral"),
                (runner.AgentWorker._lot_deferred, "workspaces.lot_wait"),
                (runner.AgentWorker._lot_workdir_blocked, "workspaces.mail_hold")):
            with self.subTest(appel=appel):
                self.assertIn(appel, inspect.getsource(methode))


class DevModuleRegistryTest(unittest.TestCase):
    """L101 T4 : la façade est obtenue par `registry.get("dev")`."""

    def test_dev_module_vient_du_registre_et_porte_la_facade(self):
        from ameesh.modules import dev, registry
        self.assertIs(workspaces.dev_module(), dev)
        self.assertIn("workspaces", registry.CAPABILITIES)
        self.assertIs(registry.get("dev").workspaces(), workspaces)

    def test_profil_nu_n_a_pas_de_module_dev(self):
        with mock.patch("ameesh.modules.registry._builtin", return_value={}):
            self.assertIsNone(workspaces.dev_module())


class BareWorkspacesProfileTest(unittest.TestCase):
    """L101 T4 : sans module `dev`, ménage et worktrees sont des no-ops documentés."""

    def setUp(self):
        self.cfg = mock.Mock(host="pc")
        self.pol = housekeeping.Policy()
        patch = mock.patch.object(workspaces, "dev_module", return_value=None)
        patch.start()
        self.addCleanup(patch.stop)
        self.tmp = tempfile.TemporaryDirectory(prefix="l101-bare-")
        self.addCleanup(self.tmp.cleanup)

    def test_before_turn_rend_un_tour_eteint(self):
        turn = workspaces.before_turn(self.cfg, self.pol, "a1", self.tmp.name,
                                      new_session=True, base_env={"KEEP": "1"})
        self.assertTrue(turn.off)
        self.assertEqual(turn.env, {})
        self.assertEqual(workspaces.after_turn(
            turn, lot="4357", turn_id="t1", db=object(), host="pc"), [])

    def test_passage_periodique_et_attach_sont_des_no_ops(self):
        self.assertEqual(
            workspaces.periodic_pass(self.cfg, object(), self.pol,
                                     host="pc", actor="runner:1"),
            {"status": "no-dev-module", "entries": []})
        holds = []
        self.assertEqual(workspaces.attach_env(self.cfg, "a1", {"KEEP": "1"}, holds), {})
        self.assertEqual(holds, [])

    def test_worktrees_et_courrier_sont_des_no_ops(self):
        self.assertEqual(workspaces.worktree_marker(self.tmp.name), {})
        self.assertEqual(workspaces.write_worktree_marker(
            self.cfg, "a1", self.tmp.name), {})
        self.assertIsNone(workspaces.adopt_moved_worktree(
            object(), {"name": "a1"}, marker_path="/absent", roots=[]))
        agent = {"name": "worker", "cwd": self.tmp.name}
        self.assertEqual(workspaces.primary_workdir(agent), self.tmp.name)
        self.assertEqual(workspaces.resolve(self.cfg, object(), agent, "7"),
                         workspaces.Workdir(self.tmp.name))
        self.assertFalse(workspaces.mail_hold(self.cfg, object(), agent, "7"))
