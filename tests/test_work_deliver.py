# SPDX-License-Identifier: AGPL-3.0-only
"""L99 T2 (#4350) — `work.deliver(connector, evidence)`, le verbe unique des
livraisons.

Trois connecteurs : `git-merge` (module `dev`, lu dans le registre), `document`
(livré hors cible, `close --delivered --sha`) et `done` (livré sans fusion,
`close --done`). `merged()`, `close --delivered/--done` et la fusion confirmée
par la porte (`actions._close_lot_on_merge`) passent par lui ; le journal du lot
nomme le connecteur ; rien ne change au stockage.
"""
from __future__ import annotations

import unittest
from unittest import mock

from ameesh import actions, work
from ameesh.modules import dev, registry

from .support import PgTestCase

SHA = "0123456789abcdef0123456789abcdef01234567"


class ConnectorRegistryTest(unittest.TestCase):
    def test_git_merge_vient_du_module_dev(self):
        found = registry.delivery_connectors()
        self.assertEqual(found, {"git-merge": ("dev", dev.deliver_git_merge)})
        self.assertEqual(registry.merge_connector(), "git-merge")

    def test_dev_est_un_module_outillage_sans_type_de_lot(self):
        """L101 T4: `dev` entre dans le registre, mais sans cycle de vie."""
        found = registry.discover()
        self.assertIn("dev", found.names())
        module = found.get("dev")
        self.assertEqual(module.storage, ())
        self.assertNotIn("lifecycle", module.capabilities)
        self.assertIn("workspaces", module.capabilities)

    def test_un_nom_pris_deux_fois_est_refuse(self):
        fake = mock.Mock(DELIVERY_CONNECTORS={"done": lambda *a, **k: None})
        with mock.patch.object(registry, "_connector_providers",
                               return_value={"x": fake}):
            with self.assertRaisesRegex(ValueError, "duplicate delivery connector"):
                registry.delivery_connectors()

    def test_connecteur_inconnu(self):
        with self.assertRaisesRegex(work.WorkError, "unknown delivery connector 'ftp'"):
            work.deliver(None, 1, connector="ftp", evidence={})

    def test_preuve_qui_n_est_pas_un_dictionnaire(self):
        with self.assertRaisesRegex(work.WorkError, "must be a mapping"):
            work.deliver(None, 1, connector="done", evidence=["x"])


class DeliverTest(PgTestCase):
    def notes(self, item_id):
        return [e["note"] or "" for e in work.events(self.db, item_id)]

    def test_document_ferme_livre_et_note_le_connecteur(self):
        lot = work.add(self.db, title="un rapport", app="ameesh")
        row = work.deliver(self.db, lot["id"], connector="document",
                           evidence={"sha": SHA}, actor="t", note="relu")
        self.assertEqual((row["state"], row["close_reason"]), ("closed", "delivered"))
        [delivered] = [m for m in work.milestones(self.db, lot["id"])
                       if m["kind"] == "delivered"]
        self.assertEqual(delivered["sha"], SHA)
        self.assertIn("delivered (head %s) — relu [via document]" % SHA[:12],
                      self.notes(lot["id"]))

    def test_close_delivered_passe_par_deliver(self):
        lot = work.add(self.db, title="un rapport", app="ameesh")
        with mock.patch.object(work, "deliver", wraps=work.deliver) as spy:
            work.close(self.db, lot["id"], delivered=True, sha=SHA, actor="t")
        spy.assert_called_once()
        self.assertEqual(spy.call_args.kwargs["connector"], "document")

    def test_document_exige_la_tete_relue(self):
        lot = work.add(self.db, title="un rapport", app="ameesh")
        with self.assertRaisesRegex(work.WorkError, "the reviewed head"):
            work.close(self.db, lot["id"], delivered=True, sha="zz", actor="t")
        with self.assertRaisesRegex(work.WorkError, "unknown evidence branch"):
            work.deliver(self.db, lot["id"], connector="document",
                         evidence={"sha": SHA, "branch": "x"})
        self.assertEqual(work.get(self.db, lot["id"])["state"], "intake")

    def test_done_ferme_et_note_le_connecteur(self):
        lot = work.add(self.db, title="une analyse", app="ameesh")
        with mock.patch.object(work, "deliver", wraps=work.deliver) as spy:
            row = work.close(self.db, lot["id"], done=True, actor="t")
        self.assertEqual(spy.call_args.kwargs["connector"], "done")
        self.assertEqual((row["state"], row["close_reason"]), ("closed", "done"))
        self.assertIn("done [via done]", self.notes(lot["id"]))
        other = work.add(self.db, title="une autre", app="ameesh")
        with self.assertRaisesRegex(work.WorkError, "takes no evidence"):
            work.deliver(self.db, other["id"], connector="done", evidence={"sha": SHA})

    def test_abandon_ne_passe_pas_par_deliver(self):
        lot = work.add(self.db, title="abandonné", app="ameesh")
        with mock.patch.object(work, "deliver") as spy:
            row = work.close(self.db, lot["id"], abandoned=True, actor="t")
        spy.assert_not_called()
        self.assertEqual(row["close_reason"], "abandoned")
        self.assertNotIn("[via", " ".join(self.notes(lot["id"])))

    def test_merged_passe_par_deliver_git_merge(self):
        lot = work.add(self.db, title="fusion locale", app="ameesh")
        with mock.patch.object(work, "record_merge",
                               return_value={"result": "merged"}) as record:
            out = work.merged(self.db, lot["id"], sha=SHA, actor="t", note="n")
        self.assertEqual(out, {"result": "merged"})
        record.assert_called_once()
        self.assertEqual(record.call_args.kwargs["sha"], SHA)
        self.assertEqual(record.call_args.kwargs["note"], "n")
        self.assertIsNone(record.call_args.kwargs["pr"])

    def test_porte_confirmee_ferme_par_git_merge(self):
        lot = work.add(self.db, title="fusion par la porte", app="ameesh")
        action = {"action_id": "act_1", "connector": "git-merge", "state": "confirmed",
                  "work_item": int(lot["id"]), "external_ref": SHA,
                  "auth_approver": "human:alice", "target": "o/r#7"}
        with mock.patch.object(actions, "get", return_value=action):
            with mock.patch.object(work, "deliver", wraps=work.deliver) as spy:
                actions._close_lot_on_merge(self.db, "act_1", "ameesh:porte")
        self.assertEqual(spy.call_args.kwargs,
                         {"connector": "git-merge", "evidence": {"action": "act_1"},
                          "actor": "ameesh:porte"})
        self.assertEqual(work.get(self.db, lot["id"])["state"], "delivered")
        self.assertTrue(any(n.startswith("merge seen by the gate (action act_1)")
                            and n.endswith("[via git-merge]")
                            for n in self.notes(lot["id"])), self.notes(lot["id"]))

    def test_git_merge_refuse_une_action_non_confirmee(self):
        lot = work.add(self.db, title="fusion", app="ameesh")
        action = {"connector": "git-merge", "state": "launched",
                  "work_item": int(lot["id"])}
        with mock.patch.object(actions, "get", return_value=action):
            with self.assertRaisesRegex(work.WorkError, "not a confirmed merge"):
                work.deliver(self.db, lot["id"], connector="git-merge",
                             evidence={"action": "act_2"})
        with self.assertRaisesRegex(work.WorkError, "unknown evidence"):
            work.deliver(self.db, lot["id"], connector="git-merge",
                         evidence={"sha": SHA, "head": SHA})
        self.assertEqual(work.get(self.db, lot["id"])["state"], "intake")


if __name__ == "__main__":
    unittest.main()
