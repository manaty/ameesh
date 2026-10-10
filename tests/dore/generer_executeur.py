# SPDX-License-Identifier: AGPL-3.0-only
"""Génère les jeux d'essai dorés de l'exécuteur médié (contrat 1.1).

    python3 tests/dore/generer_executeur.py tests/dore/executeur_mediee

Les valeurs suivent le schéma réel : états de lot de `work.STATES`, `kind`
de 0012, identifiant et empreinte d'action de 0010, clés du pilote pour
`leases.state` et `hosts.*`, identifiant d'exécuteur de 16 caractères
hexadécimaux (L110), payload du déclencheur `agent_lease` (0001)."""
import json, os, sys, uuid
sys.path.insert(0, "src")
from ameesh.executeur_mediee import contrat as C, evenements as E, porte as P, interfaces as I

OUT = sys.argv[1]
os.makedirs(OUT, exist_ok=True)
c = C.load()
EXEC = "7f3a9c2e4b1d6058"
AG, OWN, EP, HOST = "inge-front", "exec:%s:anna-portable:4121" % EXEC, 42, "anna-portable"
TS = 1791640000.12
ACC = "Bearer amx1.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
SES = "Bearer ams1.BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
MSG = {"id": 812, "sender": "coord", "recipient": AG, "body": "Lot 812 : relis la PR.",
       "kind": "notify", "payload": {}, "work_item_id": "812", "created_ts": 1791639990.5,
       "delivered_ts": None}
ROW = {"name": AG, "chantier": "site", "harness": "dsh", "host": HOST,
       "cwd": "/var/lib/ameesh-exec/work/inge-front/42", "status": "idle",
       "lease_owner": None, "lease_epoch": 41, "lease_expires_ts": None}

# relevé de ressources : les clés de host_resources (L31, 0029)
READING = {"host": HOST, "mem_available_bytes": 5368709120, "swap_used_bytes": 0, "load1": 0.4,
           "cpu_count": 4, "disk_free_bytes": 21474836480, "disk_path": "/var/lib/ameesh-exec",
           "turns_in_progress": 1}
READING_ROW = dict(READING, id=77, sampled_ts=TS)

ARGS = {  # op: (args, kwargs, résultat)
 "agents.claimable": ([HOST, [AG]], {"require_responsible": False}, [{**ROW, "status": "queued"}]),
 "agents.get": ([AG], {}, ROW),
 "agents.cwd_used": (["/var/lib/ameesh-exec/work/inge-front/42", AG], {}, False),
 "agents.harnesses": ([], {}, {AG: "dsh"}),
 "agents.set_status": ([AG, "idle", "en attente", None], {}, None),
 "agents.set_session": ([AG, "sess-5d1e", None], {}, None),
 "agents.set_session_account": ([AG, None], {}, True),
 "agents.set_pending_prompt": ([AG, None], {}, None),
 "agents.mark_event_wake": ([AG], {}, None),
 "agents.upsert": ([AG], {"chantier": None, "harness": None, "host": None,
                         "cwd": "/var/lib/ameesh-exec/work/inge-front/42", "session_id": None,
                         "status": None, "status_text": None, "model": None, "budget_usd": None}, ROW),
 "leases.claim": ([AG, OWN, 90.0], {"require_responsible": False},
                  {**ROW, "status": "queued", "lease_owner": OWN, "lease_epoch": EP,
                   "lease_expires_ts": TS + 90}),
 "leases.renew": ([AG, OWN, EP, 90.0], {}, TS + 90),
 "leases.release": ([AG, OWN, EP], {}, True),
 "leases.reap": ([HOST], {}, []),
 "leases.begin_turn": ([AG, OWN, EP, "tour en cours"], {}, True),
 "leases.end_turn": ([AG, OWN, EP], {"status": "idle", "status_text": "tour terminé",
                                      "error": None, "cost_usd": 0.031}, True),
 "leases.take_pending_prompt": ([AG, OWN, EP], {}, "Reprends le lot 812."),
 "leases.restore_prompt": ([AG, OWN, EP], {}, True),
 "leases.pause": ([AG, OWN, EP, "pression de l'hôte"], {}, True),
 "leases.clear_session": ([AG, OWN, EP], {}, True),
 "leases.set_marked_block": ([AG, OWN, EP, "dossier absent", "cwd: /x", "cwd:"], {}, "done"),
 "leases.clear_marked_block": ([AG, OWN, EP, "dossier absent", "cwd:"], {}, "done"),
 "mailbox.unread": ([AG, 50], {}, [MSG]),
 "mailbox.unread_urgent": ([AG, 50], {}, []),
 "mailbox.get": ([812], {}, MSG),
 "mailbox.reserve": ([AG, OWN, EP, "tok-9a2c"], {"porteur": "turn", "ttl_seconds": 600.0}, [MSG]),
 "mailbox.deliver": ([AG, OWN, EP, "tok-9a2c", [812]], {}, [812]),
 "mailbox.release": ([AG, "tok-9a2c", [812]], {}, 1),
 "pending_spend.put": ([AG, 0, "turn-77", "deepseek-chat"], {}, True),
 "pending_spend.set_model": ([AG, "deepseek-chat"], {}, True),
 "pending_spend.clear": ([AG], {}, True),
 "pending_spend.get": ([AG], {}, {"agent": AG, "start_index": 0, "turn": "turn-77",
                                  "model": "deepseek-chat", "created_ts": TS - 30}),
 "turn_costs.insert": ([], {"agent": AG, "harness": "dsh", "turn": "turn-77", "model": "deepseek-chat",
                            "session": "sess-5d1e", "usd": 0.031, "input_tokens": 12000,
                            "cached_input_tokens": 8000, "output_tokens": 900, "cum_usd": 0.12,
                            "cum_input_tokens": 50000, "cum_cached_input_tokens": 30000,
                            "cum_output_tokens": 4000, "spend_key": "turn-77"}, True),
 "turn_costs.last_reading": ([AG, "dsh", "sess-5d1e"], {}, {"session": "sess-5d1e", "cum_usd": 0.12,
                              "cum_input_tokens": 50000, "cum_cached_input_tokens": 30000,
                              "cum_output_tokens": 4000}),
 "turn_costs.spent": ([3600.0], {"agent": AG, "harnesses": ["dsh"]}, 0.42),
 "budgets.limits": ([], {}, [{"scope": "mesh", "window_s": 3600, "usd": 5.0,
                              "set_by": "human:responsable", "updated_ts": TS - 86400}]),
 "accounts.active": ([HOST, "dsh"], {}, None),
 "operations.balances": ([], {"provider": "deepseek", "since_s": 86400.0}, [
     {"provider": "deepseek", "currency": "USD", "total": 18.5, "granted": 0.0, "topped_up": 18.5,
      "available": 18.5, "account": None, "observed_ts": TS - 600}]),
 "operations.apply_restart": ([AG, OWN, EP], {}, {"brief": "Résumé du tour précédent.", "session_id": "sess-5d1e"}),
 "operations.message_lots": ([[812]], {}, ["812"]),
 "operations.set_session_work_item": ([AG, OWN, EP, "812"], {}, True),
 "operations.record_gauges": ([[{"harness": "dsh", "key": "requests", "used": 0.2,
                                  "resets_at": None, "window_s": 3600}]], {}, 1),
 "turn_resources.open_turn": (["turn-77", AG, HOST], {"pgid": 4188, "label": "dsh"}, None),
 "turn_resources.close_turn": (["turn-77"], {"orphan": False}, None),
 "hosts.record": ([READING], {}, dict(READING_ROW)),
 "hosts.latest": ([HOST], {}, dict(READING_ROW)),
 "hosts.current": ([HOST], {}, [dict(READING_ROW)]),
 "hosts.turns_in_progress": ([HOST], {}, 1),
 "keys.info": (["coord"], {}, {"public_key": "MCowBQYDK2VwAyEA…", "fingerprint": "sha256:9c1f…",
                               "role": "coordinator", "registered_ts": TS - 864000, "revoked_ts": None}),
 "threads.index": ([], {"project": "site", "lot": "812", "transport": "github", "host": HOST,
                         "location": "owner/repo#812", "entry_id": "c-1001", "mailbox_ids": [812],
                         "author": "agent:" + AG, "excerpt": "PR relue.", "ts": TS, "trace": {"comment_id": 1001}}, None),
 "work.mark_delegate_turn": ([AG, [812], "délégation : tour de inge-front sur le lot"], {}, [812]),
 # session
 "leases.state": ([AG], {}, {"lease_owner": OWN, "lease_epoch": EP, "status": "running", "live": True}),
 "agents.overview": ([], {}, [{"name": AG, "team": "ingénieur", "chantier": "site",
                                "canon_governed": True, "status": "running"},
                               {"name": "coord", "team": "coordinateur", "chantier": "site",
                                "canon_governed": True, "status": "idle"}]),
 "mailbox.send": ([AG, "coord", "PR prête."], {"host": None, "kind": "notify", "payload": None,
                  "work_item_id": "812", "signature": None, "signature_key": None,
                  "signed_payload": None, "nonce": None, "created_us": None, "expires_us": None},
                  {"id": 813, "created_ts": TS, "sender_project": "site", "recipient_project": "site"}),
 "mailbox.mark_delivered": ([[812]], {}, 1),
 "work.get": ([812], {}, {"id": 812, "project": "site", "title": "Page d'accueil", "state": "build",
                          "assignee": AG, "loops": 0}),
 "work.move": ([812, "qa"], {"current": "build", "loops": 0, "note": "PR ouverte", "actor": AG},
               {"id": 812, "project": "site", "title": "Page d'accueil", "state": "qa",
                "assignee": AG, "loops": 0}),
 "work.note": ([812, "build", "Tests verts.", AG], {}, None),
 "actions.get": (["act_01J9ZK3B7EQ4M8RTXW2HC5NVDA"], {"with_receipts": False}, {"action_id": "act_01J9ZK3B7EQ4M8RTXW2HC5NVDA", "state": "proposed",
                 "connector": "github", "operation": "merge_pr", "proposed_by": AG}),
 "actions.propose": ([], {"action_id": "act_01J9ZK3B7EQ4M8RTXW2HC5NVDA", "project": "site", "work_item": 812,
                          "proposed_by": AG, "connector": "github", "operation": "merge_pr",
                          "target": "owner/repo#812", "args_json": "{}", "action_class": "reversible",
                          "amount": None, "currency": None, "policy_version": "1",
                          "digest": "sha256:" + "3b7e" * 16, "dedupe": "guaranteed", "requires_receipt": True,
                          "approvers_json": "[]", "note": "fusion après revue"}, None),
}

families = {}


def case(name, op, req_body, status, resp_body, *, route="/op", token=ACC, key=None,
         resp_headers=None, extra_headers=None):
    h = {"Authorization": token, "Content-Type": "application/json"}
    if key:
        h[C.IDEMPOTENCY_HEADER] = key
    h.update(extra_headers or {})
    return {"nom": name, "op": op,
            "requete": {"methode": "POST", "chemin": C.PREFIX + route, "entetes": h, "corps": req_body},
            "reponse": {"statut": status, "entetes": resp_headers or {}, "corps": resp_body}}


for name, op in c.operations.items():
    if op.transport == "events":
        continue
    args, kwargs, value = ARGS[name]
    fence = C.Fence(AG, OWN, EP) if op.fence else None
    req = C.OpRequest(name, tuple(args), kwargs, fence)
    key = str(uuid.uuid5(uuid.NAMESPACE_URL, "ameesh-exec:" + name)) if op.write else None
    route = "/" + op.transport
    token = SES if op.transport == "session/op" else ACC
    cases = families.setdefault(op.domain, [])
    cases.append(case(name + "/ok", name, req.to_json(), 200,
                      C.OpResult(value, TS).to_json(), route=route, token=token, key=key))
    if name in ("leases.begin_turn", "agents.set_status", "mailbox.reserve", "leases.set_marked_block"):
        cases.append(case(name + "/bail-perdu", name, req.to_json(), 200,
                          C.OpResult(op.refusal, TS, fenced=True).to_json(), route=route,
                          token=token, key=str(uuid.uuid5(uuid.NAMESPACE_URL, "perdu:" + name))))
    if op.session:
        # contrat 1.1 : la même ligne, depuis la session du harnais (hook)
        skey = str(uuid.uuid5(uuid.NAMESPACE_URL, "session:" + name)) if op.write else None
        cases.append(case(name + "/session", name, req.to_json(), 200,
                          C.OpResult(value, TS).to_json(), route="/session/op", token=SES,
                          key=skey))
    if name == "leases.claim":
        cases.append(case(name + "/rejoue", name, req.to_json(), 200,
                          C.OpResult(value, TS).to_json(), route=route, token=token, key=key,
                          resp_headers={C.IDEMPOTENCY_REPLAYED_HEADER: "true"}))

for fam, cases in families.items():
    json.dump({"schema": "ameesh-exec-golden/1", "famille": fam, "cas": cases},
              open(os.path.join(OUT, fam + ".json"), "w"), ensure_ascii=False, indent=1)

# erreurs
def err(name, op, body, status, code, msg, *, route="/op", token=ACC, key=None, extra=None, retry=None):
    headers = {"Retry-After": str(int(retry))} if retry else {}
    return case(name, op, body, status, C.error_body(code, msg, retry_after=retry),
                route=route, token=token, key=key, resp_headers=headers, extra_headers=extra)

K = "0b9f6c1e-1c0a-4d1e-9a51-6c2d3f4e5a60"
claim = C.OpRequest("leases.claim", (AG, OWN, 90.0), {"require_responsible": False}).to_json()
erreurs = [
 err("forbidden_scope/autre-agent", "agents.get", C.OpRequest("agents.get", ("tresorier",)).to_json(),
     403, "forbidden_scope", "agent hors de la portée de l'exécuteur"),
 err("forbidden_scope/owner-etranger", "leases.claim",
     C.OpRequest("leases.claim", (AG, "exec:0000:autre:1", 90.0), {"require_responsible": False}).to_json(),
     403, "forbidden_scope", "owner non préfixé par l'exécuteur authentifié", key=K),
 err("host_unavailable", "leases.claim", claim, 403, "host_unavailable", "hôte indisponible", key=K),
 err("executor_revoked", "agents.get", C.OpRequest("agents.get", (AG,)).to_json(), 403,
     "executor_revoked", "exécuteur révoqué"),
 err("op_not_allowed/approbation", "approvals.consume",
     {"schema": C.SCHEMA_OP, "op": "approvals.consume", "args": [], "kwargs": {}}, 404,
     "op_not_allowed", "opération non admise", key=K),
 err("op_not_allowed/mauvaise-route", "mailbox.send",
     C.OpRequest("mailbox.send", ARGS["mailbox.send"][0], ARGS["mailbox.send"][1]).to_json(), 404,
     "op_not_allowed", "opération de session : /session/op", key=K),
 err("fence_required", "agents.set_status",
     C.OpRequest("agents.set_status", (AG, "idle", None, None)).to_json(), 400, "fence_required",
     "enveloppe de bail obligatoire", key=K),
 err("idempotency_key_required", "leases.begin_turn",
     C.OpRequest("leases.begin_turn", (AG, OWN, EP, "tour"), {}, C.Fence(AG, OWN, EP)).to_json(), 400,
     "idempotency_key_required", "Idempotency-Key obligatoire pour une écriture"),
 err("idempotency_mismatch", "leases.claim",
     C.OpRequest("leases.claim", (AG, OWN, 60.0), {"require_responsible": False}).to_json(), 409,
     "idempotency_mismatch", "clé déjà employée pour une autre requête",
     key=str(uuid.uuid5(uuid.NAMESPACE_URL, "ameesh-exec:leases.claim"))),
 err("bad_args/upsert-hors-cwd", "agents.upsert",
     C.OpRequest("agents.upsert", (AG,), {**ARGS["agents.upsert"][1], "model": "deepseek-reasoner"},
                 C.Fence(AG, OWN, EP)).to_json(), 400, "bad_args", "agents.upsert : seul cwd est admis", key=K),
 err("token_expired", "agents.get", C.OpRequest("agents.get", (AG,)).to_json(), 401, "token_expired",
     "jeton échu"),
 err("rate_limited", "agents.get", C.OpRequest("agents.get", (AG,)).to_json(), 429, "rate_limited",
     "trop de requêtes", retry=2),
 err("unavailable", "agents.get", C.OpRequest("agents.get", (AG,)).to_json(), 503, "unavailable",
     "base indisponible", retry=5),
]
json.dump({"schema": "ameesh-exec-golden/1", "famille": "erreurs", "cas": erreurs},
          open(os.path.join(OUT, "erreurs.json"), "w"), ensure_ascii=False, indent=1)

# événements
evs = [E.Event("k3f9:17", "agent_mail", {"to": AG, "id": 812}),
       E.Event("k3f9:18", "agent_lease", {"agent": AG, "owner": OWN, "epoch": EP, "status": "running"}),
       E.Event("k3f9:19", "ameesh_budget", {"scope": "mesh"})]
sse = E.format_retry() + "".join(E.format_sse(e) for e in evs) + E.format_ping()
evenements = {
 "schema": "ameesh-exec-golden/1", "famille": "evenements",
 "sse": {"requete": {"methode": "GET", "chemin": C.PREFIX + "/events",
                     "entetes": {"Authorization": ACC, "Accept": "text/event-stream",
                                 "Last-Event-ID": "k3f9:16"}},
         "reponse": {"statut": 200, "entetes": {"Content-Type": "text/event-stream"}, "texte": sse},
         "evenements": [e.to_json() for e in evs]},
 "attente_longue": {"requete": {"methode": "GET", "chemin": C.PREFIX + "/events?wait=25&after=k3f9:16",
                                "entetes": {"Authorization": ACC}},
                    "reponse": {"statut": 200, "corps": E.long_poll_body(evs, last_id="k3f9:19")}},
 "attente_longue_vide": {"requete": {"methode": "GET", "chemin": C.PREFIX + "/events?wait=25&after=k3f9:19",
                                     "entetes": {"Authorization": ACC}},
                         "reponse": {"statut": 200, "corps": E.long_poll_body([], last_id="k3f9:19")}},
 "reprise_trou": {"requete": {"methode": "GET", "chemin": C.PREFIX + "/events?wait=25&after=a001:5",
                              "entetes": {"Authorization": ACC}},
                  "reponse": {"statut": 200, "corps": E.long_poll_body(
                      [E.Event("k3f9:19", E.RESET, {"reason": "gap"})], last_id="k3f9:19")}},
}
json.dump(evenements, open(os.path.join(OUT, "evenements.json"), "w"), ensure_ascii=False, indent=1)

# porte
st_av = P.GateState("available", 7, until_ts=TS + 7200, caps={"max_concurrent": 1, "cpu_share": 0.5, "memory_mb": 4096}, reason="idle")
st_dr = P.GateState("draining", 8, drain_deadline_ts=TS + 90, caps={"max_concurrent": 1}, reason="user_active")
st_st = P.GateState("stopped", 9, reason="revoked")
porte = {
 "schema": "ameesh-exec-golden/1", "famille": "porte",
 "etats": [s.to_json() for s in (st_av, st_dr, st_st)],
 "acquittements": [P.GateAck(7, "available", ts=TS).to_json(),
                   P.GateAck(8, "draining", in_turn=[AG], held=[AG], drained=False, ts=TS + 1).to_json(),
                   P.GateAck(8, "draining", drained=True, ts=TS + 40).to_json()],
 "illisibles": [{"schema": "ameesh-host-state/0", "state": "available", "seq": 1},
                {"schema": "ameesh-host-state/1", "state": "sleeping", "seq": 2},
                {"schema": "ameesh-host-state/1", "state": "available", "seq": "3"}],
 "disponibilite": {"requete": {"methode": "PUT", "chemin": C.PREFIX + "/host/availability",
                               "entetes": {"Authorization": ACC}, "corps": P.availability_body(st_dr)},
                   "reponse": {"statut": 204}},
}
json.dump(porte, open(os.path.join(OUT, "porte.json"), "w"), ensure_ascii=False, indent=1)

# identité
hi = I.HostInfo(host=HOST, mesh="mesh-exemple", executor_id=EXEC, limits={"max_agents": 1, "resources": {
                    "min_mem_available": 536870912, "max_swap_used": 1073741824,
                    "max_load": 4.0, "min_disk_free": 2147483648}},
                lease_ttl_s=90.0, lease_renew_s=30.0, harnesses=["dsh"], models=["deepseek-chat"],
                agents=[AG], available=True, contract_version=c.version)
identite = {
 "schema": "ameesh-exec-golden/1", "famille": "identite",
 "enroll": {"requete": {"methode": "POST", "chemin": C.PREFIX + "/enroll", "corps": {
     "schema": C.SCHEMA_ENROLL, "code": "K7QF-2M9D-XW4P-8RTA-J3NC-5HVB",
     "public_key": {"kty": "EC", "crv": "P-256", "x": "f83OJ3D2xF1Bg8vub9tLe1gHMzV76e8Tus9uPHvRVEU",
                    "y": "x_FEzRu9m36HLN_tue659LNpXW6pCyStikYjKIWI5a0"},
     "proof": "<ES256 base64url sur JCS({code, public_key, server_url})>",
     "device_attestation": None, "label": "portable (VM Compute)"}},
            "reponse": {"statut": 201, "corps": {"executor_id": EXEC, "mesh": "mesh-exemple",
                                                  "host": HOST, "server_time": TS}}},
 "token": {"requete": {"methode": "POST", "chemin": C.PREFIX + "/token",
                       "corps": {"assertion": "<JWS ES256 (signature JOSE r||s) : iss=7f3a9c2e4b1d6058, aud=https://mesh.exemple, iat, exp<=iat+60, jti>"}},
           "reponse": {"statut": 200, "corps": I.IssuedToken(ACC.split()[1], TS + 600, "executor").to_json()}},
 "session_token": {"requete": {"methode": "POST", "chemin": C.PREFIX + "/session-token",
                               "entetes": {"Authorization": ACC},
                               "corps": {"fence": C.Fence(AG, OWN, EP).to_json()}},
                   "reponse": {"statut": 200, "corps": I.IssuedToken(SES.split()[1], TS + 90, "session").to_json()}},
 "host": {"requete": {"methode": "GET", "chemin": C.PREFIX + "/host", "entetes": {"Authorization": ACC}},
          "reponse": {"statut": 200, "corps": hi.to_json()}},
 "health": {"requete": {"methode": "GET", "chemin": C.PREFIX + "/health"},
            "reponse": {"statut": 200, "corps": {"schema": C.SCHEMA_HEALTH, "contract": c.version,
                                                  "server_ts": TS}}},
}
json.dump(identite, open(os.path.join(OUT, "identite.json"), "w"), ensure_ascii=False, indent=1)
print(sum(len(v) for v in families.values()), len(erreurs))
