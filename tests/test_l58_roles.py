# SPDX-License-Identifier: AGPL-3.0-only
"""L58 (0032, 0033 §7-8, étude v2 volet C) : rôles adressables."""
from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr
from unittest import mock

from ameesh import canon, registry, roles

from .support import PgTestCase
from .test_canon import _TmpMixin, codes, fiche, write


def _canon_essai(test) -> canon.Canon:
    root = test.example_copy()
    write(root, "agents/verif-a.md", fiche(type="Persona", title="verif-a",
                                          responsible="human:alice", team="ima",
                                          harness="claude"))
    write(root, "agents/verif-b.md", fiche(type="Persona", title="verif-b",
                                          responsible="human:alice", team="ima",
                                          harness="claude"))
    write(root, "membres/alice.md", fiche(type="Member", title="alice",
                                          superiors=["human:bruno"]))
    write(root, "roles/verificateur.md", fiche(type="Role", title="verificateur-bd", team="ima",
                                               holder="verif-a", deputies=["verif-b",
                                                                           "human:bruno"]))
    return canon.load(root, untrusted=True)


class CarteTest(_TmpMixin, unittest.TestCase):
    def test_role_lu_et_valide(self):
        c = _canon_essai(self)
        self.assertEqual(codes(canon.validate(c), canon.ERROR), set())
        r = c.role_cards[0]
        self.assertEqual((r.title, r.team, r.chain),
                         ("verificateur-bd", "ima", ["verif-a", "verif-b", "human:bruno"]))

    def test_erreurs(self):
        root = self.example_copy()
        write(root, "roles/a.md", fiche(type="Role", title="Mauvais_Nom", holder="x"))
        write(root, "roles/b.md", fiche(type="Role", title="sans-titulaire"))
        write(root, "roles/c.md", fiche(type="Role", title="inconnu", holder="personne",
                                        deputies=["human:fantome"]))
        write(root, "roles/d.md", fiche(type="Role", title="double", team="x", holder="ouvrier"))
        write(root, "roles/e.md", fiche(type="Role", title="double", team="x", holder="ouvrier"))
        found = codes(canon.validate(canon.load(root, untrusted=True)), canon.ERROR)
        self.assertTrue({"role-title-invalid", "role-holder-missing", "role-holder-unresolved",
                         "role-duplicate"} <= found, found)


class AdresseTest(unittest.TestCase):
    def test_adresses(self):
        self.assertEqual(roles.parse_address("role:relecteur@ima"), ("relecteur", "ima"))
        self.assertEqual(roles.parse_address("role:relecteur"), ("relecteur", None))
        for mauvaise in ("role:", "role:A", "relecteur", "role:x@", "role:x@a b"):
            with self.assertRaises(roles.RoleError):
                roles.parse_address(mauvaise)


class ResolutionTest(_TmpMixin, PgTestCase):
    def setUp(self):
        super().setUp()
        self.c = _canon_essai(self)
        registry.upsert(self.db, "verif-a", harness="claude", host="pc", mode="execute")
        registry.upsert(self.db, "verif-b", harness="claude", host="pc", mode="execute")
        registry.upsert(self.db, "envoyeur", harness="claude", host="pc", mode="execute")

    def test_titulaire(self):
        res = roles.resolve(self.db, [self.c], "role:verificateur-bd@ima")
        self.assertEqual((res.name, res.rank), ("verif-a", "titulaire"))

    def test_suppleant_si_titulaire_arrete(self):
        registry.set_status(self.db, "verif-a", "stopped", status_text="absent")
        res = roles.resolve(self.db, [self.c], "role:verificateur-bd@ima")
        self.assertEqual((res.name, res.rank), ("verif-b", "suppléant 1"))
        self.assertEqual(res.skipped, [("verif-a", "stopped")])

    def test_humain_de_la_chaine_puis_escalade(self):
        registry.set_status(self.db, "verif-a", "stopped")
        registry.set_status(self.db, "verif-b", "dead")
        res = roles.resolve(self.db, [self.c], "role:verificateur-bd@ima")
        self.assertFalse(res.ok)
        self.assertEqual(res.human, "human:bruno")
        self.assertIn("humain", res.describe())

    def test_escalade_aux_superieurs(self):
        self.c.role_cards[0].deputies = ["verif-b"]
        for n in ("verif-a", "verif-b"):
            registry.set_status(self.db, n, "stopped")
        res = roles.resolve(self.db, [self.c], "role:verificateur-bd@ima")
        self.assertFalse(res.ok)
        self.assertEqual(res.escalate, ["human:alice", "human:bruno"])

    def test_equipe_de_l_expediteur(self):
        res = roles.resolve(self.db, [self.c], "role:verificateur-bd", sender_team="ima")
        self.assertEqual(res.name, "verif-a")
        with self.assertRaises(roles.RoleError):
            roles.resolve(self.db, [self.c], "role:verificateur-bd@autre")

    def test_envoi_a_un_role(self):
        from ameesh import cli
        err = io.StringIO()
        with mock.patch("ameesh.canon.load_configured", return_value=[self.c]), \
                mock.patch("ameesh.config.load", return_value=self.cfg), \
                redirect_stderr(err):
            code = cli.main(["send", "role:verificateur-bd@ima", "relis la migration 260",
                             "--from", "envoyeur"])
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("→ verif-a (titulaire)", err.getvalue())
        self.assertEqual([r["body"] for r in self.db.query(
            "SELECT body FROM agent_mailbox WHERE recipient = %s", ("verif-a",))],
                         ["relis la migration 260"])
        registry.set_status(self.db, "verif-a", "stopped")
        registry.set_status(self.db, "verif-b", "stopped")
        with mock.patch("ameesh.canon.load_configured", return_value=[self.c]), \
                mock.patch("ameesh.config.load", return_value=self.cfg), \
                redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["send", "role:verificateur-bd@ima", "x",
                                       "--from", "envoyeur"]), 3)


if __name__ == "__main__":
    unittest.main()
