# SPDX-License-Identifier: AGPL-3.0-only
"""#3219 — l'assigné lève lui-même le hold ordinaire qu'il a posé.

Un `hold:` de règle 1 (non compté par `planned_waits.pilot_holds`) retenait la
fermeture par le contenu (#2744) ; la passe réveille son détenteur en lui
demandant de le lever (« lift the hold (… an `unhold:` note) »,
`plan_git._wake_retention`). `work.check_wait_note` refusait toute levée à une
session ordinaire (« ni humain, ni orchestrateur, ni conception ») : le hold
de l'assigné était donc levable par un pilote seulement, et l'instruction
envoyée était inexécutable.

La garde ouvre la levée à l'auteur d'une retenue active — et à lui seul, avec
l'identité de sa session prouvée (`--actor` usurpé refusé) — sans toucher aux
autres retenues : les holds d'un lot sont un **ensemble**, pas une pile. Un
hold de pilote, d'un humain ou d'un tiers reste actif et retient, même
recouvert par celui de l'assigné et après sa levée ; seule une levée de
pilote les lève tous (#1664 r2 B1). Le contrôle et l'écriture se font sous le
même verrou de ligne (B2 cx2) : un hold intercalé ne peut plus être effacé.

Le contenu prouvé ferme par ailleurs le lot sans attendre les seules retenues
de règle 1 de l'assigné (`content_retention`, `lift: True`) et la note de
fusion journalise la levée ; dès qu'une retenue d'une autre autorité reste
active, la fermeture attend.
"""
from __future__ import annotations

import contextlib
import os
import subprocess
import threading
import time
import unittest
from unittest import mock

from ameesh import db as db_mod
from ameesh import plan_git, planned_waits as W, storage, work

from .support import PgTestCase

#: identité neutre : aucune liaison de bail ne doit décider à la place du test
_IDENTITY_ENV = {
    "AGENT_MAIL_NAME": "",
    "AMEESH_RUNNER_ID": "",
    "AGENT_MESH_RUNNER_ID": "",
    "AMEESH_LEASE_EPOCH": "",
    "AGENT_MESH_LEASE_EPOCH": "",
}


@contextlib.contextmanager
def bound(name: str):
    """La session simulée est liée à `name` (identité explicite, sans bail)."""
    env = dict(_IDENTITY_ENV)
    env["AGENT_MAIL_NAME"] = name
    with mock.patch.dict(os.environ, env, clear=False):
        yield


def git(repo: str, *args: str) -> str:
    return subprocess.run(["git", "-C", repo, *args], check=True,
                          capture_output=True, text=True).stdout.strip()


class OwnHoldLiftTest(PgTestCase):
    """La garde d'écriture : qui peut lever quelle retenue, et sous quel verrou."""

    def setUp(self):
        super().setUp()
        self.register("dev-a", "claude")
        self.register("dev-b", "claude")
        self.lot = work.add(self.db, title="hold de règle 1", assignee="dev-a")
        work.move(self.db, self.lot["id"], "build")

    def _note(self, text: str, actor: str, *, session: str | None = None,
              wake_ts: float | None = None):
        # un acteur humain vient d'un terminal sans identité d'agent ; un
        # acteur agent écrit depuis la session liée à son nom.
        name = session if session is not None else (
            "" if actor.startswith("human:") else actor)
        with bound(name):
            return work.note(self.db, self.lot["id"], text, actor=actor,
                             wake_ts=wake_ts)

    def _events(self) -> list[dict]:
        return storage.of(self.db).progress.lot_hold_events(
            [self.lot["id"]], W.HOLD_NOTE_PATTERNS)

    def _active(self) -> dict[str, dict]:
        """Les retenues actives du lot, par auteur (#3219)."""
        return W.active_holds(self.db, self._events()).get(self.lot["id"], {})

    def test_l_assigne_leve_son_propre_hold_ordinaire(self):
        """Le cas du constat : `hold:` (règle 1), puis l'`unhold:` demandé par
        la passe de contenu, accepté et journalisé sous l'auteur."""
        self._note("hold: j'attends la fusion du paquet 1900", "dev-a")
        self.assertEqual(self._active()["dev-a"]["by"], "dev-a")
        self._note("unhold: paquet 1900 fusionné", "dev-a")
        self.assertEqual(self._active(), {})
        latest = work.events(self.db, self.lot["id"], 1)[0]
        self.assertEqual((latest["note"], latest["actor"]),
                         ("unhold: paquet 1900 fusionné", "dev-a"))

    def test_un_hold_de_pilote_reste_reserve_a_un_pilote(self):
        """#1664 r2 B1 inchangé : l'assigné ne lève pas le hold d'un pilote."""
        self._note("hold: gestes owner", "human:proprio")
        with self.assertRaisesRegex(work.WorkError, "neither a human"):
            self._note("unhold: à tort", "dev-a")
        self.assertEqual(set(self._active()), {"human:proprio"})

    def test_un_tiers_ordinaire_ne_leve_pas_le_hold_d_un_autre(self):
        """La levée ordinaire est celle de l'auteur, pas celle du premier venu."""
        self._note("hold: j'attends le paquet", "dev-a")
        with self.assertRaisesRegex(work.WorkError, "neither a human"):
            self._note("unhold: je crois que c'est bon", "dev-b")
        self.assertEqual(set(self._active()), {"dev-a"})

    def test_actor_usurpe_refuse(self):
        """Une session liée à dev-b ne lève pas le hold en se disant dev-a."""
        self._note("hold: j'attends le paquet", "dev-a")
        with self.assertRaisesRegex(work.WorkError, "belongs to dev-b"):
            self._note("unhold: paquet fusionné", "dev-a", session="dev-b")
        self.assertEqual(set(self._active()), {"dev-a"})

    def test_un_pilote_leve_toujours_un_hold_ordinaire(self):
        """La garde nouvelle n'ôte rien au pilote : il lève toute retenue."""
        self._note("hold: j'attends le paquet", "dev-a")
        with bound(""):
            work.note(self.db, self.lot["id"], "unhold: paquet fusionné",
                      actor="human:proprio")
        self.assertEqual(self._active(), {})

    def test_une_levee_sans_hold_reste_un_geste_de_pilote(self):
        """Sans retenue lisible, rien ne prouve l'auteur : le pilote seul écrit
        la levée (comportement d'avant #3219, conservé)."""
        with self.assertRaisesRegex(work.WorkError, "neither a human"):
            self._note("unhold: rien à lever", "dev-a")

    def test_B1_un_hold_de_pilote_survit_a_la_levee_de_l_assigne(self):
        """B1 cx2/dev2 : le hold de pilote posé *avant* celui de l'assigné
        reste actif ; la levée de l'assigné ne retire que le sien."""
        with bound(""):
            work.note(self.db, self.lot["id"], "hold: gestes owner",
                      actor="human:proprio")
        self._note("hold: j'attends le paquet", "dev-a")
        self._note("unhold: paquet fusionné", "dev-a")
        self.assertEqual(set(self._active()), {"human:proprio"})

    def test_B1_un_hold_de_pilote_pose_apres_ne_est_pas_leve_par_l_assigne(self):
        """B1, ordre inverse : le hold du pilote est la dernière pose ; la
        levée de l'assigné ne l'efface pas (elle ne retire que la sienne)."""
        self._note("hold: j'attends le paquet", "dev-a")
        with bound(""):
            work.note(self.db, self.lot["id"], "hold: gestes owner",
                      actor="human:proprio")
        self._note("unhold: paquet fusionné", "dev-a")
        self.assertEqual(set(self._active()), {"human:proprio"})

    def test_B2_un_hold_intercale_apres_le_controle_n_est_pas_efface(self):
        """B2 cx2 : même si un hold tiers s'écrit entre le contrôle et
        l'écriture (le verrou tenu, simulé ici par le même tx), la levée ne
        l'efface pas : les holds sont un ensemble."""
        self._note("hold: j'attends le paquet", "dev-a")
        real = work.check_wait_note

        def interleave(cfg, db, actor, text, **kwargs):
            allowed = real(cfg, db, actor, text, **kwargs)
            storage.of(db).work.note(self.lot["id"], "build",
                                     "hold: nouvelle retenue QA", "dev-b")
            return allowed

        with mock.patch.object(work, "check_wait_note", interleave):
            self._note("unhold: paquet fusionné", "dev-a")
        self.assertEqual(set(self._active()), {"dev-b"})

    # -- B2 : le contrôle attend le verrou de ligne du lot ------------------
    def _waits_for_lock(self, action, *, label: str) -> None:
        """`action` ne fait son contrôle qu'après avoir obtenu le verrou."""
        marker = threading.Event()
        real = work.check_wait_note

        def traced(cfg, db, actor, text, **kwargs):
            marker.set()
            return real(cfg, db, actor, text, **kwargs)

        errors: list[BaseException] = []

        def run():
            try:
                action()
            except BaseException as exc:  # noqa: BLE001 — rapporté au test
                errors.append(exc)

        blocker = db_mod.connect(self.cfg)
        thread = None
        try:
            with mock.patch.object(work, "check_wait_note", traced):
                with blocker.transaction() as tx:
                    tx.query("SELECT id FROM work_items WHERE id = %s FOR UPDATE",
                             (self.lot["id"],))
                    thread = threading.Thread(target=run)
                    thread.start()
                    time.sleep(0.5)
                    self.assertFalse(
                        marker.is_set(),
                        "le contrôle de %s s'exécute avant le verrou du lot" % label)
                thread.join(10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertTrue(marker.is_set())
        finally:
            if thread is not None:
                thread.join(10)
            blocker.close()

    def test_B2_la_levee_attend_le_verrou_du_lot(self):
        """B2 cx2 : `work note` lit le détenteur sous le verrou, pas avant."""
        self._note("hold: j'attends le paquet", "dev-a")
        writer = db_mod.connect(self.cfg)
        try:
            def action():
                with bound("dev-a"):
                    work.note(writer, self.lot["id"], "unhold: paquet fusionné",
                              actor="dev-a")
            self._waits_for_lock(action, label="note")
        finally:
            writer.close()

    def test_B2_la_levee_avec_reveil_attend_le_verrou_du_lot(self):
        """B2 : le chemin `note --wake-at` passe par le même verrou."""
        self._note("hold: j'attends le paquet", "dev-a")
        writer = db_mod.connect(self.cfg)
        try:
            def action():
                with bound("dev-a"):
                    work.note(writer, self.lot["id"], "unhold: paquet fusionné",
                              actor="dev-a", wake_ts=time.time() + 3600)
            self._waits_for_lock(action, label="note + réveil")
        finally:
            writer.close()

    def test_B2_le_move_avec_une_levee_attend_le_verrou_du_lot(self):
        """B2 : `work move --note "unhold: …"` passe par le même verrou."""
        self._note("hold: j'attends le paquet", "dev-a")
        writer = db_mod.connect(self.cfg)
        try:
            def action():
                with bound("dev-a"):
                    work.move(writer, self.lot["id"], "qa",
                              note="unhold: paquet fusionné", actor="dev-a")
            self._waits_for_lock(action, label="move")
        finally:
            writer.close()


class ContentSweepAfterOwnLiftTest(PgTestCase):
    """Le parcours réel du constat : livraison sous hold, puis levée, puis
    fermeture par la passe de contenu (#2744/#3219)."""

    def setUp(self):
        super().setUp()
        self.register("dev-a", "claude")
        self.register("dev-b", "claude")
        self.repo = os.path.join(self.tmp, "depot")
        os.makedirs(self.repo)
        git(self.repo, "init", "-q", "-b", "main")
        self._commit("README", "acme\n", "initial")

    def _commit(self, path: str, text: str, message: str) -> str:
        full = os.path.join(self.repo, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "a", encoding="utf-8") as fh:
            fh.write(text)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", message)
        return git(self.repo, "rev-parse", "HEAD")

    def _delivered(self) -> dict:
        """Un lot gelé dont le contenu est déjà dans `main`, encore en `qa`."""
        git(self.repo, "checkout", "-q", "-b", "lot/paquet")
        sha = self._commit("src/paquet.py", "x = 1\n", "paquet")
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "merge", "-q", "--no-ff", "-m", "paquet 1900", "lot/paquet")
        item = work.add(self.db, title="livré sous hold", assignee="dev-a")
        for state in ("build", "qa"):
            work.move(self.db, item["id"], state)
        # le gel réel eût marqué la cible ; le contenu y est déjà (comme
        # `tests/test_plan.py`), la preuve de livraison reste valable.
        storage.of(self.db).work.add_milestone(
            item["id"], "frozen", sha=sha, actor="ouvrier", verdict=None,
            note="ameesh-freeze-absent: main")
        return item

    def _sync(self, *, dry_run: bool = False) -> dict:
        report = plan_git.sync_merges(self.db, self.repo, target="main",
                                      dry_run=dry_run)
        return {row["work_item"]: row for row in report["results"]}

    def _notes(self, item: dict) -> list[str]:
        return [event["note"] or "" for event in work.events(self.db, item["id"])]

    def _active(self, item: dict) -> dict[str, dict]:
        events = storage.of(self.db).progress.lot_hold_events(
            [item["id"]], W.HOLD_NOTE_PATTERNS)
        return W.active_holds(self.db, events).get(item["id"], {})

    def test_leve_par_l_assigne_puis_la_passe_ferme(self):
        item = self._delivered()
        with bound("dev-a"):
            work.note(self.db, item["id"], "hold: j'attends le paquet 1900",
                      actor="dev-a")
        with bound("dev-a"):
            work.note(self.db, item["id"], "unhold: paquet 1900 fusionné",
                      actor="dev-a")
        self.assertEqual(self._active(item), {})
        result = self._sync()[item["id"]]
        self.assertEqual(result["result"], "merged")
        self.assertEqual(work.get(self.db, item["id"])["state"], "delivered")
        # la levée est déjà au journal ; la note de fusion n'a pas à la refaire
        closure = next(n for n in self._notes(item) if n.startswith("merge seen"))
        self.assertNotIn("levée du hold", closure)

    def test_la_passe_ferme_malgre_le_hold_de_l_assigne(self):
        """#3219 : la retenue de règle 1 de l'assigné ne retient pas une
        livraison prouvée ; la note de fusion journalise la levée. Un relevé à
        blanc annonce la même fermeture (`would-merge`), pas une retenue."""
        item = self._delivered()
        with bound("dev-a"):
            work.note(self.db, item["id"], "hold: j'attends le paquet 1900",
                      actor="dev-a")
        dry = self._sync(dry_run=True)[item["id"]]
        self.assertEqual(dry["result"], "would-merge")
        self.assertIn("levée du hold de l'assigné dev-a", dry["detail"])
        result = self._sync()[item["id"]]
        self.assertEqual(result["result"], "merged")
        self.assertEqual(work.get(self.db, item["id"])["state"], "delivered")
        closure = next(n for n in self._notes(item) if n.startswith("merge seen"))
        self.assertIn("levée du hold de l'assigné dev-a", closure)
        self.assertIn("j'attends le paquet 1900", closure)

    def test_un_hold_de_pilote_retient_toujours_la_fermeture(self):
        """#1664/#2744 inchangés : un hold de pilote n'est pas levé par le
        contenu (sa levée reste un geste de pilote)."""
        item = self._delivered()
        with bound(""):
            work.note(self.db, item["id"], "hold: gestes owner",
                      actor="human:proprio")
        result = self._sync()[item["id"]]
        self.assertEqual((result["result"], result["retention"]["what"]),
                         ("withheld", "hold"))
        self.assertEqual(work.get(self.db, item["id"])["state"], "qa")

    def test_B1_le_hold_de_pilote_recouvert_retient_la_fermeture(self):
        """B1 cx2/dev2 : hold de pilote puis hold de l'assigné (même état) ;
        la passe ne ferme pas sur le seul hold de l'assigné. La levée de
        l'assigné ne lève que le sien ; le contenu attend le pilote."""
        item = self._delivered()
        with bound(""):
            work.note(self.db, item["id"], "hold: gestes owner",
                      actor="human:proprio")
        with bound("dev-a"):
            work.note(self.db, item["id"], "hold: j'attends le paquet 1900",
                      actor="dev-a")
        first = self._sync()[item["id"]]
        self.assertEqual((first["result"], first["retention"]["by"]),
                         ("withheld", "human:proprio"))
        with bound("dev-a"):
            work.note(self.db, item["id"], "unhold: paquet 1900 fusionné",
                      actor="dev-a")
        self.assertEqual(set(self._active(item)), {"human:proprio"})
        second = self._sync()[item["id"]]
        self.assertEqual((second["result"], second["retention"]["by"]),
                         ("withheld", "human:proprio"))
        with bound(""):
            work.note(self.db, item["id"], "unhold: gestes faits",
                      actor="human:proprio")
        self.assertEqual(self._sync()[item["id"]]["result"], "merged")

    def test_B1_le_hold_de_pilote_pose_apres_retient_aussi(self):
        """B1, ordre inverse : le hold du pilote est la dernière pose ; la
        levée de l'assigné ne l'efface pas."""
        item = self._delivered()
        with bound("dev-a"):
            work.note(self.db, item["id"], "hold: j'attends le paquet 1900",
                      actor="dev-a")
        with bound(""):
            work.note(self.db, item["id"], "hold: gestes owner",
                      actor="human:proprio")
        with bound("dev-a"):
            work.note(self.db, item["id"], "unhold: paquet 1900 fusionné",
                      actor="dev-a")
        self.assertEqual(set(self._active(item)), {"human:proprio"})
        result = self._sync()[item["id"]]
        self.assertEqual((result["result"], result["retention"]["by"]),
                         ("withheld", "human:proprio"))

    def test_un_hold_d_un_autre_ordinaire_retient_la_fermeture(self):
        """Un hold posé par un tiers (QA, pair) garde son effet #2744 : il ne
        devient pas la retenue ordinaire de l'assigné."""
        item = self._delivered()
        with bound("dev-b"):
            work.note(self.db, item["id"], "hold: relecture de passage",
                      actor="dev-b")
        result = self._sync()[item["id"]]
        self.assertEqual((result["result"], result["retention"]["by"]),
                         ("withheld", "dev-b"))
        self.assertEqual(work.get(self.db, item["id"])["state"], "qa")
        # son auteur le lève lui-même (#3219), puis la passe ferme
        with bound("dev-b"):
            work.note(self.db, item["id"], "unhold: relecture faite", actor="dev-b")
        self.assertEqual(self._sync()[item["id"]]["result"], "merged")

    def test_l_etat_blocked_retient_meme_sous_le_hold_de_l_assigne(self):
        """#3219 n'ouvre que les retenues de règle 1 : un lot `blocked` n'est
        pas fermé par le contenu, quel que soit le hold courant."""
        item = self._delivered()
        with bound("dev-a"):
            work.note(self.db, item["id"], "hold: j'attends le paquet 1900",
                      actor="dev-a")
        work.move(self.db, item["id"], "blocked", note="passe réelle due",
                  actor="dev-a")
        result = self._sync()[item["id"]]
        self.assertEqual((result["result"], result["retention"]["what"]),
                         ("withheld", "blocked"))
        self.assertEqual(work.get(self.db, item["id"])["state"], "blocked")


if __name__ == "__main__":
    unittest.main()
