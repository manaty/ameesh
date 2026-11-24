# SPDX-License-Identifier: AGPL-3.0-only
"""#8204 (gap 6 de manaty/ameesh#6403) : un avatar garde sa session humaine.

Cas réel : avec la politique par défaut `par-lot`, le runner a oublié la
session longue d'un avatar (celle à laquelle son humain s'attache) au premier
wake de lot et ouvert une session de lot ignorante de la conversation. La
politique de session vient de l'avatar, pas du type de lot : la fiche d'avatar
déclare `session_policy: jamais` (l'onboarding la pose), le runner l'applique
quel que soit le lot, et l'oubli d'une session humaine n'est jamais silencieux
(note de fil à l'humain, événement relu par l'alerte
`attached_session_forgotten`).
"""
from __future__ import annotations

import dataclasses
import os
import shutil
import tempfile
import unittest
from unittest import mock

from ameesh import canon, exploitation, mail, notify, registry, storage, work
from ameesh.runner import AgentWorker, Runner

from . import test_canon as canon_tests
from .support import FAKEBIN, PgTestCase


def _card(avatar: bool = True, **fields) -> str:
    lines = ["---", "type: Agent", "title: av", "responsible: human:alice",
             "team: acme", "capabilities: [read]"]
    for key, value in fields.items():
        lines.append("%s: %s" % (key, value))
    if avatar:
        lines += ["avatar:", "  conversation: portable"]
    lines += ["---", "", "# av", ""]
    return "\n".join(lines)


class AvatarCardPolicyTest(unittest.TestCase):
    """La fiche d'avatar déclare `jamais` ; une rotation demandée est refusée."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="test-8204-card-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        os.makedirs(os.path.join(self.root, "agents"))
        with open(os.path.join(self.root, "index.md"), "w", encoding="utf-8") as fh:
            fh.write("---\ntype: Index\ntitle: x\n---\n")

    def load(self, text: str) -> canon.Canon:
        canon_tests.write(self.root, "agents/av.md", text)
        return canon.load(self.root, untrusted=True)

    def codes(self, loaded) -> set:
        return {f.code for f in canon.validate(loaded)}

    def test_avatar_sans_politique_declare_jamais(self):
        loaded = self.load(_card())
        self.assertEqual(loaded.agent("av").declared_policy(),
                         {"session_policy": "jamais"})
        self.assertNotIn("avatar-session-policy", self.codes(loaded))

    def test_avatar_jamais_explicite_accepte(self):
        loaded = self.load(_card(session_policy="jamais"))
        self.assertEqual(loaded.agent("av").declared_policy(),
                         {"session_policy": "jamais"})
        self.assertNotIn("avatar-session-policy", self.codes(loaded))

    def test_avatar_par_lot_ou_taille_refuse(self):
        for policy in ("par-lot", "taille"):
            with self.subTest(policy=policy):
                loaded = self.load(_card(session_policy=policy))
                self.assertIn("avatar-session-policy", self.codes(loaded))
                self.assertEqual(loaded.agent("av").declared_policy(),
                                 {"session_policy": "jamais"})

    def test_avatar_session_neuve_refusee(self):
        loaded = self.load(_card(fresh_session="true"))
        self.assertIn("avatar-session-policy", self.codes(loaded))
        self.assertEqual(loaded.agent("av").declared_policy(),
                         {"session_policy": "jamais"})

    def test_agent_ordinaire_inchange(self):
        loaded = self.load(_card(avatar=False))
        self.assertIsNone(loaded.agent("av").declared_policy())
        loaded = self.load(_card(avatar=False, session_policy="taille"))
        self.assertEqual(loaded.agent("av").declared_policy(),
                         {"session_policy": "taille"})


class EffectivePolicyTest(unittest.TestCase):
    """L'exécuteur : un avatar reste en `jamais` même sans fiche synchronisée."""

    cfg = mock.Mock(session_policy="par-lot")

    def test_releve_preference_vaut_jamais(self):
        for row in ({"releve": "preference"},
                    {"releve": "preference", "session_policy": "par-lot"}):
            with self.subTest(row=row):
                self.assertEqual(exploitation.effective_policy(self.cfg, row), "jamais")

    def test_fiche_d_avatar_synchronisee(self):
        row = {"declared_policy": {"session_policy": "jamais"}, "releve": "hote"}
        self.assertEqual(exploitation.effective_policy(self.cfg, row), "jamais")

    def test_pas_de_regression_hors_avatar(self):
        self.assertEqual(exploitation.effective_policy(self.cfg, {}), "par-lot")
        self.assertEqual(exploitation.effective_policy(
            self.cfg, {"releve": "hote", "session_policy": "taille"}), "taille")
        self.assertEqual(exploitation.effective_policy(
            self.cfg, {"releve": "election"}), "par-lot")


class _Base(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("TRUNCATE agent_lot_sessions, work_items, agent_registry CASCADE")
        self.cfg = dataclasses.replace(self.cfg, session_policy="par-lot")

    def _cwd(self, name: str) -> str:
        path = os.path.join(self.tmp, "work", name)
        os.makedirs(path, exist_ok=True)
        return path

    def _worker(self, name: str, *, avatar: bool, synced: bool = False) -> AgentWorker:
        runner = Runner(self.cfg, self.db, once=True)
        self.register(name, "claude", cwd=self._cwd(name))
        if synced:
            # la fiche d'avatar synchronisée : `canon sync` écrit la politique
            # qu'elle déclare (`jamais`, posée par l'onboarding)
            self.db.execute("UPDATE agent_registry SET declared_policy = %s::jsonb,"
                            " responsible = 'human:alice' WHERE name = %s",
                            ('{"session_policy": "jamais"}', name))
        elif avatar:
            # la relève qui porte la conversation d'un avatar, sans fiche
            # synchronisée : le cas réel (`ameesh set` absent, défaut `par-lot`)
            self.db.execute("UPDATE agent_registry SET releve = 'preference',"
                            " responsible = 'human:alice', cwd = %s WHERE name = %s",
                            (self._cwd(name), name))
        lease = registry.claim(self.db, name, runner.runner_id, 3600)
        self.assertIsNotNone(lease)
        worker = AgentWorker(runner, registry.get(self.db, name), lease)
        self.addCleanup(worker.watchdog_stop.set)
        return worker

    def _human_session(self, worker: AgentWorker, session: str) -> None:
        registry.set_session(self.db, worker.name, session, None,
                             harness="claude", cwd=self._cwd(worker.name))
        worker.write_session_file(session)
        worker.agent = registry.get(self.db, worker.name)

    def _env(self, session: str):
        return mock.patch.dict(os.environ, {
            "AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log,
            "AMEESH_SESSION_STORE_CHECK": "0", "AMEESH_TEST_NEW_SESSION": session})

    def forgotten(self, name: str) -> list:
        # le journal garde ses événements (rétention) : lecture par agent
        return storage.of(self.db).events.recent_type(
            AgentWorker.ATTACHED_FORGOTTEN_EVENT, 1, 10, object="agent:" + name)

    def alerts(self, name: str) -> list:
        return [a for a in exploitation.attached_session_alerts(self.db)
                if a["agent"] == name]


class AvatarWakeTest(_Base):
    def test_wake_de_lot_consomme_dans_la_session_humaine(self):
        """Critère 1 : `par-lot` par défaut, session longue humaine, wake de
        lot : même session, ni session de lot ni « forgotten »."""
        worker = self._worker("av1", avatar=True, synced=True)
        self.assertEqual(worker.session_policy(), "jamais")
        self._human_session(worker, "sess-humaine")
        lot = work.add(self.db, title="lot 5", assignee="av1")["id"]
        mail.send(self.db, "orch", "av1", "wake du lot", work_item_id=str(lot))
        with self._env("sess-de-lot"), \
                mock.patch.object(worker, "_open_lot_session") as ouvre, \
                mock.patch.object(worker, "_forget_lotless_session") as oublie:
            spec = worker.rotate_for_lot(worker.pick())
            self.assertEqual(spec["lot"], str(lot))
            self.assertFalse(spec.get("lot_switch_failed"))
            self.assertTrue(worker.run_turn(spec))
        ouvre.assert_not_called()
        oublie.assert_not_called()
        argv = self.turns()[-1]["argv"]
        self.assertIn("--resume", argv)
        self.assertIn("sess-humaine", argv)
        self.assertEqual(registry.get(self.db, "av1")["session_id"], "sess-humaine")
        self.assertEqual(self.forgotten("av1"), [])

    def test_agent_ordinaire_par_lot_inchange(self):
        """Critère 4 : un agent non-avatar en `par-lot` oublie toujours sa
        session sans lot au premier tour de lot (#794), sans signal humain."""
        worker = self._worker("dev1", avatar=False)
        self.assertEqual(worker.session_policy(), "par-lot")
        self._human_session(worker, "sess-veille")
        lot = work.add(self.db, title="lot A", assignee="dev1")["id"]
        mail.send(self.db, "orch", "dev1", "prends A", work_item_id=str(lot))
        with self._env("sess-A"):
            spec = worker.rotate_for_lot(worker.pick())
            self.assertTrue(worker.run_turn(spec))
        self.assertNotIn("--resume", self.turns()[-1]["argv"])
        agent = registry.get(self.db, "dev1")
        self.assertEqual((agent["session_id"], agent["session_work_item"]),
                         ("sess-A", str(lot)))
        self.assertEqual(self.forgotten("dev1"), [])


class AttachedForgottenSignalTest(_Base):
    def test_oubli_d_une_session_d_avatar_signale(self):
        """Critère 3 : l'oubli produit une note de fil adressée à l'humain,
        un événement, et l'alerte `attached_session_forgotten`."""
        worker = self._worker("av2", avatar=True)
        self._human_session(worker, "sess-humaine")
        with mock.patch.object(worker, "_fil_note") as note:
            self.assertTrue(worker._forget_lotless_session())
        humaines = [c for c in note.call_args_list
                    if (c.kwargs.get("meta") or {}).get("action")
                    == "session-humaine-oubliee"]
        self.assertEqual(len(humaines), 1)
        self.assertEqual(humaines[0].kwargs["recipients"], ["human:alice"])
        self.assertIn("sess-humaine", humaines[0].args[0])
        rows = self.forgotten("av2")
        self.assertEqual(len(rows), 1)
        alerts = self.alerts("av2")
        self.assertEqual([(a["type"], a["agent"], a["value"]) for a in alerts],
                         [("attached_session_forgotten", "av2", "sess-humaine")])
        self.assertIn("ameesh adopt av2 --session sess-humaine", alerts[0]["detail"])
        self.assertIn("attached_session_forgotten", notify.DEFAULT_TYPES)

    def test_oubli_hors_avatar_sans_signal_humain(self):
        worker = self._worker("dev2", avatar=False)
        self._human_session(worker, "sess-veille")
        with mock.patch.object(worker, "_fil_note") as note:
            self.assertTrue(worker._forget_lotless_session())
        self.assertFalse([c for c in note.call_args_list
                          if (c.kwargs.get("meta") or {}).get("action")
                          == "session-humaine-oubliee"])
        self.assertEqual(self.forgotten("dev2"), [])
        self.assertEqual(self.alerts("dev2"), [])


if __name__ == "__main__":
    unittest.main()
