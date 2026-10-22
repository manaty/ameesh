# SPDX-License-Identifier: AGPL-3.0-only
"""Shadow plan for the automatic packet train (#2446).

The mode that must run before the switch-over: at each compiled packet cutoff,
compute the packet the active train would compose, publish it in the host's
state directory, and compare it with the manifest of the packet the human
composer actually merged. Nothing is reserved, published, dispatched or
promoted: the inventory and the historical admission readers are used
read-only, and the composition replay stays in the disposable clone.

The plan replays the active composition (#2963), it does not merely qualify
members one by one: the survivors are merged in order by the same
``train_git.Repository.compose`` in the disposable clone, so the same
``land.merge``, generated-file handling, main sync and migration guard decide
the packet. A member the merge would defer (``MemberDeferred``) is published
as deferred with the same reason, and its dependents follow the engine's
ordering rule. The clone's final commit is local to the shadow, and migration
slots are computed but never reserved.

The comparison is the proof the pilot asks for: lots the composer included but
the train omitted (with the named reason the train had), lots the train would
have included and the composer did not, and the packet stamp/base that matched.
A lot already composed in a packet that was still in flight at the cutoff is
not a disagreement: it is named with its carrier ("porté par ... en vol")
instead of being reported as an extra. That composition is proven by the
packet's exact manifest at the last first-parent commit before the cutoff; a
later enrichment or an inherited manifest proves nothing, and the lot then
stays an extra ("porteur inconnu"). A missing shadow plan for a packet is not
an explained difference; it is recorded as such.

#2965: the cutoff filter is the active train's own — `Admissions.admitted` for
a queue row (the admission instant of the freeze and the gate for a row the
train enrolled itself, the queue instant for a manual admission) and the same
`admitted_ts` of the preparation for a candidate not yet enrolled. The shadow
never dates a member by `queued_ts` alone.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone

from . import (land, merge_queue, plan_git, radar, resolutions, train,
               train_admissions, train_candidates, train_git, train_history,
               train_policy)
from .canon import FETCH_TIMEOUT

SCHEMA = "ameesh-train-shadow/1"
#: kept plans and comparisons: enough for the two real packets of the proof
#: and for the preceding ones after a restart.
MAX_PLANS = 24
MAX_COMPARISONS = 48
_PACKET = re.compile(r"integration/(paquet-[^\s,()]+)")
_STAMP = re.compile(r"(\d{8}-\d{4})")
_WORK = re.compile(r"^ameesh-work: (\d+)$", re.M)
_LOT = re.compile(r"#(\d+)")
#: an open packet branch whose last composition before the cutoff is older
#: than this window was abandoned, not in flight. An unproven lot stays an
#: extra: the shadow prefers a visible disagreement to a masked one (#2682).
MAX_FLIGHT_S = 6 * 3600.0
#: open packet branches are read in a private namespace of the shadow clone;
#: the shadow never checks them out nor publishes anything.
#: The composer names both `integration/paquet-h-<stamp>` (hourly) and
#: `integration/paquet-<n>-nexlink` (#2381, #2733); one prefix covers both.
_PACKET_HEADS = "refs/heads/integration/paquet-"
_INFLIGHT = "refs/ameesh/shadow-inflight/"


def _atom(tag):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", tag)


def _stamp_of(created_ts, zone=None):
    """The packet stamp of a commit date, in the plan zone (UTC by default)."""
    return datetime.fromtimestamp(float(created_ts), zone or timezone.utc).strftime(
        "%Y%m%d-%H%M")


class _ShadowMigrations:
    """#2963: the composition-time migration guard, without the L122 effect.

    ``train_git.Repository.compose`` renumbers an added migration and audits
    the composition when the project declares a migration layout. The shadow
    replays that same guard in its disposable clone, but a plan reserves
    nothing: ``reserve`` is a no-op, so no slot is taken and no journal is
    written. Availability of the slot is an effect of the active composition,
    not a property of the packet the plan projects.
    """

    @staticmethod
    def reserve(row, path, old, number, target):
        return None


class NoTrainFacts:
    """#2682: a shadow passage lands nothing, so it has no train facts.

    ``Runner.incident_once`` asks every passage for its packet facts when a
    terminal integration run is red. The shadow mode keeps no persisted
    landing intent: the honest answer is "no train facts", the direct reader
    given as ``fallback`` stays authoritative, and this path never raises.
    """

    def __init__(self, project, *, fallback=None):
        if fallback is not None and not callable(fallback):
            raise train.TrainError("explicit incident fallback reader required")
        self.repo = radar.github_slug(project)
        self.target = merge_queue._remote_branch(project.target)
        self.fallback = fallback

    def lands(self, repo, head):
        return self.fallback(repo, head) if self.fallback is not None else []

    def diagnoses(self, repo, head):
        return None

    def links_for(self, repo, head):
        return None


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
                 provider, mechanics, repository, candidate=None, initial=None,
                 notify=None):
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
        #: the same qualified initial-history reader the active Enrollment uses
        self.initial = initial
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

    def incident_facts(self, db, *, fallback=None):
        """#2682: « aucun fait de train » pour l'ombre, et jamais une levée.

        The incident consumer (#1681) queries every train passage. The shadow
        passage has no packet landing to show; the direct landing reader must
        stay in charge instead of an AttributeError silencing it.
        """
        return NoTrainFacts(self.project, fallback=fallback)

    # -- the read-only plan --------------------------------------------------

    def plan(self, db, rules, slot, now):
        repository = train_git.Repository(
            self.project, resolutions.Replayer(self.repository_source, self.clone_path,
                                               target=self.project.target),
            migrations=_ShadowMigrations())
        base = repository.target(self.project.target)
        history = train_history.Recorded(db, repository.clone.path,
                                         provider=self.provider, review_rules=rules)
        requirement = train_policy.Compiled(rules)
        admissions = train_admissions.Admissions(db, self.project, history,
                                                 policy=requirement)
        target = merge_queue._remote_branch(self.project.target)
        rows = [row for row in admissions.entries()
                if merge_queue._remote_branch(row["target"]) == target]
        deferred, entries = [], []
        # #2965 (B3) : la même règle que l'actif, par le même lecteur —
        # l'instant d'admissibilité du gel et de la porte pour une ligne que le
        # train a lui-même enrôlée, le `queued_ts` pour une admission
        # manuelle. La file seule ne suffit plus.
        admissible = {train.identity(row) for row in
                      admissions.admitted(rows, slot, base)}
        # Every queued admission is already in the queue: the candidate
        # inventory must not present it again, even when it is deferred here.
        prepared_ids = {row["work_item_id"] for row in rows}
        entries.extend(dict(row=row, origin="queued", proof=None)
                       for row in rows)
        if self.candidate is not None:
            inventory = train_candidates.Inventory(
                db, self.project, candidate=self.candidate).read()
            preparation = train_candidates.Preparation(
                db, self.project, repository=repository, history=history,
                policy=requirement)
            enrollment = train_candidates.Enrollment(preparation, admissions,
                                                     initial=self.initial)
            enrolled = 0
            for candidate in inventory["candidates"]:
                lot = candidate["work_item_id"]
                if lot in prepared_ids:
                    continue
                if candidate["causes"]:
                    deferred.append(_deferred(lot, "candidate",
                                              "; ".join(candidate["causes"])))
                    continue
                try:
                    decision = enrollment.decide(candidate, base)
                except train.TrainError as exc:
                    deferred.append(_deferred(lot, "candidate", str(exc)))
                    continue
                # The shadow plan must not show a member that the active
                # enrollment would refuse: a withdrawal receipt or an
                # unqualified initial absence is the same refusal here.
                if decision["initial"] is not True:
                    deferred.append(_deferred(
                        lot, "candidate",
                        "no qualified initial queue history or a withdrawal: "
                        "active enrollment would refuse"))
                    continue
                prepared = decision["prepared"]
                # #2965 : l'actif enrôle le candidat puis filtre sur son
                # instant d'admissibilité (gel + porte). Une préparation dont
                # la porte s'est ouverte après le cutoff serait classée late
                # à l'enrôlement : l'ombre applique le même instant.
                instant = ((prepared.get("history") or {}).get("reviews")
                           or {}).get("admitted_ts")
                try:
                    late = not float(instant) <= float(slot)
                except (TypeError, ValueError):
                    late = True
                if late:
                    deferred.append(_deferred(
                        lot, "candidate",
                        "late admission: not admissible at the cutoff"))
                    continue
                # Active enrollment inserts the candidate with rank NULL and
                # an increasing queued_ts. A candidate is not enrolled at the
                # tick: the inventory lists lots already ready at the cutoff,
                # which the service enrolls on an earlier event. Its synthetic
                # timestamp stays just below the cutoff and increases in
                # inventory order; ``generation`` is part of the admission
                # identity used by compose, and the shadow fabricates no queue
                # row, so it uses one neutral value throughout.
                item = admissions.ops.get(lot)
                subject = dict(
                    prepared["subject"], generation=0,
                    priority=int((item or {}).get("priority") or 2),
                    rank=None,
                    queued_ts=float(slot) - 1.0 + enrolled * 0.001)
                entries.append(dict(row=subject, origin="candidate",
                                    proof=prepared))
                prepared_ids.add(lot)
                enrolled += 1
        # The active engine drops a late admission before ordering (train.py,
        # build_beta), and its dependents wait with it. Apply the same
        # admissibility rule on the queued admissions (#2965); the candidate
        # instants were checked in the inventory loop above.
        entries = [entry for entry in entries
                   if entry["origin"] == "candidate"
                   or train.identity(entry["row"]) in admissible]
        late = [row for row in rows if train.identity(row) not in admissible]
        for row in late:
            deferred.append(_deferred(row["work_item_id"], "queued",
                                      "late admission: not admissible at the cutoff"))
        if entries:
            # One ordering pass over the queued admissions and the candidates,
            # as the engine does after enrollment: a queued dependent of a
            # candidate is not dropped before that candidate is considered.
            members_rows = [entry["row"] for entry in entries]
            ordered = train.ordered(members_rows,
                                    admissions.dependencies(members_rows))
            alive = {train.identity(row) for row in ordered}
            by_admission = {train.identity(entry["row"]): entry for entry in entries}
            for entry in entries:
                if train.identity(entry["row"]) not in alive:
                    deferred.append(_deferred(
                        entry["row"]["work_item_id"], entry["origin"],
                        "open prerequisite or dependency cycle"))
            entries = [by_admission[train.identity(row)] for row in ordered]
            # #2727: the active composition merges an advanced ``main``
            # before its members; the shadow replays that same first merge.
            sync = repository.main_sync(self.project.target, base)
        else:
            sync = None
        entries, composition_deferred = self._compose(
            repository, admissions, base, sync, entries)
        deferred.extend(composition_deferred)
        seen = set()
        deferred = [row for row in deferred
                    if not (row["work_item_id"] in seen
                            or seen.add(row["work_item_id"]))]
        deferred.sort(key=lambda row: (row["work_item_id"], row["origin"]))
        members = [_member(entry["row"], origin=entry["origin"], proof=entry["proof"])
                   for entry in entries]
        stamp = datetime.fromtimestamp(slot, rules.zone()).strftime("%Y%m%d-%H%M")
        # #2727: main commits unknown to the target are a mechanical member of
        # the next packet, without review or a build lot. It is recorded only
        # when a packet has members: an empty cutoff composes nothing.
        sync = sync if members else None
        plan = dict(schema=SCHEMA, project=self.project.name,
                    target=merge_queue._remote_branch(self.project.target),
                    cutoff=float(slot), local_stamp=stamp, base=base,
                    created_ts=float(now), members=members, deferred=deferred,
                    sync=sync,
                    rules=dict(commit=rules.commit, ci_packet=rules.ci_packet,
                               merge_named_fire=rules.merge_named_fire))
        self.notify("shadow plan %s: %d member(s), %d deferred%s"
                    % (stamp, len(members), len(deferred),
                       ", sync main %s" % sync["sha"][:12] if sync else ""))
        return plan

    def _compose(self, repository, admissions, base, sync, entries):
        """Replay the active composition, deferring a refused member.

        Each attempt qualifies the queued admissions and calls the real
        :meth:`train_git.Repository.compose` in the disposable clone. The
        refusal may come from the admission reader or from the composition
        itself (``MemberDeferred``: merge conflict, generated file, migration
        guard, transport). The refused member and the members it blocks are
        deferred, exactly as the engine's composing loop does, and the
        survivors are composed again from the same base.
        """
        kept = list(entries)
        deferred = []
        while kept:
            failure = None
            for entry in kept:
                if entry["origin"] != "queued" or entry["proof"] is not None:
                    continue
                try:
                    proof = admissions.qualify([entry["row"]], base, "packet")
                    entry["proof"] = proof["members"][0]
                except train.MemberDeferred as exc:
                    failure = exc
                    break
                except train.TrainError as exc:
                    failure = train.MemberDeferred(entry["row"], str(exc))
                    break
            if failure is None:
                try:
                    repository.compose(self.project.target, base,
                                       [entry["row"] for entry in kept],
                                       sync=sync)
                except train.MemberDeferred as exc:
                    failure = exc
            if failure is None:
                break
            failed = next((entry for entry in kept
                           if train.identity(entry["row"]) == failure.admission),
                          None)
            if failed is None:
                raise train.TrainError("composition deferred an unknown admission")
            candidates = [entry["row"] for entry in kept if entry is not failed]
            available = train.ordered(candidates,
                                      admissions.dependencies(candidates))
            alive = {train.identity(row) for row in available}
            lost = [entry for entry in kept
                    if train.identity(entry["row"]) not in alive]
            for entry in lost:
                deferred.append(_deferred(
                    entry["row"]["work_item_id"], entry["origin"],
                    str(failure) if entry is failed
                    else "open prerequisite or dependency cycle"))
            kept = [entry for entry in kept
                    if train.identity(entry["row"]) in alive]
        return kept, deferred

    # -- comparison with the human composer ---------------------------------

    def compare(self, state, rules):
        plans = [plan for plan in state.get("plans", []) if plan.get("local_stamp")]
        if not plans:
            return []
        known = {row.get("packet", {}).get("sha")
                 for row in state.get("comparisons", [])}
        repository = train_git.Repository(
            self.project, resolutions.Replayer(self.repository_source, self.clone_path,
                                               target=self.project.target))
        base = repository.target(self.project.target)
        # Packets merged before the first shadow plan, or named for an hour
        # before it, are history, not an omission of the shadow mode.
        first = min(plan["cutoff"] for plan in plans)
        first_stamp = min(plan["local_stamp"] for plan in plans)
        # The plan stamps use the canon's zone; a numbered packet
        # (`paquet-<n>-nexlink`) carries no hour, so its merge commit date is
        # read in the same zone. The tests pass no rules: UTC stays bounded.
        zone = rules.zone() if rules is not None and hasattr(rules, "zone") else timezone.utc
        history = self.manual_packets(repository.clone.path, base, zone)
        packets = [packet for packet in history
                   if packet["sha"] not in known and packet["lots"]
                   and packet["created_ts"] >= first
                   and packet["stamp"] >= first_stamp]
        if not packets:
            return []
        # #2682: a packet in flight at the cutoff carries only the lots its
        # manifest had at that cutoff. A later enrichment is not proof, and an
        # unreadable manifest leaves the lot an extra, never a silent carrier.
        open_packets = self.open_packets(repository.clone.path, base, zone)
        comparisons = []
        for packet in packets:
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
            extras = sorted(shadow - manual)
            in_flight, unknown = _in_flight(
                repository.clone.path, plan, history, open_packets, packet)
            carried = {lot: in_flight[lot] for lot in extras if lot in in_flight}
            extra = [lot for lot in extras if lot not in carried]
            # #2727: the manual composer may have synced main as a lot while
            # the train does it mechanically. The exact main sha proves the
            # equivalence only when the packet commit is a pure sync (see
            # _manual_sync); an unproven one keeps its lots missing.
            plan_sync = (plan.get("sync") or {}).get("sha") or ""
            manual_sync = (_manual_sync(repository.clone.path, plan["base"],
                                        packet["sha"], plan_sync)
                           if plan_sync else None)
            proven = manual_sync if manual_sync and manual_sync["pure"] else None
            sync = dict(plan=plan_sync or None,
                        manual=(manual_sync or {}).get("sha"),
                        lots=sorted((proven or {}).get("lots") or ()))
            if manual_sync is not None and proven is None:
                sync["refused"] = dict(
                    commit=manual_sync["commit"],
                    lots=sorted(manual_sync.get("lots") or ()),
                    pure=False,
                    reason=manual_sync.get("reason") or "sync not proven")
            for lot in sync["lots"]:
                if lot in missing:
                    missing.remove(lot)
            missing.sort()
            reasons = {str(row["work_item_id"]): row["reason"]
                       for row in plan.get("deferred", [])}
            explanations = {}
            for lot in missing:
                explanations[str(lot)] = reasons.get(
                    str(lot), "not a candidate of the train at this cutoff")
            for lot in sync["lots"]:
                explanations[str(lot)] = (
                    "synchro main %s mécanique (même apport que le train)"
                    % plan_sync[:12])
            if plan_sync and manual_sync is None:
                sync["difference"] = ("le paquet manuel n'a pas apporté main "
                                      "%s" % plan_sync[:12])
            for lot in extra:
                explanations[str(lot)] = (
                    "porteur inconnu: paquet en vol non prouvé au créneau"
                    if unknown else "train would include it; composer did not")
            for lot, carrier in sorted(carried.items()):
                explanations[str(lot)] = "porté par %s en vol" % carrier
            comparisons.append(dict(
                packet=packet, plan=dict(cutoff=plan["cutoff"],
                                         local_stamp=plan["local_stamp"],
                                         matched=plan.get("matched")),
                missing=missing, extra=extra, sync=sync,
                carried={str(lot): carrier for lot, carrier in sorted(carried.items())},
                explanations=explanations))
        return comparisons

    @staticmethod
    def manual_packets(clone, base, zone=None):
        """The composer's packet merges on the first-parent history of target.

        Each entry keeps the exact branch name and its second parent (the
        branch head at merge time), so a carrier can be read as it was at an
        earlier cutoff. A packet whose own manifest is unreadable is kept:
        its lots are unknown, which must stay visible in the comparison.
        A numbered packet (`integration/paquet-<n>-nexlink`, #2733) carries no
        hour: its stamp is the merge commit date, read in the plan zone. The
        numbered composer writes no manifest; its lots are then the
        ``ameesh-work:`` trailers of the commits between the merge's first
        parent and its branch head, the exact composition it merged.
        """
        log = resolutions._run(clone, "log", "--first-parent", "-n", "80",
                               "--format=%H%x1f%P%x1f%s%x1f%ct", base)
        if log.returncode:
            raise train.TrainError("cannot read the manual packet history")
        packets = []
        for record in resolutions._text(log).splitlines():
            fields = record.split("\x1f")
            if len(fields) != 4:
                continue
            sha, raw_parents, subject, raw_ts = fields
            match = _PACKET.search(subject)
            if match is None:
                continue
            tail = match.group(1)
            created = float(raw_ts)
            named = _STAMP.search(tail)
            stamp = named.group(1) if named else _stamp_of(created, zone)
            parents = raw_parents.split()
            branch_head = parents[1] if len(parents) > 1 else sha
            manifest_lots = _manifest_lots(clone, sha, tail)
            lots = manifest_lots
            if lots is None and len(parents) > 1:
                # The numbered composer declares its units in the merge
                # trailers, not in a manifest file.
                lots = _commit_lots(clone, parents[0], branch_head)
            packets.append(dict(sha=sha, stamp=stamp,
                                created_ts=created,
                                branch="integration/" + tail, tail=tail,
                                branch_head=branch_head,
                                manifest=("reviews/integration-%s.md" % tail
                                          if manifest_lots is not None else None),
                                lots=sorted(set(lots or ()))))
        packets.reverse()  # oldest first, like the plans
        return packets

    def open_packets(self, clone, base, zone=None):
        """The composer's packet branches still open on the target (#2682).

        Read-only: the ``integration/paquet-*`` heads are fetched into a
        private namespace of the shadow clone, never checked out nor pushed.
        A branch whose tip is already an ancestor of the target has landed;
        its merge time is what dates its flight. Only branches that are still
        not on the target are listed here; their manifest is read later, at
        the exact commit that was composed before a plan cutoff. A numbered
        packet carries no hour: its stamp is the head commit date (#2733).
        """
        url = land._origin(self.project) or merge_queue.source_of(self.project.repo)
        stale = resolutions._run(clone, "for-each-ref", "--format=%(refname)", _INFLIGHT)
        if stale.returncode:
            raise train.TrainError("cannot read the shadow in-flight refs")
        refs = resolutions._text(stale).splitlines()
        if refs:
            deleted = resolutions._run(
                clone, "update-ref", "--stdin",
                stdin=("".join("delete %s\n" % ref for ref in refs)).encode())
            if deleted.returncode:
                raise train.TrainError("cannot refresh the shadow in-flight refs")
        fetched = resolutions._run(
            clone, "fetch", "--quiet", "--no-tags", url,
            "+%s*:%s*" % (_PACKET_HEADS, _INFLIGHT), timeout=FETCH_TIMEOUT)
        if fetched.returncode:
            raise train.TrainError("cannot fetch the open packet branches: %s"
                                   % resolutions._err(fetched)[-300:])
        listed = resolutions._run(clone, "for-each-ref", "--format=%(refname)", _INFLIGHT)
        if listed.returncode:
            raise train.TrainError("cannot read the shadow in-flight refs")
        open_packets = []
        for ref in resolutions._text(listed).splitlines():
            tail = "paquet-" + ref[len(_INFLIGHT):]
            head = plan_git._commit(clone, ref)
            if head is None:
                raise train.TrainError("open packet ref unreadable")
            named = _STAMP.search(tail)
            if named is not None:
                stamp = named.group(1)
            else:
                raw = resolutions._run(clone, "log", "-1", "--format=%ct", head)
                if raw.returncode:
                    raise train.TrainError("cannot read an open packet date")
                stamp = _stamp_of(resolutions._text(raw), zone)
            ancestor = resolutions._run(clone, "merge-base", "--is-ancestor", head, base)
            if ancestor.returncode == 0:
                continue
            if ancestor.returncode != 1:
                raise train.TrainError("cannot compare an open packet with the target")
            open_packets.append(dict(sha=head, stamp=stamp, tail=tail,
                                     branch="integration/" + tail))
        return open_packets


def _in_flight(clone, plan, packets, open_packets, compared):
    """({lot: carrier}, unknown) for packets in flight at the plan cutoff.

    A packet was in flight at the cutoff when its merge was not on the target
    yet: it merged after the cutoff, or its ``integration/**`` branch is still
    open. For each one, only the manifest of the last first-parent commit at
    or before the cutoff is proof; the final manifest of a later merge is not
    (#2682 B1). An unreadable manifest leaves the extra visible and sets
    ``unknown`` so the comparison says "porteur inconnu" (#2682 B2). An open
    branch last composed long before the cutoff is abandoned, not in flight.
    """
    carried = {}
    unknown = False
    stamp = plan["local_stamp"]
    cutoff = plan["cutoff"]
    for packet in packets:
        # A derived stamp dates the merge/head, not the composition cutoff.
        # Only a stamp carried by the branch name can exclude this carrier.
        if packet["sha"] == compared["sha"] or (
                _STAMP.search(packet["tail"]) and packet["stamp"] > stamp):
            continue
        if packet["created_ts"] <= cutoff:
            continue  # already merged at the cutoff: the shadow saw it on target
        lots = _lots_at(clone, packet["branch_head"], packet["tail"], cutoff)
        if lots is None:
            unknown = True
            continue
        for lot in lots:
            carried.setdefault(lot, packet["branch"])
    for packet in open_packets:
        if _STAMP.search(packet["tail"]) and packet["stamp"] > stamp:
            continue
        commit, written = _first_parent_before(clone, packet["sha"], cutoff)
        if commit is None:
            unknown = True
            continue
        if cutoff - written > MAX_FLIGHT_S:
            continue  # abandoned branch: not a carrier for this cutoff
        lots = _manifest_lots(clone, commit, packet["tail"])
        if lots is None:
            unknown = True
            continue
        for lot in lots:
            carried.setdefault(lot, packet["branch"])
    return carried, unknown


def _lots_at(clone, head, tail, cutoff):
    """The lots of a packet branch at the cutoff, or None when unprovable."""
    commit, _ = _first_parent_before(clone, head, cutoff)
    if commit is None:
        return None
    return _manifest_lots(clone, commit, tail)


def _first_parent_before(clone, head, cutoff):
    """(sha, commit timestamp) of the last first-parent commit before cutoff."""
    proc = resolutions._run(
        clone, "log", "--first-parent", "-1", "--before=@%d" % int(cutoff),
        "--format=%H%x1f%ct", head)
    if proc.returncode:
        raise train.TrainError("cannot read the packet history at the cutoff")
    text = resolutions._text(proc)
    if not text:
        return None, None
    sha, _, raw_ts = text.partition("\x1f")
    try:
        return sha, float(raw_ts)
    except ValueError as exc:
        raise train.TrainError("unreadable packet commit date") from exc


def _manifest_lots(clone, sha, tail):
    """The exact ``reviews/integration-<tail>.md`` lots, or None.

    No fallback to another packet's manifest: an inherited manifest would
    turn an unrelated branch into a false carrier (#2682 B2).
    """
    path = "reviews/integration-%s.md" % tail
    manifest = resolutions._run(clone, "show", "%s:%s" % (sha, path))
    if manifest.returncode:
        return None
    return parse_manifest(resolutions._text(manifest))


def _commit_lots(clone, base, head):
    """The lots declared by ``ameesh-work:`` trailers between ``base`` and ``head``.

    The numbered composer writes no manifest: the exact composition it merged
    is the branch's own range and its trailers. None when the range cannot be
    read, never a set inferred from another packet.
    """
    log = resolutions._run(clone, "log", "--format=%B", "%s..%s" % (base, head))
    if log.returncode:
        return None
    return sorted({int(match.group(1))
                   for match in _WORK.finditer(resolutions._text(log))})

def _manual_sync(clone, base, head, sha):
    """The packet commit that merged ``sha`` (main), with its pure-sync proof.

    The exact main parent and the ``ameesh-work`` trailer do not prove a sync:
    the commit may add unreviewed files or resolve the merge by hand. A commit
    proves it only when it has exactly two parents (the packet side, then the
    exact main sha), carries a single work trailer, and its tree equals the
    clean merge of those parents. An unproven sync exempts nothing: its lots
    stay missing, and the reason is kept on the comparison.
    """
    log = resolutions._run(clone, "rev-list", "--parents", "%s..%s" % (base, head))
    if log.returncode:
        raise train.TrainError("cannot read the packet ancestry for the main sync")
    proofs = []
    for line in resolutions._text(log).splitlines():
        fields = line.split()
        if len(fields) < 2 or sha not in fields[1:]:
            continue
        proofs.append(_sync_proof(clone, fields[0], fields[1:], sha))
    if not proofs:
        return None
    pure = [proof for proof in proofs if proof["pure"]]
    refused = [proof for proof in proofs if not proof["pure"]]
    exempt = {lot for proof in pure for lot in proof["lots"]}
    # A lot named by an impure merge stays missing even when another commit
    # happened to merge the same main sha cleanly.
    exempt -= {lot for proof in refused for lot in proof["lots"]}
    chosen = pure[0] if pure else refused[0]
    result = dict(commit=chosen["commit"], sha=sha, lots=sorted(exempt),
                  pure=bool(pure))
    if not pure:
        result["reason"] = chosen["reason"]
    return result


def _sync_proof(clone, commit, parents, sha):
    """One commit's local proof that it only merged ``sha`` into its parent."""
    proof = dict(commit=commit, parents=parents, lots=[], pure=False, reason="")
    body = resolutions._run(clone, "show", "-s", "--format=%B", commit)
    if body.returncode:
        raise train.TrainError("cannot read the packet main sync commit")
    proof["lots"] = [int(match.group(1))
                     for match in _WORK.finditer(resolutions._text(body))]
    if len(parents) != 2 or parents[1] != sha:
        proof["reason"] = "not a two-parent merge of the packet side and main"
        return proof
    if len(proof["lots"]) != 1:
        proof["reason"] = ("mechanical sync carries %d ameesh-work trailers"
                           % len(proof["lots"]))
        return proof
    clean = resolutions._run(clone, "merge-tree", "--write-tree", parents[0], sha)
    if clean.returncode == 1:
        proof["reason"] = "main merge is not clean"
        return proof
    if clean.returncode != 0:
        raise train.TrainError("cannot compute the clean main merge")
    expected = resolutions._text(clean).splitlines()
    actual = resolutions._run(clone, "rev-parse", commit + "^{tree}")
    if actual.returncode:
        raise train.TrainError("cannot read the packet main sync tree")
    if not expected or expected[0].strip() != resolutions._text(actual).strip():
        proof["reason"] = "tree differs from the clean main merge"
        return proof
    proof["pure"] = True
    return proof


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
