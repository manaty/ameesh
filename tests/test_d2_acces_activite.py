# SPDX-License-Identifier: AGPL-3.0-only
"""Étude v2 D2 (0033 §9) : qui voit l'activité d'une persona."""
from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from ameesh import activity, authz, canon, oidc, registry

from .support import PgTestCase
from .test_canon import _TmpMixin, fiche, write
from .test_oidc import CLIENT, ISS, _CleEC, _Fournisseur, jeton


def _canon(test) -> canon.Canon:
    root = test.example_copy()
    write(root, "agents/verif-a.md", fiche(type="Persona", title="verif-a",
                                          responsible="human:alice", harness="claude"))
    write(root, "membres/alice.md", fiche(type="Member", title="alice",
                                          deputies=["human:bruno"], superiors=["human:chef"],
                                          emails=["alice@exemple.org"]))
    write(root, "membres/bruno.md", fiche(type="Member", title="bruno",
                                          emails=["bruno@exemple.org"]))
    write(root, "membres/chef.md", fiche(type="Member", title="chef",
                                         superiors=["human:direction"],
                                         emails=["chef@exemple.org"]))
    write(root, "membres/direction.md", fiche(type="Member", title="direction",
                                              superiors=["human:chef"]))       # cycle
    write(root, "membres/eve.md", fiche(type="Member", title="eve",
                                        emails=["eve@exemple.org"]))
    return canon.load(root, untrusted=True)


class RelationsTest(_TmpMixin, unittest.TestCase):
    def test_qui_voit(self):
        c = _canon(self)
        self.assertEqual(authz.viewers([c], "verif-a"), {
            "human:alice": "responsable",
            "human:bruno": "suppléant du responsable",
            "human:chef": "supérieur (niveau 1)",
            "human:direction": "supérieur (niveau 2)",
        })
        self.assertEqual(authz.can_view_activity([c], "human:eve", "verif-a")[0], False)
        self.assertEqual(authz.viewers([c], "inconnue"), {})


class CommandeTest(_TmpMixin, PgTestCase):
    def setUp(self):
        super().setUp()
        self.c = _canon(self)
        registry.upsert(self.db, "verif-a", harness="claude", host="pc", mode="execute")
        self.cle = _CleEC()
        self.cache = oidc.KeyCache(fetch=_Fournisseur(self.cle))
        self.cfg_id = self.cfg.__class__(**{**self.cfg.__dict__, "identity": {"providers": [
            {"issuer": ISS, "client_id": CLIENT, "domains": ["exemple.org"]}]}})

    def lancer(self, email: str) -> int:
        token = jeton(self.cle, "ES256", email=email)
        with mock.patch("ameesh.config.load", return_value=self.cfg_id), \
                mock.patch("ameesh.canon.load_configured", return_value=[self.c]), \
                mock.patch.object(oidc, "_DEFAULT_CACHE", self.cache), \
                mock.patch("sys.stdin", io.StringIO(token)), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            code = activity.main(["verif-a", "--identity", "-"])
        self.dernier_err = err.getvalue()
        return code

    def test_responsable_suppleant_superieur_acceptes(self):
        for email in ("alice@exemple.org", "bruno@exemple.org", "chef@exemple.org"):
            self.assertEqual(self.lancer(email), 0, (email, self.dernier_err))

    def test_autre_membre_refuse(self):
        self.assertEqual(self.lancer("eve@exemple.org"), 4)
        self.assertIn("ni responsable", self.dernier_err)

    def test_inconnu_refuse(self):
        self.assertEqual(self.lancer("personne@exemple.org"), 4)
        self.assertIn("aucun membre", self.dernier_err)


if __name__ == "__main__":
    unittest.main()
