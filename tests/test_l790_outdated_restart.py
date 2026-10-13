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
   des tours, la boucle sort en `EXIT_RESTART` quand le dernier tour (et le
   travail de fond) est fini, et `main` rejoue l'invocation (`os.execv`) —
   sinon le code 75 fait relancer le service (`Restart=on-failure`).

Un tour en cours n'est pas interrompu : le redémarrage attend, comme le
drainage, que plus rien n'écrive.
"""
from __future__ import annotations

import threading
import types
import unittest
from unittest import mock

from ameesh import canon_sync, harnesses, registry
from ameesh import runner as runner_mod
from ameesh.mediated_executor import server as server_mod


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
        restart_for_stale=False,
        lock=threading.Lock(),
        workers={},
        background=_FakeBackground(0),
    )
    base.update(kwargs)
    return types.SimpleNamespace(**base)


class StaleReasonRequestsRestartTest(unittest.TestCase):
    """`canon_sync_once` demande le redémarrage et réveille la boucle."""

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


class NoClaimAfterStaleTest(unittest.TestCase):
    """`sweep` ne réclame plus aucun lot dès que le redémarrage est demandé."""

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
        with mock.patch.object(registry, "reap", return_value=[]), \
                mock.patch.object(registry, "claimable") as claimable:
            runner_mod.Runner.sweep(fake)
        claimable.assert_not_called()


class RestartExitTest(unittest.TestCase):
    """La sortie dédiée rejoue l'invocation, sinon le service relance."""

    def test_code_dedie_non_nul_et_distinct(self):
        self.assertNotEqual(runner_mod.EXIT_RESTART, 0)
        self.assertNotEqual(runner_mod.EXIT_RESTART, runner_mod.EXIT_REVOKED)

    def test_execv_rejoue_l_invocation(self):
        with mock.patch.object(runner_mod.os, "execv") as execv:
            self.assertTrue(runner_mod._restart_with_installed_code())
        execv.assert_called_once_with(
            runner_mod.sys.executable,
            [runner_mod.sys.executable] + list(runner_mod.sys.argv))

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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
