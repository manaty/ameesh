# SPDX-License-Identifier: AGPL-3.0-only
"""Read-only packet facts for the post-landing incident consumer (#1681).

The caller supplies a repository containing the exact published Git objects
and, optionally, a qualified measured-map provider keyed by repository/head.
No checkout, fetch, CI, current queue or author inference is performed here.
"""
from __future__ import annotations

import copy

from . import land, merge_queue, radar, resolutions, storage, train_diagnosis
from .train import SCHEMA, TrainError, identity
from .train_review import SHA


class PacketFacts:
    def __init__(self, db, project, path, *, impact=None, fallback=None):
        if impact is not None and not callable(impact):
            raise TrainError("explicit repository/head impact provider required")
        self.ops = storage.of(db).work
        self.project, self.path = project, path
        self.repo = radar.github_slug(project)
        self.source = merge_queue.source_of(project.repo)
        self.target = merge_queue._remote_branch(project.target)
        self.key = land.lock_key(project, self.target)
        self.impact, self.fallback = impact, fallback

    def _git(self, *args):
        result = resolutions._run(self.path, "--no-replace-objects", *args)
        if result.returncode:
            raise TrainError("complete packet Git objects required for incident facts")
        return result.stdout.decode("utf-8", "strict")

    def _plan(self, repo, head):
        if repo != self.repo or not isinstance(head, str) or not SHA.fullmatch(head):
            return None
        if merge_queue.source_of(self.project.repo) != self.source:
            raise TrainError("incident repository origin changed")
        state = self.ops.train_read(self.key)
        plan = (state or {}).get("plan")
        if not isinstance(plan, dict) or plan.get("head") != head:
            return None
        if state.get("phase") not in ("promoting", "landed"):
            raise TrainError("packet has no persisted landing intent")
        try:
            if plan["schema"] != SCHEMA or plan["source"] != self.source \
                    or plan["target"] != self.target:
                raise TrainError("incident packet belongs to another source or target")
            if any(not isinstance(plan[k], str) or not SHA.fullmatch(plan[k])
                   for k in ("base", "head", "tree")):
                raise TrainError("full packet object identities required")
            members, proof = plan["members"], plan["proof"]
            if not isinstance(members, list) or not members \
                    or proof["schema"] != "ameesh-train-admissions/1" \
                    or (proof["source"], proof["base"]) != (self.source, plan["base"]) \
                    or len(proof["members"]) != len(members):
                raise TrainError("complete packet member proof required")
            lots = set()
            for row, evidence in zip(members, proof["members"]):
                lot = row["work_item_id"]
                if type(lot) is not int or lot <= 0 or lot in lots \
                        or row["source"] != self.source \
                        or merge_queue._remote_branch(row["target"]) != self.target \
                        or not SHA.fullmatch(row["frozen_sha"]) \
                        or tuple(evidence["admission"]) != identity(row):
                    raise TrainError("packet member identity differs from its proof")
                lots.add(lot)
        except (KeyError, TypeError, ValueError) as exc:
            raise TrainError("unreadable packet member manifest") from exc
        if self._git("rev-parse", "--is-shallow-repository").strip() != "false":
            raise TrainError("complete packet history required; shallow clone refused")
        if self._git("rev-parse", "--verify", head + "^{tree}").strip() != plan["tree"]:
            raise TrainError("incident packet tree differs from persisted manifest")
        parents = self._git("rev-list", "--parents", "-n", "1", head).split()
        expected = list(dict.fromkeys([plan["base"]] + [r["frozen_sha"] for r in members]))
        if parents != [head] + expected:
            raise TrainError("incident packet parents differ from persisted members")
        return state, plan

    def lands(self, repo, head):
        """One candidate per member, using its own merge-base diff only.

        Historical member branches and current lot assignments may have moved.
        Neither is read. #1681 resolves the candidate's assignee separately.
        A missing manifest may use the ordinary landing reader; a contradictory
        manifest raises instead of falling back to an aggregate packet diff.
        """
        found = self._plan(repo, head)
        if found is None:
            return self.fallback(repo, head) if self.fallback is not None else []
        _, plan = found
        rows = []
        for member in plan["members"]:
            sha = member["frozen_sha"]
            bases = self._git("merge-base", "--all", plan["base"], sha).split()
            if len(bases) != 1:
                raise TrainError("unique member divergence base required")
            files = self._git("diff", "--no-ext-diff", "--no-textconv", "--no-renames",
                              "--name-only", "-z", bases[0], sha, "--").split("\0")
            rows.append(dict(sha=sha, lot=member["work_item_id"], author="",
                             summary="packet member #%s" % member["work_item_id"],
                             files=[name for name in files if name]))
        return rows

    def links_for(self, repo, head):
        """A qualified measured map on this repository and exact head, or None.

        The provider qualifies the artifact's provenance and scope; its schema
        and covered commit are checked again here. A stale map grants no links.
        Importing #1681 is delayed until this optional supplier port is used.
        """
        if repo != self.repo or not isinstance(head, str) or not SHA.fullmatch(head) \
                or self.impact is None:
            return None
        carte = self.impact(repo, head)
        if not isinstance(carte, dict) or carte.get("schema") != "ameesh-carte/1" \
                or carte.get("commit") != head:
            return None
        from .incident import links_of
        return links_of(carte)

    def diagnoses(self, repo, head):
        found = self._plan(repo, head)
        if found is None:
            return None
        state, plan = found
        report = state.get("diagnosis")
        packet = {k: plan[k] for k in ("source", "target", "base", "head", "tree")}
        if not isinstance(report, dict) or report.get("schema") != train_diagnosis.SCHEMA \
                or report.get("packet") != packet:
            return None
        return copy.deepcopy(report)


def consume(db, passages, *, consumer):
    """Supply exact per-project ports to #1681 on this call's connection.

    The incident consumer owns integration-branch selection and idempotency.
    Ordinary landings keep its existing reader. The explicitly supplied train
    passages add facts only for repositories they cover, even if CI covers
    additional repositories. No connection or map survives this call.
    """
    passages = tuple(p for p in passages if p.policy.enabled)
    if not passages:
        return consumer.consume(db)
    from . import ci
    readers = {}
    for passage in passages:
        reader = passage.incident_facts(db, fallback=ci.landed_commits)
        spec = consumer.INTEGRATION.get(reader.repo)
        branch = spec[0] if isinstance(spec, tuple) else spec
        if branch != reader.target or reader.repo in readers:
            raise TrainError("one exact integration target per incident repository required")
        readers[reader.repo] = reader

    def lands(repo, head):
        reader = readers.get(repo)
        return reader.lands(repo, head) if reader else ci.landed_commits(repo, head)

    def diagnoses(repo, head):
        reader = readers.get(repo)
        return reader.diagnoses(repo, head) if reader else None

    def links_for(repo, head):
        reader = readers.get(repo)
        return reader.links_for(repo, head) if reader else None

    return consumer.consume(db, lands=lands, diagnoses=diagnoses, links_for=links_for)
