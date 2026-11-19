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
        self.db.execute("TRUNCATE accord_claims")
        self.private, public, _fp = signing.write_keypair(
            os.path.join(self.tmp, "keys-alice"), "alice")
        authority.register_human_key(self.db, ALICE, signing.read_public(public),
                                     role="owner")

    def go(self, decision_id, author=ALICE, text=None) -> dict:
        entry = storage.of(self.db).conversations.append(
            agent=AVATAR, session=None, turn=None, kind="human_message", author=author,
            text=text or ("go %d a" % int(decision_id)), truncated=False, meta={},
            event_id=None, project="ameesh", lot=None, thread_text=None)
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

        view = self.ask(needs_signature=True)
        accord = self.go(view["id"])
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
        for accord in ({"agent": AVATAR, "seq": 999},
                       self.go(view["id"], author="human:bob")):
            with self.subTest(accord=accord):
                with self.assertRaises(DecisionError):
                    self.answer(view["id"], "a", key_path=self.private,
                                signer=self.avatar(accord))
        self.assertEqual(decisions.get(self.db, view["id"])["state"], "pending")

    def test_go_doit_nommer_l_option_signee(self):
        """Auditeur : « go <id> B » ne laisse pas signer A ; le go nomme
        l'option que l'avatar signe."""
        view = self.ask(needs_signature=True)
        other = self.go(view["id"], text="go %d b" % view["id"])
        with self.assertRaises(DecisionError):
            self.answer(view["id"], "a", key_path=self.private,
                        signer=self.avatar(other))
        self.assertEqual(decisions.get(self.db, view["id"])["state"], "pending")
        # le go qui nomme l'option signée passe
        signed = self.answer(view["id"], "a", key_path=self.private,
                             signer=self.avatar(self.go(view["id"])))
        self.assertEqual(signed["decision"]["answer"]["by"], ALICE)

    def test_option_non_est_couvrable(self):
        """B1 2e tour auditeur (N) : la grammaire fermée laisse couvrir une
        option nommée « non » — elle n'est plus prise pour une négation."""
        view = self.ask(needs_signature=True,
                        options=["a=oui, fusionner", "non=refuser"])
        accord = self.go(view["id"], text="go %d non" % view["id"])
        signed = self.answer(view["id"], "non", key_path=self.private,
                             signer=self.avatar(accord))
        self.assertEqual(signed["decision"]["answer"]["by"], ALICE)

    def test_avatar_signe_avec_la_cle(self):
        view = self.ask()
        with self.assertRaises(DecisionError):
            self.answer(view["id"], "a", signer=self.avatar(self.go(view["id"])))

    def test_grant_refuse_tant_que_la_porte_ne_reserve_pas(self):
        """Relecture cx2 (B) : un grant, même actif et à son nom, ne couvre pas
        une signature de décision (ni plafond, ni réservation, ni portée)."""
        grant = {"approver": ALICE, "revoked_ts": None, "until_ts": time.time() + 3600,
                 "operations": ["decision-sign"], "max_amount": 1, "consumed_amount": 1}
        view = self.ask(needs_signature=True)
        with mock.patch("ameesh.receipts.get_standing", return_value=grant):
            with self.assertRaises(DecisionError) as caught:
                self.answer(view["id"], "a", key_path=self.private,
                            signer=self.avatar({"grant": 7}))
        self.assertIn("refused", str(caught.exception))
        self.assertEqual(decisions.get(self.db, view["id"])["state"], "pending")
        key = self.ask(needs_signature=True, key_provenance=True)
        self.assertEqual(decisions.decision_operation(key), decisions.OP_DECISION_SIGN_KEY)
        with self.assertRaises(DecisionError):
            decisions.check_accord(self.db, key, ALICE, {"grant": 7}, now=time.time())
        # forme (a) : couvre aussi la clé et la provenance
        self.assertEqual(decisions.check_accord(self.db, key, ALICE,
                                                self.go(key["id"]),
                                                now=time.time(),
                                                option="a")[:len(AVATAR)], AVATAR)

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
        second = self.ask(needs_signature=True)
        accord = self.go(second["id"])
        self.answer(second["id"], "a", key_path=self.private,
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

    def test_go_d_une_autre_session_refuse(self):
        view = self.ask(needs_signature=True)
        accord = self.go(view["id"])
        other = {"by": decisions.SIGNED_BY_AVATAR, "agent": "autre-avatar", "accord": accord}
        with self.assertRaises(DecisionError):
            self.answer(view["id"], "a", key_path=self.private, signer=other)
        self.assertEqual(decisions.get(self.db, view["id"])["state"], "pending")
