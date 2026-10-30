# SPDX-License-Identifier: AGPL-3.0-only
"""#4256: project-owned validation routes, deadlines and private facts.

The mailbox remains the decision's single-winner register. Audit data never
enters work notes, threads, runner logs or the mediated executor contract.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import time
from zoneinfo import ZoneInfo

from . import storage

LOGGER = logging.getLogger(__name__)


def business_due(now, timezone):
    """One business day at the same local wall time, skipping weekends."""
    local = dt.datetime.fromtimestamp(float(now), ZoneInfo(timezone))
    due = local + dt.timedelta(days=1)
    while due.weekday() >= 5:
        due += dt.timedelta(days=1)
    return due.timestamp()


def routing(cfg, project, domain, owner):
    """Snapshot only declared authority; an absent substitute goes to owner."""
    entry = getattr(cfg, "project_planning", {}).get(project, {})
    if not isinstance(entry, dict):
        raise ValueError("invalid project_planning entry")
    settings = entry.get("validations", {})
    if not isinstance(settings, dict) or not isinstance(settings.get("domains", {}), dict):
        raise ValueError("invalid validation authority configuration")
    if settings.get("domains") and not settings.get("owner"):
        raise ValueError("validation domain routes require an explicit mesh owner")
    ZoneInfo(settings.get("owner_timezone", settings.get("timezone", "UTC")))
    recipient = owner
    owner = settings.get("owner") or owner
    if not owner:
        raise ValueError("validation needs an explicit owner in project_planning")
    domain_route = settings.get("domains", {}).get(domain, [])
    if not isinstance(domain_route, list):
        raise ValueError("validation domain route must be an ordered list")
    nodes = []
    for node in domain_route:
        if not isinstance(node, dict) or not str(node.get("human", "")).startswith("human:"):
            raise ValueError("validation route needs human identities")
        zone = node.get("timezone") or (
            settings.get("owner_timezone", settings.get("timezone", "UTC"))
            if node["human"] == owner else settings.get("timezone", "UTC"))
        ZoneInfo(zone)
        if node["human"] in {n["human"] for n in nodes}:
            raise ValueError("duplicate validation authority")
        nodes.append({"human": node["human"], "timezone": zone})
    if not nodes:
        zone = (settings.get("owner_timezone", settings.get("timezone", "UTC"))
                if recipient == owner else settings.get("timezone", "UTC"))
        nodes = [{"human": recipient, "timezone": zone}]
    ZoneInfo(nodes[0]["timezone"])
    if not str(owner).startswith("human:"):
        raise ValueError("validation owner must be a human identity")
    return {"domain": domain, "nodes": nodes, "owner": owner,
            "owner_timezone": settings.get("owner_timezone", settings.get("timezone", "UTC"))}


def stamp(ts, timezone):
    return dt.datetime.fromtimestamp(float(ts), ZoneInfo(timezone)).isoformat()


def metadata(route, *, due, defaulted, links):
    zone = route["nodes"][0]["timezone"]
    return {"domain": route["domain"], "timezone": zone, "due_local": stamp(due, zone),
            "defaulted": defaulted, "links": list(dict.fromkeys(links))}


def register(db, decision_id, route, *, now, due, defaulted, links):
    store = storage.of(db).decisions
    store.validation_register(decision_id, routing=route)
    store.validation_event(decision_id, person=route["nodes"][0]["human"],
                           kind="requested", now=now,
                           facts={"due_ts": due, "defaulted": defaulted, "links": links},
                           event_key="requested")


def can_answer(db, decision_id, human):
    route = storage.of(db).decisions.validation_route(decision_id)
    if not route:
        return False
    # Only the initial validator and the actual, single escalation recipient.
    return human == route["nodes"][0]["human"] or human == route.get("escalated_to")


def sweep(cfg, db, *, now=None):
    """Atomic row-lock transition; racing hosts or answers cannot duplicate it."""
    from . import decisions
    now = time.time() if now is None else float(now)
    if storage.is_remote(db):
        return []
    changed = []
    for view in storage.of(db).decisions.validation_due(now=now):
        try:
            with db.transaction() as tx:
                store = storage.of(tx).decisions
                row = store.validation_lock(view["id"])
                if not row or row.get("closed_ts") is not None:
                    continue
                decision = decisions._payload(row)
                if float(decision["due_ts"]) > now:
                    continue
                route = store.validation_route(view["id"])
                if not route or route.get("escalated_to"):
                    continue
                initial = route["nodes"][0]
                substitute = (route["nodes"][1] if len(route["nodes"]) > 1 else
                              {"human": route["owner"], "timezone": route["owner_timezone"]})
                needs_substitute = len(route["nodes"]) < 2
                decision["validation"]["timezone"] = substitute["timezone"]
                decision["validation"]["due_local"] = stamp(decision["due_ts"], substitute["timezone"])
                decision["validation"]["status"] = "escalated"
                if needs_substitute:
                    decision["question"] += "\nPlease name an authorized substitute."
                body = decisions.request_text(decision, requester=row["sender"],
                                              owner=substitute["human"], decision_id=view["id"])
                store.validation_escalate(view["id"], recipient=substitute["human"],
                                           decision=decision, body=body, now=now)
                store.validation_event(
                    view["id"], person=substitute["human"], kind="requested", now=now,
                    facts={"due_ts": decision["due_ts"], "escalated_from": initial["human"],
                           "requested_ts": now}, event_key="escalation_requested")
                store.validation_event(view["id"], person=initial["human"], kind="missed",
                                       now=now, event_key="missed",
                                       facts={"requested_ts": row["created_ts"],
                                              "due_ts": decision["due_ts"],
                                              "escalated_to": substitute["human"],
                                              "needs_substitute": needs_substitute})
                changed.append({"id": view["id"], "status": "escalated"})
        except Exception as exc:
            # Keep this decision retryable; isolate its rollback and continue.
            # Exception strings can contain private payload/routing details.
            LOGGER.warning("validation decision #%s could not escalate (%s)",
                           view["id"], type(exc).__name__)
    return changed


def response(db, view, *, human, text, now):
    """Record factual replies; idempotent even on a retried CLI command."""
    route = storage.of(db).decisions.validation_route(view["id"])
    if not route:
        return
    late = float(now) >= float(view["due_ts"])
    kind = "late_response" if late else "response"
    winning_response_ts = (view.get("answer") or {}).get("ts", now)
    if float(winning_response_ts) >= float(view["due_ts"]):
        storage.of(db).decisions.validation_event(
            view["id"], person=route["nodes"][0]["human"], kind="missed", now=now,
            facts={"requested_ts": view["created_ts"], "due_ts": view["due_ts"],
                   "answered_late_by": human}, event_key="missed")
    digest = hashlib.sha256((human + "\0" + text).encode()).hexdigest()
    storage.of(db).decisions.validation_event(
        view["id"], person=human, kind=kind, now=now,
        facts={"text": text, "due_ts": view["due_ts"]}, event_key="response:" + digest)
    if human != route["nodes"][0]["human"] and (view.get("answer") or {}).get("by") == human:
        storage.of(db).decisions.validation_event(
            view["id"], person=route["nodes"][0]["human"], kind="resolved", now=now,
            facts={"answered_by": human}, event_key="resolved")


def withdrawn(db, view, *, by, now):
    """#4542: a withdrawal is the outcome of every journalled request."""
    route = storage.of(db).decisions.validation_route(view["id"])
    if not route:
        return
    people = [route["nodes"][0]["human"], route.get("escalated_to")]
    for person in dict.fromkeys(p for p in people if p):
        storage.of(db).decisions.validation_event(
            view["id"], person=person, kind="withdrawn", now=now,
            facts={"requested_ts": view["created_ts"], "due_ts": view["due_ts"],
                   "withdrawn_by": by}, event_key="withdrawn:" + person)


def history(cfg, db, *, person, key_path, signer):
    """Owner key possession and a non-agent session are both required."""
    from . import authority, decisions, signing
    decisions.answering_human(cfg, db)
    if storage.is_remote(db):
        raise decisions.DecisionError("validation history is owner-only")
    info = authority.key_info(db, signer)
    if not info or info.get("key_role") != "owner" or info.get("key_revoked_ts"):
        raise decisions.DecisionError("validation history requires a live owner key")
    try:
        seed = signing.read_private(key_path)
        public = signing.check_public_key(authority.unb64(info["public_key"]))
    except (OSError, ValueError) as exc:
        raise decisions.DecisionError("cannot read validation history owner key") from exc
    challenge = authority.canonical("validation-history", {"person": person, "signer": signer})
    if not signing.verify(public, challenge, signing.sign(seed, challenge)):
        raise decisions.DecisionError("validation history requires the owner's key")
    return storage.of(db).decisions.validation_history(person=person)


def main(cfg, argv):
    from . import decisions
    parser = argparse.ArgumentParser(prog="ameesh decide validation-history")
    parser.add_argument("--person", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--as", dest="signer", required=True)
    args = parser.parse_args(argv)
    db = decisions._open(cfg)
    try:
        print(json.dumps(history(cfg, db, person=args.person, key_path=args.key,
                                 signer=args.signer), ensure_ascii=False))
    finally:
        db.close()
    return 0


def late_response(cfg, db, view, *, human, text, option, now):
    """A closed winner is immutable. One typed follow-up carries a contradiction."""
    from . import decisions
    if view["state"] != "answered" or not view.get("validation") or now < view["due_ts"]:
        return None
    answer = view.get("answer") or {}
    same = (answer.get("option") == option if option is not None
            else answer.get("text") == text)
    # The factual reply must survive a refused follow-up (closed lot,
    # invalid routing, etc.). Retrying remains safe through the event key.
    with db.transaction() as tx:
        storage.of(tx).decisions.validation_lock(view["id"])
        response(tx, view, human=human, text=text, now=now)
    with db.transaction() as tx:
        store = storage.of(tx).decisions
        store.validation_lock(view["id"])
        if same:
            if answer.get("by") != human:
                return {"decision": view, "late_recorded": True, "replayed": False}
            return None
        response_key = hashlib.sha256((human + "\0" + text).encode()).hexdigest()
        existing = store.validation_followup_id(view["id"], response_key=response_key)
        if existing:
            return {"decision": decisions.get(tx, view["id"], now),
                    "followup": existing, "relation": "contradicts", "replayed": True}
        try:
            # ask may write before refusing: isolate those writes as well.
            with tx.transaction() as followup_tx:
                followup = decisions.ask(
                    cfg, followup_tx, requester=view["requester"], lot=str(view["lot"]),
                    question=("Conflicting late response to decision #%s: %s" %
                              (view["id"], text))[:decisions.MAX_QUESTION],
                    options=["keep=Keep the recorded decision", "revise=Reconsider the decision"],
                    needs_signature=view["needs_signature"],
                    domain=view["validation"]["domain"],
                    issue_links=view["validation"]["links"], now=now)
        except decisions.DecisionError:
            store.validation_event(
                view["id"], person=human, kind="followup_pending", now=now,
                facts={"response_key": response_key, "relation": "contradicts"},
                event_key="followup_pending:" + response_key)
            return {"decision": decisions.get(tx, view["id"], now),
                    "late_recorded": True, "followup_pending": True,
                    "relation": "contradicts", "replayed": False}
        store.validation_followup(view["id"], followup_id=followup["id"], response_key=response_key)
        return {"decision": decisions.get(tx, view["id"], now),
                "followup": followup["id"], "relation": "contradicts", "replayed": False}
