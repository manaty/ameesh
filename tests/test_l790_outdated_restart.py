# SPDX-License-Identifier: AGPL-3.0-only
"""L790 : l'exécuteur périmé se redémarre entre deux tours, le serveur médié
n'avertit qu'une fois.

Suite de #780 (PR #585) : `harnesses.stale_reason` détecte un paquet changé
sous le processus et `canon_sync.sync` refuse alors d'écrire. La relecture P3
de mesh-design (13/10) laissait deux gestes manuels :

1. le serveur médié périmé journalisait son `CanonStale` à CHAQUE passe ; il
   l'annonce une fois par (hôte, raison), et le réannonce si la péremption
   change ou revient après une passe réussie ;
2. l'exécuteur périmé ne se relançait pas : `Runner.canon_sync_once` demande
   maintenant le redémarrage (`restart_for_stale`), `sweep` cesse de réclamer
   des tours, les workers déjà réclamés sortent entre deux tours (bail rendu),
   la boucle sort en `EXIT_RESTART` quand le dernier tour (et le travail de
   fond) est fini, et `main` rejoue l'invocation (`os.execv`) **dans son mode
   d'appel** (`-m ameesh.runner`, script, `-m ameesh run` — `sys.argv[0]` seul
   perdrait le `-m`) ; sinon le code 75 fait relancer le service
   (`Restart=on-failure`).

Un tour en cours n'est pas interrompu : le redémarrage attend, comme le
drainage, que plus rien n'écrive. Deux témoins vont au-delà des doublures :
un vrai cycle `AgentWorker` avec un vrai bail (`WorkerStaleExitTest`), et le
remplacement réel des deux points d'entrée en sous-processus
(`RestartEndToEndTest`).
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

from ameesh import canon_sync, harnesses, registry
from ameesh import runner as runner_mod
from ameesh.mediated_executor import server as server_mod

from .support import SRC, PgTestCase


class _FakeBackground:
    def __init__(self, running: int = 0) -> None:
        self._running = running

    def running_count(self) -> int:
        return self._running


class _FakeWorker:
    def __init__(self, alive: bool = True) -> None:
        self._alive = alive
        self.wake = threading.Event()
        self.stopping = threading.Event()

    def is_alive(self) -> bool:
        return self._alive


def _fake_runner(**kwargs):
    base = dict(
        db=object(),
        restart_for_stale=False,
        _stale_woken=False,
        lock=threading.Lock(),
        workers={},
        background=_FakeBackground(0),
    )
    base.update(kwargs)
    return types.SimpleNamespace(**base)


class StaleReasonRequestsRestartTest(unittest.TestCase):
    """`canon_sync_once` demande le redémarrage et réveille la boucle."""

    def setUp(self):
        admission = mock.patch('ameesh.migrations.require_compatible_writer')
        admission.start()
        self.addCleanup(admission.stop)

    def test_passe_perimee_demande_le_redemarrage(self):
        fake = _fake_runner(cfg=types.SimpleNamespace(canon=object()), host="atelier")
        fake.wake_all = threading.Event()
        with mock.patch("ameesh.canon.configured", return_value=[object()]), \
                mock.patch.object(harnesses, "stale_reason", return_value="paquet change"):
            self.assertFalse(runner_mod.Runner.canon_sync_once(fake))
        self.assertIs(fake.restart_for_stale, True)
        self.assertTrue(fake.wake_all.is_set())
        self.assertIs(fake._canon_complete, False)   # porte d'hôte fermée

    def test_un_seul_constat_dans_le_fil(self):
        """Un seul constat dans le fil, quel que soit le nombre de passes."""
        fake = _fake_runner(cfg=types.SimpleNamespace(canon=object()), host="atelier")
        fake.wake_all = threading.Event()
        notes = []
        fake._fil_note = lambda text, meta=None: notes.append(text)
        with mock.patch("ameesh.canon.configured", return_value=[object()]), \
                mock.patch.object(harnesses, "stale_reason", return_value="paquet change"):
            runner_mod.Runner.canon_sync_once(fake)
            runner_mod.Runner.canon_sync_once(fake)
        self.assertEqual(len(notes), 1)


class StaleRestartIdleTest(unittest.TestCase):
    """Le redémarrage attend la fin du dernier tour et du travail de fond."""

    def test_aucun_tour(self):
        fake = _fake_runner(restart_for_stale=True)
        self.assertTrue(runner_mod.Runner._stale_restart_ready(fake))

    def test_tour_en_cours(self):
        fake = _fake_runner(restart_for_stale=True)
        fake.workers = {"a": _FakeWorker(alive=True)}
        self.assertFalse(runner_mod.Runner._stale_restart_ready(fake))

    def test_travail_de_fond_en_cours(self):
        fake = _fake_runner(restart_for_stale=True)
        fake.background = _FakeBackground(1)
        self.assertFalse(runner_mod.Runner._stale_restart_ready(fake))

    def test_sans_demande(self):
        fake = _fake_runner(restart_for_stale=False)
        self.assertFalse(runner_mod.Runner._stale_restart_ready(fake))

    def test_worker_au_repos_reveille_une_fois(self):
        """Un worker au repos attend son propre événement, pas `wake_all` :
        la passe le réveille, sans le réveiller à chaque tour de boucle."""
        fake = _fake_runner(restart_for_stale=True)
        worker = _FakeWorker(alive=True)
        fake.workers = {"a": worker}
        self.assertFalse(runner_mod.Runner._stale_restart_ready(fake))
        self.assertTrue(worker.wake.is_set(), "le worker au repos devait être réveillé")
        worker.wake.clear()
        self.assertFalse(runner_mod.Runner._stale_restart_ready(fake))
        self.assertFalse(worker.wake.is_set(), "un seul réveil suffit")

    def test_workers_morts_rendent_vrai(self):
        fake = _fake_runner(restart_for_stale=True)
        fake.workers = {"a": _FakeWorker(alive=False)}
        self.assertTrue(runner_mod.Runner._stale_restart_ready(fake))


class NoClaimAfterStaleTest(unittest.TestCase):
    """`sweep` ne réclame plus aucun lot dès que le redémarrage est demandé."""

    def setUp(self):
        admission = mock.patch('ameesh.migrations.require_compatible_writer')
        admission.start()
        self.addCleanup(admission.stop)

    def test_sweep_ne_reclame_pas(self):
        fake = _fake_runner(
            restart_for_stale=True,
            power_hold=threading.Event(),
            draining=threading.Event(),
            _gate_settled=threading.Event(),
            db=object(),
            host="atelier",
        )
        fake._gate_settled.set()
        fake.expire_delegations_once = lambda: None
        # L165 (EP2) : `sweep` publie aussi les scores de rôles de l'hôte ;
        # cette doublure n'a ni horloge ni registre, comme pour #508.
        fake.role_evaluation_once = lambda: None
        # #508 : `sweep` publie la preuve de vie avant la moisson ; ce témoin
        # L790 n'a ni horloge ni registre, la doublure la neutralise.
        fake.report_liveness = lambda *args, **kwargs: None
        # #3070 : `sweep` lève les portes d'appareil restées fermées ; cette
        # doublure n'a ni porte ni horloge, la passe est neutralisée.
        fake.lift_device_gates_once = lambda: None
        # #3961 : `sweep` lève les pauses opérateur échues ; la doublure n'a
        # ni registre ni horloge, la passe est neutralisée.
        fake.expire_operator_pauses_once = lambda: None
        # L85 (#373) : `sweep` prune le journal d'événements après la moisson
        # des baux — la doublure doit fournir le point d'entrée, borné ici.
        fake.prune_events_once = lambda *args, **kwargs: None
        # #1425 (N2, suite #753) : le dessaisissement des agents arrêtés vient
        # APRÈS les gardes — sous code périmé, l'exécuteur ne touche pas la
        # file. Seule la reprise est arrêtée ici, et le dessaisissement aussi.
        fake.release_stopped_lots_once = mock.Mock(return_value=[])
        with mock.patch.object(registry, "reap", return_value=[]), \
                mock.patch.object(registry, "claimable") as claimable:
            runner_mod.Runner.sweep(fake)
        fake.release_stopped_lots_once.assert_not_called()
        claimable.assert_not_called()


class SelectedTurnRefusedTest(unittest.TestCase):
    """L790 B1 : un tour choisi avant le constat ne part pas sur le code périmé.

    La consigne déjà prise repart en attente (comme au drainage) et le refus
    n'est pas compté comme un échec de tour (la série d'échecs rapides, L48,
    n'est pas touchée)."""

    def setUp(self):
        admission = mock.patch('ameesh.migrations.require_compatible_writer')
        admission.start()
        self.addCleanup(admission.stop)

    def test_run_turn_remet_la_consigne_et_refuse(self):
        runner = types.SimpleNamespace(
            draining=threading.Event(), restart_for_stale=True, runner_id="runner-1")
        # #290 : `run_turn` refuse l'attach et prend le verrou de session
        # avant le tour ; la doublure les satisfait sans E/S (le refus périmé
        # testé reste dans `_run_guarded_turn`).
        state_dir = tempfile.TemporaryDirectory()
        self.addCleanup(state_dir.cleanup)
        worker = types.SimpleNamespace(
            runner=runner, db=object(), name="perime", epoch=1, fast_failure=True,
            _attach_pending=lambda: False, _session_lock=mock.Mock(),
            resident=object(), state_dir=state_dir.name,
            # #2810 : l'enveloppe efface les messages servis à la fin du tour.
            _servir=mock.Mock(),
            # #2096 : `run_turn` rend la mémoire du tour dans son `finally` ;
            # la doublure n'a pas d'allocateur, le rappel est inerte.
            # #2894 : `run_turn` retire d'abord un document de tour périmé ;
            # la doublure n'a pas de document, le rappel est inerte.
            _release_turn_memory=lambda *a, **k: None,
            _purge_turn_context=lambda: None)
        # #290 a séparé l'enveloppe (`run_turn`) du tour (`_run_guarded_turn`) :
        # la doublure porte la vraie logique de tour et une enveloppe inerte.
        worker._run_guarded_turn = types.MethodType(
            runner_mod.AgentWorker._run_guarded_turn, worker)
        # #2614 : `run_turn` est l'enveloppe du claim de réveil ; la doublure
        # porte aussi le tour réel, appelé par cette enveloppe.
        worker._run_turn_or_defer = types.MethodType(
            runner_mod.AgentWorker._run_turn_or_defer, worker)
        # #2894 : l'allocation de l'identifiant de tour, statique sur le worker.
        worker._with_turn_id = runner_mod.AgentWorker._with_turn_id
        # #1462 : run_turn lit d’abord la pause opérateur dans le registre —
        # agent non pausé ici, la doublure de base ne porte pas de requêtes.
        with mock.patch.object(registry, "restore_prompt", return_value=True) as restore, \
                mock.patch.object(registry, "get", return_value={}), \
                mock.patch.object(registry, "set_status") as set_status:
            self.assertFalse(runner_mod.AgentWorker.run_turn(worker, {}))
        restore.assert_called_once_with(worker.db, "perime", "runner-1", 1)
        self.assertEqual(set_status.call_args[0][:3], (worker.db, "perime", "queued"))
        self.assertFalse(worker.fast_failure)
        worker._servir.assert_called_once_with(())


class RestartExitTest(unittest.TestCase):
    """La sortie dédiée rejoue l'invocation, sinon le service relance."""

    def test_code_dedie_non_nul_et_distinct(self):
        self.assertNotEqual(runner_mod.EXIT_RESTART, 0)
        self.assertNotEqual(runner_mod.EXIT_RESTART, runner_mod.EXIT_REVOKED)

    def test_execv_rejoue_l_invocation(self):
        """Le mode `-m ameesh.runner` est conservé : `sys.argv[0]` porte le
        chemin du module, pas le mode d'appel."""
        with mock.patch.object(runner_mod.sys, "orig_argv",
                               ["/usr/bin/python3", "-m", "ameesh.runner",
                                "--host", "atelier", "--poll", "1"]), \
                mock.patch.object(runner_mod.os, "execv") as execv:
            self.assertTrue(runner_mod._restart_with_installed_code())
        attendu = [sys.executable, "-m", "ameesh.runner", "--host", "atelier", "--poll", "1"]
        execv.assert_called_once_with(attendu[0], attendu)

    def test_mode_ameesh_run_preserve(self):
        with mock.patch.object(runner_mod.sys, "orig_argv",
                               ["/usr/bin/python3", "-m", "ameesh", "run", "--poll", "2"]):
            self.assertEqual(runner_mod._restart_argv(),
                             [sys.executable, "-m", "ameesh", "run", "--poll", "2"])

    def test_options_de_l_interpreteur_conservees(self):
        with mock.patch.object(runner_mod.sys, "orig_argv",
                               ["/usr/bin/python3", "-X", "dev", "-m", "ameesh.runner"]):
            self.assertEqual(runner_mod._restart_argv(),
                             [sys.executable, "-X", "dev", "-m", "ameesh.runner"])

    def test_sans_orig_argv_sous_m_on_ne_devine_pas(self):
        """Python 3.9 : sous `-m`, `sys.argv[0]` porte le fichier du module ;
        rejouer le fichier seul casserait les imports relatifs. On ne devine
        pas : la sortie 75 laisse le service relancer (`Restart=on-failure`)."""
        with mock.patch.object(runner_mod.sys, "orig_argv", None), \
                mock.patch.object(runner_mod.os, "execv") as execv:
            self.assertEqual(runner_mod._restart_argv(), [])
            self.assertFalse(runner_mod._restart_with_installed_code())
        execv.assert_not_called()

    def test_sans_orig_argv_invocation_par_fichier_rejouee(self):
        with mock.patch.object(runner_mod.sys, "orig_argv", None), \
                mock.patch.object(runner_mod, "__package__", ""):
            self.assertEqual(runner_mod._restart_argv(),
                             [sys.executable] + list(sys.argv))

    def test_echec_d_execv_rend_faux(self):
        with mock.patch.object(runner_mod.os, "execv", side_effect=OSError("boom")):
            self.assertFalse(runner_mod._restart_with_installed_code())


class MediatedServerStaleOnceTest(unittest.TestCase):
    """Le serveur médié n'avertit qu'une fois par (hôte, raison)."""

    HOST = "atelier"

    def setUp(self):
        server_mod._stale_reported.clear()
        self.cfg = object()
        self.db = object()

    @staticmethod
    def _stale(reason: str):
        return canon_sync.CanonStale(MediatedServerStaleOnceTest.HOST, reason)

    def _run_once(self, sync):
        patches = [
            mock.patch.object(server_mod, "mediated_hosts", return_value=[self.HOST]),
            mock.patch("ameesh.canon.configured", return_value=True),
            mock.patch("ameesh.canon.from_config", return_value=object()),
            mock.patch("ameesh.canon_sync.configured_ref_for", return_value=None),
            mock.patch("ameesh.canon_sync.sync", side_effect=sync),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return server_mod.sync_canon_once(self.cfg, self.db)

    def test_avertissement_unique_par_raison(self):
        calls = []

        def sync(db, canon, host, trusted_ref=None):
            calls.append(host)
            raise self._stale("paquet change")

        with self.assertLogs("ameesh.exec", level="WARNING") as logs:
            self.assertEqual(self._run_once(sync), [])
            self.assertEqual(self._run_once(sync), [])
        self.assertEqual(len(calls), 2)
        messages = [r.getMessage() for r in logs.records]
        self.assertEqual(len([m for m in messages if "code périmé" in m]), 1)

    def test_raison_nouvelle_reavertit(self):
        raisons = iter(["paquet change", "autre paquet change"])

        def sync(db, canon, host, trusted_ref=None):
            raise self._stale(next(raisons))

        with self.assertLogs("ameesh.exec", level="WARNING") as logs:
            self._run_once(sync)
            self._run_once(sync)
        messages = [r.getMessage() for r in logs.records]
        self.assertEqual(len([m for m in messages if "code périmé" in m]), 2)

    def test_passe_reussie_rearme_l_avertissement(self):
        states = iter(["stale", "ok", "stale"])

        def sync(db, canon, host, trusted_ref=None):
            if next(states) == "stale":
                raise self._stale("paquet change")

        with self.assertLogs("ameesh.exec", level="WARNING") as logs:
            self.assertEqual(self._run_once(sync), [])           # averti
            self.assertEqual(self._run_once(sync), [self.HOST])  # réussie
            self.assertEqual(self._run_once(sync), [])           # averti de nouveau
        messages = [r.getMessage() for r in logs.records]
        self.assertEqual(len([m for m in messages if "code périmé" in m]), 2)

    def test_erreur_ordinaire_reste_signalee_a_chaque_passe(self):
        def sync(db, canon, host, trusted_ref=None):
            raise RuntimeError("base injoignable")

        with self.assertLogs("ameesh.exec", level="WARNING") as logs:
            self._run_once(sync)
            self._run_once(sync)
        messages = [r.getMessage() for r in logs.records]
        self.assertEqual(len([m for m in messages if "en échec" in m]), 2)
        self.assertEqual([m for m in messages if "code périmé" in m], [])


#: L790 : injecté par `PYTHONPATH` dans la PREMIÈRE incarnation du processus
#: de test — le constat de péremption est forcé une seule fois (marqueur), pour
#: que la seconde incarnation, née du `os.execv`, reparte sur un code sain et
#: que le témoin observe le remplacement, pas une boucle de redémarrages.
SITECUSTOMIZE_L790 = '''\
"""Témoin L790 : force un constat de péremption dans la première incarnation."""
import os

_marqueur = os.environ.get("L790_STALE_ONCE")
if _marqueur and not os.path.exists(_marqueur):
    try:
        with open(_marqueur, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
    except OSError:
        pass
    import ameesh.harnesses
    ameesh.harnesses.stale_reason = lambda: "forced by the L790 witness"
'''


class WorkerStaleExitTest(PgTestCase):
    """L790 B1 : un worker déjà réclamé sort entre deux tours, bail rendu.

    Le worker est réel (vrai `AgentWorker`, vrai bail en base) ; il vit au
    repos, sans consigne. La passe de péremption doit le réveiller et le faire
    sortir entre deux tours — aucun tour ne part sur le code périmé, et le bail
    est rendu par son `finally`."""

    def test_worker_au_repos_sort_sans_tour_et_rend_le_bail(self):
        cwd = os.path.join(self.tmp, "work", "perime")
        os.makedirs(cwd, exist_ok=True)
        self.register("perime", "claude", cwd=cwd)          # aucune consigne
        runner = runner_mod.Runner(self.cfg, self.db)
        lease = registry.claim(self.db, "perime", runner.runner_id, 300)
        self.assertIsNotNone(lease)
        worker = runner_mod.AgentWorker(runner, registry.get(self.db, "perime"), lease)
        self.addCleanup(self._sortir, worker)
        with runner.lock:                       # comme `sweep` après réclamation
            runner.workers["perime"] = worker
        worker.start()
        self.wait_for(worker.is_alive, timeout=10)
        runner.restart_for_stale = True
        pret = runner._stale_restart_ready()
        if worker.is_alive():
            self.assertFalse(pret, "la passe attend la mort du worker")
            # Pas d'assertion sur `worker.wake` ici : le worker réel peut le
            # consommer (`clear`) entre ce constat et le nôtre, puis vivre
            # jusqu'à son `finally` — l'assertion dépendrait de l'ordonnancement
            # (R2 cx1). Le réveil des workers au repos est couvert par la
            # doublure, sans course : `test_worker_au_repos_reveille_une_fois`.
        worker.join(timeout=15)
        self.assertFalse(worker.is_alive(), "le worker devait sortir entre deux tours")
        self.assertTrue(runner._stale_restart_ready(), "plus rien n'écrit : on peut se remplacer")
        self.assertEqual(len(self.turns()), 0, "aucun tour ne part sur le code périmé")
        self.assertIsNone(registry.get(self.db, "perime")["lease_owner"])

    @staticmethod
    def _sortir(worker) -> None:
        if worker.is_alive():
            worker.stopping.set()
            worker.wake.set()
            worker.join(timeout=10)


class RestartEndToEndTest(PgTestCase):
    """L790 B2 : le remplacement rejoue le VRAI mode d'appel, sous processus.

    Les deux points d'entrée réels sont lancés tels quels (`-m ameesh.runner`,
    `-m ameesh run`). La première incarnation est rendue périmée par injection
    (`sitecustomize`) : elle sort en `EXIT_RESTART` et se remplace par
    `os.execv` — même PID et même tube. La seconde incarnation doit redémarrer
    (un second « starting: host ») au lieu de mourir sur les imports relatifs,
    ce que produisait l'ancien vecteur `[python, …/runner.py]`.
    """

    def _lance(self, *entree: str) -> tuple[subprocess.Popen, str]:
        inject = os.path.join(self.tmp, "inject790")
        os.makedirs(inject, exist_ok=True)
        with open(os.path.join(inject, "sitecustomize.py"), "w", encoding="utf-8") as fh:
            fh.write(SITECUSTOMIZE_L790)
        canon = os.path.join(self.tmp, "canon790")
        os.makedirs(canon, exist_ok=True)
        marqueur = os.path.join(self.tmp, "perime790")
        env = self.env(
            AMEESH_CANON=canon,
            L790_STALE_ONCE=marqueur,
            PYTHONPATH=inject + os.pathsep + SRC,
        )
        proc = subprocess.Popen(
            [sys.executable, *entree, "--host", self.cfg.host, "--poll", "1"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            env=env, cwd=self.tmp,
        )
        self.addCleanup(lambda: proc.poll() is None and (proc.kill(), proc.wait(10)))
        return proc, marqueur

    def _apres_le_remplacement(self, proc) -> str:
        lignes: list[str] = []
        lecteur = threading.Thread(target=lambda: [lignes.append(l) for l in proc.stdout],
                                   daemon=True)
        lecteur.start()
        self.wait_for(lambda: sum("starting: host" in l for l in lignes) >= 2,
                      timeout=60)
        proc.send_signal(signal.SIGTERM)
        self.wait_for(lambda: proc.poll() is not None, timeout=60)
        lecteur.join(timeout=5)
        proc.stdout.close()
        return "".join(lignes)

    def _verifie(self, *entree: str) -> None:
        proc, marqueur = self._lance(*entree)
        sortie = self._apres_le_remplacement(proc)
        # la première incarnation (marqueur) et la seconde (execv) : même PID
        with open(marqueur, encoding="utf-8") as fh:
            self.assertEqual(int(fh.read()), proc.pid)
        self.assertEqual(proc.returncode, 0, sortie)
        self.assertEqual(sortie.count("outdated runner: restarting between turns"), 1,
                         "une seule péremption : pas de boucle de redémarrages")
        self.assertGreaterEqual(sortie.count("starting: host"), 2, sortie)
        for accident in ("ImportError", "attempted relative import", "Traceback"):
            self.assertNotIn(accident, sortie)

    def test_m_ameesh_runner_se_remplace(self):
        self._verifie("-m", "ameesh.runner")

    def test_m_ameesh_run_se_remplace(self):
        self._verifie("-m", "ameesh", "run")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
