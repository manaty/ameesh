# SPDX-License-Identifier: AGPL-3.0-only
"""Shadow plan for the automatic packet train (#2446).

The mode that must run before the switch-over: at each compiled packet cutoff,
compute the packet the active train would compose, publish it in the host's
state directory, and compare it with the manifest of the packet the human
composer actually merged. Nothing is reserved, published, dispatched or merged:
the inventory and the historical admission readers are used read-only.

The comparison is the proof the pilot asks for: lots the composer included but
the train omitted (with the named reason the train had), lots the train would
have included and the composer did not, and the packet stamp/base that matched.
A missing shadow plan for a packet is not an explained difference; it is
recorded as such.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime

from . import (merge_queue, resolutions, train, train_admissions,
               train_candidates, train_git, train_history, train_policy)

SCHEMA = "ameesh-train-shadow/1"
#: kept plans and comparisons: enough for the two real packets of the proof
#: and for the preceding ones after a restart.
MAX_PLANS = 24
MAX_COMPARISONS = 48
_MANUAL = re.compile(r"integration/paquet-h-(\d{8}-\d{4})")
_LOT = re.compile(r"#(\d+)")


def _atom(tag):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", tag)


class Store:
    """The per-project shadow state, written atomically on the host."""

    def __init__(self, path):
        self.path = path

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as source:
                document = json.load(source)
        except FileNotFoundError:
            return dict(schema=SCHEMA, plans=[], comparisons=[])
        except (OSError, ValueError) as exc:
            raise train.TrainError("shadow state unreadable: %s" % exc) from exc
        if not isinstance(document, dict) or document.get("schema") != SCHEMA \
                or not isinstance(document.get("plans"), list) \
                or not isinstance(document.get("comparisons"), list):
            raise train.TrainError("shadow state has an unknown schema")
        return document

    def save(self, document):
        folder = os.path.dirname(self.path)
        os.makedirs(folder, exist_ok=True)
        temporary = "%s.tmp-%d" % (self.path, os.getpid())
        with open(temporary, "w", encoding="utf-8") as target:
            json.dump(document, target, sort_keys=True, ensure_ascii=False, indent=1)
            target.write("\n")
        os.replace(temporary, self.path)

    def record_plan(self, document, plan):
        document = dict(document)
        plans = [row for row in document["plans"]
                 if not (row.get("cutoff") == plan["cutoff"]
                         and row.get("local_stamp") == plan["local_stamp"])]
        plans.append(plan)
        document["plans"] = plans[-MAX_PLANS:]
        self.save(document)
        return document

    def record_comparisons(self, document, comparisons):
        if not comparisons:
            return document
        document = dict(document)
        known = {row.get("packet", {}).get("sha") for row in document["comparisons"]}
        rows = [row for row in comparisons if row.get("packet", {}).get("sha") not in known]
        if not rows:
            return document
        document["comparisons"] = (document["comparisons"] + rows)[-MAX_COMPARISONS:]
        self.save(document)
        return document


class Passage:
    """One project's read-only shadow passage, with the runner service API."""

    #: the runner service does not require a packet reservation for a shadow
    #: passage, and must not run the engine's effects for it.
    shadow = True
    reservation = None

    def __init__(self, cfg, project, *, policy_path, policy_commit, workflows,
                 provider, mechanics, repository, candidate=None, notify=None):
        if not isinstance(project.name, str) or not project.name:
            raise train.TrainError("explicit shadow project required")
        if not isinstance(policy_commit, str) or not re.fullmatch(
                r"(?:[0-9a-f]{40}|[0-9a-f]{64})", policy_commit):
            raise train.TrainError("exact audited canon commit required")
        self.cfg, self.project = cfg, project
        self.policy = train.Policy(enabled=True)
        self.policy_path, self.policy_commit = policy_path, policy_commit
        self.workflows, self.provider, self.mechanics = workflows, provider, mechanics
        self.repository_source = repository
        self.candidate = candidate
        self.notify = notify or (lambda message: None)
        self.maintenance_at = None
        target = merge_queue._remote_branch(project.target)
        digest = train.digest([project.name, merge_queue.source_of(repository), target])
        folder = os.path.join(cfg.state_dir, "trains", "shadow")
        self.store = Store(os.path.join(folder, _atom(project.name) + "-" +
                                        _atom(target) + "-" + digest[:16] + ".json"))
        self.clone_path = os.path.join(folder, digest + ".clone")

    # -- runner service hooks ------------------------------------------------

    def step(self, db, now):
        result, _ = self.scheduled_step(db, now)
        return result

    def scheduled_step(self, db, now):
        rules = self.mechanics.load(self.policy_path, self.policy_commit,
                                    self.project.name)
        if rules.commit != self.policy_commit or rules.project != self.project.name:
            raise train.TrainError("compiled canon belongs to another commit or project")
        if rules.ci_packet != "full":
            raise train.TrainError("shadow train requires full packet CI in the compiled canon")
        deadline = rules.next_packet(now)
        slot = rules.last_packet(now)
        try:
            state = self.store.load()
            if not state["plans"] or state["plans"][-1].get("cutoff") != slot:
                plan = self.plan(db, rules, slot, now)
                state = self.store.record_plan(state, plan)
                result = {"result": "shadow-plan", "plan": plan}
            else:
                result = {"result": "shadow-idle"}
        except Exception as exc:  # a transient Git/SQL failure never stalls the clock
            self.notify("shadow plan failed (%s: %s)" % (type(exc).__name__, exc))
            return {"result": "shadow-error", "error": str(exc)}, deadline
        try:
            comparisons = self.compare(state, rules)
            self.store.record_comparisons(state, comparisons)
        except Exception as exc:  # the plan stays published; comparison retries next cutoff
            self.notify("shadow comparison failed (%s: %s)" % (type(exc).__name__, exc))
        return result, deadline

    # -- the read-only plan --------------------------------------------------

    def plan(self, db, rules, slot, now):
        repository = train_git.Repository(
            self.project, resolutions.Replayer(self.repository_source, self.clone_path))
        base = repository.target(self.project.target)
        history = train_history.Recorded(db, repository.clone.path,
                                         provider=self.provider, review_rules=rules)
        requirement = train_policy.Compiled(rules)
        admissions = train_admissions.Admissions(db, self.project, history,
                                                 policy=requirement)
        target = merge_queue._remote_branch(self.project.target)
        rows = [row for row in admissions.entries()
                if merge_queue._remote_branch(row["target"]) == target]
        deferred, members = [], []
        prepared_ids = set()
        if rows:
            ordered = train.ordered(rows, admissions.dependencies(rows))
            visible = {train.identity(row) for row in ordered}
            for row in rows:
                if train.identity(row) not in visible:
                    deferred.append(_deferred(row["work_item_id"], "queued",
                                              "open prerequisite or dependency cycle"))
            for row in ordered:
                if float(row.get("queued_ts") or 0) > slot:
                    deferred.append(_deferred(row["work_item_id"], "queued",
                                              "late admission: queued after the cutoff"))
                    continue
                try:
                    proof = admissions.qualify([row], base, "packet")["members"][0]
                except train.MemberDeferred as exc:
                    deferred.append(_deferred(row["work_item_id"], "queued", str(exc)))
                    continue
                except train.TrainError as exc:
                    deferred.append(_deferred(row["work_item_id"], "queued", str(exc)))
                    continue
                members.append(_member(row, origin="queued", proof=proof))
                prepared_ids.add(row["work_item_id"])
        if self.candidate is not None:
            inventory = train_candidates.Inventory(
                db, self.project, candidate=self.candidate).read()
            preparation = train_candidates.Preparation(
                db, self.project, repository=repository, history=history,
                policy=requirement)
            for candidate in inventory["candidates"]:
                lot = candidate["work_item_id"]
                if lot in prepared_ids:
                    continue
                if candidate["causes"]:
                    deferred.append(_deferred(lot, "candidate",
                                              "; ".join(candidate["causes"])))
                    continue
                try:
                    prepared = preparation.prepare(candidate, base)
                except train.TrainError as exc:
                    deferred.append(_deferred(lot, "candidate", str(exc)))
                    continue
                members.append(_member(dict(prepared["subject"], priority=2,
                                            rank=len(members), queued_ts=0),
                                       origin="candidate", proof=prepared))
                prepared_ids.add(lot)
        seen = set()
        deferred = [row for row in deferred
                    if not (row["work_item_id"] in seen
                            or seen.add(row["work_item_id"]))]
        deferred.sort(key=lambda row: (row["work_item_id"], row["origin"]))
        stamp = datetime.fromtimestamp(slot, rules.zone()).strftime("%Y%m%d-%H%M")
        plan = dict(schema=SCHEMA, project=self.project.name,
                    target=merge_queue._remote_branch(self.project.target),
                    cutoff=float(slot), local_stamp=stamp, base=base,
                    created_ts=float(now), members=members, deferred=deferred,
                    rules=dict(commit=rules.commit, ci_packet=rules.ci_packet,
                               merge_named_fire=rules.merge_named_fire))
        self.notify("shadow plan %s: %d member(s), %d deferred"
                    % (stamp, len(members), len(deferred)))
        return plan

    # -- comparison with the human composer ---------------------------------

    def compare(self, state, rules):
        plans = [plan for plan in state.get("plans", []) if plan.get("local_stamp")]
        if not plans:
            return []
        known = {row.get("packet", {}).get("sha")
                 for row in state.get("comparisons", [])}
        repository = train_git.Repository(
            self.project, resolutions.Replayer(self.repository_source, self.clone_path))
        base = repository.target(self.project.target)
        comparisons = []
        # Packets merged before the first shadow plan, or named for an hour
        # before it, are history, not an omission of the shadow mode.
        first = min(plan["cutoff"] for plan in plans)
        first_stamp = min(plan["local_stamp"] for plan in plans)
        for packet in self.manual_packets(repository.clone.path, base):
            if packet["sha"] in known or packet["created_ts"] < first \
                    or packet["stamp"] < first_stamp:
                continue
            plan = _match(plans, packet)
            if plan is None:
                comparisons.append(dict(
                    packet=packet, plan=None,
                    missing=sorted(packet["lots"]), extra=[],
                    explanations={"*": "no shadow plan at or near this packet cutoff"}))
                continue
            manual = set(packet["lots"])
            shadow = {int(row["work_item_id"]) for row in plan["members"]}
            missing = sorted(manual - shadow)
            extra = sorted(shadow - manual)
            reasons = {str(row["work_item_id"]): row["reason"]
                       for row in plan.get("deferred", [])}
            explanations = {}
            for lot in missing:
                explanations[str(lot)] = reasons.get(
                    str(lot), "not a candidate of the train at this cutoff")
            for lot in extra:
                explanations[str(lot)] = "train would include it; composer did not"
            comparisons.append(dict(
                packet=packet, plan=dict(cutoff=plan["cutoff"],
                                         local_stamp=plan["local_stamp"],
                                         matched=plan.get("matched")),
                missing=missing, extra=extra, explanations=explanations))
        return comparisons

    @staticmethod
    def manual_packets(clone, base):
        """The composer's packet merges on the first-parent history of target."""
        log = resolutions._run(clone, "log", "--first-parent", "-n", "80",
                               "--format=%H%x1f%s%x1f%ct", base)
        if log.returncode:
            raise train.TrainError("cannot read the manual packet history")
        packets = []
        for record in resolutions._text(log).splitlines():
            fields = record.split("\x1f")
            if len(fields) != 3:
                continue
            sha, subject, raw_ts = fields
            match = _MANUAL.search(subject)
            if not match:
                continue
            tree = resolutions._run(clone, "ls-tree", "-r", "--name-only", sha,
                                    "reviews/")
            if tree.returncode:
                raise train.TrainError("cannot read the manual packet manifest tree")
            names = [name for name in resolutions._text(tree).splitlines()
                     if name.startswith("reviews/integration-paquet-h-")
                     and name.endswith(".md")]
            name = next((name for name in names if match.group(1) in name),
                        names[-1] if names else None)
            if name is None:
                continue
            manifest = resolutions._run(clone, "show", "%s:%s" % (sha, name))
            if manifest.returncode:
                raise train.TrainError("cannot read the manual packet manifest")
            lots = parse_manifest(resolutions._text(manifest))
            if not lots:
                continue
            packets.append(dict(sha=sha, stamp=match.group(1),
                                created_ts=float(raw_ts),
                                manifest=name, lots=sorted(set(lots))))
        packets.reverse()  # oldest first, like the plans
        return packets


def _member(row, *, origin, proof):
    """The stable, comparable part of one admitted/candidate member."""
    item = dict(work_item_id=int(row["work_item_id"]),
                origin=origin,
                branch=row.get("branch") or "",
                frozen_sha=row.get("frozen_sha") or "",
                frozen_id=int(row.get("frozen_id") or 0),
                source=row.get("source") or "",
                risk="", reviews=0)
    policy = (proof or {}).get("policy") or {}
    item["risk"] = policy.get("risk") or ""
    item["reviews"] = int(policy.get("reviews") or 0)
    return item


def _deferred(lot, origin, reason):
    return dict(work_item_id=int(lot), origin=origin, reason=str(reason))


def _match(plans, packet):
    for plan in plans:
        if plan["local_stamp"] == packet["stamp"]:
            return dict(plan, matched="stamp")
    # The composer may name the packet after the hour it started composing;
    # accept the latest plan at or before that instant, within a bounded window.
    before = [plan for plan in plans if plan["cutoff"] <= packet["created_ts"]]
    if not before:
        return None
    plan = max(before, key=lambda row: row["cutoff"])
    if packet["created_ts"] - plan["cutoff"] > 3 * 3600:
        return None
    return dict(plan, matched="nearest-before")


def parse_manifest(text):
    """Lots declared by a composer manifest, or an empty list.

    The ``## Admissions`` list is authoritative when present (it is the
    packet's declared member list). Otherwise the composition table is read:
    each unit row names its lot(s) in the second column.
    """
    if not isinstance(text, str):
        return []
    lots, in_admissions = [], False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            in_admissions = stripped.lower().startswith("## admissions")
            continue
        if in_admissions:
            match = re.match(r"^[-*]\s*#(\d+)\b", stripped)
            if match:
                lots.append(int(match.group(1)))
    if lots:
        return lots
    for line in text.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 2 or not cells[0].isdigit():
            continue
        lots.extend(int(match.group(1)) for match in _LOT.finditer(cells[1]))
    return lots
