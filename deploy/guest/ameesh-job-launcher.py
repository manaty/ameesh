# SPDX-License-Identifier: AGPL-3.0-only
"""ameesh-job-launcher: the root-owned entry point of an offloaded job (L159b2).

Installed root-owned by `deploy/guest/install.sh` and admitted to the guest
user by a sudoers rule, as the job's own user:

    ameesh-guest ALL=(ameesh-job) NOPASSWD: /usr/local/lib/ameesh-guest/ameesh-job-launcher

`ameesh-offload-shell`, running as `ameesh-guest`, starts every phase of a job
through it. The launcher runs as the job user, so the job's own systemd user
manager (linger) still owns its slice and rootless Podman keeps its cgroup
delegation, while the job's UID is not the shell's: the kernel denies the job
the shell's `/proc` (so not the session nonce nor its streams), its HOME, its
mirrors and its caches, and the job cannot write what the shell's own git
reads.

Verbs (the sudoers rule admits the file, this script checks the rest; it
refuses to run as root, so a misconfigured rule fails closed):

    run --job ID --phase PHASE -- ARGV...   environment JSON on stdin
    open --job ID                            caps the job's slice
    kill --job ID                            SIGKILLs the job's slice
    close --job ID                           kill, then drop the runtime caps
    clean --path DIR                         removes what the job left behind
    check                                    can this launcher work here?
"""
import json
import os
import re
import shutil
import subprocess
import sys

#: root-owned, written by install.sh (the launcher takes no --config)
CONFIG = "/etc/ameesh-guest/offload.json"
SYSTEMD_RUN = "/usr/bin/systemd-run"
SYSTEMCTL = "/usr/bin/systemctl"
ENV_BIN = "/usr/bin/env"
JOB_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")
PHASE_RE = re.compile(r"^[a-z][a-z0-9-]{0,15}$")
#: the phases whose code is the client's own: they keep the process, not the
#: job's user manager (`systemd-run` needs it, its payload does not)
CLIENT_PHASES = ("command", "setup")
UNSET_MANAGER = ("-u", "XDG_RUNTIME_DIR", "-u", "DBUS_SESSION_BUS_ADDRESS")
#: the environment of a job, as JSON on stdin: a profile plus the client's
#: --env, never a secret (the job's own code gets it anyway)
ENV_MAX = 1 << 20


def die(message: str, code: int = 2) -> int:
    sys.stderr.write("ameesh-job-launcher: %s\n" % message)
    return code


def load_config() -> dict:
    with open(CONFIG, encoding="utf-8") as fh:
        cfg = json.load(fh)
    cfg.setdefault("root", "/srv/ameesh-guest")
    cfg.setdefault("agent_memory_max", "")
    return cfg


def unit_names(job_id: str) -> tuple[str, str]:
    """The same names as the shell's: `-` nests slices in systemd."""
    return ("ameesh-job-%s.slice" % job_id.replace("-", "_"),
            "ameesh-job-%s" % job_id)


def job_arg(args: list[str]) -> str:
    if len(args) != 2 or args[0] != "--job" or not JOB_RE.match(args[1]):
        raise ValueError("expected --job <id>")
    return args[1]


def runtime_env() -> None:
    """The job's own user manager: sudo reset the environment, the launcher
    puts the address back for `systemctl --user` and `systemd-run --user`."""
    run_dir = "/run/user/%d" % os.getuid()
    os.environ["XDG_RUNTIME_DIR"] = run_dir
    os.environ["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=%s/bus" % run_dir


def systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([SYSTEMCTL, "--user", *args], capture_output=True,
                          text=True, check=False)


def cmd_check() -> int:
    run_dir = os.environ.get("XDG_RUNTIME_DIR", "")
    if not os.path.isdir(run_dir) or not os.path.exists(os.path.join(run_dir, "bus")):
        return die("no systemd user manager for uid %d (loginctl enable-linger?)"
                   % os.getuid())
    if systemctl("is-system-running").returncode not in (0, 1):
        # 1 = degraded, still usable; anything else means no manager at all
        return die("systemctl --user cannot reach the user manager")
    sys.stdout.write("ok\n")
    return 0


def cmd_open(args: list[str]) -> int:
    job_id = job_arg(args)
    cfg = load_config()
    props = ["MemoryMax=%s" % cfg["agent_memory_max"], "MemorySwapMax=0"] \
        if cfg.get("agent_memory_max") else []
    if not props:
        return 0
    res = systemctl("set-property", "--runtime", unit_names(job_id)[0], *props)
    if res.returncode != 0:
        return die("cannot cap %s: %s" % (unit_names(job_id)[0], res.stderr.strip()), 75)
    return 0


def cmd_kill(args: list[str]) -> int:
    job_id = job_arg(args)
    systemctl("kill", "--signal=SIGKILL", unit_names(job_id)[0])
    return 0


def cmd_close(args: list[str]) -> int:
    job_id = job_arg(args)
    slice_name, _ = unit_names(job_id)
    systemctl("kill", "--signal=SIGKILL", slice_name)
    systemctl("stop", slice_name)
    systemctl("revert", slice_name)
    return 0


def cmd_clean(args: list[str]) -> int:
    if len(args) != 2 or args[0] != "--path":
        return die("expected --path <dir>")
    root = os.path.realpath(load_config()["root"])
    real = os.path.realpath(args[1])
    if real == root or not real.startswith(root + os.sep):
        return die("clean: %s is outside %s" % (args[1], root))
    try:
        shutil.rmtree(real)
        return 0
    except OSError:
        pass
    # the top folder belongs to the shell (it created it): empty it and let
    # the shell remove what is left
    try:
        names = os.listdir(real)
    except OSError:
        return 0
    for name in names:
        path = os.path.join(real, name)
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.unlink(path)
        except OSError:
            pass
    return 0


def cmd_run(args: list[str]) -> int:
    if len(args) < 5 or args[0] != "--job" or args[2] != "--phase" or args[4] != "--":
        return die("usage: run --job <id> --phase <phase> -- <command...>")
    job_id, phase = args[1], args[3]
    argv = args[5:]
    if not JOB_RE.match(job_id) or not PHASE_RE.match(phase) or not argv:
        return die("run: invalid job, phase or empty command")
    raw = sys.stdin.buffer.read(ENV_MAX + 1)
    if len(raw) > ENV_MAX:
        return die("run: environment over %d bytes" % ENV_MAX)
    try:
        env = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        return die("run: bad environment JSON (%s)" % exc)
    if not isinstance(env, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
        return die("run: the environment must be a JSON object of strings")
    runtime_env()
    env = dict(env)
    env["XDG_RUNTIME_DIR"] = os.environ["XDG_RUNTIME_DIR"]
    env["DBUS_SESSION_BUS_ADDRESS"] = os.environ["DBUS_SESSION_BUS_ADDRESS"]
    # the phase inherits the launcher's environment (`--scope` executes it):
    # clear it first, sudo's own variables have no business in a job
    os.environ.clear()
    os.environ.update(env)
    slice_name, unit = unit_names(job_id)
    if phase != "command":
        unit += "-" + phase
    if phase in CLIENT_PHASES:
        # the client's own code (the command, the dependency setup) does not
        # get the job's user manager: through it, it could start a unit
        # outside the job's slice (the README promises the same for L159b)
        argv = [ENV_BIN, *UNSET_MANAGER, "--"] + argv
    cmd = [SYSTEMD_RUN, "--user", "--scope", "--quiet", "--collect",
           "--slice", slice_name, "--unit", unit, "--"] + argv
    os.execv(SYSTEMD_RUN, cmd)
    return 70  # unreachable


VERBS = {"check": cmd_check, "open": cmd_open, "kill": cmd_kill,
         "close": cmd_close, "clean": cmd_clean, "run": cmd_run}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if os.geteuid() == 0:
        # the sudoers rule must target the job user: a rule that would run
        # this as root is a misconfiguration, and this script refuses it
        return die("refusing to run as root: the sudoers rule must use (ameesh-job)")
    if not argv or argv[0] not in VERBS:
        return die("allowed verbs: %s" % ", ".join(sorted(VERBS)))
    runtime_env()
    try:
        return VERBS[argv[0]](argv[1:])
    except ValueError as exc:
        return die(str(exc))
    except OSError as exc:
        return die(str(exc), 70)


if __name__ == "__main__":
    sys.exit(main())
