# SPDX-License-Identifier: AGPL-3.0-only
"""#6561 : une recomposition publiée n'est jamais lue comme « rien composé ».

Le 09/11 (h-16), `paquet-horaire.py recompose --carrier 6554 --exclude 6487
--wait 30m --cancel-ci --expect-head 40044ed04be6` a composé #6372, poussé
163770f12237, gelé et armé sa ci-full. Ses tests par unité ont ensuite retiré
la seule unité ; `paquet-auto` a retiré le paquet poussé (CI annulée, PR close)
et imprimé « aucune unité après recomposition ». `after_compose` a lu cette
ligne comme « rien composé » et clos le porteur « abandoned » : la trace disait
le contraire de ce qui s'était passé.

Témoins : ce retrait d'après publication n'est jamais « rien composé » ;
l'abandon d'un porteur vide exige la preuve que la branche porte encore sa tête
de départ (aucune tête neuve publiée) ; à défaut, aucune annulation ni clôture
ne suit, le porteur reste ouvert pour son pilote et le tic suivant ne
recompose pas.
"""
from __future__ import annotations

import unittest
from unittest import mock

from tests.test_3845_paquet_horaire import (
    HEAD, HEAD2, NOW, Base, FakeShell, carrier, ph)

BRANCH = "integration/paquet-h-20261027-04"
REF = "refs/heads/" + BRANCH
WITHDRAWN = ("tests ciblés : retrait de #3801 (attribution)\n"
             "recomposition : retrait de #3801\n"
             "aucune unité après recomposition : #3801\n")
EMPTY = "aucun lot prêt : rien à composer\n"
EARLY_PUSH = ("paquet h-20261027-04 poussé par scripts/paquet-auto.py dès la "
              "composition (canon §6, #4304) : 1 unité")


class Shell(FakeShell):
    """Faux git qui sait la tête des branches (#6561)."""

    def __init__(self):
        super().__init__()
        self.heads: dict[str, str] = {}

    def run(self, cmd, *, cwd=None, input_text=None, timeout=0):
        if cmd[0] == "git" and "ls-remote" in cmd:
            ref = str(cmd[-1])
            if ref.startswith("refs/heads/") and "*" not in ref:
                self.calls.append(list(cmd))
                sha = self.heads.get(ref, "")
                return ph.Result(0, ("%s\t%s\n" % (sha, ref)) if sha else "")
        return super().run(cmd, cwd=cwd, input_text=input_text, timeout=timeout)


class Horaire(Base):
    def setUp(self):
        super().setUp()
        self.shell = Shell()
        self.ctx.shell = self.shell
        self.ctx.epoch = 289

    def item(self) -> dict:
        item = carrier(created=NOW - 3600)
        self.shell.items[4000] = item
        self.shell.lists["qa"] = [item]
        return item

    def autos(self) -> list[list[str]]:
        return [c for c in self.shell.calls if "paquet-auto.py" in " ".join(c)]

    def gestures(self) -> list[list[str]]:
        """Les gestes que l'abandon refusé ne doit pas poser."""
        return [c for c in self.shell.calls
                if c[:3] in (["ameesh", "work", "close"],
                             ["ameesh", "ci", "cancel"])]

    @staticmethod
    def published(item: dict, at: float) -> None:
        """Ce que paquet-auto fait à la poussée : gel de la tête publiée (#4304)."""
        item["freeze"] = {"declared": {"sha": HEAD2, "at_ts": at, "id": 7}}
        item["milestones"] = [{"id": 7, "sha": HEAD2, "at_ts": at,
                               "note": EARLY_PUSH}]


class PublieeTest(Horaire):
    def test_retrait_apres_publication_n_est_pas_rien_compose(self):
        item = self.item()
        self.shell.heads[REF] = HEAD2
        self.shell.auto_out = WITHDRAWN
        got = ph.pilot_recompose(self.ctx, 4000, [], [])
        self.assertIn("abandon refusé", got)
        self.assertNotIn("rien composé", got)
        self.assertEqual(self.gestures(), [])
        self.assertTrue(any(note.startswith(ph.MARK_PUBLISHED)
                            for note in ph.marks(item)))
        self.assertEqual(self.shell.mails[-1][0], "mesh-design")
        self.assertIn("abandon refusé", self.shell.mails[-1][1])

    def test_retrait_apres_publication_refuse_meme_la_tete_de_depart(self):
        """La ligne de retrait prouve une composition : jamais « rien composé »,
        même si la branche a été ramenée à sa tête de départ."""
        item = self.item()
        self.shell.heads[REF] = HEAD
        self.shell.auto_out = WITHDRAWN
        got = ph.pilot_recompose(self.ctx, 4000, [], [])
        self.assertIn("abandon refusé", got)
        self.assertEqual(self.gestures(), [])
        self.assertTrue(any(note.startswith(ph.MARK_PUBLISHED)
                            for note in ph.marks(item)))

    def test_composition_vide_avec_tete_publiee_refuse_abandon(self):
        self.item()
        self.shell.heads[REF] = HEAD2
        self.shell.auto_out = EMPTY
        got = ph.pilot_recompose(self.ctx, 4000, [], [])
        self.assertIn("abandon refusé", got)
        self.assertEqual(self.gestures(), [])

    def test_composition_vide_sans_tete_publiee_clot_toujours(self):
        self.item()
        self.shell.heads[REF] = HEAD
        self.shell.auto_out = EMPTY
        got = ph.pilot_recompose(self.ctx, 4000, [], [])
        self.assertIn("rien composé", got)
        self.assertTrue(self.shell.ran("work", "close", "4000", "--abandoned"))
        self.assertEqual(self.shell.ran("ci", "cancel"), [])

    def test_lecture_de_branche_impossible_ne_prouve_rien(self):
        self.item()
        self.shell.auto_out = EMPTY
        with mock.patch.object(ph, "remote_branch_head", return_value=None):
            got = ph.pilot_recompose(self.ctx, 4000, [], [])
        self.assertIn("abandon refusé", got)
        self.assertEqual(self.gestures(), [])

    def test_le_tic_suivant_ne_recompose_pas(self):
        item = self.item()
        self.shell.heads[REF] = HEAD2
        self.shell.auto_out = WITHDRAWN
        ph.pilot_recompose(self.ctx, 4000, [], [])
        attempt = next(ev for ev in item["events"]
                       if str(ev["note"]).startswith(ph.MARK_ATTEMPT))
        attempt["created_ts"] = NOW - 100
        self.published(item, NOW - 50)
        before = len(self.autos())
        out = ph.tick(self.ctx)
        self.assertEqual(len(self.autos()), before)
        self.assertIn("abandonnée après publication", out)
        self.assertEqual(self.shell.ran("ci", "run"), [])
        self.assertEqual(self.gestures(), [])


if __name__ == "__main__":
    unittest.main()
