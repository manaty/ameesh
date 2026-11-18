# SPDX-License-Identifier: AGPL-3.0-only
"""External witness of the public fixture surface, contract v1.5 (#7701).

This distribution is outside ``src/`` on purpose: it is an installed consumer,
not part of the package.  It imports only the published Python surfaces
(:mod:`ameesh.public.fixture` and :mod:`ameesh.public.mail`), runs the installed
executables as subprocesses, and never imports ``tests.support`` or an internal
ameesh module.

It proves the Fixture row of docs/api/module-contract-v1-coverage.md:

* a consumer-owned disposable database is created, migrated through
  ``ameesh migrate``, used by public commands, and dropped again;
* Process admission (``ameesh process check``), a Pipeline module adoption, a
  verifier acceptance, a verifier refusal and the resulting journal are
  observed through public JSON;
* usage and accounting observations (``ameesh cost turns``, ``ameesh budget``)
  are readable on the fixture;
* mail usage, refusals and races go through :mod:`ameesh.public.mail`, with one
  connection per racing thread.

Run it with ``AMEESH_FIXTURE_ADMIN_DSN`` set to a DSN allowed to CREATE DATABASE
(see README.md).  It prints one JSON report and exits 0 only if every check
passed; the fixture drops its database before the report is printed.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from typing import Any, Mapping

from ameesh.public import fixture as public_fixture
from ameesh.public import mail as public_mail

SCHEMA = "ameesh-public-fixture-witness/1"
ACTOR = "orchestre"
PEER = "relecteur"
HOST = "atelier"
PIPELINE = "witness-delivery"
PROJECT = "witness"


class WitnessFailure(AssertionError):
    """A published element is missing or behaves differently."""


# -- the consumer's own canon: two agents, a host, a placement, a Pipeline ---
INDEX = """---
type: Index
title: witness canon
---

# witness canon

A consumer-owned canon for the public fixture witness.
"""

FEDERATION = """id: witness
root: canon
members: []
extensions:
  ameesh:
    projects:
      %s:
        modules: [work-types, %s]
""" % (PROJECT, PIPELINE)

HOST_CARD = """---
type: Host
title: %s
responsible: human:owner
policy:
  harnesses: [deepseek]
  providers: [deepseek]
  credential_modes: [api-key]
  workspaces:
    roots: {%s: /srv/%s}
  max_agents: 4
---

# %s

The consumer's disposable witness host.
""" % (HOST, PROJECT, PROJECT, HOST)

MEMBER_CARD = """---
type: Member
title: owner
description: "The consumer's witness human."
roles: [project-lead, reviewer]
authenticators: []
---

# owner

The consumer's responsible human; a real canon names its own Member.
"""


def _agent(name: str) -> str:
    return """---
type: Agent
title: %s
responsible: human:owner
capabilities: [read, propose]
harness: deepseek
provider: deepseek
credential_mode: api-key
---

# %s

The consumer's witness agent.
""" % (name, name)


def _placement(name: str) -> str:
    return """---
type: Placement
title: %s@%s
agent: %s
hosts: [%s]
credential_mode: api-key
---

# %s on %s
""" % (name, HOST, name, HOST, name, HOST)


#: The workflow block is valid canon data: the same shape a Process card uses.
WORKFLOW = """schema: process-workflow/1
initial: draft
types: [docs]
stages:
  draft: {core: intake, status: Draft}
  review: {core: qa, status: Review}
  done: {core: promoted, status: Done}
  stopped: {core: closed, status: Stopped}
transitions:
  - {from: draft, to: review, guard: planning}
  - {from: draft, to: done, guard: review_requested}
  - {from: review, to: done, guard: work_completed}
  - {from: [draft, review], to: stopped, guard: stop_work}
projection:
  project: %s
  status_field: Stage
  product_field: Product
  issue_open: [draft, review]
  issue_completed: [done]
  issue_not_planned: [stopped]
""" % PROJECT

PIPELINE_CARD = """---
type: Pipeline
title: %s
name: %s
responsible: human:owner
updated: 2026-11-18
pipeline:
%s
---

# %s

A consumer-owned Pipeline card; the module is activated by the canon.
""" % (PIPELINE, PIPELINE, "\n".join("  " + line for line in WORKFLOW.splitlines()),
       PIPELINE)

PROCESS_CARD = """---
type: Process
title: %s-process
responsible: human:owner
updated: 2026-11-18
workflow:
%s
---

# %s process

A consumer-owned Process card.  ``ameesh process check`` admits its block.
""" % (PIPELINE, "\n".join("  " + line for line in WORKFLOW.splitlines()), PIPELINE)

BROKEN_PROCESS = PROCESS_CARD.replace(
    "    review: {core: qa, status: Review}\n", "")


def canon_files() -> dict:
    """The consumer's canon, as files a real project would commit."""
    return {
        "index.md": INDEX,
        "federation.yaml": FEDERATION,
        "membres/owner.md": MEMBER_CARD,
        "hotes/%s.md" % HOST: HOST_CARD,
        "agents/%s.md" % ACTOR: _agent(ACTOR),
        "agents/%s.md" % PEER: _agent(PEER),
        "placements/%s-%s.md" % (ACTOR, HOST): _placement(ACTOR),
        "placements/%s-%s.md" % (PEER, HOST): _placement(PEER),
        "pipelines/%s.md" % PIPELINE: PIPELINE_CARD,
        "processus/%s.md" % PIPELINE: PROCESS_CARD,
    }


# -- JSON helpers -----------------------------------------------------------
def _json(proc, label: str) -> Any:
    if proc.returncode != 0:
        raise WitnessFailure("%s: exit %d: %s%s" % (
            label, proc.returncode, (proc.stderr or "").strip()[-500:],
            (proc.stdout or "").strip()[-800:]))
    return _document(proc, label)


def _document(proc, label: str) -> Any:
    """Parse the JSON document of a command that also reports refusals as JSON."""
    try:
        return json.loads(proc.stdout)
    except ValueError as exc:
        raise WitnessFailure("%s: no JSON document (exit %d): %s"
                             % (label, proc.returncode, (proc.stdout or "")[-300:])) from exc


def _ok(proc, label: str):
    if proc.returncode != 0:
        raise WitnessFailure("%s: exit %d: %s" % (label, proc.returncode,
                                                  (proc.stderr or "").strip()[-500:]))
    return proc


def _refused(proc, label: str) -> str:
    if proc.returncode == 0:
        raise WitnessFailure("%s: accepted, expected a refusal" % label)
    return (proc.stderr or "").strip()


def _error(call, label: str):
    """Run a public Python call that must refuse; return (class name, code)."""
    try:
        call()
    except Exception as exc:  # noqa: BLE001 — the published refusal is the witness
        return {"error": type(exc).__name__, "code": getattr(exc, "code", "")}
    raise WitnessFailure("%s: accepted, expected a refusal" % label)


def _race(fixture, key: str, bodies) -> list:
    """Submit the same idempotency key from one connection per thread."""
    results, errors = [], []
    barrier = threading.Barrier(len(bodies))

    def worker(body):
        connection = fixture.connect()
        try:
            barrier.wait(timeout=30)
            receipt = public_mail.submit(
                connection, ACTOR,
                public_mail.Submission(PEER, body, idempotency_key=key))
            results.append(receipt.as_dict())
        except Exception as exc:  # noqa: BLE001 — a raced refusal is an outcome
            errors.append({"error": type(exc).__name__,
                           "code": getattr(exc, "code", "")})
        finally:
            connection.close()

    threads = [threading.Thread(target=worker, args=(body,)) for body in bodies]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    if any(thread.is_alive() for thread in threads):
        raise WitnessFailure("idempotency race did not finish")
    return results + errors


# -- the scenario -----------------------------------------------------------
def run(admin_dsn: str, report: dict) -> None:
    fixture = public_fixture.Fixture(admin_dsn)
    database = ""
    try:
        fixture.create()
        database = fixture.database
        report["fixture"] = {"database": database, "prefix": public_fixture.DATABASE_PREFIX}
        if not database.startswith(public_fixture.DATABASE_PREFIX):
            raise WitnessFailure("database %r does not carry the published prefix" % database)

        # 1. Process admission, refusal and Pipeline activation.
        commit = fixture.canon(canon_files())
        report["canon"] = {"commit": commit}
        process_path = fixture.path("canon/processus/%s.md" % PIPELINE)
        check_proc = fixture.cli("canon", "check", "--json", "--host", HOST)
        check = _document(check_proc, "canon check")
        if check_proc.returncode != 0 or not check.get("ok"):
            raise WitnessFailure("canon check: %s"
                                 % (check.get("diagnostic")
                                    or [f.get("code") for f in check.get("findings", [])]))
        sync_proc = fixture.cli("canon", "sync", "--json", "--host", HOST)
        sync = _document(sync_proc, "canon sync")
        if sync_proc.returncode != 0 or sync.get("errors"):
            raise WitnessFailure("canon sync: %s" % (sync.get("errors") or sync.get("diagnostic")))
        active = _json(fixture.cli("modules", "--active", "--json"), "modules --active")
        names = {row.get("module") for row in active.get("active", [])}
        if PIPELINE not in names:
            raise WitnessFailure("active modules do not contain %s: %r" % (PIPELINE, names))

        admitted = _json(fixture.cli("process", "check", process_path, "--json"),
                         "process check")
        if not admitted.get("document"):
            raise WitnessFailure("process check returned no compiled document")
        refused = _refused(fixture.cli("process", "check", fixture.write(
            "broken-process.md", BROKEN_PROCESS), "--json"), "process check broken card")
        if "edge-undeclared" not in refused:
            raise WitnessFailure("broken Process card was refused without its named "
                                 "reason: %s" % refused[-300:])

        # 2. A lot, the Pipeline adoption, the verifier refusal and success.
        actor_env = {"AGENT_MAIL_NAME": ACTOR}
        added = _json(fixture.cli(
            "work", "add", "--title", "public fixture witness", "--app", PROJECT,
            "--new-project", "--assignee", ACTOR, "--estimate", "1h", "--type", "docs",
            "--json", "--actor", ACTOR), "work add")
        lot = added["lot"]["id"]
        adopted = _json(fixture.cli("module", PIPELINE, "adopt", str(lot), env=actor_env),
                        "module adopt")
        if adopted.get("workflow_stage") != "draft":
            raise WitnessFailure("adoption did not start at draft: %r" % adopted)
        denial = _refused(fixture.cli(
            "module", PIPELINE, "advance", str(lot), "--to", "done",
            "--guard", "review_requested", env=actor_env), "advance review_requested")
        if "verifier-refused" not in denial:
            raise WitnessFailure("expected verifier-refused, got: %s" % denial[-300:])
        accepted = _json(fixture.cli(
            "module", PIPELINE, "advance", str(lot), "--to", "review",
            "--guard", "planning", env=actor_env), "advance planning")
        if not accepted.get("accepted"):
            raise WitnessFailure("planning was not accepted: %r" % accepted)
        shown = _json(fixture.cli("work", "show", str(lot), "--json"), "work show")
        if shown.get("workflow_stage") != "review":
            raise WitnessFailure("journal stage is %r, expected review"
                                 % shown.get("workflow_stage"))
        notes = [event.get("note", "") for event in shown.get("events", [])]
        if not any("pipeline adopted" in note for note in notes):
            raise WitnessFailure("journal has no adoption event: %r" % notes[:5])
        if not any("draft" in note and "review" in note and "planning" in note
                   for note in notes):
            raise WitnessFailure("journal has no planning transition: %r" % notes[:5])
        report["process"] = {"lot": lot, "stage": shown.get("workflow_stage"),
                             "events": len(notes), "admitted": True, "refused": True}

        # 3. Native usage and accounting observation, from the public CLI.
        turns = _json(fixture.cli("cost", "turns", "--json"), "cost turns")
        budget = _json(fixture.cli("budget", "--json"), "budget")
        if not isinstance(turns, dict) or "turns" not in turns:
            raise WitnessFailure("cost turns is not the documented document: %r" % turns)
        if not isinstance(budget, dict):
            raise WitnessFailure("budget is not the documented document: %r" % budget)
        report["usage"] = {"turns": len(turns.get("turns") or []),
                           "budget_keys": sorted(budget)[:8]}

        # 4. Mail usage, refusal and races through the published Python surface.
        sent = _ok(fixture.mail("send", PEER, "cli hello", "--from", ACTOR), "agent-mail send")
        if sent.returncode != 0:
            raise WitnessFailure("agent-mail send failed")
        db = fixture.db
        receipt = public_mail.submit(
            db, ACTOR, public_mail.Submission(
                PEER, "python hello", payload={"module": {"witness": True}},
                idempotency_key="witness-usage-1"))
        found = public_mail.find(db, ACTOR, PEER, "witness-usage-1")
        if found is None or found.id != receipt.id or not found.reused:
            raise WitnessFailure("find did not return the durable receipt")
        history = public_mail.history(db, ACTOR, peer=PEER)
        bodies = [entry.body for entry in history.entries]
        if "python hello" not in bodies or "cli hello" not in bodies:
            raise WitnessFailure("history does not carry both mail paths: %r" % bodies)
        refusals = {
            "reserved_actor": _error(
                lambda: public_mail.submit(
                    db, "ameesh", public_mail.Submission(PEER, "reserved")),
                "reserved actor"),
            "unknown_recipient": _error(
                lambda: public_mail.submit(
                    db, ACTOR, public_mail.Submission("nobody-here", "unknown")),
                "unknown recipient"),
            "idempotency_conflict": _error(
                lambda: public_mail.submit(
                    db, ACTOR, public_mail.Submission(
                        PEER, "another body", idempotency_key="witness-usage-1")),
                "idempotency conflict"),
        }
        identical = _race(fixture, "witness-race-1", ["same one", "same one"])
        ids = {outcome.get("id") for outcome in identical if "id" in outcome}
        if len(ids) != 1 or len(identical) != 2:
            raise WitnessFailure("identical race did not end on one durable id: %r" % identical)
        if sum(1 for outcome in identical if outcome.get("reused")) != 1:
            raise WitnessFailure("identical race did not mark exactly one reuse: %r" % identical)
        raced_history = public_mail.history(db, ACTOR, peer=PEER)
        if sum(1 for entry in raced_history.entries if entry.body == "same one") != 1:
            raise WitnessFailure("identical race left more than one durable row")
        conflicting = _race(fixture, "witness-race-2", ["first body", "second body"])
        if len(conflicting) != 2:
            raise WitnessFailure("conflicting race did not return two outcomes: %r" % conflicting)
        if sum(1 for outcome in conflicting if "id" in outcome) != 1:
            raise WitnessFailure("conflicting race did not end on one durable row: %r" % conflicting)
        if sum(1 for outcome in conflicting if outcome.get("code") == "conflict") != 1:
            raise WitnessFailure("conflicting race did not name one conflict: %r" % conflicting)
        report["mail"] = {"usage": True, "refusals": refusals,
                          "identical": identical, "conflicting": conflicting,
                          "history": len(history.entries)}

        # 5. The named read views are the only SQL the fixture exposes.
        agents = fixture.read_view("agent_mesh_overview", ("name", "host"))
        registered = {row["name"] for row in agents}
        if not {ACTOR, PEER} <= registered:
            raise WitnessFailure("named view does not carry the two agents: %r" % registered)
        report["views"] = {"agents": sorted(registered)}
    finally:
        fixture.close()
    report["teardown"] = {"database": database, "closed": True}
    report["checks"] = [name for name in
                        ("fixture", "canon", "process", "usage", "mail", "views", "teardown")
                        if name in report]


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    admin = os.environ.get("AMEESH_FIXTURE_ADMIN_DSN", "")
    if not admin:
        print("AMEESH_FIXTURE_ADMIN_DSN is required (a DSN allowed to CREATE DATABASE)",
              file=sys.stderr)
        return 2
    report = {"schema": SCHEMA, "checks": [], "ok": False}
    try:
        run(admin, report)
        report["ok"] = True
    except Exception as exc:  # noqa: BLE001 — the report is the public result
        report["error"] = "%s: %s" % (type(exc).__name__, exc)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
