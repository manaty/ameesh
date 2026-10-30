# SPDX-License-Identifier: AGPL-3.0-only
"""#2400: land-time migration numbering in the train composition.

The frozen head of a member and its verdicts are never rewritten. The train
merges that head, then continues the target's migration numbering with the
files the merge adds (#702). Historical migrations are never touched: a member
that modifies or deletes one is deferred for an explicit reviewed delta.

The transform is deterministic and local to the train's private clone. It
executes no SQL and writes no remote ref. L122 reservations are moved before
publication and released after landing by ``Allocator``; the mapping itself
travels in the immutable train plan and in the final commit message.
"""
from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass

from . import migrations, plan_git, resolutions
from .train import MemberDeferred, TrainError

#: a numbered migration file, any width (Nexlink 3, ameesh 4)
MIGRATION = re.compile(r"^(\d+)_(.+)\.sql$")
#: a companion of a migration: same numeric prefix, `_` or `-` separator
COMPANION = re.compile(r"^(\d+)([_-])(.*)$")
MAX_NUMBER = 9999
#: modes Git may report for a regular file; a symlink or submodule is refused
REGULAR = ("100644", "100755")


@dataclass(frozen=True)
class Layout:
    """Where a project keeps its migrations and how wide its numbers are."""

    directory: str
    width: int

    def label(self, number: int) -> str:
        return str(int(number)).zfill(self.width)

    def under(self, path: str) -> bool:
        return path.startswith(self.directory + "/")


def layout(project) -> Layout | None:
    """The migration layout of a radar project, or None when it has none."""
    directory = (getattr(project, "migrations", "") or "").strip().strip("/")
    if not directory:
        return None
    known = migrations.LAYOUTS.get(migrations.repo_key(
        getattr(project, "github", "") or ""))
    return Layout(directory, migrations.width_of(project, known))


def _run(path: str, *args: str):
    return resolutions._run(path, *args)


def _git_out(path: str, *args: str) -> str:
    return plan_git._out(path, *args)


def _diff(path: str, base: str, head: str) -> list[tuple[str, str]]:
    """(status, path) of the changed files, NUL-safe and rename-free.

    ``--no-renames`` keeps a rename as a deletion plus an addition: a
    historical migration can never hide behind a rename.
    """
    proc = _run(path, "diff", "--name-status", "-z", "--no-renames",
                "--no-ext-diff", base, head, "--")
    if proc.returncode:
        raise TrainError("cannot read migration diff %s..%s" % (base, head))
    fields = proc.stdout.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    if len(fields) % 2:
        raise TrainError("unreadable migration diff")
    return [(os.fsdecode(fields[i]), os.fsdecode(fields[i + 1]))
            for i in range(0, len(fields), 2)]


def _numbered(path: str, ref: str, target: Layout) -> dict[int, list[str]]:
    proc = _run(path, "ls-tree", "-r", "--name-only", ref, "--", target.directory + "/")
    if proc.returncode:
        raise TrainError("cannot read migrations at %s" % ref)
    found: dict[int, list[str]] = {}
    for name in resolutions._text(proc).splitlines():
        match = MIGRATION.match(os.path.basename(name))
        if match:
            found.setdefault(int(match.group(1)), []).append(name)
    return found


def maximum(path: str, ref: str, target: Layout) -> int:
    """The highest migration number already present in ``ref`` (0 if none)."""
    return max(_numbered(path, ref, target), default=0)


def _mode(path: str, ref: str, name: str) -> str:
    proc = _run(path, "ls-tree", ref, "--", name)
    if proc.returncode:
        raise TrainError("cannot read %s at %s" % (name, ref))
    line = resolutions._text(proc).strip()
    if not line:
        return ""
    fields = line.split(None, 3)
    return fields[0] if fields else ""


def _read(path: str, ref: str, name: str) -> bytes:
    proc = _run(path, "cat-file", "blob", "%s:%s" % (ref, name))
    if proc.returncode:
        raise TrainError("cannot read %s at %s" % (name, ref))
    return proc.stdout


def _write(path: str, ref: str, name: str, content: bytes) -> None:
    """Write one blob into the checked-out tree, never a symlink or submodule."""
    mode = _mode(path, ref, name)
    if mode not in REGULAR:
        raise TrainError("migration artifact %s is not a regular file" % name)
    destination = os.path.join(path, name)
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    with open(destination, "wb") as fh:
        fh.write(content)
    if mode == "100755":
        os.chmod(destination, 0o755)


def _decode(data: bytes, name: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TrainError("migration artifact %s is not UTF-8" % name) from exc


def _comment_numbers(text: str, old: int, new: int) -> str:
    """Rewrite the old number only in comment lines of a renamed artifact.

    A SQL/script comment that names the migration is a trace, not a literal:
    code, strings and test fixtures keep their bytes so an incidental number
    can never change behavior. Path references are handled separately by their
    exact file name.
    """
    lines = text.splitlines(keepends=True)
    pattern = re.compile(r"(?<![0-9])%d(?![0-9])" % old)
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("--") or stripped.startswith("#") \
                or stripped.startswith("//"):
            lines[index] = pattern.sub(str(new), line)
    return "".join(lines)


def _rename(row: dict, name: str, reason: str) -> MemberDeferred:
    return MemberDeferred(row, "migration %s: %s" % (name, reason))


def rewrite(path: str, base: str, head: str, target: Layout, row: dict, *,
            expected: dict | list | None = None, allocator=None) -> dict | None:
    """Renumber the migrations one clean merge adds, and return the new tree.

    ``base`` is the composed prefix, ``head`` the merge commit of ``row``.
    The member's frozen commit is untouched. ``expected`` (the persisted plan
    entries of this lot) makes a reconstruction verify the same old and new
    numbers instead of allocating again; one dict is accepted for a lot with a
    single migration. Without it, ``allocator`` reserves each new number
    before the tree is written. #2481 : a lot may add several migrations; they
    take the next contiguous numbers in file order, and each keeps its own
    ``reserved``/``landed`` mapping and companion files.

    Returns ``{"tree", "migrations"}`` when the merge adds a migration, else
    None.
    """
    changes = _diff(path, base, head)
    historical = [name for status, name in changes
                  if status in ("M", "D", "T") and target.under(name)]
    if historical:
        raise _rename(row, historical[0],
                      "a historical migration cannot be changed by composition")
    added = [(status, name) for status, name in changes if status == "A"]
    numbered = []
    for status, name in added:
        if not target.under(name) or not name.endswith(".sql"):
            continue
        match = MIGRATION.match(os.path.basename(name))
        if match is None:
            raise _rename(row, name, "a migration file must be <number>_<name>.sql")
        number = int(match.group(1))
        if not 1 <= number <= MAX_NUMBER:
            raise _rename(row, name, "migration number outside 0001-9999")
        numbered.append((number, name, match.group(1)))
    if not numbered:
        if expected:
            raise TrainError("persisted migration mapping names lot #%s but the merge "
                             "adds no migration" % row["work_item_id"])
        return None
    numbered.sort(key=lambda entry: (entry[0], entry[1]))
    prefixes = {old: prefix for old, _, prefix in numbered}
    # The member's own additions are already in ``head``: the target's
    # numbering is the one of the composed prefix before this merge.
    first = maximum(path, base, target) + 1
    if first + len(numbered) - 1 > MAX_NUMBER:
        raise _rename(row, numbered[-1][1], "no migration number left")
    assigned = [(old, first + offset, name)
                for offset, (old, name, _prefix) in enumerate(numbered)]
    new_for_old = {old: new for old, new, _name in assigned}
    expected_by_reserved = None
    if expected is not None:
        entries = [expected] if isinstance(expected, dict) else list(expected)
        if not entries or not all(isinstance(entry, dict) for entry in entries):
            raise TrainError("persisted migration mapping no longer belongs to lot #%s"
                             % row["work_item_id"])
        expected_by_reserved = {}
        for entry in entries:
            if (entry.get("work_item_id") != row["work_item_id"]
                    or not isinstance(entry.get("reserved"), int)
                    or not isinstance(entry.get("landed"), int)
                    or entry["reserved"] in expected_by_reserved):
                raise TrainError("persisted migration mapping no longer belongs to lot #%s"
                                 % row["work_item_id"])
            expected_by_reserved[entry["reserved"]] = entry
        if set(expected_by_reserved) != set(new_for_old) or any(
                expected_by_reserved[old]["landed"] != new
                for old, new, _name in assigned):
            raise TrainError("persisted migration mapping no longer belongs to lot #%s"
                             % row["work_item_id"])
    source_old = {}
    for status, name in added:
        base_name = os.path.basename(name)
        match = COMPANION.match(base_name)
        if match is None or match.group(2) not in ("_", "-"):
            continue
        old = int(match.group(1))
        if old not in new_for_old or new_for_old[old] == old:
            continue
        source_old[name] = old
    renames: dict[str, str] = {}
    for name, old in sorted(source_old.items()):
        base_name = os.path.basename(name)
        old_prefix = prefixes[old]
        new = new_for_old[old]
        new_prefix = str(new).zfill(len(old_prefix))
        destination = os.path.join(os.path.dirname(name),
                                   new_prefix + base_name[len(old_prefix):])
        if destination == name:
            continue
        if destination in renames.values():
            raise _rename(row, name, "two companions would share %s" % destination)
        renames[name] = destination
    for name, destination in sorted(renames.items()):
        if destination not in renames and _mode(path, head, destination):
            raise _rename(row, name, "destination %s already exists" % destination)
    files_by_old: dict[int, list[dict]] = {old: [] for old, new, _name in assigned
                                           if new != old}
    for old_name, new_name in sorted(renames.items()):
        files_by_old[source_old[old_name]].append({"from": old_name, "to": new_name})
    for old, new, name in assigned:
        if new != old and name not in renames:
            raise _rename(row, name, "the added migration itself is missing")
    entries = [dict(work_item_id=row["work_item_id"], reserved=old, landed=new,
                    files=files_by_old.get(old, []))
               for old, new, _name in assigned]
    if expected_by_reserved is not None:
        for entry in entries:
            if expected_by_reserved[entry["reserved"]].get("files") != entry["files"]:
                raise TrainError("persisted migration files changed for lot #%s"
                                 % row["work_item_id"])
    if allocator is not None and expected is None:
        for old, new, _name in assigned:
            allocator.reserve(row, path, old, new, target)
    if not renames:
        # The member already continues the target: no byte changes, but the
        # reservations are still moved before publication.
        return dict(tree=_git_out(path, "rev-parse", head + "^{tree}"),
                    migrations=entries)
    names = {os.path.basename(old_name): os.path.basename(new_name)
             for old_name, new_name in renames.items()}
    name_pattern = re.compile("|".join(re.escape(name) for name in
                                     sorted(names, key=len, reverse=True)))
    rewritten: dict[str, bytes] = {}
    for status, name in changes:
        if status not in ("A", "M"):
            continue
        data = _read(path, head, name)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            if name in renames:
                raise _rename(row, name, "a renamed artifact must be UTF-8")
            continue
        changed = name_pattern.sub(lambda match: names[match.group()], text)
        if name in renames:
            changed = _comment_numbers(changed, source_old[name], new_for_old[source_old[name]])
        if changed != text:
            rewritten[name] = changed.encode("utf-8")
    resolutions._check(_run(path, "checkout", "-q", "-f", "--detach", head),
                       "checkout migration composition")
    for name, content in rewritten.items():
        _write(path, "HEAD", name, content)
    # Stage every source before writing destinations: one destination may
    # still be another migration's source. Moving preserves executable modes.
    with tempfile.TemporaryDirectory(prefix=".migration-renames-", dir=path) as tmp:
        staged = []
        for index, (old_name, new_name) in enumerate(renames.items()):
            source = os.path.join(tmp, str(index))
            os.replace(os.path.join(path, old_name), source)
            staged.append((source, new_name))
        for source, new_name in staged:
            destination = os.path.join(path, new_name)
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            os.replace(source, destination)
    touched = sorted(set(rewritten) | set(renames) | set(renames.values()))
    proc = _run(path, "add", "-A", "--", *touched)
    if proc.returncode:
        raise TrainError("cannot stage migration renumbering: %s"
                         % resolutions._err(proc)[:300])
    tree = _git_out(path, "write-tree")
    return dict(tree=tree, migrations=entries)


def audit(path: str, base: str, head: str, target: Layout) -> None:
    """Fail closed unless the additions continue the base's numbering exactly.

    Historical migrations stay byte-identical; every addition is numbered
    ``maximum+1 … maximum+n`` without a gap or a duplicate (#702).
    """
    changes = _diff(path, base, head)
    historical = [name for status, name in changes
                  if status in ("M", "D", "T") and target.under(name)]
    if historical:
        raise TrainError("train changed a historical migration: %s" % historical[0])
    added = []
    for status, name in changes:
        if status != "A" or not target.under(name) or not name.endswith(".sql"):
            continue
        match = MIGRATION.match(os.path.basename(name))
        if match is None:
            raise TrainError("train added an invalid migration name: %s" % name)
        added.append(int(match.group(1)))
    if not added:
        return
    floor = maximum(path, base, target)
    expected = list(range(floor + 1, floor + 1 + len(added)))
    if sorted(added) != expected:
        raise TrainError("train migration numbering is not contiguous: %s (expected %s)"
                         % (sorted(added), expected))


def guard_transport(path: str, base: str, head: str, target: Layout, row: dict) -> None:
    """Refuse a qualified transport that carries an unrenumbered migration.

    A transport receipt freezes its tree and delta, so composition cannot
    renumber it without invalidating the qualification it was reviewed under.
    The member is deferred: its migration must be renumbered before the
    transport is qualified (or it takes the direct-merge path).
    """
    changes = _diff(path, base, head)
    historical = [name for status, name in changes
                  if status in ("M", "D", "T") and target.under(name)]
    if historical:
        raise _rename(row, historical[0],
                      "a historical migration cannot be changed by composition")
    added = [name for status, name in changes
             if status == "A" and target.under(name) and name.endswith(".sql")]
    if added:
        raise _rename(row, added[0],
                      "a qualified transport cannot carry an unrenumbered migration")


class Allocator:
    """L122 side of the composition: add the new slots, release all at landing.

    The caller must hold the target reservation. ``reserve`` runs before the
    train branch is published; a refusal names the member and defers it rather
    than publishing a package whose number is not reserved.

    #2481 : a lot may hold several reservations, so ``reserve`` ADDS the
    composition number to the lot's slots instead of replacing its previous
    one. The old number stays reserved until landing (a failed or superseded
    package therefore leaves its slots on the lot, and a retry can add more);
    ``landed`` releases every slot at once, and the plan carries the
    authoritative old -> new mapping. Releasing the previous number earlier
    would require a batch release after all the lot's new numbers are taken,
    which the one-number contract of ``work.reserve_migration`` does not
    express.
    """

    def __init__(self, db, cfg=None, *, actor: str = "ameesh train"):
        self.db, self.cfg, self.actor = db, cfg, actor

    def reserve(self, row: dict, path: str, old: int, number: int,
                target: Layout) -> None:
        """Reserve ``number`` for the member, keeping its previous slots.

        The reservation is added before the rewritten tree is published; a
        refusal is a member deferral, never a package with an unreserved
        number. The previous number (``old``) is not released here: the plan's
        mapping keeps it meaningful until the lot lands and ``landed`` frees
        the whole set.
        """
        from . import work

        try:
            work.reserve_migration(
                self.db, row["work_item_id"], number, repo=path, cfg=self.cfg,
                actor=self.actor, skip_branches=(row.get("branch") or "",),
                skip_worktree=True, target=row.get("target"))
        except work.WorkError as exc:
            raise MemberDeferred(row, "migration %s -> %s: %s" % (
                target.label(old), target.label(number), exc)) from exc
        except Exception as exc:
            raise TrainError("cannot reserve migration %s -> %s for lot #%s: %s" % (
                target.label(old), target.label(number), row["work_item_id"], exc)) from exc

    def landed(self, plan: dict, *, path: str | None = None) -> list[str]:
        """Record the landed number and free the reservations, after promotion.

        The target already carries the plan's head, so ``work.merged`` can
        verify the exact commit. A failure never revokes the landing; it is
        returned so the passage can notify the pilot and retry later.
        #2481 : the plan carries one entry per migration; the lot is closed
        once with all its mappings in the journal, then every reservation is
        released.
        """
        from . import work

        self._align_target(plan, path)
        errors = []
        by_lot: dict[int, list[dict]] = {}
        for entry in plan.get("migrations", ()):
            by_lot.setdefault(int(entry["work_item_id"]), []).append(entry)
        for lot, entries in sorted(by_lot.items()):
            entries.sort(key=lambda entry: (int(entry["landed"]),
                                            int(entry["reserved"])))
            numbers = [int(entry["landed"]) for entry in entries]
            reserved = [int(entry["reserved"]) for entry in entries]
            note = ""
            if len(entries) > 1:
                note = "migration mapping %s -> %s" % (
                    ", ".join(str(number) for number in reserved),
                    ", ".join(str(number) for number in numbers))
            try:
                work.merged(self.db, lot, sha=plan["head"], actor=self.actor,
                            repo=path, migration_landed=numbers[0], note=note,
                            cfg=self.cfg)
            except Exception as exc:
                errors.append("lot #%s landed migration not recorded: %s" % (lot, exc))
                continue
            try:
                work.reserve_migration(self.db, lot, release=True,
                                       actor=self.actor, cfg=self.cfg)
            except Exception as exc:
                errors.append("lot #%s reservations %s not released: %s" % (
                    lot, ", ".join(str(number) for number in reserved), exc))
        return errors

    def _align_target(self, plan: dict, path: str | None) -> None:
        """Point the private clone's local target at the proven promotion.

        ``promote`` pushes to the remote and may leave the clone's local branch
        on the base; ``work.merged`` proves the merge against that local branch.
        The runtime calls this only after promotion or reconcile observed the
        exact head, so the update cannot turn an unproven merge into a proof.
        """
        from . import merge_queue

        if not path:
            return
        branch = merge_queue._remote_branch((plan.get("target") or "").strip())
        if not branch:
            return
        ref = "refs/heads/" + branch
        current = _run(path, "rev-parse", "--verify", "--quiet", ref)
        if current.returncode == 0:
            sha = resolutions._text(current).strip()
            if sha == plan["head"]:
                return
            if sha != plan.get("base"):
                # An unexpected local branch is never rewound; work.merged
                # decides from the actual ref and reports a refusal.
                return
        old = () if current.returncode else (plan.get("base") or "",)
        proc = _run(path, "update-ref", ref, plan["head"], *old)
        if proc.returncode:
            raise TrainError("cannot align local target %s on %s: %s" % (
                branch, plan["head"], resolutions._err(proc)[:300]))
