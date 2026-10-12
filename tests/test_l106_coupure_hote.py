# SPDX-License-Identifier: AGPL-3.0-only
"""L106 : survivre à une coupure de l'hôte.

Incident du 2026-10-10 : un portable s'éteint batterie vide ; au redémarrage,
les exécuteurs (unités systemd utilisateur) partent avant que le PATH de la
session ne soit importé — `claude` introuvable, `node` absent pour dsh —, et
ces erreurs de l'HÔTE sont comptées comme des échecs rapides de l'AGENT (L48) :
l'orchestrateur est arrêté. Ce module vérifie :

* la résolution fiable des binaires (configuration, PATH, PATH du gestionnaire
  systemd, emplacements connus) et de l'interpréteur d'un script ;
* « hôte non prêt » : ni échec rapide, ni arrêt ; nouvel essai ; reprise seule ;
* l'alimentation : mesure, seuils, plus de nouveau tour sur batterie faible,
  arrêt propre sur batterie critique, reprise au retour du secteur ;
* la reprise d'une session `attach` morte par l'exécuteur de l'agent ;
* la re-livraison signalée du courrier réservé et non soldé, les ressources
  orphelines d'avant le redémarrage ;
* `ameesh doctor --harness` et les unités d'exécuteur manquantes ;
* les alertes `host_not_ready` et `host_power_low`.
"""
from __future__ import annotations

import dataclasses
import os
import shutil
import stat
import tempfile
import threading
import time
import unittest
import uuid
from unittest import mock

from ameesh import adapters, exploitation, harnesses, hostcheck, mail, notify, registry
from ameesh import resources, storage
from ameesh import runner as runner_mod
from ameesh.runner import AgentWorker, Runner

from .support import FAKEBIN, PgTestCase


def _script(path: str, body: str) -> str:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _attendre(condition, timeout: float = 15.0) -> bool:
    fin = time.monotonic() + timeout
    while time.monotonic() < fin:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


# --------------------------------------------------------------------------
# résolution des binaires (sans base)
# --------------------------------------------------------------------------

class ResolutionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-l106-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.vide = os.path.join(self.tmp, "vide")
        self.connu = os.path.join(self.tmp, "connu")
        for d in (self.vide, self.connu):
            os.makedirs(d)
        self.claude = harnesses.get("claude")
        self.deepseek = harnesses.get("deepseek")

    def env(self, **extra):
        base = {"PATH": self.vide, "HOME": self.tmp, "AMEESH_HARNESS_SEARCH": self.connu}
        base.update(extra)
        return base

    def test_emplacement_connu_quand_le_path_ne_l_a_pas(self):
        """Le PATH réduit d'une unité au démarrage : trouvé aux emplacements connus."""
        _script(os.path.join(self.connu, "claude"), "#!/bin/sh\nexit 0\n")
        with mock.patch.object(adapters, "systemd_manager_path", return_value=""):
            res = adapters.resolve_harness(self.claude, env=self.env(), bins={})
        self.assertEqual(res.path, os.path.join(self.connu, "claude"))
        self.assertEqual(res.source, "emplacement connu")
        self.assertEqual(res.path_prepend, (self.connu,))

    def test_path_du_gestionnaire_systemd_relu(self):
        """Le PATH importé APRÈS le démarrage de l'unité est relu au besoin."""
        session = os.path.join(self.tmp, "session")
        os.makedirs(session)
        _script(os.path.join(session, "claude"), "#!/bin/sh\nexit 0\n")
        with mock.patch.object(adapters, "systemd_manager_path", return_value=session):
            res = adapters.resolve_harness(self.claude, env=self.env(), bins={})
        self.assertEqual(res.source, "PATH du gestionnaire systemd")
        self.assertEqual(res.path, os.path.join(session, "claude"))

    def test_ordre_variable_puis_configuration_puis_path(self):
        par_variable = _script(os.path.join(self.tmp, "claude-var"), "#!/bin/sh\n")
        par_config = _script(os.path.join(self.tmp, "claude-conf"), "#!/bin/sh\n")
        _script(os.path.join(self.vide, "claude"), "#!/bin/sh\n")
        env = self.env(AMEESH_CLAUDE_BIN=par_variable)
        self.assertEqual(adapters.resolve_harness(
            self.claude, env=env, bins={"claude": par_config}).path, par_variable)
        res = adapters.resolve_harness(self.claude, env=self.env(),
                                       bins={"claude": par_config})
        self.assertEqual((res.path, res.source), (par_config, "config harness_bins"))
        res = adapters.resolve_harness(self.claude, env=self.env(), bins={})
        self.assertEqual((res.path, res.source), (os.path.join(self.vide, "claude"), "PATH"))

    def test_chemin_configure_absent_jamais_remplace_en_silence(self):
        _script(os.path.join(self.connu, "claude"), "#!/bin/sh\n")
        with self.assertRaises(adapters.HarnessMissing) as ctx:
            adapters.resolve_harness(self.claude, env=self.env(),
                                     bins={"claude": os.path.join(self.tmp, "absent")})
        self.assertIn("config harness_bins", str(ctx.exception))

    def test_interpreteur_de_dsh_trouve_et_ajoute_au_path(self):
        """dsh est un script `#!/usr/bin/env node` : sans node dans le PATH, le
        tour sortait en 1 s. Node est cherché, puis mis en tête du PATH."""
        dsh = _script(os.path.join(self.tmp, "dsh"), "#!/usr/bin/env node\n")
        node_dir = os.path.join(self.connu, "node-bin")
        os.makedirs(node_dir)
        _script(os.path.join(node_dir, "node"), "#!/bin/sh\n")
        env = self.env(AMEESH_DSH_BIN=dsh, AMEESH_HARNESS_SEARCH=node_dir)
        with mock.patch.object(adapters, "systemd_manager_path", return_value=""):
            res = adapters.resolve_harness(self.deepseek, env=env, bins={})
        self.assertEqual(res.path, dsh)
        self.assertEqual(res.interpreter, "node")
        self.assertEqual(res.interpreter_path, os.path.join(node_dir, "node"))
        self.assertEqual(res.path_prepend, (node_dir,))
        self.assertIn("interpréteur node", res.describe())
        # l'adaptateur passe ce PATH complété au harnais
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(adapters, "systemd_manager_path", return_value=""):
            adapter = adapters.adapter_for("deepseek")
            self.assertTrue(adapter.env()["PATH"].startswith(node_dir + os.pathsep))

    def test_interpreteur_introuvable_est_une_erreur_d_hote(self):
        dsh = _script(os.path.join(self.tmp, "dsh"), "#!/usr/bin/env node\n")
        with mock.patch.object(adapters, "systemd_manager_path", return_value=""), \
                self.assertRaises(adapters.HarnessMissing) as ctx:
            adapters.resolve_harness(self.deepseek, env=self.env(AMEESH_DSH_BIN=dsh),
                                     bins={})
        self.assertIn("interpréteur node introuvable", str(ctx.exception))

    def test_replis_coupes_par_une_recherche_vide(self):
        _script(os.path.join(self.connu, "claude"), "#!/bin/sh\n")
        with mock.patch.object(adapters, "systemd_manager_path",
                               side_effect=AssertionError("ne doit pas être relu")), \
                self.assertRaises(adapters.HarnessMissing):
            adapters.resolve_harness(self.claude, env=self.env(AMEESH_HARNESS_SEARCH=""),
                                     bins={})
        self.assertEqual(adapters.systemd_manager_path({"AMEESH_HARNESS_SEARCH": ""}), "")

    def test_motifs_et_ordre_des_emplacements(self):
        a = os.path.join(self.tmp, "npx", "a", "bin")
        b = os.path.join(self.tmp, "npx", "b", "bin")
        os.makedirs(a)
        os.makedirs(b)
        os.utime(os.path.join(self.tmp, "npx", "a", "bin"), (1, 1))
        dirs = adapters.search_dirs({"AMEESH_HARNESS_SEARCH": os.pathsep.join(
            [os.path.join(self.tmp, "npx", "*", "bin"), "/nulle/part"])})
        self.assertEqual(dirs, [b, a])      # le plus récent d'abord ; absent ignoré
        defaut = adapters.search_dirs({"HOME": self.tmp,
                                       "MISE_DATA_DIR": os.path.join(self.tmp, "mise")})
        self.assertEqual(defaut, [d for d in defaut if os.path.isdir(d)])


# --------------------------------------------------------------------------
# alimentation (sans base)
# --------------------------------------------------------------------------

class AlimentationTest(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="ameesh-l106-power-")
        self.addCleanup(shutil.rmtree, self.base, True)

    def source(self, name, **fields):
        root = os.path.join(self.base, name)
        os.makedirs(root, exist_ok=True)
        for key, value in fields.items():
            with open(os.path.join(root, key), "w", encoding="utf-8") as fh:
                fh.write("%s\n" % value)

    def test_portable_sur_batterie(self):
        self.source("AC", type="Mains", online=0)
        self.source("BAT0", type="Battery", present=1, capacity=18, status="Discharging",
                    scope="System")
        self.source("hidpp_battery_0", type="Battery", scope="Device", capacity=5,
                    status="Discharging")       # la souris ne compte pas
        self.assertEqual(resources.power(self.base),
                         {"on_ac": False, "battery_percent": 18.0})

    def test_secteur_et_deux_batteries_ponderees(self):
        self.source("ADP1", type="Mains", online=1)
        self.source("BAT0", type="Battery", energy_now=10, energy_full=100,
                    status="Charging")
        self.source("BAT1", type="Battery", energy_now=30, energy_full=50,
                    status="Charging")
        self.assertEqual(resources.power(self.base),
                         {"on_ac": True, "battery_percent": round(4000 / 150, 1)})

    def test_sans_source_de_secteur_l_etat_tranche(self):
        self.source("BAT0", type="Battery", capacity=50, status="Discharging")
        self.assertIs(resources.power(self.base)["on_ac"], False)

    def test_serveur_ou_illisible_inconnu(self):
        self.assertEqual(resources.power(self.base), {"on_ac": None, "battery_percent": None})
        self.assertEqual(resources.power(os.path.join(self.base, "absent")),
                         {"on_ac": None, "battery_percent": None})

    def test_seuils_par_defaut_et_declares(self):
        limits = resources.thresholds(None)
        self.assertEqual(limits["min_battery_percent"], 25.0)
        self.assertEqual(limits["stop_battery_percent"], 10.0)

        class Policy:
            resources = {"min_battery_percent": "40%", "stop_battery_percent": 15}

        limits = resources.thresholds(Policy)
        self.assertEqual((limits["min_battery_percent"], limits["stop_battery_percent"]),
                         (40.0, 15.0))
        self.assertIsNone(resources.parse_percent(120))

    def test_etats(self):
        limits = resources.thresholds(None)
        cas = [({"on_ac": False, "battery_percent": 50}, "ok"),
               ({"on_ac": False, "battery_percent": 20}, "low"),
               ({"on_ac": False, "battery_percent": 10}, "stop"),
               ({"on_ac": True, "battery_percent": 3}, "ok"),
               ({}, "unknown")]
        for reading, attendu in cas:
            with self.subTest(reading=reading):
                self.assertEqual(resources.power_state(reading, limits)["state"], attendu)
        verdict = resources.pressure({"on_ac": False, "battery_percent": 20}, limits=limits)
        self.assertTrue(verdict["blocked"])
        self.assertFalse(verdict["critical"])  # jamais la pause des agents peu prioritaires
        self.assertEqual(verdict["breaches"][0]["key"], "min_battery_percent")

    def test_couverture_des_unites_d_executeur(self):
        unites = {
            ("list-unit-files", "--type=service", "--no-legend", "--plain"):
                "ameesh-runner-agent@.service indirect enabled\n"
                "ameesh-runner-ima.service enabled enabled\n"
                "ameesh-runner-ancien.service disabled enabled\n",
            ("show", "ameesh-runner-ima.service", "-p", "ExecStart", "--value"):
                "{ path=/x/ameesh ; argv[]=/x/ameesh run --agents ima-coord --runner-id r }",
            ("is-enabled", "ameesh-runner-agent@claude1.service"): "enabled\n",
            ("is-enabled", "ameesh-runner-agent@mesh-design.service"): "disabled\n",
        }
        couverture = hostcheck.runner_coverage(
            ["claude1", "ima-coord", "mesh-design"], run=lambda *a: unites.get(a, ""))
        self.assertEqual(couverture, {
            "claude1": "ameesh-runner-agent@claude1.service",
            "ima-coord": "ameesh-runner-ima.service",
            "mesh-design": None})
        # une unité sans --agents mène tous les agents de l'hôte
        generique = {
            ("list-unit-files", "--type=service", "--no-legend", "--plain"):
                "agent-runner.service enabled enabled\n",
            ("show", "agent-runner.service", "-p", "ExecStart", "--value"):
                "argv[]=/x/agent-runner --poll 5",
        }
        self.assertEqual(hostcheck.runner_coverage(["a"], run=lambda *a: generique.get(a, "")),
                         {"a": "agent-runner.service"})
        self.assertIsNone(hostcheck.runner_coverage(["a"], run=lambda *a: None))

    def test_couverture_des_unites_systeme_du_serveur(self):
        """L146 : sur le serveur du mesh, les personas tournent sous l'unité
        SYSTÈME `ameesh-runner@<p>` ; sans gestionnaire utilisateur (None),
        seules les unités système comptent."""
        S = hostcheck.SYSTEM
        unites = {
            (S, "list-unit-files", "--type=service", "--no-legend", "--plain"):
                "ameesh-runner@.service indirect enabled\n"
                "ameesh-runner@ima.service enabled enabled\n",
            (S, "show", "ameesh-runner@ima.service", "-p", "ExecStart", "--value"):
                "argv[]=/opt/ameesh/venv/bin/agent-runner --foreground",
            (S, "is-enabled", "ameesh-runner@mesh-design.service"): "enabled\n",
            (S, "is-enabled", "ameesh-runner@deepseek7.service"): "disabled\n",
        }
        run = lambda *a: unites.get(a, "") if a[:1] == (S,) else None  # noqa: E731
        self.assertEqual(
            hostcheck.runner_coverage(["ima", "mesh-design", "deepseek7"], run=run),
            {"ima": "ameesh-runner@ima.service",
             "mesh-design": "ameesh-runner@mesh-design.service",
             "deepseek7": None})
        # une instance sans --agents ne mène que son agent, pas tous
        self.assertEqual(hostcheck.runner_coverage(["autre"], run=run), {"autre": None})


# --------------------------------------------------------------------------
# exécuteur (base réelle)
# --------------------------------------------------------------------------

class HoteNonPretTest(PgTestCase):
    def _worker(self, name, *, prompt="tour", runner=None, **cfg):
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        self.register(name, "claude", cwd=cwd, prompt=prompt)
        runner = runner or Runner(dataclasses.replace(self.cfg, **cfg), self.db, once=True)
        lease = registry.claim(self.db, name, runner.runner_id, 3600)
        self.assertIsNotNone(lease)
        worker = AgentWorker(runner, registry.get(self.db, name), lease)
        self.addCleanup(worker.watchdog_stop.set)
        return runner, worker

    def _env(self, **extra):
        env = {"AMEESH_TEST_LOG": self.turns_log}
        env.update(extra)
        return mock.patch.dict(os.environ, env, clear=False)

    def test_harnais_introuvable_ni_echec_rapide_ni_arret(self):
        """L'incident : 5 « binaire claude introuvable » arrêtaient l'agent.
        Désormais : hôte non prêt, consigne gardée, jamais arrêté."""
        absent = os.path.join(self.tmp, "bin", "claude")
        _runner, worker = self._worker("orchestre", max_fast_failures=3)
        tours = iter(range(8))

        def pick():
            if next(tours, None) is None:
                worker.stopping.set()
                return None
            return {"kind": "prompt", "prompt": "tour", "ids": []}

        with self._env(AMEESH_CLAUDE_BIN=absent), \
                mock.patch.object(worker, "renew", return_value=True), \
                mock.patch.object(worker, "pick", side_effect=pick), \
                mock.patch.object(worker, "rotate_for_lot", side_effect=lambda s: s), \
                mock.patch.object(worker, "maybe_rotate"), \
                mock.patch.object(worker, "ensure_watchdog"), \
                mock.patch.object(worker, "release_lease"), \
                mock.patch.object(worker.wake, "wait"):
            worker.run()
        row = registry.get(self.db, "orchestre")
        self.assertEqual(worker.fast_failures, 0)
        self.assertNotEqual(row["status"], "stopped")
        self.assertEqual(row["status"], "blocked")
        self.assertEqual(row["status_text"], "hôte non prêt")
        self.assertIn("introuvable", row["last_error"])
        self.assertTrue(row["last_error"].startswith("hôte non prêt : "))
        self.assertEqual(row["pending_prompt"], "tour")

    def test_reprise_seule_quand_le_binaire_revient(self):
        dossier = os.path.join(self.tmp, "bin")
        os.makedirs(dossier, exist_ok=True)
        cible = os.path.join(dossier, "claude")
        _runner, worker = self._worker("revient", prompt="consigne gardée")
        with self._env(AMEESH_CLAUDE_BIN=cible):
            spec = worker.pick()
            self.assertEqual(spec["prompt"], "consigne gardée")
            self.assertFalse(worker.run_turn(spec))
            self.assertFalse(worker.fast_failure)
            row = registry.get(self.db, "revient")
            self.assertEqual((row["status"], row["status_text"]), ("blocked", "hôte non prêt"))
            self.assertEqual(row["pending_prompt"], "consigne gardée")
            # tant que l'hôte n'est pas prêt : rien n'est consommé
            self.assertIsNone(worker.pick())
            self.assertEqual(registry.get(self.db, "revient")["pending_prompt"],
                             "consigne gardée")
            # l'installation finit : à l'échéance, le statut est levé et le tour part
            shutil.copy(os.path.join(FAKEBIN, "claude"), cible)
            worker._host_next = 0.0
            spec = worker.pick()
            self.assertIsNotNone(spec)
            self.assertEqual(spec["prompt"], "consigne gardée")
            self.assertTrue(worker.run_turn(spec))
        self.assertEqual(registry.get(self.db, "revient")["status"], "idle")
        self.assertEqual(len(self.turns()), 1)

    def test_blocage_herite_leve_par_un_nouvel_executeur(self):
        _runner, worker = self._worker("herite")
        registry.set_marked_block(self.db, "herite", worker.runner.runner_id, worker.epoch,
                                  "hôte non prêt", "hôte non prêt : ancien", "hôte non prêt")
        worker2 = AgentWorker(worker.runner, registry.get(self.db, "herite"),
                              {"lease_epoch": worker.epoch,
                               "lease_expires_ts": time.time() + 3600})
        self.addCleanup(worker2.watchdog_stop.set)
        self.assertTrue(worker2._host_blocked)
        with self._env(AMEESH_BIN_DIR=FAKEBIN):
            self.assertIsNotNone(worker2.pick())
        self.assertNotEqual(registry.get(self.db, "herite")["status_text"], "hôte non prêt")

    def test_lancement_en_127_est_une_erreur_d_hote(self):
        """`env: 'node': No such file or directory` : code 127, rien sur la sortie."""
        faux = _script(os.path.join(self.tmp, "claude-sans-node"),
                       "#!/bin/sh\necho \"env: 'node': No such file or directory\" >&2\n"
                       "exit 127\n")
        _runner, worker = self._worker("cent27")
        with self._env(AMEESH_CLAUDE_BIN=faux):
            spec = worker.pick()
            self.assertFalse(worker.run_turn(spec))
        self.assertFalse(worker.fast_failure)
        row = registry.get(self.db, "cent27")
        self.assertEqual(row["status_text"], "hôte non prêt")
        self.assertIn("code 127", row["last_error"])
        self.assertIn("node", row["last_error"])
        self.assertEqual(row["pending_prompt"], "tour")
        self.assertTrue(worker._host_blocked)

    def test_vrai_echec_rapide_toujours_compte(self):
        """Un harnais qui sort en erreur APRÈS avoir parlé reste un échec de
        l'agent (L48 inchangé)."""
        _runner, worker = self._worker("vraiecheq")
        with self._env(AMEESH_BIN_DIR=FAKEBIN, AMEESH_TEST_EXIT="1"):
            spec = worker.pick()
            self.assertFalse(worker.run_turn(spec))
        self.assertTrue(worker.fast_failure)
        self.assertNotEqual(registry.get(self.db, "vraiecheq")["status_text"],
                            "hôte non prêt")

    def test_base_injoignable_au_demarrage_attendue_en_service(self):
        cfg = dataclasses.replace(self.cfg, db_retry_max=2.0)
        appels = []

        def connect(_cfg):
            appels.append(1)
            if len(appels) < 3:
                raise runner_mod.db_mod.Unavailable("connection refused")
            return "base"

        with mock.patch.object(runner_mod.db_mod, "connect", side_effect=connect), \
                mock.patch.object(runner_mod.Reprise, "echec", return_value=0.01), \
                mock.patch.object(runner_mod, "log"):
            self.assertEqual(runner_mod._connect_at_start(cfg, wait=True), "base")
        self.assertEqual(len(appels), 3)
        with mock.patch.object(runner_mod.db_mod, "connect",
                               side_effect=runner_mod.db_mod.Unavailable("refus")):
            self.assertIsNone(runner_mod._connect_at_start(cfg, wait=False))

    # -- alertes --------------------------------------------------------------
    def test_alerte_host_not_ready_au_responsable(self):
        _runner, worker = self._worker("alerte")
        self.db.execute("UPDATE agent_registry SET responsible = 'human:resp' "
                        "WHERE name = 'alerte'")
        with self._env(AMEESH_CLAUDE_BIN=os.path.join(self.tmp, "absent")):
            worker.run_turn(worker.pick())
        alertes = [a for a in exploitation.alerts(self.cfg, self.db)
                   if a["type"] == "host_not_ready"]
        self.assertEqual(len(alertes), 1)
        self.assertEqual(alertes[0]["agent"], "alerte")
        self.assertEqual(alertes[0]["reason"], "hote")
        self.assertEqual(alertes[0]["responsible"], "human:resp")
        self.assertIn("introuvable", alertes[0]["detail"])
        self.assertIn("host_not_ready", notify.DEFAULT_TYPES)

    def test_alerte_host_power_low_urgente_au_seuil_d_arret(self):
        hosts = storage.of(self.db).hosts
        hote = "portable-%s" % uuid.uuid4().hex[:6]
        hosts.record({"host": hote, "on_ac": False, "battery_percent": 20.0})
        alertes = [a for a in exploitation.alerts(self.cfg, self.db) if a.get("host") == hote]
        self.assertEqual([a["type"] for a in alertes], ["host_power_low"])  # pas host_pressure
        self.assertEqual(alertes[0]["reason"], "bas")
        self.assertFalse(alertes[0]["urgent"])
        message = notify.render(alertes[0], "raised", time.time())
        self.assertFalse(message.urgent)
        hosts.record({"host": hote, "on_ac": False, "battery_percent": 8.0})
        alertes = [a for a in exploitation.alerts(self.cfg, self.db) if a.get("host") == hote]
        self.assertEqual(alertes[0]["reason"], "arret")
        self.assertTrue(notify.render(alertes[0], "raised", time.time()).urgent)
        # au retour du secteur : plus d'alerte
        hosts.record({"host": hote, "on_ac": True, "battery_percent": 9.0})
        self.assertFalse([a for a in exploitation.alerts(self.cfg, self.db)
                          if a.get("host") == hote])


class AlimentationExecuteurTest(PgTestCase):
    """Garde d'alimentation de l'exécuteur, avec une mesure simulée."""

    def _runner(self, **cfg):
        runner = Runner(dataclasses.replace(self.cfg, poll=1.0, **cfg), self.db, once=False)
        runner.power_mesure = {"on_ac": True, "battery_percent": 80.0}
        runner.power_reading = lambda: dict(runner.power_mesure)
        self.addCleanup(self._arret, runner)
        return runner

    @staticmethod
    def _arret(runner):
        runner.stop.set()
        with runner.lock:
            workers = list(runner.workers.values())
        for worker in workers:
            worker.stopping.set()
            worker.wake.set()
            worker.join(timeout=10)
            worker.watchdog_stop.set()

    def _mesure(self, runner, on_ac, pct):
        runner.power_mesure = {"on_ac": on_ac, "battery_percent": pct}
        runner._pressure_at = 0.0   # pas de cache du verdict

    def test_batterie_faible_plus_de_nouveau_tour(self):
        cwd = os.path.join(self.tmp, "work", "faible")
        os.makedirs(cwd, exist_ok=True)
        self.register("faible", "claude", cwd=cwd, prompt="à garder")
        runner = Runner(self.cfg, self.db, once=True)
        runner.power_reading = lambda: {"on_ac": False, "battery_percent": 20.0}
        lease = registry.claim(self.db, "faible", runner.runner_id, 3600)
        worker = AgentWorker(runner, registry.get(self.db, "faible"), lease)
        self.addCleanup(worker.watchdog_stop.set)
        self.assertIsNone(worker.pick())
        self.assertEqual(registry.get(self.db, "faible")["pending_prompt"], "à garder")
        self.assertEqual(runner.power_guard(), "low")
        self.assertFalse(runner.power_hold.is_set())

    def test_arret_propre_puis_reprise_au_retour_du_secteur(self):
        cwd = os.path.join(self.tmp, "work", "nomade")
        os.makedirs(cwd, exist_ok=True)
        self.register("nomade", "claude", cwd=cwd)
        runner = self._runner(power_stop_grace=5.0)
        with mock.patch.dict(os.environ, {"AMEESH_BIN_DIR": FAKEBIN,
                                          "AMEESH_TEST_LOG": self.turns_log}):
            runner.sweep()
            self.assertIn("nomade", runner.workers)
            # batterie critique : arrêt propre, statut posé, bail rendu
            self._mesure(runner, False, 7.0)
            self.assertEqual(runner.power_guard(), "stop")
            self.assertTrue(runner.power_hold.is_set())
            runner._power_thread.join(timeout=30)
            row = registry.get(self.db, "nomade")
            self.assertIsNone(row["lease_owner"])
            self.assertEqual((row["status"], row["status_text"]), ("blocked", "hôte non prêt"))
            self.assertIn("batterie 7.0 %", row["last_error"])
            self.assertNotEqual(row["status"], "stopped")
            # en attente du secteur : aucun bail repris
            runner.sweep()
            self.assertNotIn("nomade", runner.workers)   # worker terminé, pas remplacé
            self.assertIsNone(registry.get(self.db, "nomade")["lease_owner"])
            # secteur revenu : réclamation reprise, statut levé sans `ameesh resume`
            self._mesure(runner, True, 8.0)
            self.assertEqual(runner.power_guard(), "ok")
            self.assertFalse(runner.power_hold.is_set())
            runner.sweep()
            self.assertTrue(_attendre(
                lambda: registry.get(self.db, "nomade")["status_text"] != "hôte non prêt"))
            self.assertIsNotNone(registry.get(self.db, "nomade")["lease_owner"])

    def test_tour_en_cours_arrete_au_dela_du_delai(self):
        cwd = os.path.join(self.tmp, "work", "long")
        os.makedirs(cwd, exist_ok=True)
        self.register("long", "claude", cwd=cwd, prompt="consigne longue")
        runner = self._runner(power_stop_grace=0.5)
        with mock.patch.dict(os.environ, {"AMEESH_BIN_DIR": FAKEBIN,
                                          "AMEESH_TEST_LOG": self.turns_log,
                                          "AMEESH_TEST_SLEEP": "60"}):
            runner.sweep()
            worker = runner.workers["long"]
            self.assertTrue(_attendre(lambda: worker.proc is not None, timeout=60))
            self._mesure(runner, False, 5.0)
            runner.power_guard()
            runner._power_thread.join(timeout=60)
            # sous charge, la fin du tour arrêté peut dépasser l'attente du fil
            # d'alimentation : le worker rend son bail de lui-même
            worker.join(timeout=60)
        row = registry.get(self.db, "long")
        self.assertEqual(row["status_text"], "hôte non prêt")
        self.assertEqual(row["pending_prompt"], "consigne longue")  # consigne remise
        self.assertIsNone(row["lease_owner"])
        self.assertFalse(worker.is_alive())


class CoupureTest(PgTestCase):
    """Après la coupure : session attach reprise, courrier re-livré, orphelins."""

    def _cwd(self, name):
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        return cwd

    def test_session_attach_morte_reprise_par_l_executeur(self):
        self.register("concepteur", "claude", cwd=self._cwd("concepteur"),
                      session="sess-interactive")
        owner = runner_mod.attach_owner(self.cfg.host)
        self.assertIsNotNone(registry.attach_claim(self.db, "concepteur", owner, 60))
        # coupure : le processus attach meurt sans rendre son bail
        self.db.execute("UPDATE agent_registry SET lease_expires_at = now() - "
                        "interval '1 second' WHERE name = 'concepteur'")
        runner = Runner(self.cfg, self.db, once=True, wait=0.5)
        with mock.patch.dict(os.environ, {"AMEESH_BIN_DIR": FAKEBIN,
                                          "AMEESH_TEST_LOG": self.turns_log}):
            runner.sweep()
        self.assertTrue(runner.did_turn)
        tour = self.turns()[-1]["argv"]
        self.assertEqual(tour[tour.index("--resume") + 1], "sess-interactive")
        self.assertIn("session interactive (ameesh attach) s'est interrompue", tour[-1])

    def test_attach_rendu_proprement_pas_de_reprise(self):
        self.register("humain", "claude", cwd=self._cwd("humain"), session="sess-h")
        owner = runner_mod.attach_owner(self.cfg.host)
        lease = registry.attach_claim(self.db, "humain", owner, 60)
        registry.release(self.db, "humain", owner, int(lease["lease_epoch"]))
        runner = Runner(self.cfg, self.db, once=True, wait=0.3)
        with mock.patch.dict(os.environ, {"AMEESH_BIN_DIR": FAKEBIN,
                                          "AMEESH_TEST_LOG": self.turns_log}):
            runner.sweep()
        self.assertFalse(runner.did_turn)

    def test_courrier_reserve_non_solde_re_livre_apres_coupure(self):
        """Coupure entre la réservation et la remise : le bail expire, le
        redémarrage le reprend, le message est remis, marqué « re-livré »."""
        self.register("coupe", "claude", cwd=self._cwd("coupe"))
        runner1 = Runner(self.cfg, self.db, once=True)
        lease = registry.claim(self.db, "coupe", runner1.runner_id, 3600)
        mid = mail.send(self.db, "orch", "coupe", "message en vol")
        rows = mail.reserve(self.db, "coupe", runner1.runner_id, int(lease["lease_epoch"]),
                            mail.new_token(), porteur="consigne")
        self.assertEqual([int(r["id"]) for r in rows], [mid])
        registry.begin_turn(self.db, "coupe", runner1.runner_id, int(lease["lease_epoch"]),
                            "tour messages (claude)")
        # coupure brutale : ni remise, ni annulation, ni bail rendu
        self.db.execute("UPDATE agent_registry SET lease_expires_at = now() - "
                        "interval '1 second' WHERE name = 'coupe'")
        runner2 = Runner(dataclasses.replace(self.cfg, runner_id="apres-coupure"), self.db,
                         once=True, wait=0.5)
        with mock.patch.dict(os.environ, {"AMEESH_BIN_DIR": FAKEBIN,
                                          "AMEESH_TEST_LOG": self.turns_log}):
            runner2.sweep()      # bail expiré relevé (`dead`), puis réclamé
        self.assertTrue(runner2.did_turn)
        consigne = self.turns()[-1]["argv"][-1]
        self.assertIn("message en vol", consigne)
        self.assertIn("re-livré", consigne)
        self.assertIsNotNone(mail.get(self.db, mid)["delivered_ts"])

    def test_ressources_d_avant_le_redemarrage_signalees(self):
        turns = storage.of(self.db).turn_resources
        avant = "tour-avant-%s" % uuid.uuid4().hex[:6]
        apres = "tour-apres-%s" % uuid.uuid4().hex[:6]
        turns.open_turn(avant, "a1", self.cfg.host, pgid=4242, label="l",
                        containers=["c-avant"])
        turns.open_turn(apres, "a2", self.cfg.host, pgid=4343, label="l")
        self.db.execute("UPDATE turn_resources SET started_at = now() - interval '2 hours'"
                        " WHERE turn_id = %s", (avant,))
        runner = Runner(self.cfg, self.db, once=True)
        demarrage = time.time() - 3600       # l'hôte a démarré il y a une heure
        with mock.patch.object(resources, "boot_time", return_value=demarrage), \
                mock.patch.object(runner_mod, "log"):
            signales = runner.report_boot_orphans()
        self.assertEqual([r["turn_id"] for r in signales], [avant])
        orphelins = [r["turn_id"] for r in turns.orphans(self.cfg.host)]
        self.assertIn(avant, orphelins)
        self.assertNotIn(apres, orphelins)
        alertes = [a for a in exploitation.alerts(self.cfg, self.db)
                   if a["type"] == "orphan_resource"]
        self.assertIn(avant, [a["turn"] for a in alertes])

    def test_tour_d_avant_le_redemarrage_sans_conteneur_solde(self):
        # L147 : aucun groupe de processus ne survit au redémarrage ; sans
        # conteneur, la ligne est soldée, plus signalée orpheline
        turns = storage.of(self.db).turn_resources
        seul = "tour-seul-%s" % uuid.uuid4().hex[:6]
        deja = "tour-deja-%s" % uuid.uuid4().hex[:6]
        turns.open_turn(seul, "a1", self.cfg.host, pgid=4242, label="l")
        turns.open_turn(deja, "a2", self.cfg.host, pgid=4343, label="l")
        turns.mark_orphan(deja)        # signalée par un démarrage précédent
        self.db.execute("UPDATE turn_resources SET started_at = now() - interval '2 hours'"
                        " WHERE turn_id IN (%s, %s)", (seul, deja))
        runner = Runner(self.cfg, self.db, once=True)
        with mock.patch.object(resources, "boot_time", return_value=time.time() - 3600), \
                mock.patch.object(runner_mod, "log"):
            restes = [r["turn_id"] for r in runner.report_boot_orphans()]
        # (une ligne d'un autre test, avec conteneur, peut rester orpheline)
        self.assertNotIn(seul, restes)
        self.assertNotIn(deja, restes)
        for turn in (seul, deja):
            self.assertEqual(turns.get(turn)["status"], "done")
        alertes = [a["turn"] for a in exploitation.alerts(self.cfg, self.db)
                   if a["type"] == "orphan_resource"]
        self.assertNotIn(seul, alertes)
        self.assertNotIn(deja, alertes)

    def _avant_le_demarrage(self, *turn_ids) -> None:
        self.db.execute("UPDATE turn_resources SET started_at = now() - interval '2 hours'"
                        " WHERE turn_id IN (%s)" % ", ".join(["%s"] * len(turn_ids)),
                        turn_ids)

    def _revue_au_demarrage(self, runtime=None) -> list:
        runner = Runner(self.cfg, self.db, once=True)
        with mock.patch.object(resources, "boot_time", return_value=time.time() - 3600), \
                mock.patch.object(runner, "container_runtime", return_value=runtime), \
                mock.patch.object(runner_mod, "log"):
            return [r["turn_id"] for r in runner.report_boot_orphans()]

    def test_tour_interrompu_sans_conteneur_note_interroge_le_moteur(self):
        # codex r1 P2 : un tour interrompu ne note aucun conteneur (relevé en
        # fin de tour) ; celui que sa politique relance après le redémarrage
        # se lit par l'étiquette du tour
        turns = storage.of(self.db).turn_resources
        relance = "tour-relance-%s" % uuid.uuid4().hex[:6]
        mort = "tour-mort-%s" % uuid.uuid4().hex[:6]
        for turn in (relance, mort):
            turns.open_turn(turn, "a1", self.cfg.host, pgid=4242, label="l")
        self._avant_le_demarrage(relance, mort)
        moteur = mock.Mock()
        moteur.list_all.return_value = [
            {"id": "c-relance", "name": "pg", "image": "postgres", "created": "",
             "labels": {"ameesh.turn": relance}},
            {"id": "c-autre", "name": "x", "image": "x", "created": "", "labels": {}}]
        restes = self._revue_au_demarrage(moteur)
        self.assertIn(relance, restes)
        self.assertNotIn(mort, restes)
        self.assertEqual(turns.get(relance)["status"], "orphan")
        self.assertEqual(turns.get(mort)["status"], "done")
        moteur.list_all.assert_called_once_with()       # un seul relevé
        alertes = [a["turn"] for a in exploitation.alerts(self.cfg, self.db)
                   if a["type"] == "orphan_resource"]
        self.assertIn(relance, alertes)

    def test_juste_apres_le_demarrage_le_solde_attend_les_conteneurs(self):
        # codex r2 P2 : l'exécuteur démarre avant l'unité qui relance le
        # conteneur étiqueté ; le moteur ne liste encore rien — la ligne n'est
        # pas soldée, la revue est refaite et voit le conteneur arrivé
        turns = storage.of(self.db).turn_resources
        relance = "tour-tardif-%s" % uuid.uuid4().hex[:6]
        mort = "tour-fini-%s" % uuid.uuid4().hex[:6]
        for turn in (relance, mort):
            turns.open_turn(turn, "a1", self.cfg.host, pgid=4242, label="l")
        self._avant_le_demarrage(relance, mort)
        runner = Runner(self.cfg, self.db, once=True)
        moteur = mock.Mock()
        moteur.list_all.return_value = []

        def revue(uptime):
            with mock.patch.object(resources, "boot_time", return_value=time.time() - uptime), \
                    mock.patch.object(runner, "container_runtime", return_value=moteur), \
                    mock.patch.object(runner, "_user_manager_state", return_value="running"), \
                    mock.patch.object(runner_mod, "log"):
                return [r["turn_id"] for r in runner.report_boot_orphans()]

        self.assertEqual(revue(60), [])
        for turn in (relance, mort):
            self.assertEqual(turns.get(turn)["status"], "running")
        self.assertIsNotNone(runner.boot_review_at)
        moteur.list_all.assert_not_called()     # #220 r3 : aucun relevé avant le feu vert
        # le conteneur arrive ; la revue suivante, délai passé, le voit
        moteur.list_all.return_value = [
            {"id": "c-tardif", "name": "pg", "image": "postgres", "created": "",
             "labels": {"ameesh.turn": relance}}]
        self.assertIn(relance, revue(runner_mod.BOOT_SETTLE_S + 1))
        self.assertEqual(turns.get(relance)["status"], "orphan")
        self.assertEqual(turns.get(mort)["status"], "done")
        self.assertIsNone(runner.boot_review_at)

    def _revue(self, runner, uptime, moteur, etat="running"):
        with mock.patch.object(resources, "boot_time", return_value=time.time() - uptime), \
                mock.patch.object(runner, "container_runtime", return_value=moteur), \
                mock.patch.object(runner, "_user_manager_state",
                                  side_effect=etat if callable(etat) else lambda: etat), \
                mock.patch.object(runner_mod, "log"):
            return [r["turn_id"] for r in runner.report_boot_orphans()]

    def _xmin(self, turn_id) -> str:
        return str(self.db.query("SELECT xmin::text AS x FROM turn_resources"
                                 " WHERE turn_id = %s", (turn_id,))[0]["x"])

    def test_le_moteur_est_releve_apres_le_feu_vert(self):
        # codex r3 P2 : à la 12e minute l'unité démarre encore ; le conteneur
        # arrive au moment où le gestionnaire passe `running` — un relevé pris
        # AVANT ce contrôle était vide et soldait la ligne pour de bon
        turns = storage.of(self.db).turn_resources
        tour = "tour-course-%s" % uuid.uuid4().hex[:6]
        turns.open_turn(tour, "a1", self.cfg.host, pgid=4242, label="l")
        self._avant_le_demarrage(tour)
        runner = Runner(self.cfg, self.db, once=True)
        moteur = mock.Mock()
        moteur.list_all.return_value = []

        def demarre():
            moteur.list_all.return_value = [
                {"id": "c-course", "name": "pg", "image": "postgres", "created": "",
                 "labels": {"ameesh.turn": tour}}]
            return "running"

        self.assertIn(tour, self._revue(runner, 12 * 60, moteur, demarre))
        self.assertEqual(turns.get(tour)["status"], "orphan")

    def test_une_revue_n_ecrit_que_ce_qui_change(self):
        # codex r3 P2 : les orphelines déjà signalées et toujours là ne sont
        # pas réécrites ; une revue différée n'écrit rien du tout
        turns = storage.of(self.db).turn_resources
        jeton = uuid.uuid4().hex[:6]
        vivantes = ["tour-vivant-%s-%d" % (jeton, i) for i in range(3)]
        for turn in vivantes:
            turns.open_turn(turn, "a1", self.cfg.host, pgid=4242, label="l")
            turns.mark_orphan(turn)
        neuve = "tour-neuf-%s" % jeton
        turns.open_turn(neuve, "a1", self.cfg.host, pgid=4242, label="l")
        self._avant_le_demarrage(*vivantes, neuve)
        moteur = mock.Mock()
        moteur.list_all.return_value = [
            {"id": "c-%s" % t, "name": "x", "image": "x", "created": "",
             "labels": {"ameesh.turn": t}} for t in vivantes + [neuve]]
        avant = {t: self._xmin(t) for t in vivantes + [neuve]}
        runner = Runner(self.cfg, self.db, once=True)
        self.assertEqual(self._revue(runner, 60, moteur), [])          # différée
        self.assertEqual({t: self._xmin(t) for t in avant}, avant)
        restes = self._revue(runner, 3600, moteur)
        self.assertTrue(set(vivantes + [neuve]) <= set(restes))
        self.assertEqual({t: self._xmin(t) for t in vivantes}, {t: avant[t] for t in vivantes})
        self.assertNotEqual(self._xmin(neuve), avant[neuve])            # running -> orphan
        self.assertEqual(turns.get(neuve)["status"], "orphan")
        apres = {t: self._xmin(t) for t in avant}
        self._revue(runner, 3600, moteur)
        self.assertEqual({t: self._xmin(t) for t in avant}, apres)

    def test_au_plus_un_lot_de_lignes_par_passage(self):
        turns = storage.of(self.db).turn_resources
        jeton = uuid.uuid4().hex[:6]
        mortes = ["tour-mort-%s-%d" % (jeton, i) for i in range(5)]
        for turn in mortes:
            turns.open_turn(turn, "a1", self.cfg.host, pgid=4242, label="l")
            turns.mark_orphan(turn)
        self._avant_le_demarrage(*mortes)
        moteur = mock.Mock()
        moteur.list_all.return_value = []
        runner = Runner(self.cfg, self.db, once=True)
        with mock.patch.object(runner_mod, "BOOT_ORPHAN_PAGE", 2), \
                mock.patch.object(runner_mod, "BOOT_REVIEW_MAX_ROWS", 2):
            self._revue(runner, 3600, moteur)
            self.assertIsNotNone(runner.boot_review_at)       # la suite, sans attendre
            self.assertLessEqual(runner.boot_review_at, time.monotonic())
            for _ in range(20):
                if runner.boot_review_at is None:
                    break
                self._revue(runner, 3600, moteur)
        self.assertIsNone(runner.boot_review_at)
        for turn in mortes:
            self.assertEqual(turns.get(turn)["status"], "done", turn)

    def test_le_solde_attend_le_gestionnaire_utilisateur(self):
        runner = Runner(self.cfg, self.db, once=True)
        for uptime, etat, pret in ((runner_mod.BOOT_SETTLE_S - 1, "running", False),
                                   (runner_mod.BOOT_SETTLE_S + 1, "starting", False),
                                   (runner_mod.BOOT_SETTLE_S + 1, "initializing", False),
                                   (runner_mod.BOOT_SETTLE_S + 1, "running", True),
                                   (runner_mod.BOOT_SETTLE_S + 1, "degraded", True),
                                   (runner_mod.BOOT_SETTLE_S + 1, None, True),
                                   (runner_mod.BOOT_SETTLE_MAX_S, "starting", True)):
            with self.subTest(uptime=uptime, etat=etat), \
                    mock.patch.object(runner, "_user_manager_state", return_value=etat):
                self.assertEqual(runner._boot_settle_ready(uptime), pret)

    def test_moteur_muet_un_tour_interrompu_reste_signale(self):
        turns = storage.of(self.db).turn_resources
        tour = "tour-muet-%s" % uuid.uuid4().hex[:6]
        turns.open_turn(tour, "a1", self.cfg.host, pgid=4242, label="l")
        self._avant_le_demarrage(tour)
        moteur = mock.Mock()
        moteur.list_all.return_value = None
        self.assertIn(tour, self._revue_au_demarrage(moteur))
        self.assertEqual(turns.get(tour)["status"], "orphan")

    def test_toutes_les_orphelines_d_avant_le_demarrage_par_page(self):
        # codex r1 P2 : plus de 500 orphelines, ou 500 plus récentes qui
        # survivent — chaque ligne d'avant le démarrage est revue
        turns = storage.of(self.db).turn_resources
        jeton = uuid.uuid4().hex[:6]
        anciennes = ["tour-ancien-%s-%d" % (jeton, i) for i in range(5)]
        recentes = ["tour-recent-%s-%d" % (jeton, i) for i in range(3)]
        for turn in anciennes + recentes:
            turns.open_turn(turn, "a1", self.cfg.host, pgid=4242, label="l")
            turns.mark_orphan(turn)
        self._avant_le_demarrage(*anciennes)
        with mock.patch.object(runner_mod, "BOOT_ORPHAN_PAGE", 2):
            restes = self._revue_au_demarrage()
        for turn in anciennes:
            self.assertEqual(turns.get(turn)["status"], "done", turn)
            self.assertNotIn(turn, restes)
        for turn in recentes:                 # ouvertes depuis : pas touchées
            self.assertEqual(turns.get(turn)["status"], "orphan", turn)
        page = turns.orphans_started_before(self.cfg.host, time.time(), 0, 2)
        self.assertEqual(len(page), 2)
        self.assertLess(int(page[0]["id"]), int(page[1]["id"]))

    def test_agent_place_sur_un_autre_hote_rendu_entre_deux_tours(self):
        # L147 : le canon a placé l'agent ailleurs pendant que ce worker
        # tenait son bail — il le rend, l'autre hôte peut le réclamer
        self.register("migrant", "claude", cwd=self._cwd("migrant"))
        runner = Runner(self.cfg, self.db, once=True)
        lease = registry.claim(self.db, "migrant", runner.runner_id, 3600)
        worker = AgentWorker(runner, registry.get(self.db, "migrant"), lease)
        self.addCleanup(worker.watchdog_stop.set)
        self.assertEqual(worker.placed_elsewhere(), "")
        self.db.execute("UPDATE agent_registry SET host = 'autre-hote' WHERE name = 'migrant'")
        self.assertEqual(worker.placed_elsewhere(), "autre-hote")
        with mock.patch.object(runner_mod, "log"):
            worker.run()
        self.assertIsNone(registry.get(self.db, "migrant").get("lease_owner"))

    def test_doctor_signale_un_agent_mene_sans_unite(self):
        self.register("sansunite", "claude", cwd=self._cwd("sansunite"))
        self.register("avecunite", "claude", cwd=self._cwd("avecunite"))
        unites = {("is-enabled", "ameesh-runner-agent@avecunite.service"): "enabled\n"}
        lignes = hostcheck.missing_runner_lines(
            self.cfg, self.db, run=lambda *a: unites.get(a, ""))
        texte = "\n".join(lignes)
        self.assertIn("sansunite", texte)
        self.assertNotIn("avecunite", texte.split("systemctl")[0])
        self.assertIn("systemctl --user enable --now ameesh-runner-agent@sansunite.service",
                      texte)
        # L146 : serveur du mesh — unité système, commande système
        S = hostcheck.SYSTEM
        unites = {
            (S, "list-unit-files", "--type=service", "--no-legend", "--plain"):
                "ameesh-runner@.service indirect enabled\n",
            (S, "is-enabled", "ameesh-runner@avecunite.service"): "enabled\n",
        }
        lignes = hostcheck.missing_runner_lines(
            self.cfg, self.db, run=lambda *a: unites.get(a, "") if a[:1] == (S,) else None)
        texte = "\n".join(lignes)
        self.assertNotIn("avecunite", texte.split("systemctl")[0])
        self.assertIn("systemctl enable --now ameesh-runner@sansunite.service", texte)

    def test_doctor_harness(self):
        self.register("dsh1", "deepseek", cwd=self._cwd("dsh1"))
        proc = self.cli("doctor", "--harness", env=self.env())
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("harnais    : deepseek [dsh1] → %s" % os.path.join(FAKEBIN, "dsh"),
                      proc.stdout)
        self.assertIn("interpréteur python3", proc.stdout)
        proc = self.cli("doctor", "--harness",
                        env=self.env(AMEESH_DSH_BIN=os.path.join(self.tmp, "absent")))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("deepseek [dsh1] KO", proc.stdout)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
