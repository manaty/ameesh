# SPDX-License-Identifier: AGPL-3.0-only
"""`ameesh offload`: run a heavy command (full suite, e2e, Postgres suites,
builds) on a lending VM instead of the workstation (lot L159b, mode b1 of the
L159 study).

  ameesh offload run [--host H] [--profile P] [--repo NAME] [--timeout S]
        [--env K=V]... [--fallback local|none] -- <command...>
  ameesh offload status [--host H] [<job>]
  ameesh offload cancel [--host H] <job>

`run`:
1. takes an exact snapshot of the working tree (tracked and untracked files,
   ignored ones excluded) through a temporary index, `git write-tree` and
   `git commit-tree`: neither the agent's index nor its refs move. The
   snapshot has no parent: no history travels;
2. asks the VM for its recent snapshots, and sends a thin pack of what they
   lack;
3. runs the command there, in a throwaway checkout of the snapshot, in the
   same subdirectory. Output and exit code come back unchanged.

The host is `--host` or `AMEESH_OFFLOAD_HOST` (an SSH destination, an alias
of ~/.ssh/config being the simplest). Without a host, when the VM is
unreachable before the job starts, or when it refuses or stops the job
(gate not `available`), the command runs on the workstation (`--fallback
local`, the default). A timeout, a cancel or a connection lost during the
job is never rerun: the suite may still be running on the VM.

`AMEESH_OFFLOAD_SSH` replaces the `ssh` command (tests, or extra options).
The VM side is `ameesh.offload_shell` (forced command of the SSH key).
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import signal
import subprocess
import sys
import tempfile
import threading

from .offload_shell import PREFIX

#: exit code when the VM refused or stopped the job and no fallback is allowed
EX_TEMPFAIL = 75
#: snapshot identity: fixed, so that the same tree gives the same commit id
SNAP_ENV = {"GIT_AUTHOR_NAME": "ameesh offload", "GIT_AUTHOR_EMAIL": "offload@ameesh",
            "GIT_COMMITTER_NAME": "ameesh offload", "GIT_COMMITTER_EMAIL": "offload@ameesh"}


class OffloadError(Exception):
    """The offload could not happen (local git, or the VM is unreachable)."""


def _note(text: str) -> None:
    sys.stderr.write("ameesh offload: %s\n" % text)
    sys.stderr.flush()


def _git(repo: str, *args: str, env: dict | None = None, input: str | None = None) -> str:
    res = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                         env=env, input=input)
    if res.returncode != 0:
        raise OffloadError("git %s: %s" % (args[0], res.stderr.strip()))
    return res.stdout.strip()


def snapshot(top: str) -> str:
    """Commit id of the exact working tree. The agent's index is copied to a
    temporary one (its stat cache keeps `git add -A` fast), never written."""
    git_dir = _git(top, "rev-parse", "--absolute-git-dir")
    with tempfile.TemporaryDirectory(prefix="ameesh-offload-") as tmp:
        index = os.path.join(tmp, "index")
        env = dict(os.environ, GIT_INDEX_FILE=index, **SNAP_ENV)
        try:
            with open(os.path.join(git_dir, "index"), "rb") as src, open(index, "wb") as dst:
                dst.write(src.read())
        except FileNotFoundError:
            pass
        # exactly the working tree: a skip-worktree entry (sparse checkout,
        # absent from disk) or an assume-unchanged one (contents not looked
        # at) of the copied index would otherwise travel as the index has it
        full = ("-c", "core.sparseCheckout=false", "-c", "index.sparse=false")
        entries = _git(top, *full, "ls-files", "-v", "-z", env=env).split("\0")
        # one flag per call: update-index applies only the last of the two
        for flag, flagged in (
                ("--no-skip-worktree", [e[2:] for e in entries if e[:1] in ("S", "s")]),
                ("--no-assume-unchanged", [e[2:] for e in entries if e[:1].islower()])):
            if flagged:
                _git(top, *full, "update-index", flag, "-z", "--stdin", env=env,
                     input="\0".join(flagged) + "\0")
        _git(top, *full, "add", "-A", env=env)
        tree = _git(top, *full, "write-tree", env=env)
    # no parent: the history (a secret deleted since, or now ignored) never
    # travels; fixed date: the same tree always gives the same snapshot
    env = dict(os.environ, GIT_AUTHOR_DATE="@0 +0000", GIT_COMMITTER_DATE="@0 +0000",
               **SNAP_ENV)
    return _git(top, "commit-tree", tree, "-m", "ameesh offload snapshot", env=env)


def known_locally(top: str, shas: list[str]) -> list[str]:
    """The commits of `shas` this repository has (earlier snapshots stay as
    loose objects until git prunes them)."""
    if not shas:
        return []
    res = subprocess.run(["git", "-C", top, "cat-file", "--batch-check=%(objectname) %(objecttype)"],
                         input="\n".join(shas) + "\n", capture_output=True, text=True)
    return [line.split()[0] for line in res.stdout.splitlines()
            if line.endswith(" commit")]


def repo_name(top: str) -> str:
    """The mirror's name on the VM: `owner-repo` of origin, else the folder."""
    url = subprocess.run(["git", "-C", top, "remote", "get-url", "origin"],
                         capture_output=True, text=True).stdout.strip()
    match = re.search(r"([^/:]+)/([^/]+?)(?:\.git)?/?$", url)
    name = "%s-%s" % match.groups() if match else os.path.basename(top)
    return re.sub(r"[^A-Za-z0-9._-]", "-", name).lstrip(".-")[:100] or "repo"


class Remote:
    def __init__(self, host: str):
        self.host = host
        self.ssh = shlex.split(os.environ.get("AMEESH_OFFLOAD_SSH") or "ssh")

    def argv(self, *words: str) -> list[str]:
        return [*self.ssh, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                "-o", "ServerAliveInterval=30", self.host, shlex.join(words)]

    def call(self, *words: str, input: bytes | None = None, stdin=None,
             capture: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(self.argv(*words), input=input, stdin=stdin,
                              capture_output=capture)


def push(remote: Remote, top: str, repo: str, snap: str) -> None:
    # the VM lists its recent snapshots: the pack leaves out what they hold
    res = remote.call("put", repo, "--have", input=b"")
    if res.returncode != 0:
        raise OffloadError("VM unreachable or refused (put --have, exit %d): %s"
                           % (res.returncode, res.stderr.decode(errors="replace").strip()))
    present = res.stdout.decode().split()
    if snap in present:
        return
    bases = known_locally(top, present)
    revs = snap + "\n" + "".join("^%s\n" % sha for sha in bases)
    # --shallow: the bases share no history with the snapshot, so git must
    # treat every one of them as an edge to leave out the objects they hold
    pack = subprocess.Popen(["git", "-C", top, "pack-objects", "--revs", "--thin",
                             "--shallow", "--stdout", "-q", "--delta-base-offset"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    writer = threading.Thread(target=lambda: (pack.stdin.write(revs.encode()),
                                              pack.stdin.close()))
    writer.start()
    sent = remote.call("put", repo, snap, stdin=pack.stdout)
    pack.stdout.close()
    writer.join()
    if pack.wait() != 0:
        raise OffloadError("git pack-objects failed")
    if sent.returncode != 0:
        raise OffloadError("VM refused the snapshot (exit %d): %s"
                           % (sent.returncode, sent.stderr.decode(errors="replace").strip()))


def run_remote(remote: Remote, words: list[str]) -> tuple[int, dict | None, str | None]:
    """Runs `run …` on the VM: stdout passes through, stderr too except the
    protocol lines. Returns (ssh exit code, result object or None, job id
    or None)."""
    # the command's stderr shares the stream: only lines tagged with this
    # session's nonce are protocol, a look-alike printed by the command is
    # relayed as output and never taken for a result
    nonce = secrets.token_hex(16)
    tag = "%s%s " % (PREFIX, nonce)
    proc = subprocess.Popen(remote.argv(words[0], "--nonce", nonce, *words[1:]),
                            stdin=subprocess.DEVNULL, stderr=subprocess.PIPE)
    state: dict = {"job": None, "result": None}

    def relay() -> None:
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode(errors="replace")
            if not line.startswith(tag):
                sys.stderr.write(line)
                sys.stderr.flush()
                continue
            kind, _, rest = line[len(tag):].rstrip("\n").partition(" ")
            if kind == "job":
                state["job"] = rest
            elif kind == "result":
                try:
                    state["result"] = json.loads(rest)
                except ValueError:
                    state["result"] = {"outcome": "error", "reason": rest}
            elif kind == "waiting":
                _note("all slots of %s are busy, waiting…" % remote.host)
            elif kind == "setup":
                _note("building the dependency cache on %s (%s)" % (remote.host, rest))
            elif kind in ("refused", "error"):
                _note("%s: %s" % (kind, rest))

    reader = threading.Thread(target=relay, daemon=True)
    reader.start()
    try:
        rc = proc.wait()
    except KeyboardInterrupt:
        if state["job"]:
            _note("cancelling job %s" % state["job"])
            remote.call("cancel", state["job"])
        proc.send_signal(signal.SIGTERM)
        rc = proc.wait()
        reader.join(timeout=5)
        return rc, state["result"] or {"outcome": "cancelled", "job": state["job"]}, state["job"]
    reader.join(timeout=5)
    return rc, state["result"], state["job"]


def run_local(argv: list[str]) -> int:
    try:
        return subprocess.call(argv)
    except FileNotFoundError:
        _note("command not found: %s" % argv[0])
        return 127
    except KeyboardInterrupt:
        return 130


def cmd_run(opts: dict, argv: list[str]) -> int:
    fallback = opts["fallback"] == "local"
    host = opts["host"]

    def fall_back(why: str, code: int = EX_TEMPFAIL) -> int:
        if not fallback:
            _note("%s; no fallback (--fallback none)" % why)
            return code
        _note("%s; running on this machine" % why)
        return run_local(argv)

    if not host:
        return fall_back("no AMEESH_OFFLOAD_HOST")
    try:
        top = _git(os.getcwd(), "rev-parse", "--show-toplevel")
        rel = os.path.relpath(os.getcwd(), top)
        repo = opts["repo"] or repo_name(top)
        snap = snapshot(top)
        remote = Remote(host)
        push(remote, top, repo, snap)
    except OffloadError as exc:
        return fall_back(str(exc))
    words = ["run", repo, snap, "--profile", opts["profile"], "--cwd", rel]
    if opts["timeout"]:
        words += ["--timeout", str(opts["timeout"])]
    for item in opts["env"]:
        words += ["--env", item]
    rc, result, job = run_remote(remote, words + ["--"] + argv)
    if result is None:
        # no result line: the connection broke (ssh 255) or the shell is not
        # ours. The suite may still run there: never rerun it here as well
        # (two heavy suites at once); the VM side cancels a job whose session
        # is gone, and we ask it too, once.
        if job:
            remote.call("cancel", job)
        _note("lost the connection to %s (ssh exit %d)%s; not rerun on this machine"
              % (host, rc, ", job %s cancelled" % job if job else ""))
        return EX_TEMPFAIL
    outcome = result.get("outcome")
    if outcome == "done":
        return rc
    reason = result.get("reason") or outcome
    if outcome in ("refused", "stopped"):
        return fall_back("%s on %s: %s" % (outcome, host, reason))
    _note("job %s %s on %s: %s" % (result.get("job"), outcome, host, reason))
    # fixed codes: ssh's own (255 when it is interrupted) says nothing here
    return {"timeout": 124, "cancelled": 130}.get(outcome, rc or 70)


def cmd_status(host: str, args: list[str]) -> int:
    res = Remote(host).call("status", *args, capture=False)
    return res.returncode


def cmd_cancel(host: str, args: list[str]) -> int:
    if len(args) != 1:
        print(__doc__)
        return 2
    return Remote(host).call("cancel", args[0], capture=False).returncode


def parse(args: list[str]) -> tuple[dict, list[str]]:
    opts = {"host": os.environ.get("AMEESH_OFFLOAD_HOST", ""), "profile": "default",
            "repo": "", "timeout": 0, "env": [], "fallback": "local"}
    rest: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            return opts, rest + args[i + 1:]
        key = arg[2:] if arg.startswith("--") else None
        if key in ("host", "profile", "repo", "timeout", "env", "fallback"):
            if i + 1 >= len(args):
                raise ValueError("%s needs a value" % arg)
            val = args[i + 1]
            if key == "env":
                opts["env"].append(val)
            elif key == "timeout":
                opts["timeout"] = int(val)
            elif key == "fallback" and val not in ("local", "none"):
                raise ValueError("--fallback local|none")
            else:
                opts[key] = val
            i += 2
            continue
        rest.append(arg)
        i += 1
    return opts, rest


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    verb, args = argv[0], argv[1:]
    if verb == "run" and "--" not in args:
        print("ameesh offload run: put `--` before the command", file=sys.stderr)
        return 2
    head = args[:args.index("--")] if "--" in args else args
    if "-h" in head or "--help" in head:
        print(__doc__)
        return 0
    try:
        opts, rest = parse(args)
    except ValueError as exc:
        print("ameesh offload: %s" % exc, file=sys.stderr)
        return 2
    if verb == "run":
        if not rest:
            print("ameesh offload run: no command after `--`", file=sys.stderr)
            return 2
        return cmd_run(opts, rest)
    if verb in ("status", "cancel"):
        if not opts["host"]:
            print("ameesh offload: no host (--host or AMEESH_OFFLOAD_HOST)", file=sys.stderr)
            return 2
        return (cmd_status if verb == "status" else cmd_cancel)(opts["host"], rest)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
