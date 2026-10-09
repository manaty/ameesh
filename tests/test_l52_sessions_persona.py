# SPDX-License-Identifier: AGPL-3.0-only
"""L52 (0032 §2) : historique des sessions d'une persona."""
from __future__ import annotations

from ameesh import persona_sessions as ps, registry

from .support import PgTestCase


class HistoriqueTest(PgTestCase):
    def setUp(self):
        super().setUp()
        registry.upsert(self.db, "verif-a", harness="deepseek", host="pc", mode="execute")

    def test_session_neuve_puis_remplacee(self):
        registry.set_session(self.db, "verif-a", "s1", "primaire")
        registry.set_session(self.db, "verif-a", "s2", None)
        rows = ps.sessions(self.db, "verif-a")
        self.assertEqual([r["session_id"] for r in rows], ["s2", "s1"])
        self.assertIsNone(rows[0]["ended_at"])
        self.assertEqual(rows[1]["end_reason"], "remplacée par s2")
        self.assertEqual((rows[1]["harness"], rows[1]["host"], rows[1]["account"]),
                         ("deepseek", "pc", "primaire"))

    def test_reprise_de_la_meme_session(self):
        registry.set_session(self.db, "verif-a", "s1", "primaire")
        ps.record_end(self.db, "verif-a", "s1", "arrêt")
        registry.set_session(self.db, "verif-a", "s1", None)
        rows = ps.sessions(self.db, "verif-a")
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["ended_at"])
        self.assertEqual(rows[0]["account"], "primaire")     # compte d'origine gardé

    def test_fin_de_session(self):
        registry.set_session(self.db, "verif-a", "s1")
        ps.record_end(self.db, "verif-a", "s1", "rotation")
        ps.record_end(self.db, "verif-a", "s1", "autre")       # déjà close : inchangée
        self.assertEqual(ps.sessions(self.db, "verif-a")[0]["end_reason"], "rotation")
        ps.record_end(self.db, "verif-a", None, "rien")          # sans session : rien

    def test_rotation_close_la_session(self):
        registry.set_session(self.db, "verif-a", "s1")
        self.db.execute("UPDATE agent_registry SET lease_owner = 'r', lease_epoch = 7, "
                        "lease_expires_at = now() + interval '5 minutes' WHERE name = 'verif-a'")
        self.assertTrue(registry.clear_session(self.db, "verif-a", "r", 7))
        self.assertEqual(ps.sessions(self.db, "verif-a")[0]["end_reason"], "rotation")


class SessionsFillesTest(PgTestCase):
    """L52b : sessions parallèles portées par des filles éphémères."""

    def setUp(self):
        super().setUp()
        registry.upsert(self.db, "verif-a", harness="deepseek", host="pc", mode="execute",
                        cwd="/w/verif-a")
        self.db.execute("UPDATE agent_registry SET responsible = 'humain', team = 'eq', "
                        "capabilities = ARRAY['read', 'report-drift', 'propose'] "
                        "WHERE name = 'verif-a'")

    def open(self, lot, **kw):
        kw.setdefault("ttl_seconds", 3600)
        return ps.open_child(self.db, "verif-a", lot, **kw)

    def test_ouverture_herite_de_la_persona(self):
        row = self.open("52", cwd="/w/lot52")
        self.assertFalse(row["existing"])
        self.assertEqual(row["name"], "verif-a.l52")
        got = self.db.query("SELECT * FROM agent_registry WHERE name = 'verif-a.l52'")[0]
        self.assertEqual((got["parent_persona"], got["created_by"], got["session_work_item"]),
                         ("verif-a", "verif-a", "52"))
        self.assertEqual((got["responsible"], got["team"], got["harness"], got["host"]),
                         ("humain", "eq", "deepseek", "pc"))
        # capacités de la persona, pas le sous-ensemble des éphémères
        self.assertEqual(list(got["capabilities"]), ["read", "report-drift", "propose"])
        self.assertTrue(got["ephemeral"])
        self.assertEqual(got["cwd"], "/w/lot52")

    def test_idempotent_et_plafond(self):
        self.open("1")
        self.assertTrue(self.open("1")["existing"])
        self.open("2", max_parallel=2)
        with self.assertRaisesRegex(ps.ChildError, "plafond"):
            self.open("3", max_parallel=2)
        self.db.execute("UPDATE agent_registry SET status = 'stopped' WHERE name = 'verif-a.l1'")
        self.assertFalse(self.open("3", max_parallel=2)["existing"])   # place libérée
        with self.assertRaisesRegex(ps.ChildError, "plus vivante"):
            self.open("1")

    def test_refus(self):
        with self.assertRaisesRegex(ps.ChildError, "lot illisible"):
            self.open("abc")
        with self.assertRaisesRegex(ps.ChildError, "inconnue"):
            ps.open_child(self.db, "personne", "4", ttl_seconds=60)
        self.open("4")
        with self.assertRaisesRegex(ps.ChildError, "éphémère"):
            ps.open_child(self.db, "verif-a.l4", "5", ttl_seconds=60)
        self.db.execute("UPDATE agent_registry SET responsible = NULL WHERE name = 'verif-a'")
        with self.assertRaisesRegex(ps.ChildError, "responsable"):
            self.open("6")
        self.db.execute("UPDATE agent_registry SET responsible = 'humain', status = 'stopped' "
                        "WHERE name = 'verif-a'")
        with self.assertRaisesRegex(ps.ChildError, "arrêtée"):
            self.open("6")

    def test_nom_pris_hors_persona(self):
        registry.upsert(self.db, "verif-a.l7", harness="deepseek", host="pc")
        with self.assertRaisesRegex(ps.ChildError, "existe déjà"):
            self.open("7")

    def test_sessions_sous_la_persona(self):
        registry.set_session(self.db, "verif-a", "m1")
        self.open("52")
        registry.set_session(self.db, "verif-a.l52", "f1")
        registry.set_session(self.db, "verif-a.l52", "f2")
        rows = {r["session_id"]: r for r in ps.sessions(self.db, "verif-a")}
        self.assertEqual(set(rows), {"m1", "f1", "f2"})
        # la session de la fille ne remplace pas celle de la persona
        self.assertIsNone(rows["m1"]["ended_at"])
        self.assertEqual(rows["f1"]["end_reason"], "remplacée par f2")
        self.assertEqual((rows["f2"]["agent"], rows["f2"]["work_item"]), ("verif-a.l52", "52"))
        self.assertEqual(rows["m1"]["agent"], "verif-a")
        ps.record_end(self.db, "verif-a.l52", "f2", "fin de lot")
        self.assertEqual({r["session_id"]: r for r in ps.sessions(self.db, "verif-a")}
                         ["f2"]["end_reason"], "fin de lot")
        self.assertEqual(ps.sessions(self.db, "verif-a.l52"), [])

    def test_heritage_relu(self):
        from ameesh import storage
        self.open("52", cwd="/w/lot52")
        self.db.execute("UPDATE agent_registry SET responsible = 'autre', model = 'm2', "
                        "capabilities = ARRAY['read'], cwd = '/w/ailleurs' "
                        "WHERE name = 'verif-a'")
        changed = storage.of(self.db).ephemerals.refresh_children("pc")
        self.assertEqual([c["name"] for c in changed], ["verif-a.l52"])
        got = self.db.query("SELECT responsible, model, capabilities, cwd FROM agent_registry "
                            "WHERE name = 'verif-a.l52'")[0]
        self.assertEqual((got["responsible"], got["model"], list(got["capabilities"])),
                         ("autre", "m2", ["read"]))
        self.assertEqual(got["cwd"], "/w/lot52")            # dossier propre à la fille
        self.assertEqual(storage.of(self.db).ephemerals.refresh_children("pc"), [])
        self.assertEqual(storage.of(self.db).ephemerals.refresh_children("autre-hote"), [])


class AcheminementParLotTest(PgTestCase):
    """L52c : le courrier d'un lot va à la fille de la persona sur ce lot."""

    def setUp(self):
        super().setUp()
        registry.upsert(self.db, "verif-a", harness="deepseek", host="pc", mode="execute")
        self.db.execute("UPDATE agent_registry SET responsible = 'humain' "
                        "WHERE name = 'verif-a'")
        ps.open_child(self.db, "verif-a", "52", ttl_seconds=3600)

    def send(self, lot=None, signed=None):
        from ameesh.backend import PgBackend
        return PgBackend(self.cfg, self.db).send("orchestre", "verif-a", "Avance sur le lot.",
                                                 work_item_id=lot, signed=signed)

    def test_route(self):
        self.assertEqual(ps.route(self.db, "verif-a", "52"), "verif-a.l52")
        self.assertEqual(ps.route(self.db, "verif-a", "53"), "verif-a")
        self.assertEqual(ps.route(self.db, "verif-a", None), "verif-a")
        self.db.execute("UPDATE agent_registry SET status = 'stopped' "
                        "WHERE name = 'verif-a.l52'")
        self.assertEqual(ps.route(self.db, "verif-a", "52"), "verif-a")

    def test_envoi(self):
        self.assertEqual(self.send("52"), ["verif-a.l52"])
        self.assertEqual(self.send("53"), ["verif-a"])
        self.assertEqual(self.send(), ["verif-a"])
        rows = self.db.query("SELECT recipient, work_item_id FROM agent_mailbox ORDER BY id")
        self.assertEqual([(r["recipient"], r["work_item_id"]) for r in rows],
                         [("verif-a.l52", "52"), ("verif-a", "53"), ("verif-a", None)])


class FinDeFilleTest(PgTestCase):
    """L52e : une fille s'éteint en fin de lot, son courrier revient à la persona."""

    def setUp(self):
        super().setUp()
        registry.upsert(self.db, "verif-a", harness="deepseek", host="pc", mode="execute")
        self.db.execute("UPDATE agent_registry SET responsible = 'humain' "
                        "WHERE name = 'verif-a'")
        self.lot = str(self.db.query(
            "INSERT INTO work_items (title, state) VALUES ('Lot', 'build') RETURNING id")[0]["id"])
        ps.open_child(self.db, "verif-a", self.lot, ttl_seconds=3600)
        self.child = ps.child_name("verif-a", self.lot)

    def deposit(self, signed=False):
        from ameesh import mail
        return mail.send(self.db, "orchestre", self.child, "Point sur le lot.",
                         work_item_id=self.lot,
                         signature_key="cle" if signed else None)

    def registre(self):
        return self.db.query("SELECT status, stop_reason FROM agent_registry WHERE name = %s",
                             (self.child,))[0]

    def test_fermeture_a_la_main(self):
        registry.set_session(self.db, self.child, "f1")
        self.deposit()
        self.deposit(signed=True)
        out = ps.close_child(self.db, self.child)
        self.assertEqual((out["persona"], out["stopped"], out["repatriated"],
                          out["kept_signed"]), ("verif-a", True, 1, 1))
        self.assertEqual(dict(self.registre()), {"status": "stopped",
                                                 "stop_reason": "fin_de_lot"})
        rows = self.db.query("SELECT recipient FROM agent_mailbox ORDER BY id")
        self.assertEqual([r["recipient"] for r in rows], ["verif-a", self.child])
        self.assertEqual(ps.sessions(self.db, "verif-a")[0]["end_reason"], "fin de lot")
        # idempotent
        again = ps.close_child(self.db, self.child)
        self.assertEqual((again["stopped"], again["repatriated"]), (False, 0))
        # le courrier du lot ne va plus à la fille arrêtée
        self.assertEqual(ps.route(self.db, "verif-a", self.lot), "verif-a")

    def test_refus(self):
        with self.assertRaisesRegex(ps.ChildError, "pas une session parallèle"):
            ps.close_child(self.db, "verif-a")
        with self.assertRaisesRegex(ps.ChildError, "inconnu"):
            ps.close_child(self.db, "personne")

    def test_fin_du_lot(self):
        self.assertEqual(ps.close_finished(self.db, "pc"), [])        # lot en cours
        self.deposit()
        self.db.execute("UPDATE work_items SET state = 'promoted', closed_at = now() "
                        "WHERE id = %s", (int(self.lot),))
        self.assertEqual(ps.close_finished(self.db, "autre-hote"), [])
        out = ps.close_finished(self.db, "pc")
        self.assertEqual([(o["name"], o["repatriated"]) for o in out], [(self.child, 1)])
        self.assertEqual(self.registre()["stop_reason"], "fin_de_lot")
        self.assertEqual(ps.close_finished(self.db, "pc"), [])         # rien de plus

    def test_echeance(self):
        self.db.execute("UPDATE agent_registry SET ephemeral_expires_at = now() - "
                        "interval '1 minute' WHERE name = %s", (self.child,))
        out = ps.close_finished(self.db, "pc")
        self.assertEqual([o["name"] for o in out], [self.child])
        self.assertEqual(self.registre()["status"], "stopped")

    def test_courrier_reste_chez_une_fille_arretee(self):
        self.db.execute("UPDATE agent_registry SET status = 'stopped', stop_reason = 'manuel' "
                        "WHERE name = %s", (self.child,))
        self.deposit()
        out = ps.close_finished(self.db, "pc")
        self.assertEqual([(o["stopped"], o["repatriated"]) for o in out], [(False, 1)])
        self.assertEqual(self.registre()["stop_reason"], "manuel")      # arrêt humain gardé


class VueUniqueTest(PgTestCase):
    """L52d : persona et sessions parallèles vues ensemble."""

    def setUp(self):
        super().setUp()
        for name in ("verif-a", "verif-b"):
            registry.upsert(self.db, name, harness="deepseek", host="pc", mode="execute",
                            chantier="p1")
        self.db.execute("UPDATE agent_registry SET responsible = 'humain' "
                        "WHERE name IN ('verif-a', 'verif-b')")
        ps.open_child(self.db, "verif-a", "52", ttl_seconds=3600)
        ps.open_child(self.db, "verif-a", "7", ttl_seconds=3600)

    def test_list_json(self):
        from ameesh import exploitation
        rows = exploitation.annotate(self.cfg, self.db, registry.overview(self.db))
        by = {r["name"]: r for r in rows}
        self.assertEqual(by["verif-a"]["children"], ["verif-a.l52", "verif-a.l7"])
        self.assertIsNone(by["verif-a"]["parent"])
        self.assertEqual(by["verif-a.l52"]["parent"], "verif-a")
        self.assertEqual(by["verif-b"]["children"], [])

    def test_list_texte(self):
        import argparse
        import contextlib
        import io
        from ameesh import mesh_cli
        out = io.StringIO()
        with mock_open_db(self.db), contextlib.redirect_stdout(out):
            mesh_cli.cmd_list(self.cfg, argparse.Namespace(json=False))
        names = [line.split("  ")[0].strip() for line in out.getvalue().splitlines()[1:]
                 if line and not line.startswith("lots")]
        a = names.index("verif-a")
        self.assertEqual(names[a + 1:a + 3], ["└ .l52", "└ .l7"])

    def test_diffusion_a_la_persona_seulement(self):
        from ameesh.backend import PgBackend
        targets = PgBackend(self.cfg, self.db).send("verif-b", "all", "Point d'équipe.")
        self.assertEqual(targets, ["verif-a"])


def mock_open_db(db):
    """`cmd_list` ouvre et ferme sa propre connexion : on lui prête celle du test."""
    from unittest import mock

    class _Prete:
        def __getattr__(self, name):
            return getattr(db, name)

        def close(self):
            pass
    return mock.patch("ameesh.mesh_cli._open", lambda cfg: _Prete())
