# SPDX-License-Identifier: AGPL-3.0-only
"""#7632 — réveil borné d'un avatar (spec avatars §4.4, décision owner du 17/11).

La fiche déclare `reveil` : les messages de l'humain, les urgences, les
demandes, les réponses et les décisions ouvrent un tour tout de suite ; le
reste est regroupé et remis au tour suivant, au plus tard à la fin de la
fenêtre (30 min par défaut). Acceptation : 100 courriers ordinaires en une
heure ouvrent au plus deux tours ; un message humain ou urgent ouvre un tour
tout de suite ; sans déclaration, rien ne change.
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import io
import json
import os
import time
import unittest
from unittest import mock

from ameesh import canon, mail, mesh_cli, registry, reveil
from ameesh.runner import AgentWorker, Runner

from .support import PgTestCase
from .test_435_agent_projects import orchestre
from .test_canon import _CanonDbCase, codes, write


def _ligne(ident: int = 1, kind: str = "notify", payload: dict | None = None,
           sender: str = "pair", created_ts: float = 0.0) -> dict:
    return {"id": ident, "sender": sender, "kind": kind, "payload": payload or {},
            "body": "texte", "created_ts": created_ts}


def _carte(bloc: list[str], **fields) -> str:
    """La fiche `orchestre` avec un bloc `reveil` en YAML de bloc."""
    texte = orchestre(**fields)
    entete, reste = texte.split("\n---\n", 1)
    return "\n".join([entete] + bloc) + "\n---\n" + reste


class PolitiqueTest(unittest.TestCase):
    def test_sans_bloc_aucune_politique(self):
        self.assertEqual(reveil.parse(None), (None, []))
        self.assertIsNone(reveil.of({}))
        self.assertIsNone(reveil.of({"reveil": None}))
        self.assertEqual(reveil.describe(None), "per mail (no `reveil` declared)")

    def test_bloc_vide_regle_de_l_owner(self):
        policy, problems = reveil.parse({})
        self.assertEqual(problems, [])
        self.assertEqual(policy, {"immediat": list(reveil.IMMEDIAT_DEFAUT),
                                  "fenetre_s": 1800})
        self.assertEqual(reveil.describe(policy),
                         "immediate: humain, urgent, request, reply, decision; "
                         "the rest grouped every 30 min")

    def test_cles_lues(self):
        for brut, attendu in (("30m", 1800), ("90s", 90), ("1h", 3600), (600, 600),
                              ("0", 0)):
            policy, problems = reveil.parse({"immediat": ["Humain", "urgent", "urgent"],
                                             "fenetre": brut})
            self.assertEqual(problems, [], brut)
            self.assertEqual(policy, {"immediat": ["humain", "urgent"], "fenetre_s": attendu})

    def test_bloc_fautif_signale_sans_retomber_sur_le_courrier(self):
        for brut, morceau in (("toujours", "mapping expected"),
                              ({"immediat": "humain"}, "list of natures"),
                              ({"immediat": ["humain", "slack"]}, "unknown nature(s) slack"),
                              ({"fenetre": "bientôt"}, "duration expected"),
                              ({"fenetre": "2d"}, "duration expected"),
                              ({"fenetre": "25h"}, "at most 24 h"),
                              ({"cadence": 3}, "unknown key(s) cadence")):
            policy, problems = reveil.parse(brut)
            self.assertTrue(any(morceau in p for p in problems), (brut, problems))
            self.assertIsNotNone(policy, brut)
            self.assertIn("humain", policy["immediat"])

    def test_ligne_du_registre(self):
        brut = {"immediat": ["humain", "request"], "fenetre_s": 900}
        self.assertEqual(reveil.of({"reveil": brut})["fenetre_s"], 900.0)
        self.assertEqual(reveil.of({"reveil": json.dumps(brut)})["immediat"],
                         ["humain", "request"])
        for illisible in ("{", 7, ["x"], {"fenetre_s": "x"}):
            self.assertIsNone(reveil.of({"reveil": illisible}), illisible)

    def test_natures(self):
        policy = reveil.parse({})[0]
        self.assertEqual(reveil.natures(_ligne(kind="request")), {"request"})
        self.assertEqual(reveil.natures(_ligne(kind="reply")), {"reply"})
        self.assertEqual(reveil.natures(_ligne(payload={"urgent": True})),
                         {"notify", "urgent"})
        self.assertIn("humain", reveil.natures(_ligne(sender="human:smichea")))
        self.assertIn("humain", reveil.natures(_ligne(), human=True))
        self.assertIn("decision", reveil.natures(
            _ligne(kind="request", payload={"decision": {"id": 4}})))
        self.assertIn("decision", reveil.natures(
            _ligne(kind="reply", payload={"decision_answer": {"decision": 4}})))
        for row in (_ligne(kind="request"), _ligne(kind="reply"),
                    _ligne(payload={"urgent": True}), _ligne(sender="human:x")):
            self.assertTrue(reveil.immediate(policy, row), row)
        for row in (_ligne(), _ligne(kind="event")):
            self.assertFalse(reveil.immediate(policy, row), row)
        # l'humain réveille toujours, même absent de la liste
        etroite = reveil.parse({"immediat": ["urgent"]})[0]
        self.assertTrue(reveil.immediate(etroite, _ligne(sender="human:x")))
        self.assertFalse(reveil.immediate(etroite, _ligne(kind="request")))

    def test_prochain_reveil(self):
        policy = reveil.parse({"fenetre": "30m"})[0]
        self.assertIsNone(reveil.next_wake(policy, [], last_turn_ts=None, now=0))
        rows = [_ligne(1, created_ts=1000.0), _ligne(2, created_ts=1500.0)]
        self.assertEqual(reveil.next_wake(policy, rows, last_turn_ts=None, now=0), 2800.0)
        self.assertEqual(reveil.next_wake(policy, rows, last_turn_ts=2000.0, now=0), 3800.0)


class CarteTest(_CanonDbCase):
    def agent(self, name="orchestre"):
        return next(a for a in self.load().agents if a.title == name)

    def erreurs(self):
        loaded = self.load()
        return codes(list(loaded.load_findings) + list(canon.validate(loaded)), canon.ERROR)

    def test_la_fiche_declare_sa_cadence(self):
        write(self.root, "agents/orchestre.md", _carte(
            ["reveil:", "  immediat: [humain, urgent, request]", "  fenetre: 20m"]))
        self.assertEqual(self.agent().reveil,
                         {"immediat": ["humain", "urgent", "request"], "fenetre_s": 1200})
        self.assertNotIn("agent-reveil-invalid", self.erreurs())

    def test_sans_declaration_rien(self):
        write(self.root, "agents/orchestre.md", orchestre())
        self.assertIsNone(self.agent().reveil)

    def test_une_cadence_fautive_est_une_erreur_de_fiche(self):
        write(self.root, "agents/orchestre.md", _carte(
            ["reveil:", "  immediat: [humain, slack]", "  fenetre: bientôt"]))
        self.assertIn("agent-reveil-invalid", self.erreurs())

    def test_sync_ecrit_puis_vide_la_colonne(self):
        write(self.root, "agents/orchestre.md", _carte(
            ["reveil:", "  fenetre: 30m"]))
        self.sync()
        self.assertEqual(self.row("orchestre")["reveil"],
                         {"immediat": list(reveil.IMMEDIAT_DEFAUT), "fenetre_s": 1800})
        self.assertEqual(self.actions(self.sync())["orchestre"], "inchangé")
        write(self.root, "agents/orchestre.md", orchestre())
        self.assertNotEqual(self.actions(self.sync())["orchestre"], "inchangé")
        self.assertIsNone(self.row("orchestre")["reveil"])


class _Horloge:
    """Le module `time` vu par l'exécuteur, sur une horloge simulée."""

    def __init__(self, start: float) -> None:
        self.now = start

    def time(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.now

    def __getattr__(self, name):
        return getattr(time, name)


class _Base(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM turn_resources")

    def _worker(self, name: str, *, policy: dict | None, batch: float = 60.0):
        cfg = dataclasses.replace(self.cfg, humans="", interrupt_senders=())
        runner = Runner(cfg, self.db, once=True)
        runner.mail_batch = batch
        runner.event_coalesce = 120.0
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        self.register(name, "claude", cwd=cwd)
        if policy is not None:
            self.db.execute("UPDATE agent_registry SET reveil = %s::jsonb WHERE name = %s",
                            (json.dumps(policy), name))
        lease = registry.claim(self.db, name, runner.runner_id, 3600)
        self.assertIsNotNone(lease)
        worker = AgentWorker(runner, registry.get(self.db, name), lease)
        self.addCleanup(worker.watchdog_stop.set)
        return runner, worker

    def _vieillir(self, name: str, secondes: float) -> None:
        self.db.execute(
            "UPDATE agent_mailbox SET created_at = now() - make_interval(secs => %s)"
            " WHERE recipient = %s AND delivered_at IS NULL", (float(secondes), name))


AVATAR = {"immediat": list(reveil.IMMEDIAT_DEFAUT), "fenetre_s": 1800}


class HeureSimuleeTest(_Base):
    """Acceptation : 100 courriers ordinaires en une heure, au plus deux tours."""

    def _simule(self, worker, arrivees: list[tuple[float, dict]], *, pas: float = 10.0,
                duree: float = 3600.0) -> list[float]:
        horloge = _Horloge(0.0)
        tours: list[float] = []
        attente: list[dict] = []
        restantes = sorted(arrivees, key=lambda a: a[0])
        policy = reveil.of(worker.agent)
        with mock.patch("ameesh.runner.time", horloge):
            while horloge.now < duree:
                while restantes and restantes[0][0] <= horloge.now:
                    attente.append(restantes.pop(0)[1])
                if attente and worker._reveil_due(policy, attente):
                    tours.append(horloge.now)
                    attente = []  # le tour emporte tout le courrier en attente
                    worker._turn_end_mono = horloge.now + 60.0  # un tour d'une minute
                    horloge.now += 60.0
                    continue
                horloge.now += pas
        return tours

    def test_cent_courriers_ordinaires_deux_tours_au_plus(self):
        _, worker = self._worker("avatar", policy=AVATAR)
        arrivees = [(i * 36.0, _ligne(i + 1, created_ts=i * 36.0)) for i in range(100)]
        tours = self._simule(worker, arrivees)
        self.assertGreaterEqual(len(tours), 1)
        self.assertLessEqual(len(tours), 2, tours)
        # remis au plus tard à la fin de la fenêtre
        self.assertLessEqual(tours[0], 1800.0 + 10.0)

    def test_evenements_ordinaires_regroupes_aussi(self):
        _, worker = self._worker("avatar", policy=AVATAR)
        arrivees = [(i * 36.0, _ligne(i + 1, kind="event", created_ts=i * 36.0))
                    for i in range(100)]
        self.assertLessEqual(len(self._simule(worker, arrivees)), 2)

    def test_humain_et_urgent_ouvrent_un_tour_tout_de_suite(self):
        _, worker = self._worker("avatar", policy=AVATAR)
        for row in ((_ligne(500, sender="human:smichea", created_ts=700.0),
                          _ligne(501, payload={"urgent": True}, created_ts=700.0),
                          _ligne(502, kind="request", created_ts=700.0),
                          _ligne(503, kind="reply", created_ts=700.0))):
            with self.subTest(row=row["id"]):
                arrivees = [(0.0, _ligne(1, created_ts=0.0)), (700.0, row)]
                tours = self._simule(worker, arrivees, duree=800.0)
                worker._turn_end_mono = None
                self.assertEqual(tours, [700.0])

    def test_sans_declaration_le_comportement_d_avant(self):
        _, worker = self._worker("ordinaire", policy=None)
        self.assertIsNone(reveil.of(worker.agent))
        horloge = _Horloge(1000.0)
        with mock.patch("ameesh.runner.time", horloge):
            rows = [_ligne(1, kind="request", created_ts=1000.0)]
            # une demande attend la fenêtre de l'exécuteur (90 s ici 60 s), comme avant
            self.assertFalse(worker._mail_due(rows))
            horloge.now += 61.0
            self.assertTrue(worker._mail_due(rows))


class ReveilReelTest(_Base):
    """Le tour réel de `pick` sous la cadence déclarée (Postgres)."""

    def test_ordinaire_retenu_puis_tour_groupe_avec_tout_le_courrier(self):
        _, worker = self._worker("avatar", policy=AVATAR)
        for i in range(5):
            mail.send(self.db, "pair", "avatar", "courrier %d" % i)
        self.assertIsNone(worker.pick())
        # la fenêtre de l'exécuteur (60 s) ne suffit plus : celle de la fiche fait foi
        self._vieillir("avatar", 120)
        self.assertIsNone(worker.pick())
        self._vieillir("avatar", 1801)
        spec = worker.pick()
        self.assertIsNotNone(spec)
        self.assertEqual(spec["kind"], "mail")
        self.assertEqual(len(spec["candidats"]), 5)  # une seule consigne pour tout

    def test_une_demande_reveille_et_emporte_le_courrier_groupe(self):
        _, worker = self._worker("avatar", policy=AVATAR)
        for i in range(3):
            mail.send(self.db, "pair", "avatar", "courrier %d" % i)
        self.assertIsNone(worker.pick())
        mail.send(self.db, "pair", "avatar", "peux-tu valider ?", kind="request")
        spec = worker.pick()
        self.assertIsNotNone(spec)
        self.assertEqual(len(spec["candidats"]), 4)

    def test_urgent_reveille_tout_de_suite(self):
        _, worker = self._worker("avatar", policy=AVATAR)
        mail.send(self.db, "pair", "avatar", "panne", payload={"urgent": True})
        self.assertIsNotNone(worker.pick())

    def test_show_montre_le_prochain_reveil_et_l_attente(self):
        _, worker = self._worker("avatar", policy=AVATAR)
        for i in range(3):
            mail.send(self.db, "pair", "avatar", "courrier %d" % i)
        mail.send(self.db, "pair", "avatar", "copie", payload={"cc": "x"})
        self._vieillir("avatar", 600)
        detail = mesh_cli.agent_detail(self.db, "avatar", cfg=self.cfg)
        wake = detail["wake"]
        self.assertEqual(wake["pending"], 3)  # la copie passive ne compte pas
        self.assertEqual(wake["immediate_pending"], 0)
        self.assertAlmostEqual(wake["next_wake_ts"], time.time() + 1200, delta=30)
        out = io.StringIO()
        with mock.patch.object(mesh_cli, "_open", return_value=self.db), \
                mock.patch.object(self.db, "close", create=True), \
                contextlib.redirect_stdout(out):
            mesh_cli.cmd_show(self.cfg, argparse.Namespace(agent="avatar", json=False))
        texte = out.getvalue()
        self.assertIn("wake     : immediate: humain, urgent, request, reply, decision; "
                      "the rest grouped every 30 min", texte)
        self.assertIn("3 grouped mail(s) pending — next grouped turn", texte)

    def test_sans_declaration_show_inchange(self):
        self._worker("ordinaire", policy=None)
        self.assertIsNone(mesh_cli.agent_detail(self.db, "ordinaire", cfg=self.cfg)["wake"])


if __name__ == "__main__":
    unittest.main()
