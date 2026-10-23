# SPDX-License-Identifier: AGPL-3.0-only
"""#3219 — l'assigné lève lui-même le hold ordinaire qu'il a posé.

Un `hold:` de règle 1 (non compté par `planned_waits.pilot_holds`) retenait la
fermeture par le contenu (#2744) ; la passe réveille son détenteur en lui
demandant de le lever (« lift the hold (… an `unhold:` note) »,
`plan_git._wake_retention`). `work.check_wait_note` refusait toute levée à une
session ordinaire (« ni humain, ni orchestrateur, ni conception ») : le hold
de l'assigné était donc levable par un pilote seulement, et l'instruction
envoyée était inexécutable.

La garde ouvre la levée à l'auteur du hold ordinaire — et à lui seul, avec
l'identité de sa session prouvée (`--actor` usurpé refusé) — sans toucher au
hold de pilote, qui reste réservé à un pilote (#1664 r2 B1 inchangé). Le
contenu prouvé ferme par ailleurs le lot sans attendre ce hold de règle 1
(`content_retention`, `lift: True`) et la note de fusion journalise la levée ;
le hold d'un pilote ou d'un autre acteur retient toujours.
"""
from __future__ import annotations

import contextlib
import os
import subprocess
import unittest
from unittest import mock

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
    """La garde d'écriture : qui peut lever quelle retenue."""

    def setUp(self):
        super().setUp()
        self.register("dev-a", "claude")
        self.register("dev-b", "claude")
        self.lot = work.add(self.db, title="hold de règle 1", assignee="dev-a")
        work.move(self.db, self.lot["id"], "build")

    def _note(self, text: str, actor: str, *, session: str | None = None):
        # un acteur humain vient d'un terminal sans identité d'agent ; un
        # acteur agent écrit depuis la session liée à son nom.
        name = session if session is not None else (
            "" if actor.startswith("human:") else actor)
        with bound(name):
            return work.note(self.db, self.lot["id"], text, actor=actor)

    def _holds(self) -> dict:
        events = storage.of(self.db).progress.lot_hold_events(
            [self.lot["id"]], W.HOLD_NOTE_PATTERNS)
        return W.explicit_holds(events)

    def test_l_assigne_leve_son_propre_hold_ordinaire(self):
        """Le cas du constat : `hold:` (règle 1), puis l'`unhold:` demandé par
        la passe de contenu, accepté et journalisé sous l'auteur."""
        self._note("hold: j'attends la fusion du paquet 1900", "dev-a")
        self.assertEqual(self._holds()[self.lot["id"]]["by"], "dev-a")
        self._note("unhold: paquet 1900 fusionné", "dev-a")
        self.assertEqual(self._holds(), {})
        latest = work.events(self.db, self.lot["id"], 1)[0]
        self.assertEqual((latest["note"], latest["actor"]),
                         ("unhold: paquet 1900 fusionné", "dev-a"))

    def test_un_hold_de_pilote_reste_reserve_a_un_pilote(self):
        """#1664 r2 B1 inchangé : l'assigné ne lève pas le hold d'un pilote."""
        self._note("hold: gestes owner", "human:proprio")
        with self.assertRaisesRegex(work.WorkError, "neither a human"):
            self._note("unhold: à tort", "dev-a")
        self.assertIn(self.lot["id"], self._holds())

    def test_un_tiers_ordinaire_ne_leve_pas_le_hold_d_un_autre(self):
        """La levée ordinaire est celle de l'auteur, pas celle du premier venu."""
        self._note("hold: j'attends le paquet", "dev-a")
        with self.assertRaisesRegex(work.WorkError, "posed by dev-a, not by dev-b"):
            self._note("unhold: je crois que c'est bon", "dev-b")
        self.assertIn(self.lot["id"], self._holds())

    def test_actor_usurpe_refuse(self):
        """Une session liée à dev-b ne lève pas le hold en se disant dev-a."""
        self._note("hold: j'attends le paquet", "dev-a")
        with self.assertRaisesRegex(work.WorkError, "belongs to dev-b"):
            self._note("unhold: paquet fusionné", "dev-a", session="dev-b")
        self.assertIn(self.lot["id"], self._holds())

    def test_un_pilote_leve_toujours_un_hold_ordinaire(self):
        """La garde nouvelle n'ôte rien au pilote : il lève toute retenue."""
        self._note("hold: j'attends le paquet", "dev-a")
        with bound(""):
            work.note(self.db, self.lot["id"], "unhold: paquet fusionné",
                      actor="human:proprio")
        self.assertEqual(self._holds(), {})

    def test_une_levee_sans_hold_reste_un_geste_de_pilote(self):
        """Sans retenue lisible, rien ne prouve l'auteur : le pilote seul écrit
        la levée (comportement d'avant #3219, conservé)."""
        with self.assertRaisesRegex(work.WorkError, "neither a human"):
            self._note("unhold: rien à lever", "dev-a")


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

    def test_leve_par_l_assigne_puis_la_passe_ferme(self):
        item = self._delivered()
        with bound("dev-a"):
            work.note(self.db, item["id"], "hold: j'attends le paquet 1900",
                      actor="dev-a")
        with bound("dev-a"):
            work.note(self.db, item["id"], "unhold: paquet 1900 fusionné",
                      actor="dev-a")
        events = storage.of(self.db).progress.lot_hold_events(
            [item["id"]], W.HOLD_NOTE_PATTERNS)
        self.assertEqual(W.explicit_holds(events), {})
        result = self._sync()[item["id"]]
        self.assertEqual(result["result"], "merged")
        self.assertEqual(work.get(self.db, item["id"])["state"], "merged")
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
        self.assertEqual(work.get(self.db, item["id"])["state"], "merged")
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
        """#3219 n'ouvre que le hold de règle 1 : un lot `blocked` n'est pas
        fermé par le contenu, quel que soit le hold courant."""
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
