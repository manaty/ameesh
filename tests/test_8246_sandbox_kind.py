# SPDX-License-Identifier: AGPL-3.0-only
"""#8246 TPK #2202 S6a — qualification de bout en bout du chemin sandbox sur kind.

Le module ne s'exécute sur un cluster que si `AMEESH_SANDBOX_KIND=1` (voir
`deploy/k8s/sandbox-witness/preuve-sandbox-kind.sh`). Sans ce drapeau, seuls
les témoins sans cluster tournent : le contrat du témoin embarqué et la
relecture du canon à la resynchronisation. Aucun staging, aucun canon réel,
aucun secret réel : le canon, la base, le relais et le Job sont jetables.

Le parcours qualifié est celui de l'étude (§1, §3–§10) :

1. un canon de test porte les blocs `sandbox` d'une carte Agent et d'une
   carte Host ; l'admission S2 en déduit le profil, le placement et l'image ;
2. S3 réserve la tranche ET l'enveloppe du compte, puis écrit l'intention
   `sandbox_turns` avant tout Job ;
3. le superviseur S4 (processus enfant réel) crée un Job Kubernetes réel par
   l'interface `KubernetesJobProvider`, avec un jeton de relais lié au tour
   (S4d-3) ;
4. le Job écrit un résultat `ameesh-sandbox-result/1` ; S1 l'accepte une seule
   fois (`ameesh_sandbox_complete`) ;
5. le reçu `ameesh-sandbox-receipt/1` se relit par `ameesh sandbox show`,
   `/api/v1/sandbox/turns/{turn}` et la composition du tour.

Cas d'acceptation de l'étude : annulation avant résultat (`revoked`),
redémarrage du superviseur avec adoption du Job, bail perdu (`stale`),
résultat en double (`duplicate`), Job orphelin (alerte `sandbox_orphan`).
"""
from __future__ import annotations

import dataclasses
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

#: `tests.support` retire à l'import toute variable `AMEESH_*` de la session
#: (isolation du banc, L142) : la configuration de la preuve kind est donc lue
#: AVANT ce module, puis conservée en constantes. Sans cela le drapeau
#: `AMEESH_SANDBOX_KIND=1` du lanceur était effacé et les cas kind sautés.
_KIND_ENV = {_name: os.environ.get(_name, "") for _name in (
    "AMEESH_SANDBOX_KIND", "AMEESH_KIND_KUBECONFIG", "AMEESH_KIND_IMAGE",
    "AMEESH_KIND_NAMESPACE", "AMEESH_KIND_RELAY_HOST", "AMEESH_KIND_RELAY",
    "AMEESH_KIND_TURN_WITNESS")}

from ameesh import (  # noqa: E402
    canon,
    exploitation,
    jcs,
    placement,
    registry,
    relay,
    sandbox_observation,
    sandbox_relay,
    serve,
    storage,
    turn_context,
)
from ameesh.sandbox_provider import (
    JobIdentity,
    JobSpec,
    ProviderUnavailable,
    job_name,
)
from ameesh.sandbox_supervisor import SandboxSupervisor
from ameesh.sandbox_turn import SandboxTurn
from ameesh.storage.postgres import sandbox as s1
from ameesh.storage.postgres import turn_admission as s3

from . import test_sandbox_canon as cards
from .support import PgTestCase

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WITNESS_PATH = _KIND_ENV["AMEESH_KIND_TURN_WITNESS"] or os.path.join(
    ROOT, "deploy", "k8s", "sandbox-witness", "turn-witness.py")

AGENT = "kind-reviewer"
HOST = "tpk-stg-reviewers"
TRANCHE = "tpk-migration-2026-10"
OWNER = "kind-owner"
EPOCH = 7
REQUEST_ID = 21
RESERVED_USD = 2
BILLING_ACCOUNT = "provider"
SOURCE = {"release": "kind-qualification", "commit": "b" * 40,
          "contract": "ameesh-sandbox/1"}

KIND = _KIND_ENV["AMEESH_SANDBOX_KIND"] == "1"
KUBECONFIG = _KIND_ENV["AMEESH_KIND_KUBECONFIG"]
IMAGE = _KIND_ENV["AMEESH_KIND_IMAGE"]
NAMESPACE = _KIND_ENV["AMEESH_KIND_NAMESPACE"] or "sandbox-witness"
RELAY_HOST = _KIND_ENV["AMEESH_KIND_RELAY_HOST"]
#: Le Job appelle le relais quand le pilote de la preuve a une route
#: pod -> hôte (runner GitHub rootful). En rootless (slirp4netns,
#: --disable-host-loopback), le chemin réseau n'existe pas : le Job ne fait
#: alors aucun appel et S3 garde la réservation `kept` (coût inconnu), ce que
#: la qualification vérifie explicitement. Le jeton reste qualifié dans les
#: deux cas.
JOB_RELAY = _KIND_ENV["AMEESH_KIND_RELAY"] == "1"
API_TOKEN = "t" * 43


def witness_module():
    """Load the in-Job witness as a module (no ameesh import, stdlib only)."""
    spec = importlib.util.spec_from_file_location("ameesh_kind_turn_witness", WITNESS_PATH)
    module = importlib.util.module_from_spec(spec)
    # Le témoin vit dans le dépôt : ne pas y laisser de `__pycache__`.
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


def read_witness():
    with open(WITNESS_PATH, encoding="utf-8") as stream:
        return stream.read()


class _RelayUpstream:
    """Faux fournisseur local (protocole Messages, réponse non diffusée)."""

    def __init__(self):
        provider = self
        self.requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                provider.requests.append(json.loads(raw))
                payload = json.dumps({
                    "id": "msg-kind",
                    "type": "message",
                    "model": "deepseek-flash",
                    "content": [{"type": "text", "text": "RELAIS-OK"}],
                    "usage": {"input_tokens": 1200, "cache_read_input_tokens": 0,
                              "output_tokens": 300},
                }).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:%d/anthropic" % self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@dataclasses.dataclass
class _Admission:
    """Everything the supervisor and the Job need from one admitted turn."""

    row: dict
    adapter: SandboxTurn
    spec: JobSpec
    options: dict
    token: str


class CanonResyncTest(cards.SandboxCase):
    """La révocation du canon est la seule révocation (aucun second geste)."""

    def admitted(self):
        commit = self.commit()
        loaded, findings = self.load(trusted=True)
        self.assertNoError(findings)
        verdict = placement.sandbox_admission(
            loaded, "tpk-reviewer", HOST, canon_ref=commit, reserve_usd=2)
        self.assertTrue(verdict.ok, verdict.diagnostic)
        return loaded, verdict

    def test_revoked_profile_refuses_new_and_fences_admitted_turn(self):
        _, admitted = self.admitted()
        self.write("agents/tpk-reviewer.md", cards.agent_card(status="revoked"))
        commit = self.commit()
        reloaded, _ = self.load(trusted=True)
        drift = placement.sandbox_recheck(reloaded, admitted, canon_ref=commit)
        self.assertEqual(drift.verdict, "revoked")

    def test_changed_grant_requests_cancellation_not_a_silent_rewrite(self):
        _, admitted = self.admitted()
        self.write("hotes/tpk-stg-reviewers.md",
                   cards.host_card(active_deadline=2400))
        commit = self.commit()
        reloaded, _ = self.load(trusted=True)
        drift = placement.sandbox_recheck(reloaded, admitted, canon_ref=commit)
        self.assertEqual(drift.verdict, "changed")
        self.assertIn("limits", drift.changed)

    def test_cosmetic_change_keeps_the_snapshot(self):
        _, admitted = self.admitted()
        # A comment outside the card digest is not a grant change; the card's
        # digest is what admission records, so a pure description edit below
        # must keep profile_digest/placement_digest equal.
        with open(os.path.join(self.root, "agents/tpk-reviewer.md"),
                  encoding="utf-8") as stream:
            text = stream.read()
        self.write("agents/tpk-reviewer.md", text + "\n<!-- note -->\n")
        commit = self.commit()
        reloaded, _ = self.load(trusted=True)
        drift = placement.sandbox_recheck(reloaded, admitted, canon_ref=commit)
        self.assertEqual(drift.verdict, "same")


class WitnessContractTest(unittest.TestCase):
    """The in-Job canonicalisation must be exactly S1's JCS digest."""

    def test_canonical_matches_jcs(self):
        module = witness_module()
        envelope = {
            "schema": "ameesh-sandbox-result/1",
            "turn_id": "sbx-1",
            "epoch": 7,
            "scope": {"schema": "ameesh-sandbox-scope/1", "pr": 8246,
                      "head": "a" * 40, "attempt": 1, "generation": 1},
            "scope_digest": "sha256:" + "c" * 64,
            "result_id": "kind-sbx-1",
            "outcome": "accepted",
            "verdict": "green",
            "findings": [{"severity": "N", "code": "kind-witness",
                          "summary": "accents é à ü"}],
            "evidence": [{"kind": "check", "name": "sandbox-kind",
                          "conclusion": "success"}],
            "usage": {"turns": 1, "tool_calls": 1, "seconds": 1, "model_calls": 1},
            "cost": {"usd": 0},
            "sandbox_session": None,
            "source": dict(SOURCE),
            "finished_at": "2026-11-24T17:02:31Z",
        }
        self.assertEqual(module.canonical(envelope).encode("utf-8"),
                         jcs.canonicalize(envelope, doubles=True))

    def test_witness_reads_only_the_turn_secret(self):
        text = read_witness()
        self.assertIn('TURN_DIR = "/run/ameesh/turn"', text)
        self.assertNotIn("AMEESH_DSN", text)
        self.assertNotIn("import ameesh", text)
        self.assertNotIn("KUBECONFIG", text)
        # No provider key or kube token may be looked up by the Job.
        self.assertNotIn("API_KEY", text)
        self.assertNotIn("serviceaccount", text.lower())


@unittest.skipUnless(KIND, "kind sandbox qualification: set AMEESH_SANDBOX_KIND=1")
class KindSandboxTest(PgTestCase):
    """One real kind cluster, one real Job per test, one disposable schema."""

    def setUp(self):
        super().setUp()
        missing = [name for name, value in (
            ("AMEESH_KIND_KUBECONFIG", KUBECONFIG),
            ("AMEESH_KIND_IMAGE", IMAGE),
            ("AMEESH_KIND_TURN_WITNESS", WITNESS_PATH),
        ) if not value]
        if JOB_RELAY and not RELAY_HOST:
            missing.append("AMEESH_KIND_RELAY_HOST")
        if missing:
            self.fail("kind qualification is incomplete: %s" % ", ".join(missing))
        for table in ("tranche_relay_requests", "sandbox_results", "sandbox_turns",
                      "tranche_events", "tranche_reservations", "tranche_limits",
                      "account_reservations", "account_budgets", "turn_costs",
                      "spend_pending", "turn_resources"):
            self.db.execute("delete from " + table)
        self.turn_id = uuid.uuid4().hex
        self.spend_key = "spend-" + self.turn_id
        registry.upsert(self.db, AGENT, chantier="qualification",
                        harness="deepseek", host=HOST)
        self.db.execute(
            "update agent_registry set lease_owner=%s, lease_epoch=%s,"
            " lease_expires_at=now() + interval '1 hour', status='running',"
            " session_id='native-kind-session', turns=0 where name=%s",
            (OWNER, EPOCH, AGENT))
        storage.of(self.db).budgets.account_put(BILLING_ACCOUNT, 100,
                                                actor="human:qualification")
        s3.tranche_create(self.db, TRANCHE, "qualification", 50,
                          starts_at="2026-01-01", actor="human:qualification",
                          admitted_ref="decision:120571")
        registry.pending_spend_begin(self.db, AGENT, 0, "sandbox", "deepseek-flash",
                                     work_item_id=8246, turn_id=self.turn_id)
        storage.of(self.db).turn_resources.open_turn(
            self.turn_id, AGENT, HOST, pgid=None, label="sandbox kind")

        self.canon_root = tempfile.mkdtemp(prefix="kind-canon-", dir=self.tmp)
        self.write_canon()
        self.loaded = canon.load(self.canon_root, ref="HEAD")
        findings = [f for f in canon.validate(self.loaded) if f.severity == canon.ERROR]
        self.assertEqual(findings, [], [f.to_dict() for f in findings])
        self.host_card = self.loaded.host(HOST)
        self.admitted = placement.sandbox_admission(
            self.loaded, AGENT, HOST,
            canon_ref=self.loaded.sources[0].commit, reserve_usd=RESERVED_USD)
        self.assertTrue(self.admitted.ok, self.admitted.diagnostic)

        self.events = []
        self.upstream = _RelayUpstream()
        self.addCleanup(self.upstream.close)
        self.relay_url = self.start_relay()

    # -- fixtures ---------------------------------------------------------
    def write_canon(self):
        self.write_card("membres/smichea.md", cards.MEMBER)
        self.write_card("agents/kind-reviewer.md", cards.agent_card().replace(
            "title: tpk-reviewer", "title: kind-reviewer", 1))
        self.write_card("hotes/tpk-stg-reviewers.md", cards.host_card(
            kubeconfig=KUBECONFIG, namespace=NAMESPACE, image=IMAGE))
        self.write_card("placements/kind-reviewer.md", cards.PLACEMENT.replace(
            "tpk-reviewer-tpk-stg-reviewers", "kind-reviewer-tpk-stg-reviewers").replace(
            "agent: tpk-reviewer", "agent: %s" % AGENT))
        cards.git(self.canon_root, "init", "-q")
        cards.git(self.canon_root, "add", "-A")
        cards.git(self.canon_root, "commit", "-q", "--allow-empty", "-m", "canon kind")
        self.canon_commit = cards.git(self.canon_root, "rev-parse", "HEAD")

    def write_card(self, relative, text):
        path = os.path.join(self.canon_root, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as stream:
            stream.write(text)

    def start_relay(self):
        bind = "0.0.0.0:0" if JOB_RELAY else "127.0.0.1:0"
        self.relay_instance = relay.Relay(
            verifier=sandbox_relay.compose(self.db, relay.StaticTokens({})),
            policy=relay.StaticPolicy({AGENT: ["deepseek-flash"]}),
            db=self.db,
            cfg=dataclasses.replace(self.cfg, budget_usd_per_hour=10.0),
            secrets=relay.ServerSecrets(
                {"DEEPSEEK_API_KEY": "kind-qualification-server-key"}),
            config=relay.RelayConfig(upstreams={"deepseek": self.upstream.url},
                                     max_tokens=4096, max_body=64 * 1024),
            prices={"deepseek-flash": [0.27, 0.07, 1.10]})
        server = relay.make_server(self.relay_instance, bind)
        threading.Thread(target=server.serve_forever, daemon=True).start()

        def stop():
            server.shutdown()
            server.server_close()
        self.addCleanup(stop)
        if not JOB_RELAY:
            return ""
        return "http://%s:%d" % (RELAY_HOST, server.server_address[1])

    def context_adapter(self):
        marker = registry.pending_spend_get(self.db, AGENT)
        request = dict(id=REQUEST_ID, sender="qualification", kind="mail",
                       created_ts=time.time(), delivered_ts=None,
                       work_item_id=8246, body="sandbox kind qualification",
                       payload={"schema": "ameesh-sandbox-request/1",
                                "scope": self.scope()})
        document = turn_context.build(
            turn_id=self.turn_id, agent=AGENT, host=HOST, kind="mail",
            started_ts=time.time(), session=None, work_item_id=8246,
            lease_owner=OWNER, lease_epoch=EPOCH,
            lease_expires_ts=time.time() + 3600, requests=[request],
            harness="deepseek", model="deepseek-flash", budget_usd=100.0,
            spent_usd=0.0, spend_key=self.spend_key, marker=marker,
            reservation=None)
        filename = turn_context.write(self.tmp, document, secrets=[])
        environ = dict(os.environ, **{
            turn_context.ENV: filename, "AMEESH_TURN_ID": self.turn_id,
            "AGENT_MAIL_NAME": AGENT, "AMEESH_RUNNER_ID": OWNER,
            "AMEESH_LEASE_EPOCH": str(EPOCH)})
        return SandboxTurn.current(self.db, environ=environ)

    @staticmethod
    def scope():
        return {"schema": "ameesh-sandbox-scope/1",
                "application": "github:example/qualification",
                "repository": "example/qualification", "pr": 8246,
                "head": "a" * 40, "base": "main", "generation": 1}

    def snapshot(self):
        limits = dict(self.admitted.limits, profile_label=self.admitted.profile,
                      placement_label=HOST)
        return dict(
            scope=self.scope(), max_attempts=self.admitted.limits["max_attempts"],
            profile_ref=self.admitted.profile_ref,
            profile_digest=self.admitted.profile_digest,
            placement_ref=self.admitted.placement_ref,
            placement_digest=self.admitted.placement_digest,
            canon_ref=self.admitted.canon_ref,
            limits=limits, image_digest=self.admitted.image_digest,
            provider=self.admitted.provider, namespace=NAMESPACE,
            source=dict(SOURCE),
            max_concurrent_jobs=self.host_card.policy.sandbox.max_concurrent_jobs)

    def admit(self):
        adapter = self.context_adapter()
        # Un test qui n'appelle pas close() lui-même laisse sinon un enfant
        # superviseur vivant jusqu'à la sortie de l'interpréteur.
        self.addCleanup(adapter.close)
        row = adapter.admit(REQUEST_ID, snapshot=self.snapshot(),
                            billing_account=BILLING_ACCOUNT, tranche_id=TRANCHE)
        self.assertEqual(row["attempt"], 1)
        self.assertEqual(row["scope"], dict(self.scope(), attempt=1))
        self.assertEqual(row["job_name"], job_name(self.turn_id))
        token, identity = sandbox_relay.issue(
            self.db, agent=AGENT, owner=OWNER, epoch=EPOCH,
            turn_id=self.turn_id, host=HOST)
        self.assertEqual(identity.turn, self.turn_id)
        self.assertEqual(sandbox_relay.Tokens(self.db).verify_token(token), identity)
        spec = JobSpec(
            JobIdentity(self.turn_id, AGENT, EPOCH, HOST,
                        self.admitted.profile, NAMESPACE),
            self.admitted.image, self.admitted.active_deadline_seconds,
            self.admitted.ttl_seconds_after_finished,
            argv=("python", "-c", read_witness(), "complete",
                  "relay" if JOB_RELAY else "norelay"))
        options = dict(
            placement=dataclasses.asdict(self.host_card.policy.sandbox),
            turn_seconds=int(self.admitted.limits["turn_seconds"]),
            secrets=dict(prompt=json.dumps(self.job_input(row)),
                         relay_token=token))
        return _Admission(row, adapter, spec, options, token)

    def job_input(self, row):
        deadline = row["deadline_ts"]
        if hasattr(deadline, "timestamp"):
            deadline = deadline.timestamp()
        elif not isinstance(deadline, (int, float)):
            deadline = str(deadline)
        return dict(
            schema="ameesh-sandbox-job/1", turn_id=self.turn_id, epoch=EPOCH,
            owner=OWNER, deadline_ts=deadline,
            image_digest=self.admitted.image_digest, scope=row["scope"],
            scope_digest=row["scope_digest"], limits=self.admitted.limits,
            source=dict(SOURCE), prompt="qualification",
            relay_url=self.relay_url)

    def start(self, admission, *, mode="complete"):
        return admission.adapter.start(
            admission.spec,
            provider_factory="ameesh.runner_sandbox:kubernetes_provider",
            state_dir=self.tmp,
            orphan_after=float(self.admitted.limits["job_seconds"]),
            timeout=float(self.admitted.limits["turn_seconds"]),
            provider_options=admission.options, new_session=True)

    def pump(self, adapter, *, timeout=180, until=("result",)):
        kinds = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            event, row = adapter.step()
            kinds.append(event["kind"])
            if event["kind"] in until or (isinstance(row, dict)
                                          and row.get("state") in s1.TERMINAL):
                return event, row, kinds
            time.sleep(0.5)
        self.fail("sandbox turn did not finish in %ss: %s (row=%r)"
                  % (timeout, kinds, self.db.query(
                      "select state,refusal,job_phase from sandbox_turns where turn_id=%s",
                      (self.turn_id,))))

    # -- inspection -------------------------------------------------------
    def kubectl(self, *args, check=True):
        proc = subprocess.run(
            ["kubectl", "--kubeconfig", KUBECONFIG, *args],
            capture_output=True, text=True, timeout=60)
        if check and proc.returncode:
            self.fail("kubectl %s: %s" % (" ".join(args), proc.stderr.strip()))
        return proc

    def job(self):
        proc = self.kubectl("-n", NAMESPACE, "get", "job",
                            job_name(self.turn_id), "-o", "json", check=False)
        if proc.returncode:
            return None
        return json.loads(proc.stdout)

    def secret(self):
        proc = self.kubectl("-n", NAMESPACE, "get", "secret",
                            job_name(self.turn_id), "-o", "json", check=False)
        if proc.returncode:
            return None
        return json.loads(proc.stdout)

    def row(self):
        return self.db.query("select * from sandbox_turns where turn_id = %s",
                             (self.turn_id,))[0]

    def receipt(self):
        return sandbox_observation.show(self.db, self.turn_id)

    def api(self, path):
        server = serve.make_server(
            serve.Context(cfg=self.cfg, human="human:qualification",
                          organisation="Example"),
            token=API_TOKEN, read=serve.db_reader(self.cfg), port=0)
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            import http.client as httplib
            connection = httplib.HTTPConnection(
                "127.0.0.1", server.server_address[1], timeout=10)
            connection.request("GET", path, headers={
                "Host": "127.0.0.1:%d" % server.server_address[1],
                "Authorization": "Bearer " + API_TOKEN})
            response = connection.getresponse()
            body = response.read()
            connection.close()
            return response.status, body
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)

    # -- assertions -------------------------------------------------------
    def assert_job_contract(self, admission):
        job = self.job()
        self.assertIsNotNone(job, "the Job disappeared before inspection")
        meta = job["metadata"]
        self.assertEqual(meta["name"], job_name(self.turn_id))
        for key, value in admission.spec.identity.labels.items():
            self.assertEqual(meta["labels"].get(key), value)
        pod = job["spec"]["template"]["spec"]
        self.assertFalse(pod.get("automountServiceAccountToken", False))
        container = pod["containers"][0]
        self.assertEqual(container["image"], IMAGE)
        self.assertEqual(container["name"], "turn")
        self.assertFalse(container.get("env"))
        argv = json.dumps(container.get("command") or [])
        self.assertNotIn(admission.token, argv)
        secret = self.secret()
        self.assertIsNotNone(secret)
        data = secret.get("data") or {}
        self.assertEqual(set(data), {"prompt", "relay_token"})
        import base64
        self.assertEqual(base64.b64decode(data["relay_token"]).decode(),
                         admission.token)
        prompt = json.loads(base64.b64decode(data["prompt"]).decode())
        self.assertNotIn(admission.token, json.dumps(prompt))

    def assert_settled(self):
        reservation = self.db.query(
            "select state,usd from tranche_reservations where spend_key=%s",
            (self.spend_key,))[0]
        if not JOB_RELAY:
            self.assertEqual(reservation["state"], "kept")
            self.assertEqual(float(reservation["usd"]), RESERVED_USD)
            self.assertEqual(self.db.query(
                "select * from turn_costs where turn_id=%s", (self.turn_id,)), [])
            return reservation
        self.assertEqual(reservation["state"], "settled")
        self.assertGreater(float(reservation["usd"]), 0)
        costs = self.db.query(
            "select usd,billing_account,source from turn_costs where turn_id=%s",
            (self.turn_id,))
        self.assertTrue(costs)
        self.assertTrue(all(row["source"] == "relay"
                            and row["billing_account"] == BILLING_ACCOUNT
                            for row in costs))
        self.assertAlmostEqual(sum(float(row["usd"]) for row in costs),
                               float(reservation["usd"]), places=6)
        return reservation

    # -- scenarios --------------------------------------------------------
    def test_end_to_end_receipt_cli_api_and_exactly_once(self):
        admission = self.admit()
        self.start(admission)
        event, receipt, kinds = self.pump(admission.adapter)
        self.assertEqual(event["kind"], "result", kinds)
        self.assert_job_contract(admission)
        self.assertEqual(receipt["schema"], "ameesh-sandbox-receipt/1")
        self.assertFalse(receipt["replayed"])
        self.assertEqual(receipt["job"]["uid"], self.job()["metadata"]["uid"])

        row = self.row()
        self.assertEqual(row["state"], "succeeded")
        self.assertEqual(row["result_sha256"], receipt["result_sha256"])
        agent = self.db.query(
            "select turns,spent_usd,session_id from agent_registry where name=%s",
            (AGENT,))[0]
        self.assertEqual(agent["turns"], 1)
        self.assertEqual(agent["session_id"], "native-kind-session")
        self.assertEqual(self.db.query("select * from spend_pending"), [])
        self.assertEqual(self.db.query(
            "select status from turn_resources where turn_id=%s", (self.turn_id,))[0]["status"],
            "done")
        self.assertEqual(len(self.db.query("select * from sandbox_results")), 1)
        self.assert_settled()

        document = self.receipt()
        self.assertEqual(document["turn_id"], self.turn_id)
        self.assertEqual(document["state"], "succeeded")
        self.assertEqual(document["receipt"]["result_id"], "kind-" + self.turn_id)
        self.assertEqual(document["counters"], {"turns": 1, "tool_calls": 1,
                                                "seconds": 1, "model_calls": 1
                                                if JOB_RELAY else 0})
        self.assertEqual(document["job"]["uid"], receipt["job"]["uid"])
        self.assertEqual(document["tranche"]["state"],
                         "settled" if JOB_RELAY else "kept")

        cli = self.mesh("sandbox", "show", self.turn_id, "--json")
        self.assertEqual(cli.returncode, 0, cli.stderr)
        self.assertEqual(json.loads(cli.stdout), document)
        status, body = self.api("/api/v1/sandbox/turns/" + self.turn_id)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), document)
        status, body = self.api("/api/v1/sandbox/turns?agent=" + AGENT)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["turns"], [document])

        # Response-loss replay: the same digest is a `replayed` receipt, the
        # generic close never runs again and the counters do not move.
        result = event["result"]
        replay = s1.ameesh_sandbox_complete(
            self.db, self.turn_id, OWNER, EPOCH, result,
            settle=s3.sandbox_settle)
        self.assertTrue(replay["replayed"])
        changed = dict(result, verdict="red")
        with self.assertRaises(s1.Refusal) as refused:
            s1.ameesh_sandbox_complete(self.db, self.turn_id, OWNER, EPOCH, changed,
                                       settle=s3.sandbox_settle)
        self.assertEqual(refused.exception.code, "duplicate")
        self.assertEqual(self.db.query(
            "select turns from agent_registry where name=%s", (AGENT,))[0]["turns"], 1)
        self.assertEqual(len(self.db.query("select * from sandbox_results")), 1)
        # A terminal turn's relay bearer is useless; the ordinary lease bearer
        # of a live sandbox agent is refused by the composed verifier.
        with self.assertRaises(relay.TokenError):
            sandbox_relay.Tokens(self.db).verify_token(admission.token)

    def test_cancel_before_result_is_revoked_and_job_deleted(self):
        admission = self.admit()
        admission.spec = dataclasses.replace(
            admission.spec,
            argv=("python", "-c", read_witness(), "slow",
                  "relay" if JOB_RELAY else "norelay"))
        self.start(admission)
        event, row, kinds = self.pump(admission.adapter, until=("bound",))
        self.assertEqual(event["kind"], "bound", kinds)
        sandbox_relay.Tokens(self.db).verify_token(admission.token)
        admission.adapter.cancel()
        self.assertEqual(self.row()["state"], "cancelling")
        candidate = dict(schema="ameesh-sandbox-result/1",
                         turn_id=self.turn_id, epoch=EPOCH,
                         scope=self.row()["scope"],
                         scope_digest=self.row()["scope_digest"],
                         result_id="kind-cancelled", outcome="accepted",
                         verdict="green", findings=[], evidence=[],
                         usage={"turns": 1, "tool_calls": 0, "seconds": 1,
                                "model_calls": 0},
                         cost={"usd": 0}, sandbox_session=None,
                         image_digest=self.admitted.image_digest,
                         source=dict(SOURCE),
                         finished_at="2026-11-24T17:02:31Z")
        with self.assertRaises(s1.Refusal) as refused:
            s1.ameesh_sandbox_complete(self.db, self.turn_id, OWNER, EPOCH,
                                       candidate, settle=s3.sandbox_settle)
        self.assertEqual(refused.exception.code, "revoked")
        event, row, kinds = self.pump(admission.adapter,
                                      until=("cancelled", "lost", "expired"))
        self.assertEqual(event["kind"], "cancelled", kinds)
        self.assertEqual(self.row()["state"], "cancelled")
        self.assertIsNotNone(self.row()["cancel_observed_at"])
        self.assertIsNone(self.job(), "the cancelled Job must be deleted")
        self.assertEqual(self.db.query("select * from sandbox_results"), [])
        self.assertEqual(self.db.query(
            "select state,usd from tranche_reservations")[0]["state"], "kept")
        with self.assertRaises(relay.TokenError):
            sandbox_relay.Tokens(self.db).verify_token(admission.token)

    def test_supervisor_restart_adopts_the_same_job(self):
        admission = self.admit()
        admission.spec = dataclasses.replace(
            admission.spec,
            argv=("python", "-c", read_witness(), "delay",
                  "relay" if JOB_RELAY else "norelay"))
        self.start(admission)
        event, _, kinds = self.pump(admission.adapter, until=("bound",))
        self.assertEqual(event["kind"], "bound", kinds)
        uid = self.job()["metadata"]["uid"]
        admission.adapter.close()  # the supervisor dies; the Job keeps running

        # Same turn/epoch, a fresh adapter and a fresh child: adoption is from
        # the durable row and the Job name, never from local state.
        restarted = self.context_adapter()
        self.addCleanup(restarted.close)
        admission.adapter = restarted
        self.start(admission)
        event, receipt, kinds = self.pump(restarted)
        self.assertEqual(event["kind"], "result", kinds)
        self.assertFalse(receipt["replayed"])
        self.assertEqual(self.job()["metadata"]["uid"], uid)
        jobs = self.kubectl("-n", NAMESPACE, "get", "jobs",
                            "-l", "ameesh.dev/turn-id=" + self.turn_id,
                            "-o", "json")
        self.assertEqual(len(json.loads(jobs.stdout)["items"]), 1)
        self.assertEqual(self.row()["state"], "succeeded")
        self.assertEqual(self.db.query(
            "select turns from agent_registry where name=%s", (AGENT,))[0]["turns"], 1)

    def test_lost_lease_is_stale_and_recovery_never_closes_replacement(self):
        admission = self.admit()
        self.start(admission)
        event, _, kinds = self.pump(admission.adapter, until=("bound",))
        self.assertEqual(event["kind"], "bound", kinds)
        self.db.execute(
            "update agent_registry set lease_owner='replacement-runner',"
            " lease_epoch=8, session_id='replacement-session', turns=0 where name=%s",
            (AGENT,))
        with self.assertRaises(relay.TokenError):
            sandbox_relay.Tokens(self.db).verify_token(admission.token)
        result = dict(schema="ameesh-sandbox-result/1", turn_id=self.turn_id,
                      epoch=EPOCH, scope=self.row()["scope"],
                      scope_digest=self.row()["scope_digest"],
                      result_id="kind-stale", outcome="accepted", verdict="green",
                      findings=[], evidence=[],
                      usage={"turns": 1, "tool_calls": 0, "seconds": 1,
                             "model_calls": 0},
                      cost={"usd": 0}, sandbox_session=None,
                      image_digest=self.admitted.image_digest, source=dict(SOURCE),
                      finished_at="2026-11-24T17:02:31Z")
        with self.assertRaises(s1.Refusal) as refused:
            s1.ameesh_sandbox_complete(self.db, self.turn_id, OWNER, EPOCH, result,
                                       settle=s3.sandbox_settle)
        self.assertEqual(refused.exception.code, "stale")

        # The runner-restart recovery path: the old row is fenced, the Job is
        # deleted and its absence observed; no result is accepted and the
        # replacement lease is never touched.
        recovery = SandboxTurn.recover(self.db, self.turn_id)
        self.addCleanup(recovery.close)
        recovery.start(admission.spec,
                       provider_factory="ameesh.runner_sandbox:kubernetes_provider",
                       state_dir=self.tmp,
                       orphan_after=float(self.admitted.limits["job_seconds"]),
                       timeout=float(self.admitted.limits["turn_seconds"]),
                       provider_options=admission.options, new_session=True)
        event, _, kinds = self.pump(recovery, timeout=180,
                                    until=("lost", "expired", "cancelled"))
        self.assertIn(event["kind"], ("lost", "expired"), kinds)
        self.assertEqual(self.row()["state"], event["kind"])
        self.assertIsNone(self.job(), "the recovered Job must be deleted")
        replacement = self.db.query(
            "select lease_owner,lease_epoch,session_id,turns from agent_registry"
            " where name=%s", (AGENT,))[0]
        self.assertEqual((replacement["lease_owner"], replacement["lease_epoch"],
                          replacement["session_id"], replacement["turns"]),
                         ("replacement-runner", 8, "replacement-session", 0))
        self.assertEqual(self.db.query("select * from sandbox_results"), [])

    def test_unobserved_deletion_is_orphaned_and_alerts(self):
        admission = self.admit()
        admission.spec = dataclasses.replace(
            admission.spec,
            argv=("python", "-c", read_witness(), "slow",
                  "relay" if JOB_RELAY else "norelay"))
        self.start(admission)
        event, _, kinds = self.pump(admission.adapter, until=("bound",))
        self.assertEqual(event["kind"], "bound", kinds)
        admission.adapter.cancel()
        self.assertEqual(self.row()["state"], "cancelling")
        job = self.job()
        self.assertIsNotNone(job)
        base = _real_provider(admission)

        class UnobservableDelete:
            """Delegates to the real provider, but the delete observation fails."""

            def __getattr__(self, name):
                return getattr(base, name)

            def delete(self, namespace, name, *, uid=None):
                raise ProviderUnavailable("qualification: observation lost")

        # The real Job exists; only the post-delete observation is unavailable,
        # which is exactly the durable fact that must stay `orphaned`.
        supervisor = SandboxSupervisor(
            UnobservableDelete(), self.events.append,
            orphan_after=float(self.admitted.limits["job_seconds"]))
        observation = supervisor.step(self.row(), admission.spec)
        self.assertEqual(observation.kind, "orphaned")
        admission.adapter.consume(observation.as_dict())
        self.assertEqual(self.row()["state"], "orphaned")
        events = {r["type"] for r in self.db.query(
            "select type from ameesh_events where object=%s", ("turn:" + self.turn_id,))}
        self.assertIn("sandbox.orphan", events)

        # No live lease and no observed termination: the durable facts raise
        # the operator alert. Nothing is inferred from the Job's phase.
        self.db.execute(
            "update agent_registry set lease_expires_at=now() - interval '1 second'"
            " where name=%s", (AGENT,))
        alerts = exploitation._sandbox_alerts(self.db, time.time() + 1)
        self.assertIn(("sandbox_orphan", self.turn_id),
                      {(alert["type"], alert.get("turn_id")) for alert in alerts})

        # Recovery closes the old row without touching the registry, and the
        # alert disappears once the termination is observed.
        base.delete(NAMESPACE, job_name(self.turn_id))
        s1.ameesh_sandbox_cancel(self.db, self.turn_id, OWNER, EPOCH,
                                observed=True, recovery=True,
                                settle=s3.sandbox_settle)
        self.assertEqual(self.row()["state"], "cancelled")
        self.assertIsNotNone(self.row()["cancel_observed_at"])
        self.assertEqual(exploitation._sandbox_alerts(self.db, time.time() + 1), [])
        self.assertEqual(self.db.query(
            "select turns from agent_registry where name=%s", (AGENT,))[0]["turns"], 0)


def _real_provider(admission):
    from ameesh.runner_sandbox import kubernetes_provider
    return kubernetes_provider(admission.options)
