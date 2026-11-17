# SPDX-License-Identifier: AGPL-3.0-only
"""#7631 (point 4) : une décision signée par l'avatar se distingue en base
d'une décision signée par l'owner ; l'avatar signe sur un accord vérifié."""
from __future__ import annotations

import os
import time
from unittest import mock

from ameesh import authority, decisions, signing, storage
from ameesh.decisions import DecisionError

from .test_l124_decisions import ALICE, _Base

AVATAR = "bureau"


class AvatarDecisionTest(_Base):
    def setUp(self):
        super().setUp()
        self.db.execute("DELETE FROM conversation_log WHERE agent = %s", (AVATAR,))
        self.db.execute("DELETE FROM conversation_heads WHERE agent = %s", (AVATAR,))
        self.private, public, _fp = signing.write_keypair(
            os.path.join(self.tmp, "keys-alice"), "alice")
        authority.register_human_key(self.db, ALICE, signing.read_public(public),
                                     role="owner")

    def go(self, author=ALICE) -> dict:
        entry = storage.of(self.db).conversations.append(
            agent=AVATAR, session=None, turn=None, kind="human_message", author=author,
            text="go", truncated=False, meta={}, event_id=None, project="ameesh", lot=None,
            thread_text=None)
        return {"agent": AVATAR, "seq": int(entry["seq"])}

    def avatar(self, accord) -> dict:
        return {"by": decisions.SIGNED_BY_AVATAR, "agent": AVATAR, "accord": accord}

    def meta(self, result) -> dict:
        approval = result["decision"]["answer"]["proof"]["approval_id"]
        return self.db.query("SELECT meta FROM mesh_approvals WHERE id = %s",
                             (approval,))[0]["meta"]

    def test_owner_et_avatar_se_distinguent_en_base(self):
        owner = self.answer(self.ask(needs_signature=True)["id"], "a", key_path=self.private)
        self.assertEqual(owner["decision"]["answer"]["signer"], {"by": "owner"})
        self.assertEqual(self.meta(owner)["signed_by"], "owner")
        self.assertTrue(decisions.verify_proof(self.db, owner["decision"])[0])

        accord = self.go()
        view = self.ask(needs_signature=True)
        signed = self.answer(view["id"], "a", key_path=self.private,
                             signer=self.avatar(accord))
        answer = signed["decision"]["answer"]
        self.assertEqual(answer["signer"], self.avatar(accord))
        self.assertEqual(answer["by"], ALICE)
        meta = self.meta(signed)
        self.assertEqual((meta["signed_by"], meta["agent"], meta["accord"]),
                         ("avatar", AVATAR, accord))
        self.assertTrue(decisions.verify_proof(self.db, signed["decision"])[0])
        # le signataire est dans l'empreinte signée : le changer casse la preuve
        forged = dict(signed["decision"], answer=dict(answer, signer={"by": "owner"}))
        self.assertFalse(decisions.verify_proof(self.db, forged)[0])

    def test_accord_absent_ou_d_un_autre_refuse(self):
        view = self.ask(needs_signature=True)
        for accord in ({"agent": AVATAR, "seq": 999}, self.go(author="human:bob")):
            with self.subTest(accord=accord):
                with self.assertRaises(DecisionError):
                    self.answer(view["id"], "a", key_path=self.private,
                                signer=self.avatar(accord))
        self.assertEqual(decisions.get(self.db, view["id"])["state"], "pending")

    def test_avatar_signe_avec_la_cle(self):
        with self.assertRaises(DecisionError):
            self.answer(self.ask()["id"], "a", signer=self.avatar(self.go()))

    def grant(self, **extra):
        return dict({"approver": ALICE, "revoked_ts": None, "until_ts": time.time() + 3600,
                     "operations": ["decision-sign"]}, **extra)

    def test_grant_couvre_sauf_cle_ou_provenance(self):
        view = self.ask(needs_signature=True)
        with mock.patch("ameesh.receipts.get_standing", return_value=self.grant()):
            signed = self.answer(view["id"], "a", key_path=self.private,
                                 signer=self.avatar({"grant": 7}))
        self.assertEqual(self.meta(signed)["accord"], {"grant": 7})
        for bad in (self.grant(revoked_ts=1.0), self.grant(until_ts=time.time() - 1),
                    self.grant(approver="human:bob"), self.grant(operations=["spend"]), None):
            with self.subTest(grant=bad):
                with mock.patch("ameesh.receipts.get_standing", return_value=bad):
                    with self.assertRaises(DecisionError):
                        decisions.check_accord(self.db, self.ask(), ALICE, {"grant": 7},
                                               now=time.time())
        key = self.ask(needs_signature=True, key_provenance=True)
        self.assertEqual(decisions.decision_operation(key), decisions.OP_DECISION_SIGN_KEY)
        with mock.patch("ameesh.receipts.get_standing", return_value=self.grant()):
            with self.assertRaises(DecisionError):
                decisions.check_accord(self.db, key, ALICE, {"grant": 7}, now=time.time())
        # forme (a) : couvre aussi la clé et la provenance
        self.assertEqual(decisions.check_accord(self.db, key, ALICE, self.go(),
                                                now=time.time())[:len(AVATAR)], AVATAR)

    def test_reclassement_vers_cle_seulement(self):
        view = self.ask()
        self.assertEqual(decisions.decision_operation(view), decisions.OP_DECISION_SIGN)
        marked = decisions.reclassify_key_provenance(self.cfg, self.db, view["id"],
                                                     actor=AVATAR)
        self.assertTrue(marked["key_provenance"])
        doubt = self.ask(question="Remplacer la clé de l'owner sur bureau-goal ?")
        self.assertEqual(decisions.decision_operation(doubt), decisions.OP_DECISION_SIGN_KEY)

    def test_garde_de_session(self):
        accord = {"agent": AVATAR, "seq": 1}
        with mock.patch.object(decisions, "agent_session_reason", return_value=None):
            with self.assertRaises(DecisionError):
                decisions.avatar_answerer(self.cfg, self.db, accord)
        with mock.patch.object(decisions, "agent_session_reason", return_value="agent"), \
                mock.patch.dict(os.environ, {"AGENT_MAIL_NAME": AVATAR}):
            with mock.patch.object(decisions, "avatar_human", return_value=None):
                with self.assertRaises(DecisionError):
                    decisions.avatar_answerer(self.cfg, self.db, accord)
            with mock.patch.object(decisions, "avatar_human", return_value=ALICE):
                who, signer = decisions.avatar_answerer(self.cfg, self.db, accord)
        self.assertEqual((who.human, signer), (ALICE, self.avatar(accord)))

    def test_journal_distingue_et_signale(self):
        from ameesh import reserved_gestures
        since = time.time() - 1
        self.answer(self.ask(needs_signature=True)["id"], "a", key_path=self.private)
        accord = self.go()
        self.answer(self.ask(needs_signature=True)["id"], "a", key_path=self.private,
                    signer=self.avatar(accord))
        # une signature d'avant #7631 : meta sans signed_by
        legacy = self.ask(needs_signature=True)
        signed = self.answer(legacy["id"], "a", key_path=self.private)
        self.db.execute("UPDATE mesh_approvals SET meta = meta - 'signed_by' WHERE id = %s",
                        (signed["decision"]["answer"]["proof"]["approval_id"],))
        view = reserved_gestures.journal(self.db, since_ts=since, until_ts=time.time() + 1)
        rows = [g for g in view["gestures"] if g["kind"] == "decision"]
        self.assertEqual([g["signed_by"] for g in rows], ["owner", "avatar", None])
        self.assertEqual(rows[1]["accord"], accord)
        self.assertEqual([(s["code"], s["decision"]) for s in view["signals"]],
                         [(reserved_gestures.UNRECORDED_SIGNER, legacy["id"])])
