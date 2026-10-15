# SPDX-License-Identifier: AGPL-3.0-only
"""Complete historical reads for train admissions; no provider is activated.

The caller supplies the audited review requirement and exact provenance
provider. This reader neither classifies a lot nor infers missing authors.
It works on fetched Git objects, never on a mutable checkout's diff.
"""
from __future__ import annotations

from . import resolutions, storage, train_review
from .train import TrainError, identity


class Recorded:
    def __init__(self, db, path, *, provider):
        if provider is None:
            raise TrainError("trusted exact provenance provider required")
        self.db, self.path, self.provider = db, path, provider

    def _git(self, *args):
        proc = resolutions._run(self.path, *args)
        if proc.returncode:
            raise TrainError("cannot read complete train history: %s" % " ".join(args))
        return resolutions._text(proc)

    def qualify(self, row, base, *, required):
        head = row["frozen_sha"]
        if not isinstance(base, str) or not train_review.SHA.fullmatch(base) \
                or not isinstance(head, str) or not train_review.SHA.fullmatch(head):
            raise TrainError("exact full base and frozen head required")
        # An apparently complete rev-list in a shallow clone can silently
        # omit a content author. Never change shared shallow boundaries here.
        if self._git("rev-parse", "--is-shallow-repository") != "false":
            raise TrainError("complete Git history required; shallow clone refused")
        self._git("merge-base", base, head)
        commits = self._git("rev-list", "--reverse", "--topo-order",
                            base + ".." + head).splitlines()
        # No LIMIT 100: an older blocked vote must survive newer OKs by
        # other actors and repeated freezes. SQL LIMIT NULL is unbounded.
        marks = storage.of(self.db).work.milestones(row["work_item_id"], None)
        freezes = [mark for mark in marks if mark["kind"] == "frozen"]
        if not freezes:
            raise TrainError("current freeze missing from complete history")
        freeze = max(freezes, key=train_review._order)
        raw = str(freeze.get("sha") or "")
        if freeze["id"] != row["frozen_id"] or len(raw) < 7 or not head.startswith(raw) \
                or self._git("rev-parse", "--verify", raw + "^{commit}") != head:
            raise TrainError("admission no longer names the latest exact freeze")
        proof = train_review.qualify_recorded(self.db, head, commits, marks,
                                             required=required, provider=self.provider)
        return dict(schema="ameesh-train-history/1", base=base,
                    admission=identity(row), reviews=proof)
