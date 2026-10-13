# SPDX-License-Identifier: AGPL-3.0-only
"""IT lifecycle: releases, publications and observed artifact execution."""
from __future__ import annotations

NAME = "it"
TYPES = ("release", "publication")

#: L244 (#420) : états de cycle de vie qu'une règle `require_qa` peut garder.
QA_STATES = ("merged", "promoted")


def descriptor(options=None):
    """L244 : `options` porte `require_qa` (états gardés par un verdict QA `ok`)."""
    options = dict(options or {})
    require_qa = options.pop("require_qa", None)
    if options:
        raise ValueError("unknown IT module option(s): %s" % ", ".join(sorted(options)))
    if require_qa is not None:
        if not isinstance(require_qa, list) or any(not isinstance(s, str) for s in require_qa):
            raise ValueError("it.require_qa must be a list of lifecycle states")
        if len(set(require_qa)) != len(require_qa):
            raise ValueError("it.require_qa: duplicate state")
        for state in require_qa:
            if state not in QA_STATES:
                raise ValueError("it.require_qa: state %r cannot require QA (choose from %s)"
                                 % (state, ", ".join(QA_STATES)))
    rules = [{"types": list(TYPES), "states": ["merged", "promoted"],
              "milestones": {"verdict": ["ok"]},
              "subject_kind": "delivery", "evidence_kind": "proof"}]
    schemas = {
        "delivery": {"strings": ["artifact", "sha256"], "subject_field": "sha256",
                     "patterns": {"sha256": "^[0-9a-f]{64}$"}},
        "proof": {"strings": ["artifact", "sha256", "output"],
                  "arrays": ["command"], "equals": {"exit_code": 0},
                  "subject_field": "sha256", "binding_kind": "delivery",
                  "writer_role": "ameesh_it_proof",
                  "patterns": {"sha256": "^[0-9a-f]{64}$"}},
        "qa": {"strings": ["sha256", "verdict", "report"],
               "arrays": ["scope"],
               "patterns": {"sha256": "^[0-9a-f]{64}$", "verdict": "^(ok|blocked)$"},
               "subject_field": "sha256", "binding_kind": "delivery",
               "writer_role": "ameesh_it_qa",
               # L244 : le verdict vient d'un porteur du rôle `qa`, distinct
               # de l'assigne, de l'auteur de la livraison et de celui de la
               # preuve (séparation des devoirs, refus `qa-same-actor`).
               "actor_role": "qa",
               "separation_of_duties": {"assignee": True, "declaration": True,
                                        "evidence": "proof"}},
    }
    if require_qa:
        # The gate reads the current delivery and demands a matching QA verdict
        # `ok`; a `blocked` verdict or an older delivery keeps it closed.
        rules.append({"types": list(TYPES), "states": list(require_qa),
                      "subject_kind": "delivery", "evidence_kind": "qa",
                      "evidence_verdict": "ok"})
        # R2 #420 (B3) : la séparation d'avec l'auteur de la preuve n'a de sens
        # que si la preuve est attribuable. L'assistant sudo écrivait
        # `uid:<SUDO_UID>` : un projet gardé par `require_qa` exige donc une
        # preuve signée par l'agent lié à son exécuteur.
        schemas["proof"]["runner_bound_actor"] = True
    return {
        "types": list(TYPES),
        "schemas": schemas,
        "rules": rules,
    }


def item(db, item_id):
    from ... import work
    from ..storage import ModuleStore
    row = work.get(db, item_id)
    if row is None:
        raise ValueError("lot %s does not exist" % item_id)
    project = row.get("app") or row.get("workstream") or ""
    if row["type"] not in TYPES or not ModuleStore(db).enabled(project, NAME):
        raise ValueError("lot %s requires an active IT module and release/publication type" % item_id)
    return row


def declare(db, item_id, artifact, *, actor):
    from .execution import digest
    from ..storage import ModuleStore
    item(db, item_id)
    path, sha = digest(artifact)
    return ModuleStore(db).record(item_id, NAME, "delivery", sha,
                                  {"artifact": path, "sha256": sha}, actor=actor)


def prove(db, item_id, artifact, *, actor, runner=None, arguments=(), timeout=60, execution_user=None,
          snapshot_root=None, snapshot_limits=None, identity=None, invoker=None):
    from .execution import execute
    from ..storage import ModuleStore
    item(db, item_id)
    store = ModuleStore(db)
    delivery = store.latest(item_id, NAME, "delivery")
    if delivery is None:
        raise ValueError("declare the delivered artifact before running its proof")
    evidence = execute(artifact, runner=runner, arguments=arguments, timeout=timeout,
                       execution_user=execution_user, snapshot_root=snapshot_root,
                       expected_sha=delivery["subject"], snapshot_limits=snapshot_limits)
    if evidence["sha256"] != delivery["subject"]:
        raise ValueError("executed artifact differs from the declared delivery")
    # R2 #420 (B3) : l'identité liée de l'agent demandeur, pour que la preuve
    # soit attribuable (la séparation des devoirs du verdict QA la compare) ;
    # `invoker` garde la trace de l'uid qui a demandé l'exécution.
    if identity:
        evidence.update(identity)
    if invoker:
        evidence["invoker"] = invoker
    # The database rechecks this exact declaration under the item lock.
    return store.record(item_id, NAME, "proof", evidence["sha256"], evidence,
                        actor=actor, binding_id=delivery["id"])


def record_qa(db, item_id, *, sha256, verdict, scope, report, actor):
    """L244 (#420): le verdict QA d'un porteur du rôle `qa`, lié à la livraison courante.

    Le contrôle de rôle et la séparation des devoirs sont faits par le garde SQL
    (sous le verrou du lot), pas ici : cette fonction ne fait que valider la
    forme et la liaison à la déclaration courante."""
    from ..storage import ModuleStore
    item(db, item_id)
    store = ModuleStore(db)
    delivery = store.latest(item_id, NAME, "delivery")
    if delivery is None:
        raise ValueError("declare the delivered artifact before recording its QA")
    if sha256 != delivery["subject"]:
        raise ValueError("QA digest differs from the current delivery; "
                         "declare or prove that artifact first")
    payload = {"sha256": sha256, "verdict": verdict, "scope": [s for s in scope if s],
               "report": report}
    return store.record(item_id, NAME, "qa", sha256, payload,
                        actor=actor, binding_id=delivery["id"])


def main(db, argv, *, actor):
    import argparse
    import json
    from ..storage import ModuleStore
    split = argv.index("--") if "--" in argv else len(argv)
    options, arguments = argv[:split], argv[split + 1:]
    parser = argparse.ArgumentParser(prog="ameesh module it")
    sub = parser.add_subparsers(dest="operation", required=True)
    for name in ("delivery", "proof"):
        command = sub.add_parser(name)
        command.add_argument("lot", type=int)
        command.add_argument("--artifact", required=True)
        if name == "proof":
            command.add_argument("--runner", help="optional interpreter/compatibility executable")
            command.add_argument("--timeout", type=float, default=60)
    qa = sub.add_parser("qa")
    qa.add_argument("lot", type=int)
    qa.add_argument("--sha256", required=True, help="digest of the delivered artifact")
    qa.add_argument("--verdict", required=True, choices=("ok", "blocked"))
    qa.add_argument("--scope", default="", help="what was exercised, comma-separated")
    qa.add_argument("--report", required=True, help="link to the QA report")
    show = sub.add_parser("show")
    show.add_argument("lot", type=int)
    args = parser.parse_args(options)
    if arguments and args.operation != "proof":
        parser.error("artifact arguments are only valid for proof")
    if args.operation == "show":
        row = ModuleStore(db).milestones(args.lot)
    elif args.operation == "delivery":
        row = declare(db, args.lot, args.artifact, actor=actor)
    elif args.operation == "qa":
        # L244 (N2 #420) : une signature de droits exige une identité liée
        # (bail du runner, ou liaison de session L41), jamais un nom seulement
        # auto-déclaré par `AGENT_MAIL_NAME`.
        from ... import config as config_mod, identity
        binding = identity.require_bound(config_mod.load(), db, what="the QA verdict")
        if actor and actor != binding.name:
            raise ValueError("the QA verdict is signed by the bound session %s, not %s"
                             % (binding.name, actor))
        scope = [s.strip() for s in args.scope.split(",")]
        row = record_qa(db, args.lot, sha256=args.sha256, verdict=args.verdict,
                        scope=scope, report=args.report, actor=binding.name)
    else:
        from ... import config as config_mod, identity
        from .proof_service import request
        project_row = item(db, args.lot)
        project = project_row.get("app") or project_row.get("workstream") or ""
        # R2 #420 (B3) : le helper sudo ne voyait que `uid:<SUDO_UID>` ; l'agent
        # demandeur est lié à son exécuteur, et cette identité part avec la
        # demande d'exécution pour rendre la preuve attribuable.
        binding = identity.require_bound(config_mod.load(), db, what="the IT proof",
                                         sources=("runner",))
        row = request(project, args.lot, args.artifact, runner=args.runner,
                      arguments=arguments, timeout=args.timeout,
                      identity={"agent": binding.name, "runner_id": binding.runner_id,
                                "lease_epoch": binding.epoch})
    print(json.dumps(row, ensure_ascii=False, default=str))
    return 0


def _qa_states(policy):
    """États gardés par une règle `require_qa` du module IT (L244)."""
    states = []
    for rule in (policy or {}).get("rules") or []:
        if rule.get("evidence_kind") == "qa":
            states.extend(rule.get("states") or [])
    return sorted(set(states))


def audit(store, project):
    """Point 12: execution evidence and, under `require_qa`, the QA verdict."""
    out = []
    for row in store.audit_evidence(project, NAME, TYPES, "delivery", "proof"):
        if row["proved"]:
            continue
        declaration = row["declaration_id"]
        missing = "delivery declaration missing" if declaration is None else "execution proof missing or stale"
        detail = ("IT procedure deviation: %s lot #%s in project %s: %s. "
                  "Declare the delivered artifact and record successful execution for its current "
                  "SHA-256 with ameesh module it delivery/proof; audit never executes it."
                  % (row["type"], row["id"], project, missing))
        out.append({"type": "module_deviation", "module": NAME,
                    "rule": "execution-proof", "project": project, "lot": row["id"],
                    "title": row["title"], "agent": None, "assignee": row["assignee"],
                    "lot_state": row["state"],
                    "declaration_id": declaration, "subject": row["subject"],
                    "since": row["declaration_ts"] or row["created_ts"],
                    "value": 1, "threshold": 0,
                    "reason": "it:execution-proof:%s" % (declaration or "undeclared"),
                    "detail": detail, "action": detail})
    out.extend(_qa_findings(store, project))
    return out


def _qa_findings(store, project):
    """L244: `missing_qa` (pas de verdict `ok` courant) et `stale_qa` (verdict lié à une
    livraison antérieure), sur les seuls états gardés par `require_qa`."""
    policy = store.policy(project, NAME)
    states = _qa_states(policy)
    if not states:
        return []
    out = []
    for row in store.audit_qa(project, NAME, TYPES, "delivery", "qa", states):
        if row["declaration_id"] is None:
            continue  # la livraison manquante est déjà un constat execution-proof
        evidence = row["evidence_id"]
        if evidence is not None and row["evidence_binding"] == row["declaration_id"]:
            if row["evidence_verdict"] == "ok":
                continue
            rule = "missing_qa"
            detail = ("IT procedure deviation: QA verdict is %r for the current delivery of %s lot "
                      "#%s in project %s; the gate stays closed until a QA holder records `ok` with "
                      "ameesh module it qa."
                      % (row["evidence_verdict"], row["type"], row["id"], project))
        elif evidence is not None:
            rule = "stale_qa"
            detail = ("IT procedure deviation: QA verdict of %s lot #%s in project %s is bound to an "
                      "earlier delivery; the current delivery needs its own verdict with ameesh "
                      "module it qa." % (row["type"], row["id"], project))
        else:
            rule = "missing_qa"
            detail = ("IT procedure deviation: %s lot #%s in project %s is in %s and requires QA for "
                      "its current delivery; record the verdict with ameesh module it qa after real "
                      "execution." % (row["type"], row["id"], project, row["state"]))
        out.append({"type": "module_deviation", "module": NAME,
                    "rule": rule, "project": project, "lot": row["id"],
                    "title": row["title"], "agent": None, "assignee": row["assignee"],
                    "lot_state": row["state"],
                    "declaration_id": row["declaration_id"], "subject": row["subject"],
                    "since": row["declaration_ts"] or row["created_ts"],
                    "value": 1, "threshold": 0,
                    "reason": "it:%s:%s" % (rule, row["declaration_id"]),
                    "detail": detail, "action": detail})
    return out
