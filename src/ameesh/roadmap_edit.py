# SPDX-License-Identifier: AGPL-3.0-only
"""Roadmap editing and portable, transactional JSON exchange (#2179).

No execution lots or agents are created. Dependency edits share the work graph
lock; compare-and-swap also protects against older clients that do not lock.
"""
from __future__ import annotations

import hashlib
import json
import re

from . import planned_waits, roadmap, storage, work

SCHEMA = "ameesh-plan/1"
MAX_BYTES = 10 * 1024 * 1024
MAX_ENTRIES = 1000
_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_COMMITMENT = re.compile(r"^(?:commitment\s+)?e([1-9][0-9]{0,18})$", re.I)
_FIELDS = {"what": "what", "due": "due_on", "kind": "kind", "status": "status",
           "owner": "owner", "project": "project", "lot": "work_item_id",
           "card": "package_id", "source_type": "source_kind", "source": "source_ref",
           "depends_on": "depends_on", "note": "note"}
EDITABLE_FIELDS = tuple(field for field in _FIELDS if field != "status")


class InvalidDependencies(roadmap.RoadmapError):
    def __init__(self, report):
        self.report = report
        super().__init__("invalid dependencies: %s" % json.dumps(report["errors"], ensure_ascii=False))


def ident(value) -> int:
    text = str(value).strip()
    if text.lower().startswith("e"):
        text = text[1:]
    if not re.fullmatch(r"[1-9][0-9]{0,18}", text) or int(text) > 9223372036854775807:
        raise roadmap.RoadmapError("expected a commitment id (e18 or 18): %r" % value)
    return int(text)


def token(row: dict) -> str:
    """Optimistic editing token returned by `get`; opaque to the caller."""
    body = {k: row.get(k) for k in _FIELDS.values()}
    body["updated_ts"] = row.get("updated_ts")
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def export_key(row: dict) -> str:
    if row.get("proposal_key"):
        return row["proposal_key"]
    # Stable for old rows without a key, including after editing their text.
    identity = "%s:%.6f" % (row["id"], float(row.get("created_ts") or 0))
    return "export:" + hashlib.sha256(identity.encode()).hexdigest()


def _reference(ref: str) -> str:
    ref = ref.strip()
    match = _COMMITMENT.fullmatch(ref)
    return "e" + str(int(match[1])) if match else ref


def payload(row: dict) -> dict:
    value = dict({k: row.get(v) for k, v in _FIELDS.items()}, key=export_key(row))
    refs = value["depends_on"]
    if isinstance(refs, list) and all(isinstance(ref, str) for ref in refs):
        # The documented spellings (e1, E1, commitment e1) identify the same
        # edge. An export/import must not require rewriting legacy spelling.
        value["depends_on"] = list(dict.fromkeys(_reference(ref) for ref in refs))
    return value


def get(db, value) -> dict:
    rows = storage.of(db).roadmap.commitments(ids=[ident(value)])
    if not rows:
        raise roadmap.RoadmapError("commitment %s not found" % value)
    return dict(rows[0], etag=token(rows[0]))


def listing(db, *, project=None, statuses=None) -> list[dict]:
    rows = storage.of(db).roadmap.commitments(statuses=statuses)
    return [r for r in rows if not project or r.get("project") in (None, project)]


def export(db, *, project=None, all_states=False) -> dict:
    rows = listing(db, project=project, statuses=None if all_states else roadmap.LIVE_STATUSES)
    aliases = {r["id"]: export_key(r) for r in rows}
    entries = []
    for row in rows:
        entry = payload(row)
        entry["depends_on"] = []
        for reference in row.get("depends_on") or []:
            match = (_COMMITMENT.fullmatch(reference.strip())
                     if isinstance(reference, str) else None)
            entry["depends_on"].append("@" + aliases[int(match[1])] if match and int(match[1]) in aliases else reference)
        entries.append(entry)
    return {"schema": SCHEMA, "commitments": entries}


def document(text: str) -> dict:
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise roadmap.RoadmapError("plan file exceeds 10 MiB")
    def unique_fields(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON field: %s" % key)
            result[key] = value
        return result

    try:
        body = json.loads(text, object_pairs_hook=unique_fields)
    except (ValueError, RecursionError) as exc:
        raise roadmap.RoadmapError("invalid plan JSON: %s" % exc) from None
    if not isinstance(body, dict) or body.get("schema") != SCHEMA:
        raise roadmap.RoadmapError("expected schema %s" % SCHEMA)
    if set(body) - {"schema", "commitments"}:
        raise roadmap.RoadmapError("unknown plan fields: %s" % ", ".join(sorted(set(body) - {"schema", "commitments"})))
    entries = body.get("commitments")
    if not isinstance(entries, list) or len(entries) > MAX_ENTRIES:
        raise roadmap.RoadmapError("commitments must be a list of at most %d entries" % MAX_ENTRIES)
    keys = set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("key"), str) or not _KEY.fullmatch(entry["key"]):
            raise roadmap.RoadmapError("each entry needs a stable key (1–256 letters, digits, ._:/-)")
        if entry["key"] in keys:
            raise roadmap.RoadmapError("duplicate plan key: %s" % entry["key"])
        keys.add(entry["key"])
        unknown = set(entry) - set(_FIELDS) - {"key"}
        if unknown:
            raise roadmap.RoadmapError("%s: unknown fields: %s" % (entry["key"], ", ".join(sorted(unknown))))
    return body


def _normalize(db, fields, *, base=None, now=None, literal_dates=False, warnings=None):
    before = payload(base) if base else None
    value = dict(before or {"kind": "commitment", "status": "proposed",
        "owner": None, "project": None, "lot": None, "card": None,
        "source_type": "conversation", "source": None, "note": "", "depends_on": [], "due": None})
    unknown = set(fields) - set(_FIELDS) - {"key"}
    if unknown:
        raise roadmap.RoadmapError("unknown fields: %s" % ", ".join(sorted(unknown)))
    value.update(fields)
    if not isinstance(value.get("what"), str) or not value["what"].strip():
        raise roadmap.RoadmapError("empty commitment: say what")
    value["what"] = " ".join(value["what"].split())
    if value["kind"] not in ("commitment", "decision") or value["status"] not in roadmap.STATUSES:
        raise roadmap.RoadmapError("invalid commitment kind or status")
    if value["source_type"] not in roadmap.SOURCE_KINDS:
        raise roadmap.RoadmapError("unknown source type: %r" % value["source_type"])
    for key in ("owner", "project", "card", "source"):
        if value[key] is not None and (not isinstance(value[key], str) or not value[key].strip()):
            raise roadmap.RoadmapError("%s must be nonempty text or null" % key)
        if value[key] is not None:
            value[key] = value[key].strip()
    if not isinstance(value["note"], str):
        raise roadmap.RoadmapError("note must be text")
    due = value["due"]
    if due is not None:
        if literal_dates and (not isinstance(due, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", due)):
            raise roadmap.RoadmapError("JSON dates must be YYYY-MM-DD or null")
        value["due"] = roadmap.parse_day(due, now=now)
    if value["status"] in ("open", "done") and value["kind"] == "commitment" and value["due"] is None:
        raise roadmap.RoadmapError("a commitment needs a date (or --proposed)")
    lot = value["lot"]
    if lot is not None:
        text = str(lot).strip().lstrip("#")
        if isinstance(lot, bool) or not re.fullmatch(r"[0-9]{1,18}", text) or int(text) <= 0:
            raise roadmap.RoadmapError("lot must be a positive task number or null")
        value["lot"] = int(text)
        item = work.get(db, value["lot"])
        # A stored lot/project pair that has since gone stale must not lock an
        # edit of an unrelated field: only a newly introduced pair raises.
        stale_lot = before is not None and value["lot"] == before.get("lot")
        if item is None:
            if warnings is not None and stale_lot:
                warnings.append({"code": "stale_task", "reference": "task #%s" % value["lot"]})
            else:
                raise roadmap.RoadmapError("task %s not found" % lot)
        else:
            item_project = item.get("app") or item.get("workstream")
            if value["project"] and item_project and value["project"] != item_project:
                if warnings is not None and stale_lot and value["project"] == before.get("project"):
                    warnings.append({"code": "stale_project_conflict",
                                     "reference": "task #%s" % value["lot"],
                                     "message": "project %s conflicts with attached task %s (%s)"
                                                % (value["project"], value["lot"], item_project)})
                else:
                    raise roadmap.RoadmapError("project conflicts with attached task %s (%s)" % (lot, item_project))
            value["project"] = value["project"] or item_project or None
            if base is None or ("lot" in fields and "owner" not in fields):
                value["owner"] = value["owner"] or item.get("assignee") or None
    if value["card"] and storage.of(db).packages.get(value["card"]) is None:
        if warnings is not None and before is not None and value["card"] == before.get("card"):
            warnings.append({"code": "stale_package", "reference": value["card"]})
        else:
            raise roadmap.RoadmapError("unknown WorkPackage: %s" % value["card"])
    refs = value["depends_on"]
    if not isinstance(refs, list) or any(not isinstance(r, str) or not r.strip() for r in refs):
        raise roadmap.RoadmapError("depends_on must be a list of nonempty references")
    value["depends_on"] = list(dict.fromkeys(_reference(r) for r in refs))
    if value.get("key") is not None and (not isinstance(value["key"], str) or not _KEY.fullmatch(value["key"])):
        raise roadmap.RoadmapError("invalid stable key")
    return value


def _components(nodes, edges):
    """Strongly connected components (Kosaraju, iterative).

    Imported graphs can be long: no recursion, and one pass rather than a
    reachability walk per introduced edge.
    """
    order, seen = [], set()
    for start in nodes:
        if start in seen:
            continue
        seen.add(start)
        stack = [(start, iter(edges.get(start, ())))]
        while stack:
            node, children = stack[-1]
            for child in children:
                if child in nodes and child not in seen:
                    seen.add(child)
                    stack.append((child, iter(edges.get(child, ()))))
                    break
            else:
                order.append(node)
                stack.pop()
    reverse = {node: [] for node in nodes}
    for node in nodes:
        for child in edges.get(node, ()):
            if child in nodes:
                reverse[child].append(node)
    component, number = {}, 0
    for start in reversed(order):
        if start in component:
            continue
        component[start] = number
        stack = [start]
        while stack:
            node = stack.pop()
            for parent in reverse[node]:
                if parent not in component:
                    component[parent] = number
                    stack.append(parent)
        number += 1
    return component


def _references(db, rows, *, roots=None, validate_fields=False, introduced=None):
    """Validate references and cycles in the reachable commitment graph.

    ``introduced`` is the set of ``(node, reference)`` edges the current edit
    adds or changes. When given, an invalid or cyclic edge that already existed
    is reported as a warning (``"stale": True``) instead of an error: a legacy
    reference must not lock the editing of an unrelated field. When it is None
    (``plan validate``), every edge is checked strictly.
    """
    errors, warnings, edges = [], [], {}
    touched = {node for node, _ref in introduced} if introduced is not None else None

    def bad(node, issue, ref=None):
        if introduced is not None and (
                (node, _reference(ref)) not in introduced if ref is not None else node not in touched):
            warnings.append(dict(issue, stale=True))
        else:
            errors.append(issue)

    packages = {p["id"] for p in storage.of(db).packages.all() if p.get("present", True)}
    tasks = {}
    todo = list(rows if roots is None else roots)
    visited = set()
    while todo:
        node = todo.pop()
        if node in visited:
            continue
        visited.add(node)
        row = rows[node]
        edges[node] = []
        if validate_fields:
            try:
                row = _normalize(db, row, literal_dates=True)
            except roadmap.RoadmapError as exc:
                errors.append({"id": node, "code": "invalid_commitment", "message": str(exc)})
                continue
        refs = row.get("depends_on") or []
        if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs):
            bad(node, {"id": node, "code": "invalid_dependencies"})
            continue
        for ref in refs:
            target = ref if ref.startswith("@") else None
            match = _COMMITMENT.fullmatch(ref)
            if match:
                target = "e"+str(int(match[1]))
            if target is not None:
                if target not in rows:
                    bad(node, {"id": node, "code": "unknown_dependency", "reference": ref}, ref)
                    continue
                edges[node].append(target)
                todo.append(target)
                other = rows[target]
                if other.get("status") == "cancelled":
                    warnings.append({"id": node, "code": "cancelled_dependency", "reference": ref})
                if row.get("due") and other.get("due") and row["due"] < other["due"]:
                    warnings.append({"id": node, "code": "dependency_due_later", "reference": ref})
                continue
            if planned_waits._GATE.fullmatch(ref):
                continue
            match = planned_waits._LOT.fullmatch(ref)
            if match:
                task = int(match[1])
                if task not in tasks:
                    tasks[task] = work.get(db, task)
                if tasks[task] is None:
                    bad(node, {"id": node, "code": "unknown_task", "reference": ref}, ref)
                elif row.get("lot") == task:
                    bad(node, {"id": node, "code": "self_dependency", "reference": ref}, ref)
                continue
            package_ref = planned_waits._PACKAGE.fullmatch(ref)
            if (package_ref[1] if package_ref else ref) not in packages:
                bad(node, {"id": node, "code": "unknown_package", "reference": ref}, ref)
    # Kahn's algorithm avoids recursion limits on long, valid imported chains.
    degree = {node: 0 for node in edges}
    for children in edges.values():
        for child in children:
            if child in degree:
                degree[child] += 1
    ready = [node for node, n in degree.items() if n == 0]
    while ready:
        node = ready.pop()
        for child in edges.get(node, ()):
            degree[child] -= 1
            if degree[child] == 0:
                ready.append(child)
        del degree[node]
    if degree:
        issue = {"code": "dependency_cycle", "ids": sorted(degree)}
        fresh = False
        if introduced is not None:
            # An introduced edge only introduces the cycle when its target can
            # already climb back to its source: both ends sit in the same
            # strongly connected component. An edge leaving an inherited cycle
            # towards a dependency downstream of it stays safe (warning only),
            # as does the rest of the inherited cycle.
            component = _components(degree, edges)
            fresh = any(source in degree and child in degree
                        and component[source] == component[child]
                        for source, child in introduced)
        if introduced is not None and not fresh:
            warnings.append(dict(issue, stale=True))
        else:
            errors.append(issue)
    return {"valid": not errors, "errors": errors, "warnings": warnings}


def _require_valid(report):
    if not report["valid"]:
        raise InvalidDependencies(report)


def validate(db, *, project=None):
    rows = storage.of(db).roadmap.commitments()
    graph = {"e"+str(r["id"]): payload(r) for r in rows}
    roots = ["e"+str(r["id"]) for r in rows if r["status"] in roadmap.LIVE_STATUSES
             and (not project or r.get("project") in (None, project))]
    return dict(_references(db, graph, roots=roots, validate_fields=True), schema="ameesh-plan-validation/1")


def _database_row(value, *, actor):
    return dict({column: value.get(field) for field, column in _FIELDS.items()},
                proposal_key=value.get("key"), created_by=actor)


def create(db, fields, *, actor="", now=None):
    # Serialization covers lookup, reference validation and insertion.
    with db.transaction() as tx:
        st = storage.of(tx)
        st.work_dependencies.lock()
        value = _normalize(tx, fields, now=now)
        existing = st.roadmap.commitments()
        if value.get("key"):
            same = next((r for r in existing if export_key(r) == value["key"]), None)
            if same:
                if payload(same) != value:
                    raise roadmap.RoadmapError("stable key already has different content: %s (use plan update)" % value["key"])
                return dict(same, unchanged=True)
        graph = {"e"+str(r["id"]): payload(r) for r in existing}
        graph["@new"] = value
        _require_valid(_references(tx, graph, roots=["@new"]))
        if value["status"] not in roadmap.LIVE_STATUSES:
            raise roadmap.RoadmapError("new commitments must be open or proposed")
        return st.roadmap.add_commitment(_database_row(value, actor=actor))


def update(db, value, fields, *, if_match=None, now=None, warnings=None):
    collected = [] if warnings is None else warnings
    with db.transaction() as tx:
        st = storage.of(tx)
        st.work_dependencies.lock()
        current = get(tx, value)
        if if_match is not None and if_match != current["etag"]:
            raise roadmap.RoadmapError("commitment changed: get it again before updating")
        if current["status"] not in roadmap.LIVE_STATUSES:
            raise roadmap.RoadmapError("terminal commitment: metadata cannot be edited")
        if not fields:
            raise roadmap.RoadmapError("nothing to update")
        if "key" in fields or "status" in fields:
            raise roadmap.RoadmapError("keys are immutable; use accept/done/cancel to change status")
        prepared = _normalize(tx, fields, base=current, now=now, warnings=collected)
        node = "e"+str(current["id"])
        # A legacy jsonb value may hold non-string refs (written outside the
        # CLI): they cannot be hashed, and they are not edges of this edit.
        previous = {ref for ref in payload(current)["depends_on"] if isinstance(ref, str)}
        introduced = {(node, ref) for ref in prepared["depends_on"] if ref not in previous}
        graph = {"e"+str(r["id"]): payload(r) for r in st.roadmap.commitments()}
        graph[node] = prepared
        report = _references(tx, graph, roots=[node], introduced=introduced)
        _require_valid(report)
        collected.extend(report["warnings"])
        if prepared == payload(current):
            return dict(current, unchanged=True)
        changes = {column: prepared[field] for field, column in _FIELDS.items()
                   if prepared[field] != current.get(column)}
        result = st.roadmap.set_commitment(current["id"], current=roadmap.LIVE_STATUSES,
                                          values=changes, expect=current)
        if result is None:
            raise roadmap.RoadmapError("commitment changed while updating: retry")
        return dict(result, etag=token(result))


def _prepare_import(db, body, existing, *, update_existing):
    keyed = {export_key(r): r for r in existing}
    aliases = {entry["key"]: ("e"+str(keyed[entry["key"]]["id"]) if entry["key"] in keyed else "@"+entry["key"])
               for entry in body["commitments"]}
    graph = {"e"+str(r["id"]): payload(r) for r in existing}
    plan, introduced, warnings = [], set(), []
    for entry in body["commitments"]:
        current = keyed.get(entry["key"])
        normalized = _normalize(db, entry, base=current, literal_dates=True, warnings=warnings)
        normalized["depends_on"] = list(dict.fromkeys(aliases.get(ref[1:], ref) if ref.startswith("@") else ref
                                                       for ref in normalized["depends_on"]))
        node = aliases[entry["key"]]
        graph[node] = normalized
        previous = ({ref for ref in payload(current)["depends_on"] if isinstance(ref, str)}
                    if current else set())
        introduced.update((node, ref) for ref in normalized["depends_on"] if ref not in previous)
        action = "create"
        if current:
            if normalized == payload(current):
                action = "unchanged"
            elif not update_existing:
                raise roadmap.RoadmapError("key %s differs: use --update to edit existing entries" % entry["key"])
            elif current["status"] not in roadmap.LIVE_STATUSES:
                raise roadmap.RoadmapError("terminal commitment cannot be updated: %s" % entry["key"])
            elif current["status"] == "open" and normalized["status"] == "proposed":
                raise roadmap.RoadmapError("an open commitment cannot become a proposal: %s" % entry["key"])
            else:
                action = "update"
        plan.append({"node": node, "action": action, "value": normalized, "current": current})
    validation = _references(db, graph, roots=[p["node"] for p in plan], introduced=introduced)
    _require_valid(validation)
    return plan, warnings + validation["warnings"]


def import_plan(db, body, *, apply=False, update_existing=False, actor=""):
    body = document(json.dumps(body, ensure_ascii=False))
    if not apply:
        rows = storage.of(db).roadmap.commitments()
        plan, warnings = _prepare_import(db, body, rows, update_existing=update_existing)
        return {"schema": "ameesh-plan-import/1", "applied": False,
                "warnings": warnings,
                "changes": [{"action": p["action"], "id": p["current"]["id"] if p["current"] else None,
                             **p["value"]} for p in plan]}
    with db.transaction() as tx:
        st = storage.of(tx)
        st.work_dependencies.lock()
        plan, warnings = _prepare_import(tx, body, st.roadmap.commitments(), update_existing=update_existing)
        actual, output = {}, []
        # Allocate all identities first; the transaction hides incomplete links.
        for p in plan:
            if p["current"]:
                actual[p["node"]] = p["current"]["id"]
            else:
                initial = dict(p["value"], depends_on=[])
                row = st.roadmap.add_commitment(_database_row(initial, actor=actor))
                actual[p["node"]] = row["id"]
        for p in plan:
            wanted = dict(p["value"])
            wanted["depends_on"] = list(dict.fromkeys("e"+str(actual[r]) if r in actual else r for r in wanted["depends_on"]))
            ident_value = actual[p["node"]]
            if p["action"] == "unchanged":
                row = p["current"]
            else:
                values = _database_row(wanted, actor=actor)
                row = st.roadmap.set_commitment(ident_value, current=roadmap.STATUSES,
                    values=values, expect=p["current"])
                if row is None:
                    raise roadmap.RoadmapError("commitment changed during import: retry")
            output.append({"key": wanted["key"], "id": row["id"], "action": p["action"], "status": row["status"]})
        return {"schema": "ameesh-plan-import/1", "applied": True, "warnings": warnings, "changes": output}
