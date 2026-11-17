# SPDX-License-Identifier: AGPL-3.0-only
"""#7631 (point 1) : reçu par clé fichier sous accord journalisé.

La façade `ed25519` reste refusée pour une autorité humaine (spec §8.1), sauf
si le canon nomme, sur CET authentificateur, l'opération approuvée et que le
reçu cite l'entrée du journal de conversation (agent, seq) où l'humain a
donné son accord. Sans `accord_operations` au canon : rien ne change.
"""
from __future__ import annotations

import os
import time

from ameesh import actions, receipts, storage
from ameesh.actions import ActionError
from ameesh.connectors.shell_noop import ShellNoopConnector

from .support import PgTestCase, apply_authenticators
from .webauthn_soft import ORIGIN, RP_ID, SoftEd25519, SoftWebAuthn, make_request

ALICE = "human:alice"
AVATAR = "bureau"
POLICY = receipts.Policy(rp_id=RP_ID, origins=(ORIGIN,))
COMMIT = "a" * 40


def member(title: str, *entries: dict) -> dict:
    return {"title": title, "canon_ref": "members/%s.md@%s" % (title, COMMIT),
            "authenticators": list(entries)}


class AccordBase(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute(
            "TRUNCATE actions, action_attempts, action_events, authenticators, "
            "standing_approvals, standing_reservations, mesh_consumed_nonces "
            "RESTART IDENTITY CASCADE")
        self.db.execute("DELETE FROM conversation_log WHERE agent = %s", (AVATAR,))
        self.db.execute("DELETE FROM conversation_heads WHERE agent = %s", (AVATAR,))
        self.key = SoftEd25519()
        self.passkey = SoftWebAuthn("ES256")
        self.sync(["prod-deploy", "decision-sign"])

    def sync(self, operations) -> dict:
        entry = self.key.entry()
        if operations is not None:
            entry["accord_operations"] = operations
        return apply_authenticators(self.db, [member("alice", entry, self.passkey.entry())])

    def go(self, *, author: str = ALICE, kind: str = "human_message", age: float = 0) -> dict:
        entry = storage.of(self.db).conversations.append(
            agent=AVATAR, session=None, turn=None, kind=kind, author=author,
            text="go pour la mise en production", truncated=False, meta={},
            event_id=None, project="ameesh", lot=None, thread_text=None)
        if age:
            self.db.execute("UPDATE conversation_log SET created_at = now() - make_interval("
                            "secs => %s) WHERE agent = %s AND seq = %s",
                            (float(age), AVATAR, entry["seq"]))
        return {"agent": AVATAR, "seq": int(entry["seq"])}

    def receipt(self, accord=None, **kwargs) -> dict:
        request = make_request(ALICE, **kwargs)
        if accord is not None:
            request["accord"] = accord
        return self.key.receipt(request)

    def verify(self, receipt, operation="prod-deploy", **kwargs):
        return receipts.verify_receipt(self.db, receipt, POLICY,
                                       expected_operation=operation, **kwargs)

    def assertRefused(self, verdict, code: str):
        self.assertFalse(verdict.ok, "accepté à tort : %s" % verdict.reason)
        self.assertEqual(verdict.code, code, verdict.reason)


class FormeTest(AccordBase):
    def test_accord_signe_et_borne(self):
        request = make_request(ALICE)
        request["accord"] = {"agent": AVATAR, "seq": 3}
        self.assertEqual(receipts.check_request(request), "action")
        for bad in ({"agent": AVATAR}, {"agent": AVATAR, "seq": 0},
                    {"agent": AVATAR, "seq": "3"}, {"agent": "a b", "seq": 1},
                    {"agent": AVATAR, "seq": 1, "extra": 1}, [AVATAR, 1]):
            with self.subTest(accord=bad):
                with self.assertRaises(receipts.ReceiptError):
                    receipts.check_request(dict(request, accord=bad))
        # l'accord entre dans le challenge : le retirer ou le changer casse la signature
        receipt = self.key.receipt(request)
        receipt["request"] = dict(request, accord={"agent": AVATAR, "seq": 4})
        self.assertNotEqual(receipts.challenge(receipt["request"]),
                            receipts.challenge(request))

    def test_accord_hors_action_et_grant_refuse(self):
        login = {"v": 1, "approver": ALICE, "decision": "view",
                 "summary_digest": "sha256:" + "2" * 64, "requested_by": "agent:x",
                 "nonce": "AAAAAAAAAAAAAAAAAAAAAA", "iat": 1790000000, "exp": 1790000600,
                 "login_id": "lgn_" + "0" * 26, "host": "h", "provider": "p",
                 "account": "", "scope": "", "accord": {"agent": AVATAR, "seq": 1}}
        with self.assertRaises(receipts.ReceiptError):
            receipts.check_request(login)

    def test_operations_seulement_sur_cle_fichier(self):
        entry = self.passkey.entry()
        entry["accord_operations"] = ["prod-deploy"]
        result = apply_authenticators(self.db, [member("alice", self.key.entry(), entry)])
        self.assertTrue(any("only for an ed25519 file key" in e for e in result["errors"]),
                        result["errors"])
        self.assertEqual(result["frozen"], ["human:alice"])


class VerificationTest(AccordBase):
    def test_accepte_sous_accord_pour_une_operation_nommee(self):
        accord = self.go()
        verdict = self.verify(self.receipt(accord))
        self.assertTrue(verdict.ok, verdict.reason)
        self.assertEqual(verdict.accord, accord)
        self.assertTrue(verdict.file_key)
        self.assertEqual(verdict.facade, "ed25519")

    def test_sans_accord_refuse_comme_avant(self):
        self.assertRefused(self.verify(self.receipt()), receipts.FACADE)

    def test_operation_non_nommee_refusee(self):
        self.assertRefused(self.verify(self.receipt(self.go()), operation="spend"),
                           receipts.ACCORD_OPERATION)

    def test_operation_absente_refusee(self):
        self.assertRefused(self.verify(self.receipt(self.go()), operation=None),
                           receipts.UNBOUND)

    def test_canon_sans_politique_rien_ne_change(self):
        """TPK : aucun `accord_operations` → la clé fichier ne vaut jamais."""
        self.assertEqual(self.sync(None)["errors"], [])
        self.assertRefused(self.verify(self.receipt(self.go())), receipts.ACCORD_OPERATION)

    def test_retrait_au_canon_applique(self):
        accord = self.go()
        self.assertTrue(self.verify(self.receipt(accord)).ok)
        result = self.sync(["decision-sign"])
        self.assertEqual(len(result["updated"]), 1, result)
        self.assertRefused(self.verify(self.receipt(accord)), receipts.ACCORD_OPERATION)

    def test_entree_de_journal_verifiee(self):
        cases = {
            "absente": {"agent": AVATAR, "seq": 999},
            "autre humain": self.go(author="human:bob"),
            "pas un message humain": self.go(kind="turn_end"),
            "trop ancienne": self.go(age=receipts.DEFAULT_MAX_TTL + 60),
        }
        for label, accord in cases.items():
            with self.subTest(label):
                self.assertRefused(self.verify(self.receipt(accord)), receipts.ACCORD)
        # écrite après la signature
        accord = self.go()
        late = self.receipt(accord, iat=int(time.time()) - 600, ttl=1200)
        self.assertRefused(self.verify(late), receipts.ACCORD)

    def test_grant_borne_aux_operations_nommees(self):
        until = int(time.time()) + 3600
        standing = {"connector": "shell-noop", "operations": ["prod-deploy"],
                    "class": "irreversible", "max_amount": 0, "currency": None,
                    "until": until}
        accord = self.go()
        ok = self.receipt(accord, standing=standing)
        self.assertTrue(receipts.verify_receipt(self.db, ok, POLICY, kind="standing").ok)
        wide = self.receipt(accord, standing=dict(standing,
                                                  operations=["prod-deploy", "spend"]))
        self.assertRefused(receipts.verify_receipt(self.db, wide, POLICY, kind="standing"),
                           receipts.ACCORD_OPERATION)


class PorteTest(AccordBase):
    def setUp(self) -> None:
        super().setUp()
        self.noop = ShellNoopConnector(os.path.join(self.tmp, "noop"), timeout=10.0)

    def propose(self, operation: str) -> dict:
        return actions.propose(self.db, self.noop, operation=operation, project="demo",
                               proposed_by="agent:bureau", target="prod:ameesh",
                               args={"version": "1.58.0"}, action_class="irreversible")

    def test_mise_en_production_approuvee_sous_accord(self):
        action = self.propose("prod-deploy")
        accord = self.go()
        request = actions.approval_request(action, ALICE, accord=accord)
        bound = actions.approve(self.db, action["action_id"], self.key.receipt(request),
                                policy=POLICY, by="bureau")
        self.assertEqual(bound["state"], "approved")
        self.assertIn("accord %s#%d" % (AVATAR, accord["seq"]),
                      " ".join(e.get("note") or "" for e in actions.events(
                          self.db, action["action_id"])))

    def test_porte_refuse_une_operation_non_nommee(self):
        action = self.propose("spend")
        request = actions.approval_request(action, ALICE, accord=self.go())
        with self.assertRaises(ActionError) as caught:
            actions.approve(self.db, action["action_id"], self.key.receipt(request),
                            policy=POLICY, by="bureau")
        self.assertEqual(caught.exception.code, receipts.ACCORD_OPERATION)
        self.assertEqual(actions.get(self.db, action["action_id"])["state"], "proposed")


class GrantTest(AccordBase):
    """Point 3 : un « go » permanent devient un grant borné, signé par clé
    fichier sous accord, avec le cadre de charte qu'il inscrit."""

    def standing(self, **extra) -> dict:
        return dict({"connector": "journal", "operations": ["decision-sign"],
                     "class": "irreversible", "max_amount": 0, "currency": None,
                     "until": int(time.time()) + 7 * 86400}, **extra)

    def test_cadre_de_charte_signe_et_borne(self):
        request = make_request(ALICE, standing=self.standing(
            charter={"frame": "sign-decisions", "version": "1"}))
        self.assertEqual(receipts.check_request(request), "standing")
        for bad in ({"frame": "sign-decisions"}, {"frame": "a b", "version": "1"},
                    {"frame": "f", "version": 1}, "sign-decisions"):
            with self.subTest(charter=bad):
                with self.assertRaises(receipts.ReceiptError):
                    receipts.check_request(make_request(
                        ALICE, standing=self.standing(charter=bad)))

    def test_inscrit_puis_liste(self):
        accord = self.go()
        receipt = self.receipt(accord, standing=self.standing(
            charter={"frame": "sign-decisions", "version": "1"}))
        verdict, grant_id = receipts.register_standing(self.db, receipt, POLICY,
                                                       registered_by="bureau")
        self.assertTrue(verdict.ok, verdict.reason)
        rows = receipts.list_standing(self.db)
        self.assertEqual([r["id"] for r in rows], [grant_id])
        row = rows[0]
        self.assertEqual(row["operations"], ["decision-sign"])
        self.assertEqual(row["charter"], {"frame": "sign-decisions", "version": "1"})
        self.assertEqual(row["accord"], accord)
        self.assertEqual(row["facade"], "ed25519")
        receipts.revoke_standing(self.db, grant_id, by="human:alice")
        self.assertEqual(receipts.list_standing(self.db), [])
        self.assertEqual(len(receipts.list_standing(self.db, include_inactive=True)), 1)

    def test_grant_hors_operations_nommees_refuse(self):
        receipt = self.receipt(self.go(), standing=self.standing(operations=["spend"]))
        verdict, grant_id = receipts.register_standing(self.db, receipt, POLICY,
                                                       registered_by="bureau")
        self.assertIsNone(grant_id)
        self.assertEqual(verdict.code, receipts.ACCORD_OPERATION, verdict.reason)


class JournalTest(AccordBase):
    """Point 5 : la vue des gestes réservés d'une période."""

    def setUp(self) -> None:
        super().setUp()
        self.noop = ShellNoopConnector(os.path.join(self.tmp, "noop"), timeout=10.0)

    def test_mise_en_production_inscrite(self):
        from ameesh import reserved_gestures
        action = actions.propose(self.db, self.noop, operation="prod-deploy", project="demo",
                                 proposed_by="agent:bureau", target="prod:ameesh",
                                 args={"version": "1.58.0"}, action_class="irreversible")
        other = actions.propose(self.db, self.noop, operation="send", project="demo",
                                proposed_by="agent:bureau", target="client:1", args={})
        accord = self.go()
        request = actions.approval_request(action, ALICE, accord=accord)
        actions.approve(self.db, action["action_id"], self.key.receipt(request),
                        policy=POLICY, by="bureau")
        actions.execute(self.db, action["action_id"], self.noop, policy=POLICY, by="bureau")
        now = time.time()
        view = reserved_gestures.journal(self.db, since_ts=now - 3600, until_ts=now + 1)
        ids = [g.get("action_id") for g in view["gestures"] if g["kind"] == "action"]
        self.assertEqual(ids, [action["action_id"]])
        self.assertNotIn(other["action_id"], ids)
        g = [g for g in view["gestures"] if g["kind"] == "action"][0]
        self.assertEqual((g["gesture"], g["proposed_by"], g["approver"], g["accord"],
                          g["state"]),
                         ("production", "agent:bureau", ALICE, accord, "confirmed"))
        self.assertTrue(g["receipt"])
        # hors de la période : rien
        self.assertEqual(reserved_gestures.journal(self.db, since_ts=now - 7200,
                                                   until_ts=now - 3600)["gestures"], [])
