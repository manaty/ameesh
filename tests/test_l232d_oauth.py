# SPDX-License-Identifier: AGPL-3.0-only
"""L232d (#366) : le superviseur de connexion, sur des CLI factices.

Chaque adaptateur est un script déterministe : flux reconnu, flux inconnu,
refus fournisseur, sortie trop longue, processus qui ignore SIGTERM. On
vérifie qu'aucune valeur privée (URL d'autorisation, code) ne sort du rappel
`publish`, ne survit au-delà de la décision, et qu'une seule demande vit par
`(hôte, harnais, compte)`.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import time
import unittest

from ameesh import login


def wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


class Sink:
    """Rappel `publish` factice : garde ce qu'on lui donne pour le vérifier."""

    def __init__(self, answer: bool = True):
        self.payloads: list[dict] = []
        self.answer = answer

    def __call__(self, payload: dict) -> dict:
        self.payloads.append(dict(payload))
        if not self.answer:
            raise RuntimeError("refused")
        return {"login_id": "lgn_" + "a" * 26,
                "link": "https://approve.example/login/" + "t" * 26}


class AdapterSpecTest(unittest.TestCase):
    def test_commandes_epinglees(self):
        self.assertEqual(login.ADAPTERS["claude"].argv,
                         ("claude", "auth", "login", "--claudeai"))
        self.assertEqual(login.ADAPTERS["codex"].argv, ("codex", "login", "--device-auth"))
        gh = login.ADAPTERS["gh"].argv
        self.assertEqual(gh[:3], ("gh", "auth", "login"))
        self.assertIn("--web", gh)
        self.assertIn("github.com", gh)
        # facturation API et stockage non sûr : jamais transmis
        self.assertIn("--console", login.ADAPTERS["claude"].refused)
        self.assertIn("setup-token", login.ADAPTERS["claude"].refused)
        self.assertIn("--clipboard", login.ADAPTERS["gh"].refused)
        self.assertIn("--insecure-storage", login.ADAPTERS["gh"].refused)

    def test_check_extra_refuse_les_options_ecartees(self):
        self.assertEqual(login.check_extra(login.ADAPTERS["gh"], ("--web",)), ("--web",))
        for harness, option in (("claude", "--console"), ("claude", "--console=true"),
                                ("gh", "--insecure-storage"), ("gh", "--clipboard"),
                                ("codex", "--api-key")):
            with self.assertRaises(login.LoginError) as caught:
                login.check_extra(login.ADAPTERS[harness], (option,))
            self.assertEqual(caught.exception.category, "flow_unrecognized")

    def test_versions_epinglees(self):
        gh = login.ADAPTERS["gh"]
        self.assertTrue(login.supports(gh, "2.102.0"))
        self.assertTrue(login.supports(gh, "2.102"))
        self.assertFalse(login.supports(gh, "2.101.9"))
        self.assertFalse(login.supports(gh, "2.102.0-rc.1"))
        self.assertFalse(login.supports(gh, ""))
        self.assertFalse(login.supports(gh, None))


class FlowTest(unittest.TestCase):
    def test_scan_flow_attend_la_sortie_complete(self):
        gh = login.ADAPTERS["gh"]
        partial = "First copy your one-time code: WXYZ-9876\nOpen https://github.com/login/device"
        self.assertIsNone(login.scan_flow(gh, "2.102.0", "Open this URL: https://github.com"))
        flow = login.scan_flow(gh, "2.102.0", partial)
        self.assertIsNotNone(flow)
        self.assertEqual(flow.code, "WXYZ-9876")
        self.assertEqual(flow.url, "https://github.com/login/device")
        # un flux incomplet (URL sans code) n'est pas encore reconnu
        self.assertIsNone(login.scan_flow(gh, "2.102.0", "https://github.com/login/device"))

    def test_scan_flow_refuse_un_hote_non_officiel(self):
        gh = login.ADAPTERS["gh"]
        with self.assertRaises(login.LoginError) as caught:
            login.scan_flow(gh, "2.102.0", "code WXYZ-9876 https://evil.example/login")
        self.assertEqual(caught.exception.category, "flow_unrecognized")

    def test_scan_flow_refuse_une_version_non_epinglee(self):
        with self.assertRaises(login.LoginError) as caught:
            login.scan_flow(login.ADAPTERS["gh"], "2.101.0", "https://github.com/login/device")
        self.assertEqual(caught.exception.category, "unsupported_version")

    def test_parse_flow_exige_un_flux_complet(self):
        with self.assertRaises(login.LoginError) as caught:
            login.parse_flow(login.ADAPTERS["gh"], "2.102.0", "rien a voir")
        self.assertEqual(caught.exception.category, "flow_unrecognized")

    def test_claude_est_un_collage_en_retour_sans_code_imprime(self):
        flow = login.scan_flow(login.ADAPTERS["claude"], "2.1.296",
                               "Open https://claude.ai/oauth/authorize?client_id=x")
        self.assertIsNotNone(flow)
        self.assertEqual(flow.code, "")

    def test_repr_ne_montre_ni_url_ni_code(self):
        flow = login.Flow(url="https://github.com/login/device", code="WXYZ-9876")
        self.assertEqual(repr(flow), "Flow(<private>)")
        request = login.Request(id="lgn_" + "a" * 26, host="h", harness="gh",
                                account="default", home="/home/x", version="2.102.0",
                                created=1.0, expires=2.0, state=login.VIEWING,
                                login_id="lgn_" + "a" * 26)
        shown = repr(request) + str(request.describe())
        self.assertNotIn("/home/x", shown)
        self.assertNotIn("https://", shown)


class EnvTest(unittest.TestCase):
    def test_environnement_epure(self):
        base = {"PATH": "/bin", "HOME": "/tmp/x", "GH_TOKEN": "secret",
                "GITHUB_TOKEN": "secret", "ANTHROPIC_API_KEY": "sk-secret",
                "OPENAI_API_KEY": "sk-secret", "BROWSER": "firefox"}
        env = login.login_env(login.ADAPTERS["gh"], "/srv/gh", base)
        self.assertEqual(env["PATH"], "/bin")
        self.assertEqual(env["GH_CONFIG_DIR"], "/srv/gh")
        self.assertEqual(env["NO_COLOR"], "1")
        for name in ("GH_TOKEN", "GITHUB_TOKEN", "ANTHROPIC_API_KEY",
                     "OPENAI_API_KEY", "BROWSER"):
            self.assertNotIn(name, env)
        # la base n'est jamais modifiée
        self.assertIn("GH_TOKEN", base)
        # le dossier de chaque harnais
        self.assertEqual(login.login_env(login.ADAPTERS["claude"], "/srv/claude", base)
                         ["CLAUDE_CONFIG_DIR"], "/srv/claude")
        self.assertEqual(login.login_env(login.ADAPTERS["codex"], "/srv/codex", base)
                         ["CODEX_HOME"], "/srv/codex")


class FakeCliTest(unittest.TestCase):
    """Banc commun : scripts factices et superviseur instrumenté."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="l232d-oauth-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.home = os.path.join(self.dir, "home")
        os.makedirs(self.home, mode=0o700)
        self.spawned: list = []
        self.clock = Clock()

    def cli(self, name: str, body: str) -> str:
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\n" + body + "\n")
        os.chmod(path, stat.S_IRWXU)
        return path

    def environ(self) -> dict:
        return {"PATH": self.dir + ":" + os.environ.get("PATH", ""),
                "HOME": self.home, "GH_TOKEN": "canary-token"}

    def supervisor(self, publish=None, health=None, grace: float = login.TERMINATE_GRACE_S):
        def spawn(argv, **kwargs):
            proc = subprocess.Popen(argv, **kwargs)
            self.spawned.append(proc)
            return proc

        return login.Supervisor(registry=login.Registry(clock=self.clock), clock=self.clock,
                                spawn=spawn, publish=publish or Sink(),
                                health=health or (lambda request: "healthy"), grace=grace)

    def begin(self, supervisor, harness: str = "gh", version: str = "2.102.0",
              account: str = "default", **kwargs):
        return supervisor.begin(host="ameesh-agents-1", harness=harness, account=account,
                                home=self.home, version=version, approver="human:alice",
                                requested_by="agent:impl-04", environ=self.environ(), **kwargs)

    def dead(self, proc) -> bool:
        return proc.poll() is not None


class SupervisorTest(FakeCliTest):
    def test_gh_publie_le_flux_prive_puis_done(self):
        self.cli("gh", "printf 'First copy your one-time code: WXYZ-9876\\n"
                       "Open this URL: https://github.com/login/device\\n'; sleep 30")
        sink = Sink()
        sup = self.supervisor(publish=sink)
        request = self.begin(sup)
        self.assertTrue(wait_for(lambda: request.state == login.VIEWING),
                        "state=%s category=%s" % (request.state, request.category))
        self.assertEqual(len(sink.payloads), 1)
        payload = sink.payloads[0]
        self.assertEqual(payload["url"], "https://github.com/login/device")
        self.assertEqual(payload["code"], "WXYZ-9876")
        self.assertEqual(payload["approver"], "human:alice")
        self.assertEqual(payload["host"], "ameesh-agents-1")
        self.assertEqual(len(str(request.describe())), len(str(request.describe())))
        self.assertNotIn("WXYZ-9876", str(request.describe()) + repr(request))
        self.assertNotIn("https://", str(request.describe()) + repr(request))
        # `provider_exp` est borné par le service : la demande porte son échéance
        self.assertLessEqual(payload["provider_exp"], int(request.expires) + 1)
        done = sup.finish(request.id, "done")
        self.assertEqual(done.state, login.DONE)
        self.assertEqual(done.category, "healthy")
        self.assertTrue(wait_for(lambda: self.dead(self.spawned[0])))
        self.assertEqual(sup.registry.live(), [])

    def test_une_seule_demande_par_hote_harnais_compte(self):
        self.cli("gh", "printf 'WXYZ-9876 https://github.com/login/device\\n'; sleep 30")
        sup = self.supervisor()
        first = self.begin(sup)
        with self.assertRaises(login.LoginError) as caught:
            self.begin(sup, account="default")
        self.assertEqual(caught.exception.category, "already_pending")
        sup.finish(first.id, "cancel")
        second = self.begin(sup)
        self.assertNotEqual(first.id, second.id)
        sup.finish(second.id, "cancel")

    def test_annulation_avant_tout_flux(self):
        self.cli("gh", "sleep 30")
        sink = Sink()
        sup = self.supervisor(publish=sink)
        request = self.begin(sup)
        done = sup.finish(request.id, "cancel")
        self.assertEqual((done.state, done.category), (login.CANCELLED, "cancelled"))
        self.assertEqual(sink.payloads, [])
        self.assertTrue(wait_for(lambda: self.dead(self.spawned[0])))

    def test_flux_non_reconnu(self):
        self.cli("gh", "printf 'nothing to see here\\n'; exit 2")
        sup = self.supervisor()
        request = self.begin(sup)
        self.assertTrue(wait_for(lambda: request.state == login.FAILED))
        self.assertEqual(request.category, "flow_unrecognized")

    def test_refus_fournisseur_dans_la_sortie(self):
        self.cli("gh", "printf 'not logged in\\n'; exit 1")
        sup = self.supervisor()
        request = self.begin(sup)
        self.assertTrue(wait_for(lambda: request.state == login.FAILED))
        self.assertEqual(request.category, "provider_refused")

    def test_sortie_trop_longue_refusee(self):
        self.cli("gh", "dd if=/dev/zero bs=1024 count=96 2>/dev/null | tr '\\0' x; sleep 30")
        sup = self.supervisor()
        sup.capture_max = 8 * 1024
        request = self.begin(sup)
        self.assertTrue(wait_for(lambda: request.state == login.FAILED))
        self.assertEqual(request.category, "flow_unrecognized")
        self.assertTrue(wait_for(lambda: self.dead(self.spawned[0])))

    def test_expiration_tue_et_efface(self):
        self.cli("gh", "printf 'WXYZ-9876 https://github.com/login/device\\n'; sleep 30")
        sink = Sink()
        sup = self.supervisor(publish=sink)
        request = self.begin(sup, ttl=10)
        self.assertTrue(wait_for(lambda: request.state == login.VIEWING))
        self.clock.now += 11
        expired = sup.reap()
        self.assertEqual([r.id for r in expired], [request.id])
        self.assertEqual((request.state, request.category), (login.EXPIRED, "expired"))
        self.assertTrue(wait_for(lambda: self.dead(self.spawned[0])))

    def test_done_sans_sante_est_un_refus(self):
        self.cli("gh", "printf 'WXYZ-9876 https://github.com/login/device\\n'; sleep 30")
        sup = self.supervisor(health=lambda request: "failing")
        request = self.begin(sup)
        self.assertTrue(wait_for(lambda: request.state == login.VIEWING))
        done = sup.finish(request.id, "done")
        self.assertEqual((done.state, done.category), (login.DONE, "provider_refused"))

    def test_un_processus_qui_ignore_sigterm_est_tue(self):
        self.cli("gh", "printf 'WXYZ-9876 https://github.com/login/device\\n'; "
                       "trap '' TERM; sleep 30")
        sup = self.supervisor(grace=0.2)
        request = self.begin(sup)
        self.assertTrue(wait_for(lambda: request.state == login.VIEWING))
        started = time.monotonic()
        sup.finish(request.id, "cancel")
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertTrue(wait_for(lambda: self.dead(self.spawned[0])))
        self.assertEqual(self.spawned[0].returncode, -9)

    def test_service_de_defi_en_panne(self):
        self.cli("gh", "printf 'WXYZ-9876 https://github.com/login/device\\n'; sleep 30")
        sup = self.supervisor(publish=Sink(answer=False))
        request = self.begin(sup)
        self.assertTrue(wait_for(lambda: request.state == login.FAILED))
        self.assertEqual(request.category, "failed")
        self.assertTrue(wait_for(lambda: self.dead(self.spawned[0])))

    def test_paste_back_refuse_sans_canal_prive(self):
        sup = self.supervisor()
        with self.assertRaises(login.LoginError) as caught:
            self.begin(sup, harness="claude", version="2.1.296")
        self.assertEqual(caught.exception.category, "flow_unrecognized")
        self.assertEqual(sup.registry.live(), [])
        self.assertEqual(self.spawned, [])

    def test_version_non_epinglee_refusee_avant_tout_processus(self):
        sup = self.supervisor()
        with self.assertRaises(login.LoginError) as caught:
            self.begin(sup, version="2.101.0")
        self.assertEqual(caught.exception.category, "unsupported_version")
        self.assertEqual(sup.registry.live(), [])
        self.assertEqual(self.spawned, [])

    def test_aucun_adaptateur_pour_un_harnais_inconnu(self):
        sup = self.supervisor()
        with self.assertRaises(login.LoginError) as caught:
            self.begin(sup, harness="dsh")
        self.assertEqual(caught.exception.category, "unsupported_version")

    def test_le_jeton_herite_ne_part_pas_au_cli(self):
        self.cli("gh", "if [ -n \"$GH_TOKEN\" ]; then printf 'token inherited\\n'; exit 4; fi; "
                       "printf 'WXYZ-9876 https://github.com/login/device\\n'; sleep 30")
        sup = self.supervisor()
        request = self.begin(sup)
        self.assertTrue(wait_for(lambda: request.state == login.VIEWING),
                        "state=%s category=%s" % (request.state, request.category))

    def test_decision_inconnue_refusee(self):
        self.cli("gh", "printf 'WXYZ-9876 https://github.com/login/device\\n'; sleep 30")
        sup = self.supervisor()
        request = self.begin(sup)
        with self.assertRaises(login.LoginError) as caught:
            sup.finish(request.id, "maybe")
        self.assertEqual(caught.exception.category, "format")
        sup.finish(request.id, "cancel")

    def test_une_demande_close_ne_se_rejoue_pas(self):
        self.cli("gh", "printf 'WXYZ-9876 https://github.com/login/device\\n'; sleep 30")
        sup = self.supervisor()
        request = self.begin(sup)
        sup.finish(request.id, "cancel")
        with self.assertRaises(login.LoginError):
            sup.finish(request.id, "cancel")

    def test_attente_du_recu_puis_decision(self):
        self.cli("gh", "printf 'WXYZ-9876 https://github.com/login/device\\n'; sleep 30")
        sup = self.supervisor()
        request = self.begin(sup)
        self.assertTrue(wait_for(lambda: request.state == login.VIEWING))
        seen = []

        def fetch(login_id):
            seen.append(login_id)
            if len(seen) < 3:
                return "pending", {"login_id": login_id}
            return "signed", {"v": 1}

        done = login.wait_for_decision(sup, request, fetch=fetch, verify=lambda receipt: "done",
                                       timeout=5.0, interval=0.01)
        self.assertEqual((done.state, done.category), (login.DONE, "healthy"))
        self.assertEqual(seen, [request.login_id] * 3)
        self.assertTrue(wait_for(lambda: self.dead(self.spawned[0])))

    def test_attente_du_recu_expire_au_service(self):
        self.cli("gh", "printf 'WXYZ-9876 https://github.com/login/device\\n'; sleep 30")
        sup = self.supervisor()
        request = self.begin(sup)
        self.assertTrue(wait_for(lambda: request.state == login.VIEWING))
        done = login.wait_for_decision(sup, request, fetch=lambda login_id: ("expired", {}),
                                       verify=lambda receipt: "done", timeout=5.0)
        self.assertEqual((done.state, done.category), (login.EXPIRED, "expired"))
        self.assertTrue(wait_for(lambda: self.dead(self.spawned[0])))

    def test_attente_du_recu_echue_localement(self):
        self.cli("gh", "printf 'WXYZ-9876 https://github.com/login/device\\n'; sleep 30")
        sup = self.supervisor()
        request = self.begin(sup)
        self.assertTrue(wait_for(lambda: request.state == login.VIEWING))
        moments = [100.0]

        def monotonic():
            moments[0] += 10.0
            return moments[0]

        done = login.wait_for_decision(sup, request, fetch=lambda login_id: ("pending", {}),
                                       verify=lambda receipt: "done", timeout=5.0,
                                       monotonic=monotonic, sleep=lambda seconds: None)
        self.assertEqual((done.state, done.category), (login.EXPIRED, "expired"))
        self.assertTrue(wait_for(lambda: self.dead(self.spawned[0])))

    def test_attente_du_recu_refus_de_verification(self):
        self.cli("gh", "printf 'WXYZ-9876 https://github.com/login/device\\n'; sleep 30")
        sup = self.supervisor()
        request = self.begin(sup)
        self.assertTrue(wait_for(lambda: request.state == login.VIEWING))

        def verify(receipt):
            raise login.LoginError("LOGIN_MISMATCH", "the challenge is for another host")

        with self.assertRaises(login.LoginError):
            login.wait_for_decision(sup, request, fetch=lambda login_id: ("signed", {}),
                                    verify=verify, timeout=5.0)
        # un reçu non vérifié ne clôt pas la demande : l'appelant décide
        self.assertEqual(request.state, login.VIEWING)
        sup.finish(request.id, "cancel")


class ReceiptTest(unittest.TestCase):
    @staticmethod
    def request(**overrides):
        fields = {"id": "lgn_" + "c" * 26, "host": "ameesh-agents-1", "harness": "gh",
                  "account": "default", "home": "/home/x", "version": "2.102.0",
                  "created": 1.0, "expires": 2.0, "state": login.VIEWING,
                  "login_id": "lgn_" + "d" * 26, "scope": "git", "service_exp": 1234}
        fields.update(overrides)
        return login.Request(**fields)

    def test_expected_login_recalcule_depuis_notre_demande(self):
        request = self.request()
        self.assertEqual(login.expected_login(request),
                         {"login_id": request.login_id, "host": "ameesh-agents-1",
                          "provider": "gh", "account": "default", "scope": "git",
                          "exp": 1234})

    def test_receipt_decision_lie_et_consomme(self):
        from ameesh import receipts
        seen = {}

        class Verdict:
            ok = True
            code = "OK"
            reason = ""
            decision = "cancel"

        def fake(db, receipt, policy=None, **kwargs):
            seen.update(kwargs)
            seen["db"] = db
            seen["policy"] = policy
            return Verdict()

        original = receipts.verify_receipt
        receipts.verify_receipt = fake
        try:
            decision = login.receipt_decision(object(), self.request(), {"v": 1},
                                              policy="policy", consume_by="agent:impl-04")
        finally:
            receipts.verify_receipt = original
        self.assertEqual(decision, "cancel")
        self.assertEqual(seen["kind"], "login")
        self.assertEqual(seen["consume_by"], "agent:impl-04")
        self.assertEqual(seen["expect_decision"], None)
        self.assertEqual(seen["expected_login"]["login_id"], "lgn_" + "d" * 26)
        self.assertEqual(seen["expected_login"]["exp"], 1234)
        self.assertEqual(seen["policy"], "policy")

    def test_receipt_decision_refuse(self):
        from ameesh import receipts

        class Verdict:
            ok = False
            code = "LOGIN_MISMATCH"
            reason = "the challenge is for another host"
            decision = ""

        original = receipts.verify_receipt
        receipts.verify_receipt = lambda *args, **kwargs: Verdict()
        try:
            with self.assertRaises(login.LoginError) as caught:
                login.receipt_decision(object(), self.request(), {"v": 1},
                                       consume_by="agent:impl-04")
        finally:
            receipts.verify_receipt = original
        self.assertEqual(caught.exception.category, "LOGIN_MISMATCH")

    def test_publisher_transmet_scope_et_echeance(self):
        calls = []

        class Client:
            @staticmethod
            def create_login_request(cfg, **kwargs):
                calls.append((cfg, kwargs))
                return {"login_id": "lgn_" + "e" * 26, "link": "https://approve.example/l"}

        publish = login.publisher(Client, "cfg", approver="human:alice",
                                  requested_by="agent:impl-04", scope="defaut")
        answer = publish({"host": "h", "provider": "gh", "account": "default",
                          "url": "https://github.com/login/device", "code": "WXYZ-9876",
                          "scope": "", "provider_exp": 1234})
        self.assertEqual(answer["login_id"], "lgn_" + "e" * 26)
        cfg, kwargs = calls[0]
        self.assertEqual(cfg, "cfg")
        self.assertEqual(kwargs["scope"], "defaut")
        self.assertEqual(kwargs["provider_exp"], 1234)
        self.assertEqual(kwargs["approver"], "human:alice")
        self.assertEqual(kwargs["requested_by"], "agent:impl-04")


class RegistryTest(unittest.TestCase):
    def test_describe_ne_porte_que_des_champs_publics(self):
        request = login.Request(id="lgn_" + "a" * 26, host="h", harness="gh",
                                account="default", home="/home/x", version="2.102.0",
                                created=1.0, expires=2.0, state=login.VIEWING,
                                login_id="lgn_" + "b" * 26, link="https://approve.example/t")
        self.assertEqual(sorted(request.describe()),
                         ["account", "category", "expires", "harness", "host", "id",
                          "login_id", "pid", "scope", "state", "version"])
        self.assertNotIn("link", request.describe())


if __name__ == "__main__":
    unittest.main()
