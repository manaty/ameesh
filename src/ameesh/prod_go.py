# SPDX-License-Identifier: AGPL-3.0-only
"""#7726: signed, bounded production authorization; no prose heuristics."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re
import time

from . import mechanics, storage

LINE = re.compile(
    r"^prod-go/1 project=([a-z][a-z0-9-]*) sha=([0-9a-f]{40}) "
    r"version=(release-[A-Za-z0-9.-]+) deploy=([a-z0-9,-]+) "
    r"migrations=([a-z0-9,-]+) merge=([a-z0-9,-]+) "
    r"until=(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)$", re.M)


class ProdGoError(RuntimeError):
    pass


def parse(question, options=()):
    text = str(question or "")
    if "prod-go/" not in text:
        return None
    matches = list(LINE.finditer(text))
    if len(matches) != 1 or text.count("prod-go/") != 1:
        raise ProdGoError("one exact prod-go/1 declaration required")
    project, sha, version, deploy, migrations, merge, until = matches[0].groups()
    try:
        expiry = datetime.strptime(until, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError as exc:
        raise ProdGoError("prod-go/1 until is not a UTC date") from exc
    keys = {o["key"] for o in options}
    allowed = {}
    for operation, value in (("deploy", deploy), ("migrate", migrations), ("release", merge)):
        selected = [] if value == "-" else value.split(",")
        if not selected and value != "-" or len(set(selected)) != len(selected) or any(k not in keys for k in selected):
            raise ProdGoError("prod-go/1 %s names an unknown or repeated option" % operation)
        allowed[operation] = tuple(selected)
    return {"project": project, "sha": sha, "version": version, "until": expiry, "allowed": allowed}


def max_hours(cfg):
    value = mechanics.configured(cfg).prod_go_max_hours
    if type(value) is not int or value <= 0:
        raise ProdGoError("canon prod_go_max_hours is not compiled")
    return value


def validate_request(cfg, question, options, project, *, now=None):
    rule = parse(question, options)
    if rule is None:
        return None
    now = time.time() if now is None else now
    if rule["project"] != project:
        raise ProdGoError("prod-go/1 project differs from the decision's project")
    if not now < rule["until"] <= now + max_hours(cfg) * 3600:
        raise ProdGoError("prod-go/1 until is expired or exceeds canon prod_go_max_hours")
    return rule


def require_unlocked(view, *, now=None):
    """Called under the supersession row locks too; no replacement in-window."""
    rule = parse(view.get("question"), view.get("options") or ())
    now = time.time() if now is None else now
    if rule and view.get("state") == "answered" and now < rule["until"]:
        raise ProdGoError("prod-go/1 decision #%s cannot be withdrawn or replaced before until; cancel the production run and hold prod-deploy" % view.get("id"))


def export_proof(cfg, db, decision_id, *, now=None):
    from . import authority, decisions
    now = time.time() if now is None else now
    view = decisions.get(db, int(decision_id), now)
    if not view or view.get("state") != "answered":
        raise ProdGoError("production decision is not answered")
    decisions.require_current(db, view)
    ok, reason = decisions.verify_proof(db, view)
    if not ok:
        raise ProdGoError("production owner signature is not VALID: " + reason)
    rule = parse(view.get("question"), view.get("options") or ())
    if not rule or rule["project"] != view.get("project") or now >= rule["until"]:
        raise ProdGoError("missing, mismatched or expired prod-go/1")
    proof = view["answer"]["proof"]
    rows = storage.of(db).approvals.recent(action=decisions.APPROVAL_ACTION,
        artifact_hash=proof["digest"], limit=20)
    row = next((r for r in rows if r["id"] == proof["approval_id"]), None)
    if row is None:
        raise ProdGoError("signed approval is unavailable")
    signed = json.loads(row["signed_payload"].split("|", 2)[2])
    if signed.get("artifact_kind") != decisions.APPROVAL_KIND or signed.get("decision") != "approved":
        raise ProdGoError("approval does not authorize this decision")
    if type(signed.get("ts")) is not int or not signed["ts"] / 1e6 < rule["until"] <= signed["ts"] / 1e6 + max_hours(cfg) * 3600:
        raise ProdGoError("production window exceeds signed timestamp plus canon prod_go_max_hours")
    if type(signed.get("expires")) is not int or not now * 1e6 < signed["expires"] or rule["until"] * 1e6 > signed["expires"]:
        raise ProdGoError("production window exceeds signed approval expiry")
    info = authority.key_info(db, row["approver"])
    answer = view["answer"]
    return {"schema": "ameesh-prod-go-proof/1", "decision": view,
            "answer_document": decisions.answer_document(view, answer["text"], answer.get("option"), answer["by"]),
            "signed_payload": row["signed_payload"], "signature": row["signature"],
            "public_key": info["public_key"], "public_key_fingerprint": info["public_key_fingerprint"]}


def authorize(cfg, db, decision_id, *, project, sha, operation, now=None):
    proof = export_proof(cfg, db, decision_id, now=now)
    view = proof["decision"]
    rule = parse(view["question"], view["options"])
    if rule["project"] != project or rule["sha"] != sha:
        raise ProdGoError("production decision project/SHA differs from target")
    if operation not in rule["allowed"] or view["answer"].get("option") not in rule["allowed"][operation]:
        raise ProdGoError("chosen option does not authorize production " + operation)
    return proof


def check_release_merge(cfg, db, lot, source, target, sha, *, gh=None):
    """Shared by packet_guard: queue, composer, fire, land and merge-pr."""
    if not source.startswith("release/"):
        return
    if db is None:
        raise ProdGoError("release authorization requires the decision database")
    from . import land, work
    item = work.get(db, int(lot))
    project = (item or {}).get("app")
    if not project:
        raise ProdGoError("release has no project")
    document = mechanics.configured(cfg)
    override = document.override(project)
    if override is None:
        raise ProdGoError("release project is not declared in compiled canon")
    production = override.production_branch
    if not production:
        return  # project explicitly has no production branch (e.g. mesh staging)
    if target != production:
        return
    ref = str(item.get("pr_ref") or "")
    match = re.fullmatch(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([1-9][0-9]*)", ref)
    if match:
        args = [match[2], "--repo", match[1]]
    elif re.fullmatch(r"https://github.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[1-9][0-9]*", ref):
        args = [ref]
    else:
        raise ProdGoError("release requires a linked GitHub PR")
    proc = (gh or land._gh)(["pr", "view", *args, "--json", "body,headRefOid,headRefName,baseRefName"], timeout=60)
    if proc.returncode:
        raise ProdGoError("cannot read release PR decision")
    view = json.loads(proc.stdout)
    if view.get("headRefOid") != sha or view.get("headRefName") != source or view.get("baseRefName") != target:
        raise ProdGoError("release PR head/base changed")
    ids = re.findall(r"^Production decision:\s*#?([1-9][0-9]*)\s*$", view.get("body") or "", re.M)
    if len(ids) != 1:
        raise ProdGoError("release PR requires one Production decision: #<id> line")
    authorize(cfg, db, ids[0], project=project, sha=sha, operation="release")
