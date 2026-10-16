# SPDX-License-Identifier: AGPL-3.0-only
"""L48 : une série d'échecs rapides (harnais introuvable, qui sort aussitôt)
n'est plus relancée toutes les 5 s sans fin : attente doublée à chaque échec,
puis arrêt de l'agent (`stop_reason` = erreur) au-delà du seuil."""
from __future__ import annotations

import dataclasses
import os
from unittest import mock

from ameesh import registry
from ameesh.runner import AgentWorker, Runner

from .support import PgTestCase


def secondes(attentes):
    """#1270 : l'attente est découpée au temps **restant**, mesuré à l'instant
    de l'appel — la valeur porte donc les microsecondes écoulées. On compare à
    la milliseconde : l'assertion ne dépend plus de l'ordonnanceur."""
    return [round(t, 3) for t in attentes]


class Horloge:
    """#1914 : horloge monotone simulée — l'attente simulée de `_boucle` avance
    son temps, la sonde ne dort jamais."""

    def __init__(self, depart: float = 1000.0):
        self.t = depart

    def maintenant(self) -> float:
        return self.t

    def avance(self, secondes: float) -> None:
        self.t += max(0.0, secondes or 0.0)


class EchecsRapidesTest(PgTestCase):
    def _worker(self, name="rapide", **cfg):
        os.makedirs(os.path.join(self.tmp, "work", name), exist_ok=True)
        self.register(name, "claude", cwd=os.path.join(self.tmp, "work", name), prompt="tour")
        runner = Runner(dataclasses.replace(self.cfg, **cfg), self.db, once=False)
        # #1914 : bail réel — le blocage « session lock held » est marqué et
        # fencé par le bail (posé et levé sous notre epoch).
        lease = registry.claim(self.db, name, runner.runner_id, 3600)
        self.assertIsNotNone(lease)
        return AgentWorker(runner, registry.get(self.db, name), lease)

    def test_attente_doublee_et_bornee(self):
        worker = self._worker(failure_backoff_max=30.0)
        attentes = []
        for n in range(1, 7):
            worker.fast_failures = n
            attentes.append(worker.failure_wait())
        self.assertEqual(attentes, [5.0, 10.0, 20.0, 30.0, 30.0, 30.0])

    def _boucle(self, worker, resultats, horloge=None):
        """Fait tourner `run()` sur une suite de tours simulés (True = réussi,
        False = échec rapide, None = échec lent, « differe » = tour reporté
        #1686, « verrou » = report par verrou de session tenu #1914), sans
        attente réelle. Avec `horloge`, chaque attente simulée avance son
        temps."""
        suite = iter(resultats)

        def tour(_spec):
            r = next(suite, "fin")
            if r == "fin":
                worker.stopping.set()
                return True
            if r in ("differe", "verrou"):
                # #1686 N2 : tour reporté (attach ou verrou de session) ; il
                # n'a pas échoué et `fast_failure` garde la valeur du tour
                # précédent, comme dans le worker réel (le chemin court ne
                # repasse pas par `_run_guarded_turn`). #1914 : `run_turn` laisse
                # le motif dans `deferred_reason`.
                worker.deferred_reason = ("attach requested" if r == "differe"
                                          else "session lock held")
                return None
            worker.fast_failure = r is False
            return bool(r)

        attentes = []

        def attente(timeout=None):
            attentes.append(timeout)
            if horloge is not None:
                horloge.avance(timeout)

        with mock.patch.object(worker, "renew", return_value=True), \
                mock.patch.object(worker, "pick", return_value={"kind": "consigne"}), \
                mock.patch.object(worker, "rotate_for_lot", side_effect=lambda s: s), \
                mock.patch.object(worker, "maybe_rotate"), \
                mock.patch.object(worker, "apply_restart_if_requested"), \
                mock.patch.object(worker, "ensure_watchdog"), \
                mock.patch.object(worker, "release_lease"), \
                mock.patch.object(worker, "run_turn", side_effect=tour), \
                mock.patch.object(worker.wake, "wait", side_effect=attente):
            worker.run()
        return attentes

    def test_serie_d_echecs_rapides_arrete_l_agent(self):
        worker = self._worker(max_fast_failures=3)
        attentes = self._boucle(worker, [False, False, False, False])
        self.assertEqual(secondes(attentes), [5.0, 10.0])  # pas d'attente après l'arrêt
        row = registry.get(self.db, "rapide")
        self.assertEqual(row["status"], "stopped")
        self.assertEqual(row["stop_reason"], "erreur")
        self.assertIn("3 consecutive fast failures", row["status_text"])
        self.assertEqual(row["pending_prompt"], "tour")  # la consigne reste en attente

    def test_un_succes_remet_la_serie_a_zero(self):
        worker = self._worker(max_fast_failures=3)
        attentes = self._boucle(worker, [False, False, True, False, False, True])
        self.assertEqual(secondes(attentes), [5.0, 10.0, 5.0, 10.0])
        self.assertNotEqual(registry.get(self.db, "rapide")["status"], "stopped")

    def test_echec_lent_ne_compte_pas(self):
        worker = self._worker(max_fast_failures=2)
        self._boucle(worker, [None, None, None, False])
        self.assertNotEqual(registry.get(self.db, "rapide")["status"], "stopped")

    def test_tour_differe_ne_compte_pas_comme_un_echec(self):
        """#1686 N2 : un tour reporté (attach demandé ou verrou de session tenu)
        n'a pas échoué — la consigne est restaurée. Il ne compte ni comme échec
        rapide, ni comme échec lent : pas d'attente d'échec (5 s), seulement le
        pas court de nouvelle tentative (0,5 s)."""
        worker = self._worker(max_fast_failures=2)
        attentes = self._boucle(worker, ["differe", "differe", "differe", False])
        self.assertEqual(secondes(attentes), [0.5, 0.5, 0.5, 5.0])
        self.assertNotEqual(registry.get(self.db, "rapide")["status"], "stopped")

    def test_tour_differe_ne_remet_pas_la_serie_a_zero(self):
        """Un report ne masque pas les échecs rapides qui l'entourent : la série
        continue de compter et l'agent s'arrête au seuil, sans attente après
        l'arrêt."""
        worker = self._worker(max_fast_failures=2)
        attentes = self._boucle(worker, [False, "differe", False])
        self.assertEqual(secondes(attentes), [5.0, 0.5])
        self.assertEqual(worker.fast_failures, 2)
        row = registry.get(self.db, "rapide")
        self.assertEqual(row["status"], "stopped")
        self.assertEqual(row["stop_reason"], "erreur")

    def test_verrou_de_session_tenu_devient_visible_et_espace(self):
        """#1914 : un verrou session-use tenu par un tiers (hors attach) ne fait
        plus prendre et rendre la consigne deux fois par seconde sans fin :
        passé le délai de grâce, le worker journalise **une fois**, pose le
        statut « session lock held » et espace ses reprises ; le premier tour
        lancé remet la série à zéro."""
        worker = self._worker(max_fast_failures=2)
        worker.deferred_lock_grace = lambda: 2.0
        horloge = Horloge()
        worker.monotonic = horloge.maintenant
        statuts = []
        avance_reelle = horloge.avance

        def avance(dt):
            avance_reelle(dt)
            statuts.append((registry.get(self.db, "rapide") or {}).get("status_text") or "")

        horloge.avance = avance
        with mock.patch("ameesh.runner.log") as journal:
            attentes = self._boucle(worker, ["verrou"] * 40 + [True], horloge=horloge)
        # journalisé une fois, et le statut a bien été posé pendant la tenue
        lignes = [c.args[0] for c in journal.call_args_list
                  if c.args and "session lock held" in str(c.args[0])]
        self.assertEqual(len(lignes), 1, lignes)
        self.assertTrue(any(t.startswith("session lock held") for t in statuts), statuts)
        self.assertTrue(all(t.startswith("session lock held") for t in statuts[4:]), statuts)
        # le pas court tient pendant la grâce (2 s), puis s'allonge et se borne
        self.assertEqual(secondes(attentes[:4]), [0.5, 0.5, 0.5, 0.5])
        self.assertEqual(secondes(attentes[4:8]), [1.0, 2.0, 4.0, 8.0])
        self.assertEqual(max(attentes), worker.DEFERRED_RETRY_MAX)
        # sur 10 min de verrou tenu, le nombre de prises reste borné (une boucle
        # à 0,5 s en ferait 1200) : la consigne n'est plus martelée
        cumul, prises = 0.0, 0
        for a in attentes:
            if cumul > 600.0:
                break
            prises += 1
            cumul += a
        self.assertLessEqual(prises, 30)
        self.assertGreater(cumul, 600.0)
        # premier tour lancé : série et statut remis à zéro
        self.assertIsNone(worker.deferred_since)
        self.assertEqual(worker.deferred_streak, 0)
        self.assertFalse(worker.deferred_alerted)
        row = registry.get(self.db, "rapide")
        self.assertFalse((row["status_text"] or "").startswith("session lock held"),
                         row["status_text"])

    def test_attach_demande_ne_pose_pas_le_statut_de_verrou(self):
        """#1914 : seul un verrou tenu par un tiers est rendu visible — un
        attach demandé reste un report silencieux au pas court (#1686 N2
        inchangé : le résident reprend la main dès la passation finie)."""
        worker = self._worker(max_fast_failures=2)
        worker.deferred_lock_grace = lambda: 2.0
        horloge = Horloge()
        worker.monotonic = horloge.maintenant
        with mock.patch("ameesh.runner.log") as journal:
            attentes = self._boucle(worker, ["differe"] * 8 + [True], horloge=horloge)
        row = registry.get(self.db, "rapide")
        self.assertFalse((row["status_text"] or "").startswith("session lock held"),
                         row["status_text"])
        self.assertFalse(worker.deferred_alerted)
        self.assertEqual(secondes(attentes), [0.5] * 8)  # aucun espacement
        lignes = [c.args[0] for c in journal.call_args_list
                  if c.args and "session lock held" in str(c.args[0])]
        self.assertEqual(lignes, [])

    def test_verrou_ne_recouvre_pas_un_autre_blocage(self):
        """#1914 : un agent déjà bloqué pour une autre raison (budget, auth,
        hôte…) garde son motif : le verrou tenu est journalisé et les reprises
        espacées, mais la marque d'un autre blocage n'est ni recouverte ni
        levée."""
        worker = self._worker(max_fast_failures=2)
        worker.deferred_lock_grace = lambda: 2.0
        horloge = Horloge()
        worker.monotonic = horloge.maintenant
        registry.set_marked_block(self.db, "rapide", worker.runner.runner_id,
                                  worker.epoch, "budget : plafond",
                                  "budget : plafond horaire", "budget : ")
        with mock.patch("ameesh.runner.log") as journal:
            attentes = self._boucle(worker, ["verrou"] * 8, horloge=horloge)
        row = registry.get(self.db, "rapide")
        self.assertEqual(row["status"], "blocked")
        self.assertEqual(row["status_text"], "budget : plafond")
        self.assertEqual(row["last_error"], "budget : plafond horaire")
        lignes = [c.args[0] for c in journal.call_args_list
                  if c.args and "session lock held" in str(c.args[0])]
        self.assertEqual(len(lignes), 1, lignes)
        # l'attente s'espace bien au-delà de la grâce, sans poser de marque
        self.assertEqual(secondes(attentes[4:6]), [1.0, 2.0])
        self.assertFalse(worker.deferred_alerted)
