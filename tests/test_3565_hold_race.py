# SPDX-License-Identifier: AGPL-3.0-only
"""#3565 (panier de #3556) : la course hold/assign de la passe de prise, et la
voie de proposition de relecture alignée sur le filtre des holds.

N1 : les holds étaient lus une fois avant la boucle d'assignation ; un hold
pilote posé entre cette lecture et `work.assign` était manqué. La prise relit
maintenant les holds dans sa transaction, après le verrou de ligne du lot, et
annule l'écriture s'il en apparaît un.

N2 : la proposition de relecture/correction d'un lot `qa` suit la même lecture
que la roadmap : un lot sous hold pilote vivant n'est pas proposé, et sa levée
ou un réveil échu postérieur à la pose le rend proposable.
"""
from __future__ import annotations

import json
import threading
from unittest.mock import patch

from ameesh import backlog, db as db_mod, storage, work

from .test_1677_prise_empilee import SHA, _Prise
from .test_l165_prise_lots import (SELECTION_DEADLINE_S, _Lots,
                                   _attendre_selection)


class HoldRaceTest(_Lots):
    def note_hold(self, lot, text="hold: spec attendue", actor="human:proprio"):
        storage.of(self.db).work.note(lot["id"], lot.get("state") or "intake",
                                      text, actor)

    def test_hold_placed_after_selection_refuses_take(self):
        """N1 : le hold arrive entre la lecture groupée et `work.assign`."""
        self.agent("claude1", team="nexlink")
        lot = self.lot("spec attendue", priority=1)
        check = work.check_assignee
        placed = {"done": False}

        def place_hold(*args, **kwargs):
            if not placed["done"]:
                self.note_hold(lot)
                placed["done"] = True
            return check(*args, **kwargs)

        with patch.object(work, "check_assignee", side_effect=place_hold):
            out = self.take()
        self.assertEqual(out, [])
        self.assertIsNone(work.get(self.db, lot["id"])["assignee"])
        self.assertFalse(any(m["work_item_id"] == str(lot["id"])
                             for m in self.courriers()))

        # La levée rend le lot prenable au passage suivant.
        self.note_hold(lot, "unhold: spec recue")
        self.assertEqual([t["item"]["id"] for t in self.take()], [lot["id"]])

    def test_hold_committed_during_take_transaction_refuses_assignment(self):
        """N1 : le hold est posé dans une transaction concurrente pendant que
        la prise sélectionne ; le verrou de ligne ordonne les deux écritures,
        et la relecture dans la transaction de prise voit le hold.

        #8250 : le témoin n'attend plus un délai fixe de 15 s, qui dépendait de
        la charge du runner. La sélection est attendue comme dans #8139, puis
        le commit du hold est ordonné par l'attente de verrou elle-même : la
        transaction reste ouverte jusqu'à ce que la prise demande la ligne du
        lot (`pg_locks`, un verrou `transactionid` non accordé sur son xid).
        Elle commite alors le hold pendant que la prise attend, jamais avant
        ni après un tour de CPU."""
        self.agent("claude1", team="nexlink")
        lot = self.lot("spec attendue", priority=1)
        selected = threading.Event()
        result, errors = [], []
        check = work.check_assignee

        def selected_candidate(*args, **kwargs):
            selected.set()
            return check(*args, **kwargs)

        def take_concurrently():
            connection = None
            try:
                connection = db_mod.connect(self.cfg)
                with patch.object(work, "check_assignee",
                                  side_effect=selected_candidate):
                    result.extend(backlog.auto_take(
                        self.cfg, connection, now=self.now, paid=("deepseek",),
                        budget_check=lambda _: "", orchestras=[]))
            except Exception as exc:      # noqa: BLE001 — remonté au témoin
                errors.append(exc)
            finally:
                if connection is not None:
                    connection.close()

        worker = threading.Thread(target=take_concurrently, daemon=True)
        try:
            with self.db.transaction() as tx:
                storage.of(tx).work.note(lot["id"], "intake",
                                         "hold: spec attendue", "human:proprio")
                xid = int(tx.query("SELECT txid_current() AS xid")[0]["xid"])
                pid = tx.query("SELECT pg_backend_pid() AS pid")[0]["pid"]
                worker.start()
                _attendre_selection(self, tx, pid, selected, worker, errors)

                def awaiting_lot_lock():
                    # Le rendez-vous : la prise attend le verrou de la ligne
                    # que cette transaction tient (un `transactionid` non
                    # accordé sur son xid). On commite alors le hold pendant
                    # que la prise attend.
                    if tx.query(
                            "SELECT 1 FROM pg_locks"
                            " WHERE locktype = 'transactionid' AND NOT granted"
                            "   AND transactionid::text = %s LIMIT 1",
                            (str(xid),)):
                        return True
                    if not worker.is_alive():
                        self.fail("la prise concurrente a terminé sans demander "
                                  "le verrou du lot ; erreurs : %r" % (errors,))
                    return False

                # Borne de secours seulement (60 s, ×5 sous psql comme le
                # banc) : elle ne fait pas attendre un fil qui n'aurait pas
                # encore eu son tour, elle échoue si le verrou n'est jamais
                # demandé.
                self.wait_for(
                    awaiting_lot_lock, timeout=60,
                    msg=lambda: "la prise concurrente n'a pas atteint le verrou "
                                "du lot ; erreurs : %r" % (errors,))
        finally:
            if worker.ident is not None:
                worker.join(SELECTION_DEADLINE_S)
        self.assertFalse(worker.is_alive(), "la prise concurrente n'a pas terminé")
        self.assertEqual(errors, [])
        self.assertEqual(result, [])
        self.assertIsNone(work.get(self.db, lot["id"])["assignee"])

    def test_ordinary_hold_placed_after_selection_does_not_block(self):
        """La relecture ne refuse pas tout `hold:` : seul un hold de pilote
        retient la prise (le hold ordinaire du réveil reste permis)."""
        self.agent("claude1", team="nexlink")
        lot = self.lot("pret", priority=1)
        check = work.check_assignee
        placed = {"done": False}

        def place_hold(*args, **kwargs):
            if not placed["done"]:
                self.note_hold(lot, "hold: note auteur", "claude1")
                placed["done"] = True
            return check(*args, **kwargs)

        with patch.object(work, "check_assignee", side_effect=place_hold):
            out = self.take()
        self.assertEqual([t["item"]["id"] for t in out], [lot["id"]])
        self.assertEqual(work.get(self.db, lot["id"])["assignee"], "claude1")


class _Proposal(_Prise):
    """Les petits appuis de `test_1677` (auteur, lot gelé en qa, courriers),
    sans hériter des témoins de cette classe."""

    def auteur(self, name="auteur", harness="claude", team="nexlink"):
        self.agent(name, harness=harness, team=team)

    def qa(self, title, *, branch="lot/qa", sha=SHA, author_family="claude"):
        item = self.lot(title, assignee="auteur", app="nexlink")
        self.db.execute("UPDATE work_items SET state = 'qa', branch = %s"
                        " WHERE id = %s", (branch, item["id"]))
        work.milestone(self.db, item["id"], "frozen", sha=sha, actor="test")
        self.marque(item["id"], [{"name": "auteur", "family": author_family}],
                    sha=sha)
        return item

    def mails(self, name):
        rows = self.db.query("SELECT body, payload FROM agent_mailbox"
                             " WHERE recipient = %s ORDER BY id", (name,))
        out = []
        for row in rows:
            payload = row["payload"]
            out.append({"body": row["body"],
                        "payload": json.loads(payload) if isinstance(payload, str)
                        else payload})
        return out


class ReviewProposalHoldTest(_Proposal):
    def note_hold(self, lot, text="hold: spec attendue"):
        storage.of(self.db).work.note(lot["id"], "qa", text, "human:proprio")

    def test_qa_lot_under_pilot_hold_is_not_proposed(self):
        """N2 : un lot `qa` sous hold pilote n'est pas proposé à un relecteur."""
        self.auteur()
        lot = self.qa("relire", branch="lot/a-relire")
        self.agent("codex1", harness="codex", team="nexlink")
        self.note_hold(lot)

        self.assertEqual(self.take(), [])
        self.assertEqual(self.mails("codex1"), [])

        # La levée rend la voie de proposition.
        self.note_hold(lot, "unhold: spec recue")
        self.assertEqual([(t["kind"], t["item"]["id"]) for t in self.take()],
                         [("review", lot["id"])])

    def test_due_wake_after_hold_releases_review_proposal(self):
        """N2 : le réveil échu postérieur à la pose libère aussi la
        proposition, comme la prise (#3556)."""
        self.auteur()
        lot = self.qa("relire", branch="lot/a-relire")
        self.agent("codex1", harness="codex", team="nexlink")
        self.note_hold(lot)
        work.schedule_wake(self.db, lot["id"], self.now + 60,
                           text="rendez-vous", actor="test")

        self.assertEqual(self.take(), [])
        self.assertEqual(
            [(t["kind"], t["item"]["id"]) for t in self.take(now=self.now + 120)],
            [("review", lot["id"])])
