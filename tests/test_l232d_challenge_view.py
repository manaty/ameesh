# SPDX-License-Identifier: AGPL-3.0-only
"""L232d (#366) : le défi de connexion d'ameesh-approve, de bout en bout.

Le lien seul est MUET : l'URL d'autorisation et le code à usage unique ne
sortent du service qu'après une assertion WebAuthn (`view`). Le reçu signé
(`done` | `cancel`) ne les contient jamais, pas même par un condensat, et le
reçu « terminé » ne prouve pas le grant fournisseur (la santé se vérifie à
part, hors de ce fichier).

Même banc que tests/test_approve.py : serveur réel sur la boucle locale,
passkey logicielle (tests/webauthn_soft.py), horloge avançable.
"""
from __future__ import annotations

import concurrent.futures
import dataclasses
import json
import os
import re
import shutil
import stat
import tempfile
import time
import unittest
from types import SimpleNamespace
from urllib.parse import urlsplit

from ameesh import receipts
from ameesh.approve.config import ApproveConfig
from ameesh.approve.config import ApproveConfig, ApproveConfigError
from ameesh.approve.service import ApproveError, ApproveService
from ameesh.approve.store import Store

from .test_approve import AGENT, ALICE, ApproveBase
from .webauthn_soft import SoftWebAuthn

CODE = "CANARY-CODE-1234"
URL = "https://github.com/login/device"


class LoginReceiptFormTest(unittest.TestCase):
    """La forme du reçu `login` (receipts.check_request), sans base."""

    @staticmethod
    def request(**overrides) -> dict:
        request = {
            "v": receipts.REQUEST_VERSION,
            "approver": ALICE,
            "login_id": "lgn_" + "a" * 26,
            "host": "ameesh-agents-1",
            "provider": "gh",
            "account": "smichea",
            "scope": "",
            "summary_digest": "sha256:" + "0" * 64,
            "requested_by": AGENT,
            "nonce": receipts.b64u_encode(os.urandom(16)),
            "iat": 100,
            "exp": 200,
            "decision": "view",
        }
        request.update(overrides)
        return request

    def test_forme_valide(self):
        self.assertEqual(receipts.check_request(self.request()), "login")
        for decision in ("done", "cancel"):
            self.assertEqual(receipts.check_request(self.request(decision=decision)), "login")

    def test_decision_inconnue_refusee(self):
        for decision in ("approve", "deny", "open"):
            with self.assertRaises(receipts.ReceiptError):
                receipts.check_request(self.request(decision=decision))

    def test_ni_action_ni_empreinte(self):
        with self.assertRaises(receipts.ReceiptError):
            receipts.check_request(self.request(action_id="act_" + "a" * 26))
        with self.assertRaises(receipts.ReceiptError):
            receipts.check_request(self.request(digest="sha256:" + "0" * 64))

    def test_login_id_et_champs_controles(self):
        for overrides in ({"login_id": "lgn_short"}, {"host": ""}, {"host": "a b"},
                          {"provider": ""}, {"account": "a\nb"}, {"scope": "- x"}):
            with self.assertRaises(receipts.ReceiptError):
                receipts.check_request(self.request(**overrides))

    def test_identifiants_vides_admis(self):
        self.assertEqual(receipts.check_request(self.request(account="", scope="")), "login")


class LoginCreationContractTest(unittest.TestCase):
    def test_compte_gh_vide_et_portee_texte_acceptes_par_le_service_et_le_recu(self):
        with tempfile.TemporaryDirectory(prefix="l232d-contract-") as directory:
            cfg = ApproveConfig(state_dir=directory, public_url="https://approve.example.test")
            service = ApproveService(cfg, None, None, service_token="service", clock=lambda: 1000)
            service._credentials = lambda _approver: ["credential"]
            for fields in ({"account": "", "scope": ""},
                           {"account": "alice@example.test", "scope": "read repository + profile"}):
                with self.subTest(fields=fields):
                    answer = service.create_login_request(dict(
                        host="h", provider="gh", approver=ALICE, requested_by=AGENT,
                        url=URL, code=CODE, **fields))
                    record = service.store.get_login(answer["login_id"])
                    self.assertEqual(receipts.check_request(
                        dict(record["request"], decision="done")), "login")


class LoginApiMixin:
    """Banc de l'API des défis : ouverte, avec son jeton DÉDIÉ (le jeton de
    service de l'équipe n'y est jamais accepté, avis B1 dev4)."""

    LOGIN_API = True

    def login_api(self, method: str, path: str, body=None, **kwargs):
        """Appel de l'API des défis : jeton de connexion, pas celui d'équipe."""
        kwargs.setdefault("host", self.local)
        kwargs.setdefault("auth", self.login_token)
        return self.http(method, path, body, **kwargs)

    def open_view(self, path):
        status, headers, text = self.page(path)
        self.assertEqual(status, 200, text)
        self.assertSecurityHeaders(headers)
        page = self.page_data(text, "ameesh-login")
        status, payload = self.submit(path, self.sign(self.alice, page, decision="view"))
        self.assertEqual(status, 200, payload)
        return page, payload


class LoginChallengeTest(LoginApiMixin, ApproveBase):
    def create_login(self, **overrides):
        body = {"host": "ameesh-agents-1", "provider": "gh", "account": "smichea",
                "approver": ALICE, "requested_by": AGENT, "url": URL, "code": CODE}
        body.update(overrides)
        status, _headers, payload = self.login_api("POST", "/login-requests", body)
        self.assertEqual(status, 201, payload)
        data = json.loads(payload)
        self.assertRegex(data["login_id"], r"^lgn_[0-9a-z]{26}$")
        link = urlsplit(data["link"])
        self.assertEqual("%s://%s" % (link.scheme, link.netloc), "https://approve.example.test")
        self.assertNotIn(CODE, payload.decode())
        self.assertNotIn(URL, payload.decode())
        return data, link.path

    def fetch_login_receipt(self, login_id):
        status, _headers, payload = self.login_api("GET", "/login-receipts/" + login_id)
        return status, json.loads(payload)

    def verify(self, raw, *, login_id, exp, **kwargs):
        expected = {"login_id": login_id, "host": "ameesh-agents-1", "provider": "gh",
                    "account": "smichea", "exp": exp}
        policy = receipts.Policy(rp_id="approve.example.test",
                                 origins=("https://approve.example.test",))
        return receipts.verify_receipt(self.server_db, raw, policy, kind="login",
                                       expected_login=expected, **kwargs)

    # -- le lien seul est muet, l'assertion ouvre ---------------------------
    def test_lien_muet_puis_vue_privee_puis_recu(self):
        data, path = self.create_login()
        status, pending = self.fetch_login_receipt(data["login_id"])
        self.assertEqual((status, pending["status"]), (202, "pending"))

        status, headers, text = self.page(path)
        self.assertEqual(status, 200, text)
        # ni URL ni code dans la page publique ; aucune assertion encore
        self.assertNotIn(CODE, text)
        self.assertNotIn(URL, text)
        page = self.page_data(text, "ameesh-login")
        self.assertNotIn("challenge-done", page)
        self.assertIn("challenge-view", page)
        self.assertIn("challenge-cancel", page)

        # `done` sans avoir ouvert la vue : refusé, même avec une assertion
        # correctement signée sur le défi `done` (le service ne l'expose pas
        # avant la vue ; le test le recalcule depuis la demande du service)
        record = self.service.store.get_login(data["login_id"])
        done_challenge = receipts.b64u_encode(
            receipts.challenge(dict(record["request"], decision="done")))
        status, refused = self.submit(path, self.sign(
            self.alice, {"challenge-done": done_challenge}, decision="done"))
        self.assertEqual(status, 409, refused)
        self.assertEqual(refused["error"], "not_viewed")

        # l'assertion `view` rend l'URL et le code, une seule fois dans la réponse
        status, payload = self.submit(path, self.sign(self.alice, page, decision="view"))
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["url"], URL)
        self.assertEqual(payload["code"], CODE)
        self.assertIn("challenge_done", payload)
        # le JavaScript de la page lit la réponse de `view` sous ces noms : un
        # nom absent (`challengeDone`) laissait « I finished » inerte (R dev4)
        js = os.path.join(os.path.dirname(receipts.__file__), "approve", "static",
                          "approve.js")
        with open(js, encoding="utf-8") as fh:
            read = set(re.findall(r"\bpayload\.([A-Za-z_]+)", fh.read()))
        self.assertTrue(read)
        self.assertLessEqual(read, set(payload), "approve.js reads keys the view lacks")
        # rouvrir la vue ne consomme pas le nonce : une seconde assertion marche
        status, again = self.submit(path, self.sign(self.alice, page, decision="view"))
        self.assertEqual(status, 200, again)

        # `done` signé après la vue : reçu enregistré
        status, signed = self.submit(path, self.sign(
            self.alice, {"challenge-done": payload["challenge_done"]}, decision="done"))
        self.assertEqual(status, 200, signed)
        self.assertEqual(signed["decision"], "done")
        status, document = self.fetch_login_receipt(data["login_id"])
        self.assertEqual(status, 200)
        self.assertEqual(document["request"]["decision"], "done")
        self.assertEqual(document["request"]["login_id"], data["login_id"])
        self.assertEqual(document["request"]["host"], "ameesh-agents-1")
        self.assertNotIn(CODE, json.dumps(document))
        self.assertNotIn(URL, json.dumps(document))
        self.assertEqual(document["request"]["summary_digest"],
                         receipts_render_summary_digest(document["request"]))
        record = self.service.store.get_login(data["login_id"])
        self.assertIn("ameesh-login-summary/1", record["summary"])
        self.assertNotIn(CODE, record["summary"])
        self.assertNotIn(URL, record["summary"])

        # ameesh (le demandeur) VÉRIFIE puis CONSOMME le nonce lui-même
        verdict = self.verify(
            document, login_id=data["login_id"], exp=document["request"]["exp"],
            expect_decision="done", consume_by=AGENT)
        self.assertTrue(verdict.ok, verdict.reason)
        self.assertEqual(verdict.login_id, data["login_id"])
        self.assertEqual(verdict.approver, ALICE)
        replay = self.verify(
            document, login_id=data["login_id"], exp=document["request"]["exp"],
            expect_decision="done", consume_by=AGENT)
        self.assertFalse(replay.ok)
        self.assertEqual(replay.code, receipts.REPLAY)

        # le lien est consommé : la page ne sert plus
        status, _headers, text = self.page(path)
        self.assertEqual(status, 410, text)

    def test_annulation_avant_vue(self):
        data, path = self.create_login()
        page = self.page_data(self.page(path)[2], "ameesh-login")
        status, result = self.submit(path, self.sign(self.alice, page, decision="cancel"))
        self.assertEqual(status, 200, result)
        self.assertEqual(result["decision"], "cancel")
        status, raw = self.fetch_login_receipt(data["login_id"])
        self.assertEqual(status, 200)
        verdict = self.verify(raw, login_id=data["login_id"],
                              exp=raw["request"]["exp"],
                              expect_decision="cancel")
        self.assertTrue(verdict.ok, verdict.reason)

    def test_annulation_apres_vue(self):
        data, path = self.create_login()
        page, opened = self.open_view(path)
        status, result = self.submit(path, self.sign(
            self.alice, {"challenge-cancel": opened["challenge_cancel"]}, decision="cancel"))
        self.assertEqual(status, 200, result)
        self.assertEqual(result["decision"], "cancel")
        # le code privé est effacé
        private = self.service.store.login_private(data["login_id"])
        self.assertIsNone(private)

    # -- liaison : le reçu vaut pour CE défi, pas un autre ------------------
    def test_recu_lie_au_defi(self):
        data, path = self.create_login()
        _page, opened = self.open_view(path)
        status, _signed = self.submit(path, self.sign(
            self.alice, {"challenge-done": opened["challenge_done"]}, decision="done"))
        self.assertEqual(status, 200)
        status, raw = self.fetch_login_receipt(data["login_id"])
        exp = raw["request"]["exp"]
        wrong = self.verify(raw, login_id=data["login_id"], exp=exp, expect_decision="done")
        self.assertTrue(wrong.ok)
        for overrides in ({"host": "other-host"}, {"provider": "codex"},
                          {"account": "other"}, {"exp": exp + 60},
                          {"login_id": "lgn_" + "b" * 26}):
            expected = {"login_id": data["login_id"], "host": "ameesh-agents-1",
                        "provider": "gh", "account": "smichea", "exp": exp}
            expected.update(overrides)
            verdict = receipts.verify_receipt(
                self.server_db, raw, receipts.Policy(
                    rp_id="approve.example.test",
                    origins=("https://approve.example.test",)),
                kind="login", expected_login=expected, expect_decision="done")
            self.assertFalse(verdict.ok, overrides)
            self.assertEqual(verdict.code, receipts.LOGIN_MISMATCH, overrides)

    def test_consommation_exige_la_liaison_complete(self):
        data, path = self.create_login()
        _page, opened = self.open_view(path)
        self.submit(path, self.sign(self.alice,
                                    {"challenge-done": opened["challenge_done"]},
                                    decision="done"))
        _status, raw = self.fetch_login_receipt(data["login_id"])
        exp = raw["request"]["exp"]
        policy = receipts.Policy(rp_id="approve.example.test",
                                 origins=("https://approve.example.test",))
        verdict = receipts.verify_receipt(
            self.server_db, raw, policy, kind="login",
            expected_login={"login_id": data["login_id"]}, expect_decision="done",
            consume_by=AGENT)
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.code, receipts.UNBOUND)

    def test_recu_login_refuse_comme_action(self):
        data, path = self.create_login()
        _page, opened = self.open_view(path)
        self.submit(path, self.sign(self.alice,
                                    {"challenge-done": opened["challenge_done"]},
                                    decision="done"))
        _status, raw = self.fetch_login_receipt(data["login_id"])
        verdict = receipts.verify_receipt(
            self.server_db, raw, receipts.Policy(
                rp_id="approve.example.test", origins=("https://approve.example.test",)),
            kind="action")
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.code, receipts.KIND)

    # -- refus de forme et de domaine ---------------------------------------
    def test_url_hors_domaine_refusee(self):
        for url in ("https://evil.example/login", "http://github.com/login/device",
                    "https://github.com.evil.example/x", "https://user:pw@github.com/x"):
            status, _headers, payload = self.login_api("POST", "/login-requests", {
                "host": "ameesh-agents-1", "provider": "gh", "account": "smichea",
                "approver": ALICE, "requested_by": AGENT, "url": url, "code": CODE})
            self.assertEqual(status, 400, payload)

    def test_provider_et_code_controles(self):
        for overrides in ({"provider": "gemini"}, {"code": "has space"}, {"host": "a b"},
                          {"account": "a" * 200}, {"scope": "x\ny"}):
            status, _headers, payload = self.login_api("POST", "/login-requests", {
                "host": "ameesh-agents-1", "provider": "gh", "account": "smichea",
                "approver": ALICE, "requested_by": AGENT, "url": URL, "code": CODE,
                **overrides})
            self.assertEqual(status, 400, payload)

    def test_jeton_de_connexion_exige(self):
        # sans jeton : 401
        status, _headers, _payload = self.http("POST", "/login-requests", {
            "host": "h", "provider": "gh", "account": "a", "approver": ALICE,
            "requested_by": AGENT, "url": URL, "code": CODE}, host=self.local)
        self.assertEqual(status, 401)
        # le jeton de SERVICE DE L'ÉQUIPE n'ouvre JAMAIS un défi (avis B1 dev4) :
        # tous les agents le lisent, et l'humain signerait un code généré ailleurs
        status, _headers, payload = self.api("POST", "/login-requests", {
            "host": "h", "provider": "gh", "account": "a", "approver": ALICE,
            "requested_by": AGENT, "url": URL, "code": CODE})
        self.assertEqual(status, 401, payload)
        self.assertEqual(json.loads(payload)["error"], "unauthorized")
        # ni sur la lecture du reçu
        self.assertEqual(self.api("GET", "/login-receipts/lgn_" + "a" * 26)[0], 401)
        # le jeton de connexion ne vaut pas non plus pour l'API d'approbation
        status, _headers, payload = self.http(
            "POST", "/requests", {"action_id": "act_" + "a" * 26, "approver": ALICE,
                                  "requested_by": AGENT},
            host=self.local, auth=self.login_token)
        self.assertEqual(status, 401, payload)

    def test_page_dit_declare_non_verifie(self):
        """B1(b) : la page ne présente plus l'hôte ni le demandeur comme un fait."""
        _data, path = self.create_login()
        status, _headers, text = self.page(path)
        self.assertEqual(status, 200, text)
        self.assertIn("declared by the caller, not verified", text)
        self.assertIn("generated", text)
        self.assertIn("only continue if you started this connection yourself", text)
        self.assertNotIn("An agent requests a", text)

    def test_ttl_borne(self):
        # 15 min au plus
        _data, _path = self.create_login(account="ttl-default")
        record = self.service.store.get_login(
            next(name[:-5] for name in os.listdir(os.path.join(
                self.acfg.state_dir, "login-requests"))))
        self.assertEqual(record["request"]["exp"] - record["request"]["iat"], 900)
        # l'échéance fournisseur, plus courte, gagne
        now = int(self.clock())
        data, _path = self.create_login(provider_exp=now + 120)
        record = self.service.store.get_login(data["login_id"])
        self.assertLessEqual(record["request"]["exp"] - record["request"]["iat"], 120)
        # une échéance passée est refusée
        status, _headers, _payload = self.login_api("POST", "/login-requests", {
            "host": "ameesh-agents-1", "provider": "gh", "account": "smichea",
            "approver": ALICE, "requested_by": AGENT, "url": URL, "code": CODE,
            "provider_exp": now - 1})
        self.assertEqual(status, 400)

    # -- expiration et élagage ---------------------------------------------
    def test_expiration(self):
        data, path = self.create_login()
        page = self.page_data(self.page(path)[2], "ameesh-login")
        challenge = page["challenge-view"]
        self.clock.offset = 901
        self.assertEqual(self.page(path)[0], 410)
        status, payload = self.fetch_login_receipt(data["login_id"])
        self.assertEqual((status, payload["status"]), (410, "expired"))
        status, refused = self.submit(path, self.sign(
            self.alice, {"challenge-view": challenge}, decision="view"))
        self.assertEqual(status, 410, refused)

    def test_elagage(self):
        data, path = self.create_login()
        state = self.acfg.state_dir
        private = os.path.join(state, "login-private", data["login_id"] + ".json")
        self.assertTrue(os.path.exists(private))
        self.assertTrue(self.service.store.close_login(data["login_id"]))
        closed = os.path.join(state, "login-closed", data["login_id"])
        self.assertTrue(os.path.exists(closed + ".lock"))
        self.service.store.prune(self.clock() + receipts.DEFAULT_MAX_TTL + 10 * 86400)
        for path_ in (os.path.join(state, "login-requests", data["login_id"] + ".json"),
                      private,
                      os.path.join(state, "login-views", data["login_id"]),
                      closed, closed + ".lock"):
            self.assertFalse(os.path.exists(path_), path_)
        self.assertEqual(self.page(path)[0], 404)

    # -- secrets : permissions, journaux, état privé ------------------------
    def test_permissions_et_canari(self):
        data, path = self.create_login()
        state = self.acfg.state_dir
        private = os.path.join(state, "login-private", data["login_id"] + ".json")
        public = os.path.join(state, "login-requests", data["login_id"] + ".json")
        self.assertEqual(stat.S_IMODE(os.stat(state).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(private).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(public).st_mode), 0o600)
        with open(private, "r", encoding="utf-8") as handle:
            self.assertIn(CODE, handle.read())
        with open(public, "r", encoding="utf-8") as handle:
            body = handle.read()
        self.assertNotIn(CODE, body)
        self.assertNotIn(URL, body)
        _page, opened = self.open_view(path)
        self.submit(path, self.sign(self.alice,
                                    {"challenge-done": opened["challenge_done"]},
                                    decision="done"))
        self.assertNotIn(CODE, self.log.getvalue())
        # le code privé est effacé dès le reçu signé
        self.assertIsNone(self.service.store.login_private(data["login_id"]))
        self.assertFalse(os.path.exists(private))


class LoginTokenIsolationTest(unittest.TestCase):
    def test_meme_jeton_refuse_meme_dans_deux_fichiers_distincts(self):
        cfg = ApproveConfig(
            rp_id="approve.example.test", origins=("https://approve.example.test",),
            login_api="open", token_file="/service-token", login_token_file="/login-token")
        with self.assertRaisesRegex(ApproveConfigError, "DISTINCT"):
            ApproveService(cfg, None, None, service_token="shared-test-token",
                           login_token="shared-test-token", store=object())


class LoginApiClosedTest(ApproveBase):
    """L'API des défis est FERMÉE par défaut (avis B1 dev4, option a) : aucun
    jeton, ni celui de l'équipe ni celui du superviseur, n'y ouvre un défi
    tant que le service ne l'a pas ouverte par écrit (`login_api: "open"`)."""

    LOGIN_API = False

    def test_fermee_par_defaut(self):
        body = {"host": "ameesh-agents-1", "provider": "gh", "account": "smichea",
                "approver": ALICE, "requested_by": AGENT, "url": URL, "code": CODE}
        for auth in (None, self.service_token, self.login_token):
            kwargs = {"host": self.local}
            if auth is not None:
                kwargs["auth"] = auth
            status, _headers, payload = self.http("POST", "/login-requests", body, **kwargs)
            self.assertEqual(status, 403, (auth, payload))
            self.assertEqual(json.loads(payload)["error"], "login_api_closed", payload)
        for auth in (None, self.service_token, self.login_token):
            kwargs = {"host": self.local}
            if auth is not None:
                kwargs["auth"] = auth
            self.assertEqual(
                self.http("GET", "/login-receipts/lgn_" + "a" * 26, **kwargs)[0], 403)

    def test_service_sans_jeton_de_connexion_reste_ferme(self):
        """Même config ouverte, sans jeton dédié chargé : refus (fail closed)."""
        opened = dataclasses.replace(self.acfg, login_api="open")
        service = ApproveService(opened, None, None, service_token=self.service_token)
        self.assertFalse(service.login_api_open)
        self.assertFalse(service.check_login_token(self.login_token))
        # et un jeton vide ne vaut rien non plus
        self.assertFalse(service.check_login_token(""))


def receipts_render_summary_digest(request: dict) -> str:
    """L'empreinte du résumé, recalculée depuis la demande signée."""
    from ameesh.approve import render
    return render.summary_digest(render.render_login_summary(request))


class LoginUniquenessTest(LoginApiMixin, ApproveBase):
    """#366/B3 : un seul défi vivant par (hôte, fournisseur, compte), réservé
    par le service ; la clôture technique libère la place, efface la charge
    privée et ne forge jamais de reçu humain."""

    def create_login(self, **overrides):
        body = {"host": "ameesh-agents-1", "provider": "gh", "account": "smichea",
                "approver": ALICE, "requested_by": AGENT, "url": URL, "code": CODE}
        body.update(overrides)
        status, _headers, payload = self.login_api("POST", "/login-requests", body)
        return status, payload

    def close_login(self, login_id):
        status, _headers, payload = self.login_api("POST", "/login-close/" + login_id, {})
        return status, payload

    def test_un_seul_defi_vivant_par_tuple(self):
        status, payload = self.create_login()
        self.assertEqual(status, 201, payload)
        first = json.loads(payload)
        status, payload = self.create_login()
        self.assertEqual(status, 409, payload)
        self.assertEqual(json.loads(payload)["error"], "already_pending")
        # un autre compte est un autre tuple
        self.assertEqual(self.create_login(account="other")[0], 201)
        # la clôture technique libère la place
        status, closed = self.close_login(first["login_id"])
        self.assertEqual((status, json.loads(closed)["closed"]), (200, True))
        self.assertEqual(self.create_login()[0], 201)

    def test_le_recu_humain_libere_la_place(self):
        _status, payload = self.create_login()
        data = json.loads(payload)
        path = urlsplit(data["link"]).path
        _page, opened = self.open_view(path)
        status, signed = self.submit(path, self.sign(
            self.alice, {"challenge-done": opened["challenge_done"]}, decision="done"))
        self.assertEqual(status, 200, signed)
        self.assertEqual(self.create_login()[0], 201)

    def test_l_echeance_libere_la_place(self):
        now = int(self.clock())
        self.assertEqual(self.create_login(provider_exp=now + 60)[0], 201)
        self.assertEqual(self.create_login()[0], 409)
        self.clock.offset = 61
        self.assertEqual(self.create_login()[0], 201)

    def test_cloture_technique_efface_et_ne_ressuscite_rien(self):
        _status, payload = self.create_login()
        data = json.loads(payload)
        login_id, path = data["login_id"], urlsplit(data["link"]).path
        self.assertIsNotNone(self.service.store.login_private(login_id))
        status, closed = self.close_login(login_id)
        self.assertEqual((status, json.loads(closed)["closed"]), (200, True))
        # charge privée effacée, aucun reçu forgé
        self.assertIsNone(self.service.store.login_private(login_id))
        self.assertFalse(self.service.store.has_login_receipt(login_id))
        status, _headers, payload = self.login_api("GET", "/login-receipts/" + login_id)
        self.assertEqual((status, json.loads(payload)["status"]), (410, "closed"))
        # le lien muet ne s'ouvre plus
        self.assertEqual(self.page(path)[0], 410)
        # une assertion tardive ne ressuscite rien
        record = self.service.store.get_login(login_id)
        challenge = receipts.b64u_encode(receipts.challenge(
            dict(record["request"], decision="view")))
        status, _payload = self.submit(path, self.sign(
            self.alice, {"challenge-view": challenge}, decision="view"))
        self.assertEqual(status, 410)

    def test_la_cloture_technique_n_efface_pas_un_recu_humain(self):
        _status, payload = self.create_login()
        data = json.loads(payload)
        path = urlsplit(data["link"]).path
        _page, opened = self.open_view(path)
        self.submit(path, self.sign(
            self.alice, {"challenge-done": opened["challenge_done"]}, decision="done"))
        self.assertEqual(self.close_login(data["login_id"])[0], 200)
        status, _headers, _payload = self.login_api("GET", "/login-receipts/" + data["login_id"])
        self.assertEqual(status, 200)  # le reçu humain reste la preuve


class LoginReservationRaceTest(unittest.TestCase):
    """#366/B3 : réservation atomique entre processus — deux `Store` sur le
    même état, aucun verrou en mémoire partagé."""

    def test_deux_stores_ne_prennent_pas_la_meme_place(self):
        root = tempfile.mkdtemp(prefix="l232d-live-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        stores = [Store(root), Store(root)]
        now = time.time()

        def record(i):
            return {"login_id": "lgn_" + ("%026x" % i), "exp": now + 900,
                    "link_exp": now + 900,
                    "request": {"host": "h", "provider": "gh", "account": "a",
                                "exp": now + 900}}

        def claim(i):
            return stores[i % 2].reserve_login(record(i), now=now)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(claim, range(8)))
        self.assertEqual(sum(1 for taken in results if taken), 1)
        winner = results.index(True)
        self.assertTrue(stores[winner % 2].release_login(record(winner)))
        self.assertTrue(stores[(winner + 1) % 2].reserve_login(record(winner + 1), now=now))


class LoginClosureRaceTest(unittest.TestCase):
    """Ordres concurrents imposés, sans réseau, base ni attente d'horloge."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="l232d-close-race-")
        self.addCleanup(directory.cleanup)
        self.service = ApproveService(ApproveConfig(
            state_dir=directory.name, public_url="https://approve.example.test"),
            None, None, service_token="fake", clock=lambda: 1000)
        self.service._credentials = lambda _approver: ["fake-credential"]
        self.body = dict(host="h", provider="gh", account="a", approver=ALICE,
                         requested_by=AGENT, url=URL, code=CODE)

    def test_assertion_chargee_avant_cloture_ne_cree_pas_de_recu_apres(self):
        service = self.service
        first = service.create_login_request(self.body)
        login_id = first["login_id"]
        token = urlsplit(first["link"]).path.rsplit("/", 1)[1]
        service.store.mark_login_viewed(login_id)
        service._assertion = lambda _body, _decisions: "done"
        service._login_receipt = lambda record, _body, decision: (
            {"request": dict(record["request"], decision=decision)},
            SimpleNamespace(ok=True, approver=ALICE))
        live = service._live_login
        successor = []

        def close_after_read(value):
            record = live(value)
            service.close_login(login_id)
            successor.append(service.create_login_request(self.body))
            service._live_login = live
            return record

        service._live_login = close_after_read
        with self.assertRaises(ApproveError) as caught:
            service.login_submit(token, {})
        self.assertEqual((caught.exception.status, caught.exception.code), (410, "closed"))
        self.assertFalse(service.store.has_login_receipt(login_id))
        self.assertEqual(service.login_receipt(login_id)[0], 410)
        self.assertEqual(service.login_receipt(successor[0]["login_id"])[0], 202)

    def test_cloture_tardive_ancienne_ne_libere_pas_le_successeur(self):
        service = self.service
        first = service.create_login_request(self.body)
        service.close_login(first["login_id"])
        successor = service.create_login_request(self.body)
        service.close_login(first["login_id"])
        with self.assertRaises(ApproveError) as caught:
            service.create_login_request(self.body)
        self.assertEqual((caught.exception.status, caught.exception.code),
                         (409, "already_pending"))
        self.assertEqual(service.login_receipt(successor["login_id"])[0], 202)
        self.assertIsNotNone(service.store.login_private(successor["login_id"]))
