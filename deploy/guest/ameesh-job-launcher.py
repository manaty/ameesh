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

The client's own code (`command`, `setup`) does not run under this launcher:
it runs under the internal `exec` verb, inside the scope, which strips the
manager's address from its environment and installs a seccomp filter denying
`socket(AF_UNIX)` before the payload's first instruction (the rule masks the
domain to the kernel's 32-bit `int`, so a widened domain is refused too).
Removing the variables was not enough (B3 of the R1 review of #354): a
same-UID payload reconstructed `/run/user/<uid>/bus` and asked the manager
for a unit outside the job's slice. `socketpair(2)` stays allowed (asyncio,
multiprocessing), and the filter cannot be removed: it is inherited across
fork and exec, so the payload cannot reach the manager, not even through
this launcher. The profile hooks (`before`, `after`) are root-owned
install-side code (rootless Podman needs the manager) and keep it; they are
never the payload's own code.
"""
import errno
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
JOB_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")
PHASE_RE = re.compile(r"^[a-z][a-z0-9-]{0,15}$")
#: the phases whose code is the client's own: they keep the process, not the
#: job's user manager (`systemd-run` needs it, its payload does not), and
#: they run under the seccomp filter of `exec`
CLIENT_PHASES = ("command", "setup")
#: the user manager's address: the launcher itself needs it for `systemd-run`,
#: the client's code never does
BUS_ENV = ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")
#: the filter the payload runs under: `socket(AF_UNIX)` is refused, everything
#: else (including socketpair) is allowed. The kernel reads `domain` as an
#: `int` (32 bits) while libseccomp compares the whole 64-bit register: an
#: equality test on `AF_UNIX` let `syscall(SYS_socket, 0x100000001, ...)`
#: through (B3 of the R2 reading), so the rule masks the argument instead.
SECCOMP_SO = "libseccomp.so.2"
AF_UNIX = 1
SCMP_ACT_ALLOW = 0x7FFF0000
SCMP_ACT_ERRNO = 0x00050000 | (errno.EAFNOSUPPORT & 0xFFFF)
SCMP_CMP_MASKED_EQ = 7
#: the width of the kernel's `int domain`, i.e. of every 32-bit `socket(2)`
#: argument: anything above it is invisible to the syscall
ARG_MASK32 = 0xFFFFFFFF
#: the widened domain the self-test probes: AF_UNIX in the low word, a
#: non-zero high word. `SCMP_CMP_EQ` alone let it start a real AF_UNIX socket
WIDENED_AF_UNIX = 0x100000001
PR_SET_NO_NEW_PRIVS = 38
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


def install_socket_filter() -> int:
    """Refuse `socket(AF_UNIX)` to this process and everything it starts.

    libseccomp resolves the syscall number for the running architecture; the
    rule only touches the `socket` syscall, `socketpair` is left alone. The
    comparison masks the domain to the kernel's `int` width: an equality test
    on the full 64-bit register was bypassed by `socket(0x100000001)` (B3 of
    the R2 reading), which the kernel still reads as AF_UNIX. The filter
    cannot be removed (seccomp filters stack, and this process sets
    no_new_privs): a payload that reaches here can never reach the job's user
    manager, whatever environment it reconstructs. Returns the resolved
    syscall number, for the caller's own probes. Raises OSError when the
    filter cannot be installed, so the caller fails closed."""
    import ctypes

    class ArgCmp(ctypes.Structure):
        _fields_ = [("arg", ctypes.c_uint), ("op", ctypes.c_int),
                    ("datum_a", ctypes.c_uint64), ("datum_b", ctypes.c_uint64)]

    try:
        lib = ctypes.CDLL(SECCOMP_SO, use_errno=True)
    except OSError as exc:
        raise OSError(errno.ENOSYS, "cannot load %s: %s" % (SECCOMP_SO, exc))
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_rule_add_array.restype = ctypes.c_int
    lib.seccomp_rule_add_array.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                           ctypes.c_int, ctypes.c_uint,
                                           ctypes.POINTER(ArgCmp)]
    lib.seccomp_load.restype = ctypes.c_int
    lib.seccomp_load.argtypes = [ctypes.c_void_p]

    ctx = lib.seccomp_init(SCMP_ACT_ALLOW)
    if not ctx:
        raise OSError(errno.ENOMEM, "seccomp_init failed")
    try:
        nr = lib.seccomp_syscall_resolve_name(b"socket")
        if nr < 0:
            raise OSError(errno.ENOSYS, "libseccomp does not know socket(2)")
        # datum_a is the mask, datum_b the value (seccomp.h.in, scmp_arg_cmp):
        # (arg & 0xffffffff) == AF_UNIX is what the kernel's int domain means
        rule = ArgCmp(0, SCMP_CMP_MASKED_EQ, ARG_MASK32, AF_UNIX)
        rc = lib.seccomp_rule_add_array(ctx, SCMP_ACT_ERRNO, nr, 1,
                                        ctypes.byref(rule))
        if rc != 0:
            raise OSError(-rc, "seccomp_rule_add_array: %d" % rc)
        if ctypes.CDLL(None, use_errno=True).prctl(PR_SET_NO_NEW_PRIVS, 1,
                                                   0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "prctl(PR_SET_NO_NEW_PRIVS)")
        rc = lib.seccomp_load(ctx)
        if rc != 0:
            raise OSError(-rc, "seccomp_load: %d" % rc)
    finally:
        lib.seccomp_release(ctx)
    return nr


def filter_selftest() -> int:
    """What the payload will really get, in a child that dies with it: AF_UNIX
    refused (plain and widened), socketpair (asyncio, multiprocessing)
    allowed. 0 when the filter works, 1 otherwise: `check` then refuses the
    job instead of running the client's code unconfined."""
    pid = os.fork()
    if pid < 0:
        return 1
    if pid == 0:
        code = 1
        try:
            nr = install_socket_filter()
            import ctypes
            import socket
            try:
                socket.socket(socket.AF_UNIX)
            except OSError as exc:
                if exc.errno != errno.EAFNOSUPPORT:
                    raise
            else:
                raise OSError(0, "AF_UNIX still allowed")
            # B3 of the R2 reading: the kernel reads `domain` as an int, so a
            # raw syscall with a non-zero high word is still AF_UNIX. The
            # masked rule must refuse it; an unmasked one returned a live fd.
            # Only the fd matters: on an architecture where socket(2) is
            # multiplexed the raw probe cannot build a socket either way.
            libc = ctypes.CDLL(None, use_errno=True)
            libc.syscall.restype = ctypes.c_long
            fd = libc.syscall(ctypes.c_long(nr),
                              ctypes.c_ulonglong(WIDENED_AF_UNIX),
                              ctypes.c_ulonglong(socket.SOCK_STREAM),
                              ctypes.c_ulonglong(0))
            if fd >= 0:
                os.close(fd)
                raise OSError(0, "a widened AF_UNIX domain still allowed")
            left, right = socket.socketpair()
            left.close()
            right.close()
        except BaseException:
            code = 1
        else:
            code = 0
        os._exit(code)
    _, status = os.waitpid(pid, 0)
    return 0 if os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0 else 1


def cmd_exec(args: list[str]) -> int:
    """In the scope, just before the client's code: strip the manager's
    address, install the filter, exec. Fail closed."""
    if len(args) < 2 or args[0] != "--":
        return die("usage: exec -- <command...>")
    for key in BUS_ENV:
        os.environ.pop(key, None)
    try:
        install_socket_filter()
    except OSError as exc:
        return die("cannot confine the payload (%s): refusing to run it" % exc, 78)
    try:
        os.execvp(args[1], args[1:])
    except OSError as exc:
        return die("cannot exec %s: %s" % (args[1], exc), 70)
    return 70  # unreachable


def cmd_check() -> int:
    run_dir = os.environ.get("XDG_RUNTIME_DIR", "")
    if not os.path.isdir(run_dir) or not os.path.exists(os.path.join(run_dir, "bus")):
        return die("no systemd user manager for uid %d (loginctl enable-linger?)"
                   % os.getuid())
    if systemctl("is-system-running").returncode not in (0, 1):
        # 1 = degraded, still usable; anything else means no manager at all
        return die("systemctl --user cannot reach the user manager")
    if filter_selftest() != 0:
        return die("the payload's seccomp filter cannot be installed "
                   "(libseccomp missing?): refusing the job")
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
        # the client's own code: systemd-run needs the manager's address in
        # *its* environment, the payload does not get it, and it runs under
        # `exec`'s filter, so it cannot ask the manager for a unit outside
        # the job's slice either (B3 of the R1 review of #354)
        argv = [sys.executable, "-I", os.path.abspath(__file__), "exec",
                "--"] + argv
    cmd = [SYSTEMD_RUN, "--user", "--scope", "--quiet", "--collect",
           "--slice", slice_name, "--unit", unit, "--"] + argv
    os.execv(SYSTEMD_RUN, cmd)
    return 70  # unreachable


VERBS = {"check": cmd_check, "open": cmd_open, "kill": cmd_kill,
         "close": cmd_close, "clean": cmd_clean, "run": cmd_run,
         "exec": cmd_exec}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if os.geteuid() == 0:
        # the sudoers rule must target the job user: a rule that would run
        # this as root is a misconfiguration, and this script refuses it
        return die("refusing to run as root: the sudoers rule must use (ameesh-job)")
    if not argv or argv[0] not in VERBS:
        return die("allowed verbs: %s" % ", ".join(sorted(VERBS)))
    if argv[0] != "exec":
        # `exec` runs inside the scope, on the payload's own environment:
        # putting the manager's address back is the one thing it must not do
        runtime_env()
    try:
        return VERBS[argv[0]](argv[1:])
    except ValueError as exc:
        return die(str(exc))
    except OSError as exc:
        return die(str(exc), 70)


if __name__ == "__main__":
    sys.exit(main())
