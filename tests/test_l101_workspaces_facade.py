# SPDX-License-Identifier: AGPL-3.0-only
"""L101 T2 — le ménage est sorti du runner derrière `ameesh.workspaces`.

Le runner ne connaît plus `menage` ni `housekeeping` : il passe par la façade
`ameesh.workspaces` (`policy`, `before_turn`, `after_turn`, `periodic_pass`,
`tmpfs_pressure`, `attach_env`). La façade ne porte aucune politique propre :
elle délègue à `housekeeping` avec les mêmes seuils, verrous et journaux. Ce
test fige la séparation (aucun import interdit dans le runner) et chaque
délégation, pour qu'une reprise du couplage ne passe pas inaperçue.
"""
from __future__ import annotations

import ast
import pathlib
import unittest
from unittest import mock

from ameesh import housekeeping, workspaces

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
