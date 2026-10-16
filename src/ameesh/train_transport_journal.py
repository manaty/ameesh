# SPDX-License-Identifier: AGPL-3.0-only
"""Reconstructible transport index in the existing lot journal (#1467).

Importing records facts, never a verdict or an admission. The explicit
qualification resolver receives the original sources on the current DB
connection, including their authors and complete lot history. It must check
their authority, scope and continued applicability; this adapter deliberately
does not interpret natural-language GO notes or infer policy from ref names.
"""
from __future__ import annotations

import copy
import json

from . import land, merge_queue, plan_git, resolutions, storage, train_transports, work
from .canon import FETCH_TIMEOUT
from .train import TrainError, digest, identity
from .train_review import SHA

SCHEMA = "ameesh-train-transport-index/1"
PREFIX = SCHEMA + " "
FACTS = {"source", "target", "lot", "source_head", "base", "head", "tree", "delta", "ref"}


def _prefix(lot):
    return PREFIX + str(lot) + " "


def _fingerprints(rows):
    # Keep source contents at their original access-controlled location.
    # The reconstructible index records identity, not a copy of private mail.
    return [dict(reference=row["reference"], author=row["author"],
                 digest=digest(row["record"])) for row in rows]


def _receipt(facts, qualification, row):
    return dict(schema=train_transports.SCHEMA, admission=list(identity(row)),
                qualification=copy.deepcopy(qualification),
                **{key: facts[key] for key in FACTS - {"lot", "source_head"}})


def _row(facts):
    # Only structural validation. No synthetic generation is ever persisted
    # or used for admission: the real one is attached in Recorded.__call__.
    return dict(work_item_id=facts["lot"], generation=1, target=facts["target"],
                frozen_id=1, frozen_sha=facts["source_head"], source=facts["source"])


def _validate(document):
    if not isinstance(document, dict) or set(document) != {"schema", "facts", "message", "qualification", "originals"} \
            or document["schema"] != SCHEMA:
        raise TrainError("structured transport journal index required")
    facts = document["facts"]
    if not isinstance(facts, dict) or set(facts) != FACTS \
            or type(facts["lot"]) is not int or facts["lot"] <= 0 \
            or not isinstance(facts["source_head"], str) or not SHA.fullmatch(facts["source_head"]):
        raise TrainError("exact unbound transport facts required")
    row = _row(facts)
    train_transports.binding(_receipt(facts, document["qualification"], row),
        source=facts["source"], target=facts["target"], row=row, base=facts["base"])
    if not isinstance(document["message"], str) or not isinstance(document["originals"], list) \
            or len(document["originals"]) != len(document["qualification"]["references"]):
        raise TrainError("original qualification sources required")


def _git(path, *args):
    proc = resolutions._run(path, "--no-replace-objects", *args)
    if proc.returncode:
        raise TrainError("cannot read original transport Git facts")
    return resolutions._text(proc)


def originals(db, path, lot, references):
    """Resolve exact IDs, never the last note, last verdict or nearby mail."""
    st = storage.of(db)
    result = []
    for reference in references:
        kind = reference["kind"]
        if kind in ("work-event", "milestone"):
            rows = (st.work.events(lot, None) if kind == "work-event" else
                    st.work.milestones(lot, None))
            matches = [row for row in rows if row["id"] == reference["id"]
                       and row["work_item_id"] == lot]
            if len(matches) != 1:
                raise TrainError("original %s:%s absent or outside lot #%s" %
                                 (kind, reference["id"], lot))
            raw = matches[0]
            if kind == "milestone":
                raw = {key: raw[key] for key in ("id", "work_item_id", "kind", "sha",
                                                "actor", "verdict", "note", "at_ts")}
            author = raw["actor"]
        elif kind == "mail":
            raw = st.mailbox.get(reference["id"])
            if raw is None:
                raise TrainError("original mail:%s unavailable" % reference["id"])
            # Delivery/reservation metadata may change after import without
            # changing the original author's statement or its qualification.
            raw = {key: raw.get(key) for key in ("id", "sender", "recipient", "body", "kind",
                "payload", "host", "signature", "signature_key", "signed_payload", "nonce",
                "created_ts", "signature_expires_ts")}
            author = raw["sender"]
        else:  # immutable, versioned manifest; its author is still no vote
            commit, name = reference["commit"], reference["path"]
            raw = dict(commit=commit, path=name,
                       content=_git(path, "show", commit + ":" + name),
                       metadata=_git(path, "show", "-s", "--format=raw", commit))
            author = _git(path, "show", "-s", "--format=%an <%ae>", commit)
        if not isinstance(author, str) or not author.strip():
            raise TrainError("original qualification author unavailable")
        result.append(dict(reference=copy.deepcopy(reference), author=author,
                           record=copy.deepcopy(raw)))
    return result


def import_ref(db, project, path, ref, *, lot, source_head, qualification, actor):
    """Import a published two-parent handoff and existing qualification IDs.

    No generation or freeze ID is stored here. Repeating/concurrently importing
    the same projection has one journal effect. A later source withdrawal is
    checked by Recorded, never hidden by this immutable index snapshot.
    """
    if not isinstance(qualification, dict):
        raise TrainError("structured existing qualification references required")
    source = merge_queue.source_of(project.repo)
    target = merge_queue._remote_branch(project.target)
    prefix = "refs/ameesh/resolutions/%d/" % lot
    if not isinstance(ref, str) or not ref.startswith(prefix):
        raise TrainError("published transport ref must belong to this lot")
    plan_git.check_ref(ref, "transport ref")
    remote = land._origin(project) or project.repo
    slot = "refs/ameesh/train-imports/" + digest([source, ref])
    proc = resolutions._run(path, "fetch", "--quiet", "--no-tags", remote,
                            "+%s:%s" % (ref, slot), timeout=FETCH_TIMEOUT)
    if proc.returncode:
        raise TrainError("published transport ref unavailable")
    head = _git(path, "rev-parse", "--verify", slot + "^{commit}")
    parents = _git(path, "rev-list", "--parents", "-n", "1", head).split()
    if len(parents) != 3:
        raise TrainError("two-parent transport commit required for import")
    facts = dict(source=source, target=target, lot=lot, source_head=source_head,
                 base=parents[1], head=head, tree=_git(path, "rev-parse", head + "^{tree}"),
                 delta=train_transports.delta(path, parents[1], head), ref=ref)
    document = dict(schema=SCHEMA, facts=facts, qualification=copy.deepcopy(qualification),
                    message=_git(path, "show", "-s", "--format=%B", head),
                    originals=[None] * len(qualification.get("references", [])))
    _validate(document)
    train_transports.verify(path, _receipt(facts, qualification, _row(facts)), _row(facts))
    document["originals"] = _fingerprints(originals(db, path, lot, qualification["references"]))
    # Store the original message too: the index can be audited/rebuilt from
    # the published object, without treating its prose as qualification.
    text = _prefix(lot) + json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    work.note(db, lot, text, actor=actor, once=True)
    return document


class Recorded:
    """Passage port: all source reads use the supplied current connection.

    resolve(db, *, facts, qualification, originals, history) must return
    {"facts": <same exact facts>, "qualification": <same references/kind>,
     "active": True}, after resolving the originals' meaning and authority.
    None, an absent resolver or another scope is a named member deferral.
    This is the same explicit trust boundary as historical provenance, with
    no default inference from a journal projection's author or free prose.
    """
    def __init__(self, project, *, resolve=None):
        self.project, self.resolve = project, resolve

    def _sources(self, path, facts, references):
        remote = land._origin(self.project) or self.project.repo
        live = resolutions._run(path, "ls-remote", "--exit-code", remote, facts["ref"],
                                timeout=FETCH_TIMEOUT)
        if live.returncode or resolutions._text(live).split() != [facts["head"], facts["ref"]]:
            raise TrainError("published transport ref withdrawn or moved")
        for reference in references:
            if reference["kind"] == "manifest" \
                    and plan_git._commit(path, reference["commit"]) != reference["commit"]:
                fetched = resolutions._run(path, "fetch", "--quiet", "--no-tags", remote,
                                          reference["commit"], timeout=FETCH_TIMEOUT)
                if fetched.returncode:
                    raise TrainError("original versioned manifest unavailable")

    def __call__(self, db, *, source, target, row, base, path, expected=None):
        lot = row["work_item_id"]
        if (merge_queue.source_of(self.project.repo),
                merge_queue._remote_branch(self.project.target)) != (source, target):
            raise TrainError("transport reader belongs to another project context")
        item = storage.of(db).work.get(lot)
        if item is None:
            raise TrainError("transport source lot disappeared")
        documents = {}
        seen = False
        prefix = _prefix(lot)
        for event in storage.of(db).work.events_noted(prefix, item["created_ts"]):
            if event["work_item_id"] != lot:
                continue
            seen = True
            try:
                document = json.loads(event["note"][len(prefix):])
                _validate(document)
            except (TypeError, ValueError, KeyError, TrainError) as exc:
                raise TrainError("unreadable transport index for lot #%s" % lot) from exc
            facts = document["facts"]
            if facts["lot"] != lot or (facts["source"], facts["target"]) != (source, target):
                raise TrainError("transport index belongs to another lot, source or target")
            if facts["source_head"] == row["frozen_sha"] and facts["base"] == base:
                documents[digest(document)] = document
        if not documents:
            if expected is not None or seen:
                raise TrainError("transport index absent for current freeze and intermediate base")
            return None
        if len(documents) != 1:
            raise TrainError("ambiguous existing transport qualification for lot #%s" % lot)
        document = next(iter(documents.values()))
        facts, qualification = document["facts"], document["qualification"]
        self._sources(path, facts, qualification["references"])
        current = originals(db, path, lot, qualification["references"])
        if _fingerprints(current) != document["originals"]:
            raise TrainError("original transport qualification facts changed")
        if not callable(self.resolve):
            raise TrainError("original transport qualification resolver unavailable")
        history = dict(events=storage.of(db).work.events(lot, None),
                       milestones=storage.of(db).work.milestones(lot, None))
        answer = self.resolve(db, facts=copy.deepcopy(facts),
            qualification=copy.deepcopy(qualification), originals=current, history=history)
        if not isinstance(answer, dict) or set(answer) != {"facts", "qualification", "active"} \
                or answer["active"] is not True or answer["facts"] != facts \
                or answer["qualification"] != qualification:
            raise TrainError("original transport qualification absent, withdrawn or outside context")
        return _receipt(facts, qualification, row)
