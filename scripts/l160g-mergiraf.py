#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""L160g (#184): replay the merges of a repository with and without the
mergiraf syntactic merge driver, and compare both to the merge actually
committed.

The sample, frozen in a manifest (`--manifest-out`): every two-parent merge
reachable from the source's refs (GitHub's synthetic `refs/pull/*/merge`
excluded), committed (committer date) before `--before`, with the refs it
was read from. `--manifest` replays exactly the merges of a manifest, and
refuses one whose commit or parents are missing: the same sample, whatever
the clone's refs have become. Each merge is redone twice with `git
merge-tree --write-tree`: plain, then with mergiraf declared as the merge
driver of every language it knows (`mergiraf languages --gitattributes`,
through `core.attributesFile`). For each file the two replays treat
differently:

* `resolved`: in conflict plain, merged by mergiraf; `same` when its result
  is the blob the merge commit holds, `differs` otherwise (to read by hand:
  a wrong resolution, or an edit the author made in the merge commit);
* `still`: in conflict both ways;
* `markers`: set on a `resolved` or `altered` file whose mergiraf result
  holds more conflict-marker lines than either parent (reported merged,
  markers left in);
* `altered`: merged cleanly both ways, but not to the same content: mergiraf
  changed a merge git did on its own; `same`/`differs` as above, and
  whether plain git matched the commit;
* `new-conflict`: merged cleanly plain, in conflict with mergiraf.

Isolation (#393 r1). `--repo` is only read: the replay runs in a temporary
bare mirror that borrows its objects (`git clone --mirror --shared`), so
every object merge-tree writes lands there, and the whole temporary
directory goes at the end, whatever happens. Git runs offline and without
the user's configuration: no system or global config (so no merge driver
but mergiraf's, no hook, no alias), no lazy fetch from a promisor remote, no
replace objects, no prompt, in-tree `.gitattributes` ignored (`attr.tree` is
the empty tree). The only writes outside the temporary directory are the
outputs asked for: stdout, `--manifest-out` and `--diffs DIR` (the
`differs` cases, mergiraf vs commit, as patches).

    scripts/l160g-mergiraf.py --repo nexlink.git --before 2026-10-11T04:31 \\
        --manifest-out nexlink.manifest.json --mergiraf ~/bin/mergiraf \\
        --diffs diffs/nexlink > nexlink.jsonl
    scripts/l160g-mergiraf.py --repo nexlink.git --manifest nexlink.manifest.json \\
        --mergiraf ~/bin/mergiraf > nexlink.jsonl
"""
from __future__ import annotations

import argparse
import collections
import fnmatch
import json
import os
import re
import subprocess
import sys
import tempfile
import time

#: the empty tree: `attr.tree` pointed at it reads no in-tree .gitattributes
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
#: the sampling rule, written into the manifest
RULE = ("two-parent merges reachable from every ref but refs/pull/*/merge, "
        "committer date before the cutoff (git rev-list --merges --all "
        "--exclude=refs/pull/*/merge --before=<cutoff>)")


def offline_env() -> dict:
    """Git without the user's or system's configuration, never online."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_NO_LAZY_FETCH="1", GIT_NO_REPLACE_OBJECTS="1",
               GIT_TERMINAL_PROMPT="0", GIT_ALLOW_PROTOCOL="file")
    return env


ENV = offline_env()


def git(repo: str, *args: str, config: tuple[str, ...] = (), check: bool = True,
        text: bool = True) -> subprocess.CompletedProcess:
    cmd = ["git", "-C", repo, "-c", "attr.tree=" + EMPTY_TREE,
           "-c", "core.hooksPath=" + os.devnull]
    for item in config:
        cmd += ["-c", item]
    done = subprocess.run(cmd + list(args), capture_output=True, text=text, env=ENV)
    if check and done.returncode not in (0, 1):
        raise RuntimeError("git %s: %s" % (" ".join(args), done.stderr.strip()))
    return done


def mirror(source: str, into: str) -> str:
    """A bare mirror of `source` in `into`, borrowing its objects (read
    only): what merge-tree writes stays in the mirror."""
    path = os.path.join(into, "replay.git")
    done = subprocess.run(["git", "clone", "--quiet", "--mirror", "--shared",
                           source, path],
                          capture_output=True, text=True, env=ENV)
    if done.returncode != 0:
        raise RuntimeError("git clone: %s" % done.stderr.strip())
    git(path, "remote", "remove", "origin")     # nothing to fetch from, ever
    return path


def refs(repo: str) -> dict[str, str]:
    out = {}
    for line in git(repo, "for-each-ref", "--format=%(objectname) %(refname)").stdout.splitlines():
        oid, name = line.split(" ", 1)
        if not fnmatch.fnmatch(name, "refs/pull/*/merge"):
            out[name] = oid
    return out


def merges(repo: str, before: str | None) -> list[tuple[str, str, str]]:
    args = ["rev-list", "--merges", "--parents", "--exclude=refs/pull/*/merge", "--all"]
    if before:
        args.insert(1, "--before=" + before)
    out = []
    for line in git(repo, *args).stdout.splitlines():
        ids = line.split()
        if len(ids) == 3:
            out.append(tuple(ids))
    return out


def from_manifest(repo: str, manifest: dict) -> list[tuple[str, str, str]]:
    """The manifest's merges, each checked present with the same parents."""
    out = []
    for merge, p1, p2 in manifest["merges"]:
        done = git(repo, "rev-list", "--parents", "-n", "1", merge, check=False)
        if done.returncode != 0 or done.stdout.split() != [merge, p1, p2]:
            raise RuntimeError("merge %s of the manifest is missing or differs" % merge)
        out.append((merge, p1, p2))
    return out


def replay(repo: str, p1: str, p2: str, config: tuple[str, ...] = ()) -> tuple[str, set[str]]:
    """(tree, conflicted paths) of `git merge-tree` on p1 and p2."""
    done = git(repo, "merge-tree", "--write-tree", "--name-only", "-z", p1, p2,
               config=config)
    head, _, rest = done.stdout.partition("\0")
    conflicted: set[str] = set()
    if done.returncode == 1:
        for item in rest.split("\0"):
            if not item:
                break       # an empty field ends the file list
            conflicted.add(item)
    return head.strip(), conflicted


def blob(repo: str, tree: str, path: str) -> str | None:
    done = git(repo, "rev-parse", "--verify", "-q", "%s:%s" % (tree, path), check=False)
    return done.stdout.strip() if done.returncode == 0 else None


#: a conflict marker anywhere in a line: mergiraf 0.19.1 can glue one to the
#: end of the previous line and still report the file merged (exit 0)
MARKER = re.compile(r"(<{7}|>{7}) |\|{7} ")


def markers(repo: str, oid: str | None) -> int:
    """Lines of a blob that carry a conflict marker."""
    if not oid:
        return 0
    text = git(repo, "cat-file", "blob", oid, text=False).stdout.decode("utf-8", "replace")
    return sum(1 for line in text.splitlines() if MARKER.search(line))


def changed(repo: str, a: str, b: str) -> list[str]:
    return git(repo, "diff-tree", "-r", "--name-only", "-z", a, b).stdout.split("\0")[:-1] \
        if a != b else []


def language(path: str, patterns: list[str]) -> str:
    name = os.path.basename(path)
    for pattern in patterns:
        if fnmatch.fnmatch(name, pattern):
            return pattern
    return "-"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", required=True, help="source repository, only read")
    parser.add_argument("--mergiraf", default="mergiraf")
    parser.add_argument("--before", help="cutoff (committer date) of the sample")
    parser.add_argument("--manifest", help="replay exactly the merges of this manifest")
    parser.add_argument("--manifest-out", help="write the sample's manifest here")
    parser.add_argument("--diffs")
    parser.add_argument("--only-conflicts", action="store_true",
                        help="replay with mergiraf only the merges that conflict plain")
    args = parser.parse_args(argv)
    if args.manifest and args.before:
        parser.error("--manifest carries its own cutoff")

    with tempfile.TemporaryDirectory(prefix="l160g-") as work:
        return run(args, work)


def run(args, work: str) -> int:
    attrs = subprocess.run([args.mergiraf, "languages", "--gitattributes"],
                           capture_output=True, text=True, check=True, env=ENV).stdout
    patterns = [line.split()[0] for line in attrs.splitlines() if line.strip()]
    attributes = os.path.join(work, "mergiraf.gitattributes")
    with open(attributes, "w", encoding="utf-8") as fh:
        fh.write(attrs)
    plain = ("core.attributesFile=" + os.devnull,)
    config = ("core.attributesFile=" + attributes, "merge.mergiraf.name=mergiraf",
              "merge.mergiraf.driver=%s merge --git %%O %%A %%B -s %%S -x %%X -y %%Y "
              "-p %%P -l %%L" % args.mergiraf)
    repo = mirror(os.path.abspath(args.repo), work)
    if args.manifest:
        with open(args.manifest, encoding="utf-8") as fh:
            manifest = json.load(fh)
        sample = from_manifest(repo, manifest)
    else:
        sample = merges(repo, args.before)
        manifest = {"source": git(args.repo, "config", "--get", "remote.origin.url",
                                  check=False).stdout.strip() or None,
                    "cutoff": args.before, "rule": RULE, "refs": refs(repo),
                    "merges": [list(m) for m in sample]}
    if args.manifest_out:
        with open(args.manifest_out, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=1, sort_keys=True)
            fh.write("\n")
    if args.diffs:
        os.makedirs(args.diffs, exist_ok=True)

    totals: collections.Counter = collections.Counter()
    per_lang: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    spent = 0.0
    for merge, p1, p2 in sample:
        plain_tree, plain_conf = replay(repo, p1, p2, plain)
        totals["merges"] += 1
        if plain_conf:
            totals["conflicting"] += 1
        elif args.only_conflicts:
            continue
        start = time.monotonic()
        mg_tree, mg_conf = replay(repo, p1, p2, config)
        spent += time.monotonic() - start
        files = []
        for path in sorted(set(changed(repo, plain_tree, mg_tree)) | plain_conf):
            lang = language(path, patterns)
            actual = blob(repo, merge, path)
            ours = blob(repo, mg_tree, path)
            if path in plain_conf:
                kind = "still" if path in mg_conf else "resolved"
            elif path in mg_conf:
                kind = "new-conflict"
            else:
                kind = "altered"
            verdict = None
            if kind in ("resolved", "altered"):
                verdict = "same" if ours == actual else "differs"
            entry = {"path": path, "lang": lang, "kind": kind, "verdict": verdict}
            if verdict and markers(repo, ours) > max(markers(repo, blob(repo, p1, path)),
                                                     markers(repo, blob(repo, p2, path))):
                entry["markers"] = True     # reported merged, markers left in
                totals["markers_left"] += 1
            if kind == "altered":
                entry["plain_same"] = blob(repo, plain_tree, path) == actual
            files.append(entry)
            per_lang[lang][kind + ("/" + verdict if verdict else "")] += 1
            if verdict == "differs" and args.diffs and actual and ours:
                patch = git(repo, "diff", "--no-color", ours, actual).stdout
                name = "%s-%s.patch" % (merge[:10], path.replace("/", "_"))
                with open(os.path.join(args.diffs, name), "w", encoding="utf-8") as out:
                    out.write("# merge %s %s (%s)\n# --- mergiraf  +++ committed\n%s"
                              % (merge, path, kind, patch))
        if plain_conf:
            if not mg_conf:
                totals["fully_resolved"] += 1
            elif mg_conf < plain_conf:
                totals["partly_resolved"] += 1
        print(json.dumps({"merge": merge, "parents": [p1, p2],
                          "conflicted": sorted(plain_conf),
                          "conflicted_mergiraf": sorted(mg_conf), "files": files}))
        sys.stdout.flush()
    print("merges %(merges)d, conflicting %(conflicting)d, fully resolved by mergiraf "
          "%(fully_resolved)d, partly %(partly_resolved)d, merged with markers left "
          "%(markers_left)d" % collections.defaultdict(int, totals),
          file=sys.stderr)
    print("mergiraf replay time: %.1f s" % spent, file=sys.stderr)
    for lang, counts in sorted(per_lang.items()):
        print("  %-14s %s" % (lang, dict(sorted(counts.items()))), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
