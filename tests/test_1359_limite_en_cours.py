# SPDX-License-Identifier: AGPL-3.0-only
"""#1359 : limite d'en-cours (§4 du canon, owner 14/10/2026) — au plus
`build.max_in_build` lots en `build` par agent (canon compilé, #2451),
surplus rendu à la file d'équipe. Le plafond est ici une valeur de fixture
explicite (`CAP`) ; sa lecture au canon est couverte par
test_2451_canon_consommateurs.

Couvre : la prise atomique sous plafond (`assign_capped`), le refus du
troisième lot dans la prise automatique, la restitution CAS (`work return`),
le balayage idempotent (`ameesh work wip`) avec son dry-run et son courrier,
l'exception pilote explicite, et les compteurs de `ameesh list` / progress.
"""
from __future__ import annotations

import os
import time
from unittest import mock

from ameesh import backlog, handoff, mail, notify, progress, registry, storage, work, wip
from ameesh.db import DbError
from ameesh.storage import interface

from .support import FIXTURE_BUILD_CAP, PgTestCase, mechanics_canon

HEURE = 3600.0


#: plafond §4 de fixture (`support.FIXTURE_BUILD_CAP`, à la place du canon, #2451)
CAP = FIXTURE_BUILD_CAP


class _Base(PgTestCase):
    def canon_env(self) -> dict:
        """Un sous-processus ne voit pas le plafond de fixture : il lit un canon
        Git minimal (`support.mechanics_canon`, plafond 2), comme en vrai."""
        root = mechanics_canon(os.path.join(self.tmp, "canon"))
        return self.env(AMEESH_CANON=root, AMEESH_CANON_REF="HEAD")

    def agent(self, name, harness="deepseek", *, status="idle", team="ameesh",
              since_s=2 * HEURE):
        registry.upsert(self.db, name, harness=harness, host=self.cfg.host)
        self.db.execute(
            "UPDATE agent_registry SET status = %s, lease_owner = %s,"
            " lease_expires_at = now() + interval '1 hour', team = %s,"
            " status_since = now() - make_interval(secs => %s),"
            " last_turn_at = now() - make_interval(secs => %s)"
            " WHERE name = %s",
            (status, "runner-%s@h" % name, team, since_s, since_s, name))

    def lot(self, title, *, app="ameesh", assignee=None, branch=None, age_s=HEURE):
        item = work.add(self.db, title=title, app=app, assignee=assignee, branch=branch)
        # un lot tout juste créé n'est pas encore prenable (LOT_MIN_AGE_S) :
        # l'ancienneté est simulée, comme dans les tests de la prise L165
        self.db.execute("UPDATE work_items SET created_at = created_at - make_interval("
                        "secs => %s) WHERE id = %s", (age_s, item["id"]))
        return work.get(self.db, item["id"])

    def build(self, item):
        return work.move(self.db, item["id"], "build", note="en build", actor="human:proprio")

    def aged(self, item_id, hours):
        """Recule toute l'activité d'un lot (le balayage rend les moins récents)."""
        self.db.execute("UPDATE work_items SET updated_at = now() - make_interval("
                        "hours => %s) WHERE id = %s", (int(hours), item_id))
        self.db.execute("UPDATE work_item_events SET created_at = now() - make_interval("
                        "hours => %s) WHERE work_item_id = %s", (int(hours), item_id))

    def builds_of(self, assignee):
        return storage.of(self.db).work.count_builds(assignee)

    def courriers(self, recipient=None):
        sql = ("SELECT sender, recipient, kind, work_item_id, body FROM agent_mailbox")
        params: tuple = ()
        if recipient:
            sql += " WHERE recipient = %s"
            params = (recipient,)
        return self.db.query(sql + " ORDER BY id", params)

    def take(self, **kw):
        kw.setdefault("now", time.time())
        kw.setdefault("paid", ("deepseek",))
        kw.setdefault("budget_check", lambda agent: "")
        kw.setdefault("orchestras", [])
        return backlog.auto_take(self.cfg, self.db, **kw)

    def attributions(self, item_id):
        """Les notes d'ATTRIBUTION du lot (§4 : elles portent l'exception
        pilote), dans l'ordre du journal."""
        return [e["note"] for e in work.events(self.db, item_id)
                if (e.get("note") or "").startswith(work.ASSIGN_NOTES)]

    def surplus(self, item_id, *, current="dev1", **kw):
        """Une restitution de surplus directe (les gardes du balayage)."""
        kw.setdefault("marker", work.WIP_EXCEPTION_MARK)
        kw.setdefault("prefixes", work.ASSIGN_NOTES)
        kw.setdefault("max_build", CAP)
        kw.setdefault("note", "restitution")
        kw.setdefault("actor", "ameesh")
        return storage.of(self.db).work.unassign_surplus(item_id, current=current, **kw)


class PriseSousPlafondTest(_Base):
    def test_prise_atomique_sous_le_plafond(self):
        self.agent("dev1")
        self.agent("dev2")
        for i in range(2):
            self.build(self.lot("occupé %d" % i, assignee="dev1"))
        st = storage.of(self.db).work
        libre1 = self.lot("libre 1")
        self.assertIsNone(st.assign_capped(libre1["id"], "dev1", current=None,
                                           note="prise", actor="ameesh",
                                           max_build=CAP))
        self.assertEqual(self.builds_of("dev1"), 2)
        self.assertIsNone(work.get(self.db, libre1["id"])["assignee"])
        # sous le plafond, la prise passe
        libre2 = self.lot("libre 2")
        self.assertIsNotNone(st.assign_capped(libre2["id"], "dev2", current=None,
                                              note="prise", actor="ameesh",
                                              max_build=CAP))
        self.assertEqual(work.get(self.db, libre2["id"])["assignee"], "dev2")
        # CAS : un lot qui a bougé sous nos pieds n'est pas assigné
        libre3 = self.lot("libre 3")
        self.assertIsNone(st.assign_capped(libre3["id"], "dev2", current="quelqu_un",
                                           note="prise", actor="ameesh",
                                           max_build=CAP))
        self.assertIsNone(work.get(self.db, libre3["id"])["assignee"])

    def test_la_prise_automatique_ignore_l_agent_au_plafond(self):
        self.agent("dev1")
        self.agent("dev2")
        for i in range(2):
            self.build(self.lot("occupé %d" % i, assignee="dev1"))
        libre = self.lot("à prendre")
        out = self.take()
        self.assertEqual([(t["agent"], t["item"]["id"]) for t in out],
                         [("dev2", libre["id"])])
        self.assertEqual(work.get(self.db, libre["id"])["assignee"], "dev2")

    def test_plafond_atteint_a_l_ecriture_garde_le_lot_pour_un_autre(self):
        """Le contrôle avant la prise passe, puis `assign_capped` refuse pour
        plafond (une prise concurrente du même agent) : le lot n'a pas bougé et
        doit rester offert à l'agent suivant, pas être retiré de la file
        d'essai."""
        self.agent("dev1")
        self.agent("dev2")
        libre = self.lot("à prendre")
        st = storage.of(self.db)
        real = type(st.work).assign_capped
        calls: list = []

        def capped(self_, item_id, assignee, **kw):
            if not calls:               # le premier essai (dev1) : plafond
                calls.append(assignee)
                return None
            return real(self_, item_id, assignee, **kw)

        with mock.patch.object(type(st.work), "assign_capped", capped):
            out = self.take()
        self.assertEqual([(t["agent"], t["item"]["id"]) for t in out],
                         [("dev2", libre["id"])])
        self.assertEqual(work.get(self.db, libre["id"])["assignee"], "dev2")


class RestitutionTest(_Base):
    def test_retour_cas_conserve_branche_et_journal(self):
        self.agent("dev1")
        lot = self.build(self.lot("avec branche", assignee="dev1",
                                  branch="lot/1359-limite-en-cours"))
        row = work.return_to_queue(self.db, lot["id"], actor="dev1", cfg=self.cfg)
        self.assertIsNone(row["assignee"])
        self.assertEqual(row["branch"], "lot/1359-limite-en-cours")
        self.assertEqual(row["state"], "build")
        notes = [e["note"] for e in work.events(self.db, lot["id"])]
        self.assertTrue(any(n.startswith(wip.RETURN_NOTE) for n in notes), notes)
        self.assertEqual(self.builds_of("dev1"), 0)
        # déjà rendu : rien à rendre
        with self.assertRaises(work.WorkError):
            work.return_to_queue(self.db, lot["id"], actor="dev1", cfg=self.cfg)

    def test_un_agent_ne_rend_pas_le_lot_d_un_autre(self):
        self.agent("dev1")
        self.agent("dev2")
        lot = self.build(self.lot("à dev1", assignee="dev1"))
        with self.assertRaises(work.WorkError):
            work.return_to_queue(self.db, lot["id"], actor="dev2", cfg=self.cfg)
        self.assertEqual(work.get(self.db, lot["id"])["assignee"], "dev1")
        # un humain rend le lot
        row = work.return_to_queue(self.db, lot["id"], actor="human:proprio", cfg=self.cfg)
        self.assertIsNone(row["assignee"])

    def test_restitution_cas_refusee_si_l_assigne_a_bouge(self):
        self.agent("dev1")
        lot = self.build(self.lot("cas", assignee="dev1"))
        st = storage.of(self.db).work
        self.assertIsNone(st.unassign(lot["id"], current="dev2", note="n", actor="ameesh"))
        self.assertEqual(work.get(self.db, lot["id"])["assignee"], "dev1")

    def test_le_retour_manuel_refuse_hors_intake_build(self):
        self.agent("dev1")
        lot = self.build(self.lot("en revue", assignee="dev1"))
        work.move(self.db, lot["id"], "qa", note="en revue", actor="dev1")
        with self.assertRaises(work.WorkError) as cm:
            work.return_to_queue(self.db, lot["id"], actor="dev1", cfg=self.cfg)
        self.assertIn("only an intake or build", str(cm.exception))
        self.assertEqual(work.get(self.db, lot["id"])["assignee"], "dev1")
        # la course : un lot passé en qa entre la lecture et l'écriture ne se
        # rend pas non plus (l'état est relu dans la transaction qui écrit)
        st = storage.of(self.db).work
        self.assertIsNone(st.unassign(lot["id"], current="dev1", note="n",
                                      actor="ameesh", states=work.RETURN_STATES))
        self.assertEqual(work.get(self.db, lot["id"])["assignee"], "dev1")
        # un lot `intake` assigné se rend, lui
        intake = self.lot("intake assigné", assignee="dev1")
        row = work.return_to_queue(self.db, intake["id"], actor="dev1", cfg=self.cfg)
        self.assertIsNone(row["assignee"])
        self.assertEqual(row["state"], "intake")

    def test_chaque_restitution_recompte_le_surplus_sous_verrou(self):
        self.agent("dev1")
        lots = [self.build(self.lot("occupé %d" % i, assignee="dev1")) for i in range(4)]
        first = self.surplus(lots[0]["id"])
        self.assertIsNotNone(first)
        self.assertIsNone(first["assignee"])
        self.assertEqual(self.builds_of("dev1"), 3)
        second = self.surplus(lots[1]["id"])
        self.assertIsNotNone(second)
        self.assertEqual(self.builds_of("dev1"), 2)
        # le troisième essai ne trouve plus de surplus : rien n'est écrit
        self.assertIsNone(self.surplus(lots[2]["id"]))
        self.assertEqual(work.get(self.db, lots[2]["id"])["assignee"], "dev1")
        self.assertEqual(self.builds_of("dev1"), 2)

    def test_la_restitution_du_surplus_relit_etat_et_exception(self):
        self.agent("dev1")
        lots = [self.build(self.lot("occupé %d" % i, assignee="dev1")) for i in range(4)]
        # un lot passé en qa n'est plus de l'en-cours : pas rendu
        work.move(self.db, lots[0]["id"], "qa", note="en revue", actor="dev1")
        self.assertIsNone(self.surplus(lots[0]["id"]))
        self.assertEqual(work.get(self.db, lots[0]["id"])["assignee"], "dev1")
        # une exception pilote posée après la lecture protège le lot
        work.assign(self.db, lots[1]["id"], "dev1", actor="human:proprio", cfg=self.cfg,
                    wip_exception="reprise urgente")
        self.assertIsNone(self.surplus(lots[1]["id"]))
        self.assertEqual(work.get(self.db, lots[1]["id"])["assignee"], "dev1")
        # le surplus se compte SANS les lots exceptés : 3 lots en build dont 1
        # excepté → 2 sans exception, plus de surplus
        self.assertIsNone(self.surplus(lots[2]["id"]))
        self.assertEqual(work.get(self.db, lots[2]["id"])["assignee"], "dev1")
        self.assertEqual(self.builds_of("dev1"), 3)
        # l'exception est toujours là, et le lot de qa ne compte pas
        st = storage.of(self.db).work
        self.assertEqual(st.wip_exceptions([lots[1]["id"]], work.WIP_EXCEPTION_MARK,
                                           work.ASSIGN_NOTES), [lots[1]["id"]])

    def test_le_lot_rendu_reste_prenable_avant_les_intake(self):
        self.agent("dev1")
        self.agent("dev2")
        lot = self.build(self.lot("rendu", assignee="dev1", branch="lot/rendu"))
        work.return_to_queue(self.db, lot["id"], actor="dev1", cfg=self.cfg)
        st = storage.of(self.db).work
        rows = st.unassigned_lots(50)
        self.assertIn(lot["id"], [int(i["id"]) for i in rows])
        intake = self.lot("intake neuf")
        ids = [int(i["id"]) for i in st.unassigned_lots(50)]
        self.assertLess(ids.index(lot["id"]), ids.index(intake["id"]))
        row = next(i for i in rows if int(i["id"]) == lot["id"])
        self.assertTrue(row["returned_to_queue"])
        self.assertTrue(backlog.lot_takeable(row))

    def test_un_build_jamais_rendu_n_est_pas_a_la_file(self):
        # L165/#1193 : un lot `build` sans assigné n'est pas forcément rendu
        # (prérequis, travail confié par courrier) — il ne passe pas à la file.
        self.agent("dev1")
        lot = self.lot("prérequis")
        work.move(self.db, lot["id"], "build", note="en cours", actor="human:proprio")
        st = storage.of(self.db).work
        self.assertNotIn(lot["id"], [int(i["id"]) for i in st.unassigned_lots(50)])
        rows = {int(r["id"]): r
                for r in storage.of(self.db).operations.open_lots_activity(50)}
        self.assertFalse(rows[lot["id"]]["returned_to_queue"])
        # témoin : avec une équipe connue, le lot reste refusé tant qu'il n'a
        # pas été rendu ; le même lot marqué rendu serait prenable
        row = dict(rows[lot["id"]], lot_team="ameesh", required_capabilities=[])
        self.assertFalse(backlog.lot_takeable(row))
        self.assertTrue(backlog.lot_takeable(dict(row, returned_to_queue=True)))
        self.assertEqual(self.take(), [])

    def test_un_element_de_la_file_en_build_doit_etre_rendu(self):
        self.agent("dev1")
        item = backlog.add(self.db, title="dette", value_score=50,
                           expected_value="gain mesurable")
        work.assign(self.db, item["id"], "dev1", actor="human:proprio", cfg=self.cfg)
        work.move(self.db, item["id"], "build", note="en build", actor="human:proprio")
        self.assertNotIn(item["id"], [int(i["id"]) for i in backlog.items(self.db)])
        work.return_to_queue(self.db, item["id"], actor="human:proprio", cfg=self.cfg)
        self.assertIn(item["id"], [int(i["id"]) for i in backlog.items(self.db)])

    def test_la_prise_automatique_reprend_un_lot_rendu(self):
        self.agent("dev1")
        self.agent("dev2")
        for i in range(2):      # dev1 reste au plafond après la restitution
            self.build(self.lot("occupé %d" % i, assignee="dev1"))
        lot = self.build(self.lot("rendu", assignee="dev1", branch="lot/rendu"))
        work.return_to_queue(self.db, lot["id"], actor="dev1", cfg=self.cfg)
        out = self.take()
        self.assertEqual([(t["agent"], t["item"]["id"]) for t in out],
                         [("dev2", lot["id"])])
        item = work.get(self.db, lot["id"])
        self.assertEqual((item["assignee"], item["branch"], item["state"]),
                         ("dev2", "lot/rendu", "build"))


class CourseSurLeCompteTest(_Base):
    """cx1 R2 (#1053) : le recensement des lots d'un agent verrouille TOUTES
    ses lignes `build` (id croissant : ordre stable) AVANT de lire les
    exceptions. Sans cela, une réattribution d'un AUTRE lot compté pendant le
    compte écrit une note d'attribution sans marqueur : son exception s'efface
    sous la lecture, le lot est compté « libre » et le balayage rend un lot qui
    n'est plus du surplus."""

    #: le verrou du compte tient jusqu'à son COMMIT : le geste concurrent est
    #: TOUJOURS bloqué, ce délai ne fait qu'écourter l'attente du témoin
    LOCK_TIMEOUT = "300ms"

    def _compte_sous_geste(self, candidat, cible, geste):
        """Le compte de surplus de `candidat`, avec `geste(tx)` exécuté depuis
        une AUTRE session au premier appel de `_wip_exception_locked` pour
        `cible` — donc après la lecture du recensement, avant celle de son
        exception. Le geste part dans sa propre transaction avec un
        `lock_timeout` court : il aboutit si la ligne n'est pas verrouillée,
        sinon il est bloqué (SQLSTATE 55P03). Rend `(rendu, out, autre)` ;
        `out` porte `tente`, `bloque` et l'erreur, `autre` reste ouverte pour
        rejouer le geste après le compte (fermeture automatique en fin de
        test)."""
        st = storage.of(self.db)
        real = type(st.work)._wip_exception_locked
        autre = self.connect()
        self.addCleanup(autre.close)
        out: dict = {"tente": False, "bloque": None, "erreur": None}

        def hook(self_, item_id, marker, prefixes):
            if not out["tente"] and int(item_id) == int(cible):
                out["tente"] = True
                try:
                    with autre.transaction() as tx:
                        tx.execute("SET LOCAL lock_timeout = '%s'" % self.LOCK_TIMEOUT)
                        geste(tx)
                    out["bloque"] = False
                except Exception as exc:      # la ligne est tenue par le compte
                    out["bloque"] = True
                    out["erreur"] = exc
            return real(self_, item_id, marker, prefixes)

        with mock.patch.object(type(st.work), "_wip_exception_locked", hook):
            rendu = self.surplus(candidat)
        return rendu, out, autre

    def assertGesteBloque(self, out):
        """Le geste a été bloqué par le verrou du compte, jamais par autre
        chose : le code SQLSTATE tranche (55P03, lock_not_available)."""
        self.assertTrue(out["tente"], "le geste n'a pas été déclenché")
        self.assertIsInstance(out["erreur"], DbError, out["erreur"])
        self.assertEqual(out["erreur"].sqlstate, "55P03", str(out["erreur"]))
        self.assertTrue(out["bloque"])

    def test_la_reaffectation_d_un_autre_lot_excepte_ne_rend_pas_le_candidat(self):
        """Le témoin de cx1 : A/B/C chez dev1, B excepté (A et C libres = 2,
        aucun surplus). B est réattribué à dev2 pendant le compte de A : sans
        le verrou de B, sa note sans marqueur efface son exception, le compte
        voit 3 lots libres et rend A — que dev1 n'aurait plus eu à rendre."""
        self.agent("dev1")
        self.agent("dev2")
        lots = [self.build(self.lot("occupé %d" % i, assignee="dev1"))
                for i in range(3)]
        a, b = lots[0], lots[1]
        work.assign(self.db, b["id"], "dev1", actor="human:proprio", cfg=self.cfg,
                    wip_exception="reprise urgente")
        rendu, out, autre = self._compte_sous_geste(
            a["id"], b["id"],
            lambda tx: work.assign(tx, b["id"], "dev2", actor="human:proprio",
                                   cfg=self.cfg))
        self.assertGesteBloque(out)
        # B garde son exception pendant le compte : A n'est pas du surplus
        self.assertIsNone(rendu)
        self.assertEqual(work.get(self.db, a["id"])["assignee"], "dev1")
        # le compte fini, la réattribution passe : dev1 garde A et C (2, plafond)
        work.assign(autre, b["id"], "dev2", actor="human:proprio", cfg=self.cfg)
        self.assertEqual(work.get(self.db, b["id"])["assignee"], "dev2")
        self.assertEqual(self.builds_of("dev1"), 2)

    def test_la_mise_en_qa_d_un_autre_lot_est_serialisee_par_le_compte(self):
        """A/B/C chez dev1, tous libres : le passage en `qa` de B attend la fin
        du compte. Le compte voit donc encore B en `build` (3 libres) et A,
        moins récemment actif, est bien du surplus."""
        self.agent("dev1")
        lots = [self.build(self.lot("occupé %d" % i, assignee="dev1"))
                for i in range(3)]
        a, b = lots[0], lots[1]
        rendu, out, _ = self._compte_sous_geste(
            a["id"], b["id"],
            lambda tx: work.move(tx, b["id"], "qa", note="en revue", actor="dev1"))
        self.assertGesteBloque(out)
        self.assertIsNotNone(rendu)
        self.assertEqual(rendu["id"], a["id"])
        # après le compte, le geste passe : B en revue ne compte plus
        work.move(self.db, b["id"], "qa", note="en revue", actor="dev1")
        self.assertEqual(work.get(self.db, b["id"])["state"], "qa")
        self.assertEqual(self.builds_of("dev1"), 1)

    def test_une_exception_posee_sur_un_autre_lot_attend_la_fin_du_compte(self):
        """A/B/C chez dev1, tous libres. Le pilote pose une exception sur C
        pendant le compte de A : le geste attend, le compte voit C encore
        libre (3 libres, A est du surplus) ; l'exception s'applique ensuite."""
        self.agent("dev1")
        lots = [self.build(self.lot("occupé %d" % i, assignee="dev1"))
                for i in range(3)]
        a, c = lots[0], lots[2]
        rendu, out, _ = self._compte_sous_geste(
            a["id"], c["id"],
            lambda tx: work.assign(tx, c["id"], "dev1", actor="human:proprio",
                                   cfg=self.cfg, wip_exception="reprise urgente"))
        self.assertGesteBloque(out)
        self.assertIsNotNone(rendu)
        self.assertEqual(rendu["id"], a["id"])
        # le compte fini, l'exception passe : C protégé, dev1 garde B et C
        work.assign(self.db, c["id"], "dev1", actor="human:proprio", cfg=self.cfg,
                    wip_exception="reprise urgente")
        st = storage.of(self.db).work
        self.assertEqual(st.wip_exceptions([c["id"]], work.WIP_EXCEPTION_MARK,
                                           work.ASSIGN_NOTES), [c["id"]])
        self.assertEqual(self.builds_of("dev1"), 2)


class BrancheSeuleTest(_Base):
    """L118 restauré (§4) : `work assign` sur le MÊME assigné avec une branche
    ne pose que la branche — délégation en cours (L40) et exception pilote (§4)
    restent, aucune attribution n'est écrite."""

    def test_meme_assigne_avec_branche_garde_la_delegation(self):
        self.agent("dev1")
        self.agent("dev2")
        lot = self.build(self.lot("délégué", assignee="dev1"))
        work.delegate(self.db, lot["id"], "dev2", within="30m", actor="dev1",
                      cfg=self.cfg)
        before = work.get(self.db, lot["id"])
        self.assertEqual(before["assignee"], "dev2")
        self.assertTrue(before["delegated_by"])
        attrs = self.attributions(lot["id"])
        row = work.assign(self.db, lot["id"], "dev2", actor="human:proprio",
                          cfg=self.cfg, branch="lot/1359-branche")
        self.assertEqual(row["item"]["branch"], "lot/1359-branche")
        after = work.get(self.db, lot["id"])
        # la délégation n'est pas annulée : le lot n'a pas été réattribué
        self.assertEqual(after["assignee"], "dev2")
        self.assertEqual(after["delegated_by"], before["delegated_by"])
        self.assertEqual(after["due_ts"], before["due_ts"])
        self.assertEqual(self.attributions(lot["id"]), attrs)

    def test_meme_assigne_avec_branche_garde_l_exception_pilote(self):
        self.agent("dev1")
        lot = self.build(self.lot("exception", assignee="dev1"))
        work.assign(self.db, lot["id"], "dev1", actor="human:proprio", cfg=self.cfg,
                    wip_exception="reprise urgente")
        attrs = self.attributions(lot["id"])
        self.assertTrue(any(work.WIP_EXCEPTION_MARK in n for n in attrs))
        row = work.assign(self.db, lot["id"], "dev1", actor="dev1", cfg=self.cfg,
                          branch="lot/1359-exception")
        self.assertEqual(row["item"]["branch"], "lot/1359-exception")
        # aucune attribution de plus : l'exception tient et protège le lot
        self.assertEqual(self.attributions(lot["id"]), attrs)
        st = storage.of(self.db).work
        self.assertEqual(st.wip_exceptions([lot["id"]], work.WIP_EXCEPTION_MARK,
                                           work.ASSIGN_NOTES), [lot["id"]])
        self.assertIsNone(self.surplus(lot["id"]))

    def test_une_exception_avec_le_meme_assigne_reecrit_l_attribution(self):
        """Avec une raison, `work assign` reste une attribution : l'exception
        est réécrite et la délégation en cours annulée (L40)."""
        self.agent("dev1")
        self.agent("dev2")
        lot = self.build(self.lot("délégué", assignee="dev1"))
        work.delegate(self.db, lot["id"], "dev2", within="30m", actor="dev1",
                      cfg=self.cfg)
        work.assign(self.db, lot["id"], "dev2", actor="human:proprio", cfg=self.cfg,
                    wip_exception="reprise urgente")
        after = work.get(self.db, lot["id"])
        self.assertIsNone(after["delegated_by"])
        self.assertTrue(any(work.WIP_EXCEPTION_MARK in n
                            for n in self.attributions(lot["id"])))


class NotesAttributionTest(_Base):
    """§4 : toutes les notes qui CONFIENT un lot sont des attributions — la
    dernière porte l'exception pilote, une postérieure sans marqueur l'efface."""

    def test_les_textes_sont_partages(self):
        self.assertEqual(handoff._ASSIGN_NOTES, interface.ASSIGN_NOTE_PREFIXES)
        self.assertEqual(work.ASSIGN_NOTES, interface.ASSIGN_NOTE_PREFIXES)
        self.assertEqual(backlog.AUTO_TAKE_NOTE, interface.AUTO_TAKE_NOTE)
        self.assertEqual(backlog.LOT_TAKE_NOTE, interface.LOT_TAKE_NOTE)
        self.assertEqual(handoff.HANDOFF_NOTE, interface.HANDOFF_NOTE)
        for prefix in (interface.DELEGATED_NOTE, interface.LOT_TAKE_NOTE,
                       interface.AUTO_TAKE_NOTE, interface.HANDOFF_NOTE):
            self.assertIn(prefix, interface.ASSIGN_NOTE_PREFIXES)

    def test_une_delegation_posterieure_efface_l_exception(self):
        self.agent("dev1")
        self.agent("dev2")
        lot = self.build(self.lot("exception", assignee="dev1"))
        work.assign(self.db, lot["id"], "dev1", actor="human:proprio", cfg=self.cfg,
                    wip_exception="reprise urgente")
        st = storage.of(self.db).work
        self.assertEqual(st.wip_exceptions([lot["id"]], work.WIP_EXCEPTION_MARK,
                                           work.ASSIGN_NOTES), [lot["id"]])
        work.delegate(self.db, lot["id"], "dev2", within="30m", actor="dev1",
                      cfg=self.cfg)
        self.assertEqual(st.wip_exceptions([lot["id"]], work.WIP_EXCEPTION_MARK,
                                           work.ASSIGN_NOTES), [])

    def test_une_prise_automatique_posterieure_efface_l_exception(self):
        self.agent("dev1")
        self.agent("dev2")
        lot = self.build(self.lot("exception", assignee="dev1"))
        work.assign(self.db, lot["id"], "dev1", actor="human:proprio", cfg=self.cfg,
                    wip_exception="reprise urgente")
        work.return_to_queue(self.db, lot["id"], actor="human:proprio", cfg=self.cfg)
        st = storage.of(self.db).work
        self.assertEqual(st.wip_exceptions([lot["id"]], work.WIP_EXCEPTION_MARK,
                                           work.ASSIGN_NOTES), [lot["id"]])
        done = st.assign_capped(lot["id"], "dev2", current=None,
                                note="%s par dev2, au repos" % backlog.LOT_TAKE_NOTE,
                                actor="ameesh", max_build=CAP)
        self.assertIsNotNone(done)
        # la prise est une attribution : l'exception de la précédente est morte
        self.assertEqual(st.wip_exceptions([lot["id"]], work.WIP_EXCEPTION_MARK,
                                           work.ASSIGN_NOTES), [])


class BalayageTest(_Base):
    def test_dry_run_puis_apply_idempotent(self):
        self.agent("dev1")
        lots = []
        for i in range(4):
            item = self.build(self.lot("occupé %d" % i, assignee="dev1",
                                       branch="lot/1359-%d" % i))
            self.aged(item["id"], 10 - i)   # lots[0] le moins récemment actif
            lots.append(item)
        out = wip.sweep(self.cfg, self.db, dry_run=True)
        self.assertEqual([r["item"]["id"] for r in out],
                         [lots[0]["id"], lots[1]["id"]])
        self.assertTrue(all(r["dry_run"] for r in out))
        self.assertEqual(self.builds_of("dev1"), 4)
        self.assertEqual(self.courriers(), [])
        out = wip.sweep(self.cfg, self.db)
        self.assertEqual([r["item"]["id"] for r in out],
                         [lots[0]["id"], lots[1]["id"]])
        self.assertEqual(self.builds_of("dev1"), 2)
        for item in (lots[0], lots[1]):
            row = work.get(self.db, item["id"])
            self.assertIsNone(row["assignee"])
            self.assertEqual(row["state"], "build")
            self.assertEqual(row["branch"], item["branch"])
        mails = self.courriers("dev1")
        self.assertEqual([m["work_item_id"] for m in mails],
                         [str(lots[0]["id"]), str(lots[1]["id"])])
        self.assertIn("WIP limit", mails[0]["body"])
        # idempotent : plus rien à rendre, plus de courrier
        self.assertEqual(wip.sweep(self.cfg, self.db), [])
        self.assertEqual(len(self.courriers("dev1")), 2)

    def test_l_humain_et_l_exception_pilote_sont_laisses(self):
        self.agent("dev1")
        self.agent("dev2")
        human = self.build(self.lot("à un humain", assignee="human:proprio"))
        lots = []
        for i in range(4):
            item = self.build(self.lot("occupé %d" % i, assignee="dev1"))
            self.aged(item["id"], 10 - i)
            lots.append(item)
        # sans pilote : refus, et la raison est obligatoire
        with self.assertRaises(work.WorkError):
            work.assign(self.db, lots[0]["id"], "dev1", actor="dev1", cfg=self.cfg,
                        wip_exception="je garde")
        with self.assertRaises(work.WorkError):
            work.assign(self.db, lots[0]["id"], "dev1", actor="human:proprio",
                        cfg=self.cfg, wip_exception="   ")
        # le pilote pose l'exception sur le plus ancien : il reste
        work.assign(self.db, lots[0]["id"], "dev1", actor="human:proprio", cfg=self.cfg,
                    wip_exception="reprise urgente")
        out = wip.sweep(self.cfg, self.db)
        self.assertEqual([r["item"]["id"] for r in out], [lots[1]["id"]])
        self.assertEqual(work.get(self.db, lots[0]["id"])["assignee"], "dev1")
        self.assertEqual(work.get(self.db, human["id"])["assignee"], "human:proprio")
        # une réassignation postérieure sans marqueur efface l'exception
        work.assign(self.db, lots[0]["id"], "dev2", actor="human:proprio", cfg=self.cfg)
        self.assertEqual(
            storage.of(self.db).work.wip_exceptions([lots[0]["id"]],
                                                    work.WIP_EXCEPTION_MARK,
                                                    work.ASSIGN_NOTES),
            [])

    def test_cli_wip_et_return(self):
        self.agent("dev1")
        for i in range(3):
            lot = self.build(self.lot("cli %d" % i, assignee="dev1"))
        proc = self.mesh("work", "wip")
        # #2451 : sans canon, le balayage refuse au lieu de supposer un plafond
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("build.max_in_build", proc.stderr)
        proc = self.mesh("work", "wip", env=self.canon_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("dry run: nothing written", proc.stdout)
        self.assertEqual(self.builds_of("dev1"), 3)
        proc = self.mesh("work", "return", str(lot["id"]), "--actor", "dev1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("returned to the team queue", proc.stdout)
        self.assertIsNone(work.get(self.db, lot["id"])["assignee"])

    def test_deux_balayages_interleaves_ne_rendent_que_le_surplus(self):
        """Deux balayages lisent les mêmes 4 lots avant que l'un n'écrive : le
        second ne rend rien de plus, chaque retour recomptant sous le verrou de
        l'agent."""
        self.agent("dev1")
        lots = []
        for i in range(4):
            item = self.build(self.lot("occupé %d" % i, assignee="dev1"))
            self.aged(item["id"], 10 - i)
            lots.append(item)
        other = self.connect()
        second: list = []
        crossed: list = []
        real_get = work.get

        def interleaved(db, item_id):
            item = real_get(db, item_id)
            if not crossed:
                crossed.append(True)
                # B a lu les mêmes 4 lots et rend son surplus avant que A
                # n'écrive : A ne doit pas en rendre deux de plus
                second.extend(wip.sweep(self.cfg, other, limit=10))
            return item

        try:
            with mock.patch.object(work, "get", interleaved):
                first = wip.sweep(self.cfg, self.db, limit=10)
        finally:
            other.close()
        self.assertEqual([r["item"]["id"] for r in second],
                         [lots[0]["id"], lots[1]["id"]])
        self.assertEqual(first, [])
        self.assertEqual(self.builds_of("dev1"), 2)
        self.assertEqual(len(self.courriers("dev1")), 2)

    def test_une_exception_posee_apres_la_lecture_protege_le_lot(self):
        """Le pilote pose l'exception entre la lecture du balayage et son
        écriture : le lot est relu sous verrou et n'est pas rendu."""
        self.agent("dev1")
        lots = []
        for i in range(4):
            item = self.build(self.lot("occupé %d" % i, assignee="dev1"))
            self.aged(item["id"], 10 - i)
            lots.append(item)
        real_get = work.get
        posed: list = []

        def interleaved(db, item_id):
            item = real_get(db, item_id)
            if not posed:
                posed.append(True)
                work.assign(self.db, lots[0]["id"], "dev1", actor="human:proprio",
                            cfg=self.cfg, wip_exception="reprise urgente")
            return item

        with mock.patch.object(work, "get", interleaved):
            out = wip.sweep(self.cfg, self.db, limit=10)
        self.assertEqual([r["item"]["id"] for r in out], [lots[1]["id"]])
        self.assertEqual(work.get(self.db, lots[0]["id"])["assignee"], "dev1")
        self.assertEqual(self.builds_of("dev1"), 3)
        st = storage.of(self.db).work
        self.assertEqual(st.wip_exceptions([lots[0]["id"]], work.WIP_EXCEPTION_MARK,
                                           work.ASSIGN_NOTES), [lots[0]["id"]])

    def test_la_lecture_du_balayage_est_dediee_aux_build(self):
        """Le balayage lit les lots en `build` par une requête dédiée : le
        plafond global des lots ouverts ne peut pas masquer un surplus."""
        self.agent("dev1")
        for i in range(3):
            self.aged(self.lot("intake %d" % i)["id"], 20 - i)
        builds = [self.build(self.lot("occupé %d" % i, assignee="dev1"))
                  for i in range(2)]
        st = storage.of(self.db)
        rows = st.operations.build_lots_activity(10)
        self.assertEqual([int(r["id"]) for r in rows], [b["id"] for b in builds])
        # la lecture globale, bornée, ne voit que les intake plus anciens
        glob = [int(r["id"]) for r in st.operations.open_lots_activity(2)]
        self.assertNotIn(builds[0]["id"], glob)


class CompteursTest(_Base):
    def test_list_montre_l_en_cours_et_le_depassement(self):
        self.agent("dev1")
        self.agent("dev2")
        for i in range(3):
            self.build(self.lot("occupé %d" % i, assignee="dev1"))
        self.build(self.lot("un seul", assignee="dev2"))
        # #2451 : sans canon, la vue montre l'en-cours sans juger le dépassement
        proc = self.mesh("list")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("§4 cap unknown", proc.stdout)
        self.assertNotIn("3!", proc.stdout)
        proc = self.mesh("list", env=self.canon_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = proc.stdout.splitlines()
        header = lines[0]
        self.assertIn("BUILD", header)
        col = header.index("BUILD")
        row = next(line for line in lines if line.startswith("dev1"))
        self.assertEqual(row[col:col + 5].strip(), "3!")
        row = next(line for line in lines if line.startswith("dev2"))
        self.assertEqual(row[col:col + 5].strip(), "1")
        self.assertIn("wip      : 1 agent(s) above the §4 cap (2 lots in build): dev1",
                      proc.stdout)

    def test_progress_porte_l_en_cours_tous_projets(self):
        self.agent("dev1", team="ameesh")
        self.agent("dev2", team="nexlink")
        for i in range(3):
            self.build(self.lot("ameesh %d" % i, app="ameesh", assignee="dev1"))
        self.build(self.lot("nexlink", app="nexlink", assignee="dev1", branch="lot/nx"))
        snap = progress.snapshot(self.db, self.cfg)
        agents = {a["name"]: a for a in snap["agents"]}
        # tous projets confondus : le plafond est global, pas par équipe
        self.assertEqual(agents["dev1"]["builds"], 4)
        self.assertEqual(agents["dev2"]["builds"], 0)
        text = progress.format_text(snap, width=100)
        self.assertIn("4 lots in build (above the §4 cap of 2)", text)


class BranchementNotifyTest(_Base):
    """§4 : le balayage est branché sur notify, coupé par défaut ; sans
    `--wip-apply` (ou sous le `--dry-run` global) il ne rend rien."""

    def test_le_balayage_notify_est_coupe_par_defaut(self):
        parser = notify.build_parser()
        self.assertIsNone(notify.wip_settings(parser.parse_args([])))
        self.assertEqual(notify.wip_settings(parser.parse_args(["--wip-max-returns", "3"])),
                         {"limit": 3, "apply": False})
        self.assertEqual(
            notify.wip_settings(parser.parse_args(["--wip-max-returns", "3", "--wip-apply"])),
            {"limit": 3, "apply": True})

    def _trois_lots(self):
        self.agent("dev1")
        lots = []
        for i in range(3):
            item = self.build(self.lot("occupé %d" % i, assignee="dev1"))
            self.aged(item["id"], 10 - i)   # lots[0] le moins récemment actif
            lots.append(item)
        return lots

    def test_notify_sans_apply_ne_rend_rien_et_releve(self):
        lots = self._trois_lots()
        records = []
        notifier = notify.Notifier(self.cfg, notify.parse_config({}), log=lambda t: None,
                                   emit=records.append, clock=time.time,
                                   wip={"limit": 10, "apply": False})
        notifier.run_pass(self.db, thresholds={})
        self.assertEqual(self.builds_of("dev1"), 3)
        self.assertEqual(self.courriers(), [])
        releves = [r for r in records if r["event"] == "wip_return"]
        self.assertEqual([r["lot"] for r in releves], [lots[0]["id"]])
        self.assertTrue(all(r["delivery"] == "a_blanc" for r in releves))
        self.assertIn("WIP return", notify._format(releves[0]))

    def test_notify_apply_rend_le_surplus_et_previ_ent_l_agent(self):
        lots = self._trois_lots()
        records = []
        notifier = notify.Notifier(self.cfg, notify.parse_config({}), log=lambda t: None,
                                   emit=records.append, clock=time.time,
                                   wip={"limit": 10, "apply": True})
        notifier.run_pass(self.db, thresholds={})
        self.assertEqual(self.builds_of("dev1"), 2)
        self.assertIsNone(work.get(self.db, lots[0]["id"])["assignee"])
        self.assertEqual(work.get(self.db, lots[1]["id"])["assignee"], "dev1")
        releves = [r for r in records if r["event"] == "wip_return"]
        self.assertEqual([r["lot"] for r in releves], [lots[0]["id"]])
        self.assertEqual(releves[0]["delivery"], "rendu")
        mails = self.courriers("dev1")
        self.assertEqual([m["work_item_id"] for m in mails], [str(lots[0]["id"])])
        # #5025 N3 : ce courrier EST l'avis de réassignation à la file ; la
        # clé `reassigned` le fait remettre au point sûr d'un tour en cours.
        payload = self.db.query(
            "SELECT payload FROM agent_mailbox WHERE recipient = 'dev1'"
            " ORDER BY id DESC LIMIT 1")[0]["payload"] or {}
        self.assertIn("reassigned", payload)
        self.assertTrue(mail.mid_turn({"payload": payload}))

    def test_le_dry_run_global_force_le_dry_run(self):
        self._trois_lots()
        notifier = notify.Notifier(self.cfg, notify.parse_config({}), log=lambda t: None,
                                   emit=lambda r: None, clock=time.time, dry_run=True,
                                   wip={"limit": 10, "apply": True})
        notifier.run_pass(self.db, thresholds={})
        self.assertEqual(self.builds_of("dev1"), 3)
        self.assertEqual(self.courriers(), [])
