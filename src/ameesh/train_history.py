# SPDX-License-Identifier: AGPL-3.0-only
"""Complete historical reads for train admissions; one reading with the gate.

The train no longer has its own verdict rule (#2681). `Recorded.qualify_subject`
reads the exact frozen range and the complete milestone history, then calls
`merge_queue.review_gate` — the same code the merge queue, the fire and
`paquet-candidats` use in their current-registry reading. The train supplies
the **historical** family source through `family_of` (exact receipts, then the
turn journal), so a verdict is counted with the family it had at its date, not
the agent's family today. It also passes the complete `authors` list, so the
common gate refuses a co-author's own verdict and another agent of a
co-author's family, not only the reference assignee (#2681 B, dev1/cx2).

Authors are read once for the train and the designation (`review_authors`,
#2681): exact commit receipts (#1465), the reference author at freeze, and a
designated reviewer's first-round correction under the §2 threshold. A commit
without a receipt is attributed to its agent committer's session family; a
third party without a receipt stays unknown and refuses the admission.

A blocked verdict on an earlier head is not read by the gate: only the frozen
head's verdicts count (canon §2). The same-head rule is unchanged — a block on
the freeze is lifted only by that reviewer's later `ok` on the freeze.

The caller supplies the audited review requirement (count and classified
files). This reader neither classifies a lot nor infers missing authors. It
works on fetched Git objects, never on a mutable checkout's diff.
"""
from __future__ import annotations

from . import (merge_queue, resolutions, review_authors, review_lot,
               review_provenance, storage, train_review)
from .train import TrainError, identity


class Recorded:
    def __init__(self, db, path, *, provider, review_rules=None):
        if provider is None:
            raise TrainError("trusted exact provenance provider required")
        self.db, self.path, self.provider = db, path, provider
        self.review_rules = review_rules
        if review_rules is not None:
            self.families = tuple(getattr(review_rules, "families", ()) or ())
        else:
            try:
                self.families = tuple(review_lot.compiled_families(db))
            except Exception:  # noqa: BLE001 - gate will refuse if it matters
                self.families = ()

    def _git(self, *args):
        proc = resolutions._run(self.path, *args)
        if proc.returncode:
            raise TrainError("cannot read complete train history: %s" % " ".join(args))
        return resolutions._text(proc)

    def qualify(self, row, base, *, required, klass=None):
        proof = self.qualify_subject(row, base, required=required, klass=klass)
        return dict(schema="ameesh-train-history/1", base=base,
                    admission=identity(row), reviews=proof["reviews"])

    def qualify_subject(self, row, base, *, required, klass=None):
        """Qualify a real freeze before queue allocation, without a generation.

        This is historical evidence only. Admission still needs an actual
        queue insertion and a fresh qualification of that persisted identity.
        """
        if not isinstance(klass, dict):
            raise TrainError("explicit classified review requirement expected")
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
        if not commits:
            raise TrainError("exact full base and frozen head required")
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
        item = storage.of(self.db).work.get(row["work_item_id"])
        if item is None:
            raise TrainError("lot no longer open for train admission")
        try:
            reference = review_authors.reference_author(
                self.db, item, freeze, self.families,
                provider=self.provider, strict=True)
            authors, unknown = review_authors.authors(
                self.db, item, freeze, commits, self.path, self.families,
                reference=reference, provider=self.provider, strict=True)
            if unknown:
                raise TrainError("unknown historical author for commit %s" % unknown[0])
            missing_family = [entry for entry in authors if not entry["family"]]
            if missing_family:
                raise TrainError("unknown model family for author %s"
                                 % missing_family[0]["name"])
            # La porte ne lit que les verdicts de la tête gelée : une preuve
            # d'une tête antérieure ne peut pas refuser l'admission.
            on_freeze = [mark for mark in marks if mark.get("kind") == "verdict"
                         and merge_queue._same_sha(mark.get("sha"), head, self.path)]
            family_of = review_authors.verdict_families(
                self.db, on_freeze, self.families, provider=self.provider, strict=True)
        except review_provenance.ProvenanceError as exc:
            raise TrainError(str(exc)) from exc
        # Le train lit l'auteur **au gel** (la même référence que la
        # désignation), pas une réassignation ultérieure.
        historical = dict(item)
        if reference.get("name"):
            historical["assignee"] = reference["name"]
        family_of[str(reference.get("name") or "").strip().lower()] = \
            str(reference.get("family") or "")
        gate = merge_queue.review_gate(
            self.db, marks, head, repo=self.path, klass=klass, item=historical,
            required=required, authors=authors,
            family_of=lambda name: family_of.get(str(name or "").strip().lower(), ""))
        if gate.get("error"):
            raise TrainError(gate["error"])
        if not gate.get("ok"):
            raise TrainError(gate.get("why") or "review gate refused the frozen head")
        reviews = dict(schema="ameesh-train-review-gate/1", head=head,
                       required=gate.get("required"), gate=gate, authors=authors)
        return dict(schema="ameesh-train-subject-history/1", base=base,
                    subject={key: row[key] for key in
                             ("work_item_id", "frozen_id", "frozen_sha")},
                    reviews=reviews)
