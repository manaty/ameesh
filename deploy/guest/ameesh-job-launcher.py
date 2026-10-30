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

Every phase that is not a profile hook (`before`, `after`) runs under the
internal `exec` verb, inside the scope. `exec` strips the manager's address,
installs a seccomp filter, and only then applies the payload's environment,
which travelled as inert JSON chunks -- never as live variables of
`systemd-run` or of the wrapper, both of which load ld.so before any filter
(B1 of the R2 review of #354: `LD_AUDIT=./x.so` from the client's `--env`
ran a constructor inside `/usr/bin/systemd-run` and inside the wrapper, with
the manager's address in hand). The filter denies `socket(AF_UNIX)` (masked
to the kernel's 32-bit `int`, so a widened domain is refused too),
`io_uring_setup/enter/register` (which build an AF_UNIX socket without
`socket(2)`, B2), and `ptrace`, `process_vm_readv`, `process_vm_writev` and
`pidfd_getfd` (which would inject into a same-UID process that still holds
the bus, B4). `socketpair(2)` stays allowed (asyncio, multiprocessing), and
the filter cannot be removed: it is inherited across fork and exec, so the
payload cannot reach the manager, not even through this launcher. The
profile hooks are root-owned install-side code (rootless Podman needs the
manager) and keep it; they never receive the client's `--env` and the
launcher strips the loader variables from what they do receive.
"""
import errno
from datetime import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import time

#: root-owned, written by install.sh (the launcher takes no --config)
CONFIG = "/etc/ameesh-guest/offload.json"
SYSTEMD_RUN = "/usr/bin/systemd-run"
SYSTEMCTL = "/usr/bin/systemctl"
JOB_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")
PHASE_RE = re.compile(r"^[a-z][a-z0-9-]{0,15}$")
#: the phases whose code is the VM's own (the root-owned profiles): they keep
#: the job's user manager (rootless Podman needs it). Anything else -- the
#: client's code, and every name a client could invent -- goes through `exec`,
#: under the filter (N1 of the #354 R2 review: a whitelist, not the old
#: blacklist of client phases that left an unknown phase with the manager).
HOOK_PHASES = ("before", "after")
#: ld.so and libc read these before any `main`: the launcher never hands them
#: to `systemd-run` nor to the `exec` wrapper (B1 of the #354 R2 review). The
#: payload's own environment is applied after the filter, in `exec`.
LOADER_ENV_PREFIXES = ("LD_", "MALLOC_")
LOADER_ENV_KEYS = frozenset(("GCONV_PATH", "GLIBC_TUNABLES", "LOCPATH",
                             "HOSTALIASES", "NLSPATH", "RES_OPTIONS"))
#: the payload's environment travels as inert JSON chunks, out of the live
#: environment of any process that has the manager's address. 60 kB per chunk
#: stays under Linux' 128 kB limit for one environment string.
ENV_CHUNK = "AMEESH_JOB_ENV_"
ENV_COUNT = "AMEESH_JOB_ENV_COUNT"
ENV_CHUNK_BYTES = 60000
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
SCMP_ACT_ERRNO = 0x00050000
SCMP_CMP_MASKED_EQ = 7
#: the width of the kernel's `int domain`, i.e. of every 32-bit `socket(2)`
#: argument: anything above it is invisible to the syscall
ARG_MASK32 = 0xFFFFFFFF
#: the widened domain the self-test probes: AF_UNIX in the low word, a
#: non-zero high word. `SCMP_CMP_EQ` alone let it start a real AF_UNIX socket
WIDENED_AF_UNIX = 0x100000001
PR_SET_NO_NEW_PRIVS = 38
#: every syscall the filter must be able to name: a libseccomp or an
#: architecture that does not know one of them refuses the job (fail closed)
FILTER_SYSCALLS = ("socket", "io_uring_setup", "io_uring_enter",
                   "io_uring_register", "ptrace", "process_vm_readv",
                   "process_vm_writev", "pidfd_getfd")
#: io_uring builds an AF_UNIX socket without `socket(2)` (B2 of the R2
#: review); ptrace and its siblings inject into a same-UID process that still
#: holds the bus, e.g. the user manager or an `after` hook (B4)
IO_URING_SYSCALLS = ("io_uring_setup", "io_uring_enter", "io_uring_register")
INJECTION_SYSCALLS = ("ptrace", "process_vm_readv", "process_vm_writev",
                      "pidfd_getfd")
#: Yama: >= 1 confines what a same-UID process may trace (defence in depth
#: under the filter for the bus-holding processes, B4)
YAMA_SCOPE = "/proc/sys/kernel/yama/ptrace_scope"
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


def syscall_nr(name: str) -> int:
    """The running architecture's number for `name`, or -1: what the self-test
    probes with, the same libseccomp the filter is built from."""
    import ctypes
    try:
        lib = ctypes.CDLL(SECCOMP_SO)
    except OSError:
        return -1
    lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    return int(lib.seccomp_syscall_resolve_name(name.encode()))


def strip_loader(env: dict) -> dict:
    """Drop what ld.so and libc read before `main`: a hook runs in the job's
    scope with the manager's address, and a client-planted `LD_AUDIT` would
    load its library there before any filter (B1 of the #354 R2 review)."""
    return {k: v for k, v in env.items()
            if not k.startswith(LOADER_ENV_PREFIXES) and k not in LOADER_ENV_KEYS}


def process_env() -> dict:
    """What `systemd-run` and the `exec` wrapper may see: the manager's
    address and a PATH, never a client variable (B1 of the #354 R2 review)."""
    return {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "XDG_RUNTIME_DIR": os.environ["XDG_RUNTIME_DIR"],
            "DBUS_SESSION_BUS_ADDRESS": os.environ["DBUS_SESSION_BUS_ADDRESS"]}


def pack_env(env: dict, into: dict) -> None:
    """The payload's environment as inert JSON chunks, so it is nowhere in the
    live environment of `systemd-run` or of the wrapper (B1)."""
    raw = json.dumps(env, ensure_ascii=True)
    count = -(-len(raw) // ENV_CHUNK_BYTES)  # ceil, and 0 for an empty env
    into[ENV_COUNT] = str(count)
    for i in range(count):
        into["%s%d" % (ENV_CHUNK, i)] = raw[i * ENV_CHUNK_BYTES:
                                            (i + 1) * ENV_CHUNK_BYTES]


def take_env(env: dict) -> dict:
    """Read back the payload's environment and remove its chunks. Raises
    ValueError on anything malformed: `exec` then refuses to run (B1)."""
    try:
        count = int(env.pop(ENV_COUNT, "0"))
    except ValueError:
        raise ValueError("bad %s" % ENV_COUNT) from None
    if not 0 <= count <= 64:
        raise ValueError("bad %s: %d" % (ENV_COUNT, count))
    parts = []
    for i in range(count):
        key = "%s%d" % (ENV_CHUNK, i)
        value = env.pop(key, None)
        if value is None:
            raise ValueError("missing %s" % key)
        parts.append(value)
    if not parts:
        return {}
    data = json.loads("".join(parts))
    if not isinstance(data, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
        raise ValueError("the payload environment is not an object of strings")
    return data


def yama_scope() -> int | None:
    """`kernel.yama.ptrace_scope`, or None when the host has no Yama."""
    try:
        with open(YAMA_SCOPE, encoding="utf-8") as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def install_socket_filter() -> int:
    """Confine this process and everything it starts: what the payload must
    not do is refused by the kernel before its first instruction.

    `socket(AF_UNIX)` is refused with the domain masked to the kernel's
    `int` width (an equality test on the full 64-bit register was bypassed by
    `socket(0x100000001)`, B3 of the R2 reading); `io_uring_setup/enter/
    register` are refused because io_uring builds an AF_UNIX socket without
    `socket(2)` (B2); `ptrace`, `process_vm_readv`, `process_vm_writev` and
    `pidfd_getfd` are refused because they would inject into a same-UID
    process that still holds the bus (B4). `socketpair` is left alone
    (asyncio, multiprocessing). The filter cannot be removed (filters stack,
    and this process sets no_new_privs): a payload that reaches here can
    never reach the job's user manager, whatever environment it reconstructs.
    Returns the resolved `socket` number, for the caller's own probes. Raises
    OSError when a syscall is unknown or the filter cannot be installed, so
    the caller fails closed."""
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

    def resolve(name: str) -> int:
        number = lib.seccomp_syscall_resolve_name(name.encode())
        if number < 0:
            # older libseccomp, or an architecture without it: the job is
            # refused (check -> the client falls back) rather than run with a
            # confinement that misses a documented bypass
            raise OSError(errno.ENOSYS, "libseccomp does not know %s(2)" % name)
        return number

    def deny(ctx, name: str, err: int) -> None:
        rc = lib.seccomp_rule_add_array(ctx, SCMP_ACT_ERRNO | (err & 0xFFFF),
                                        resolve(name), 0, None)
        if rc != 0:
            raise OSError(-rc, "seccomp_rule_add_array(%s): %d" % (name, rc))

    ctx = lib.seccomp_init(SCMP_ACT_ALLOW)
    if not ctx:
        raise OSError(errno.ENOMEM, "seccomp_init failed")
    try:
        # datum_a is the mask, datum_b the value (seccomp.h.in, scmp_arg_cmp):
        # (arg & 0xffffffff) == AF_UNIX is what the kernel's int domain means
        nr = resolve("socket")
        rule = ArgCmp(0, SCMP_CMP_MASKED_EQ, ARG_MASK32, AF_UNIX)
        rc = lib.seccomp_rule_add_array(ctx, SCMP_ACT_ERRNO
                                        | (errno.EAFNOSUPPORT & 0xFFFF), nr, 1,
                                        ctypes.byref(rule))
        if rc != 0:
            raise OSError(-rc, "seccomp_rule_add_array(socket): %d" % rc)
        for name in IO_URING_SYSCALLS:
            deny(ctx, name, errno.ENOSYS)
        for name in INJECTION_SYSCALLS:
            deny(ctx, name, errno.EPERM)
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
    refused (plain and widened), io_uring refused (B2), ptrace refused (B4),
    socketpair (asyncio, multiprocessing) allowed. 0 when the filter works, 1
    otherwise: `check` then refuses the job instead of running the client's
    code unconfined."""
    pid = os.fork()
    if pid < 0:
        return 1
    if pid == 0:
        code = 1
        try:
            nr = install_socket_filter()
            import ctypes
            import socket
            libc = ctypes.CDLL(None, use_errno=True)
            libc.syscall.restype = ctypes.c_long
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
            fd = libc.syscall(ctypes.c_long(nr),
                              ctypes.c_ulonglong(WIDENED_AF_UNIX),
                              ctypes.c_ulonglong(socket.SOCK_STREAM),
                              ctypes.c_ulonglong(0))
            if fd >= 0:
                os.close(fd)
                raise OSError(0, "a widened AF_UNIX domain still allowed")
            # B2: io_uring used to build an AF_UNIX socket without socket(2)
            uring = syscall_nr("io_uring_setup")
            if uring < 0:
                raise OSError(0, "libseccomp does not know io_uring_setup")
            ctypes.set_errno(0)
            fd = libc.syscall(ctypes.c_long(uring), ctypes.c_uint(8),
                              ctypes.create_string_buffer(256))
            if fd >= 0:
                os.close(fd)
                raise OSError(0, "io_uring_setup still allowed")
            if ctypes.get_errno() != errno.ENOSYS:
                raise OSError(0, "io_uring_setup refused with errno %d"
                              % ctypes.get_errno())
            # B4: ptrace would inject into a same-UID process that holds the
            # bus; an invalid request cannot answer EPERM on its own
            ptrace = syscall_nr("ptrace")
            if ptrace < 0:
                raise OSError(0, "libseccomp does not know ptrace")
            ctypes.set_errno(0)
            rc = libc.syscall(ctypes.c_long(ptrace), ctypes.c_long(0xdeadbeef),
                              ctypes.c_void_p(0), ctypes.c_void_p(0),
                              ctypes.c_void_p(0))
            if rc != -1 or ctypes.get_errno() != errno.EPERM:
                raise OSError(0, "ptrace still reachable")
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
    """In the scope, just before the client's code: read the payload's
    environment out of its chunks, strip the manager's address, install the
    filter, then apply that environment and exec. Fail closed."""
    if len(args) < 2 or args[0] != "--":
        return die("usage: exec -- <command...>")
    try:
        payload = take_env(os.environ)
    except ValueError as exc:
        return die("cannot read the payload environment (%s): refusing to run it"
                   % exc, 78)
    for key in BUS_ENV:
        os.environ.pop(key, None)
    try:
        install_socket_filter()
    except OSError as exc:
        return die("cannot confine the payload (%s): refusing to run it" % exc, 78)
    # B1 of the #354 R2 review: the client's own environment (LD_AUDIT…) only
    # touches this process now, with the filter installed and impossible to
    # remove
    os.environ.update(payload)
    try:
        os.execvp(args[1], args[1:])
    except OSError as exc:
        return die("cannot exec %s: %s" % (args[1], exc), 70)
    return 70  # unreachable


def cmd_check(args: list[str]) -> int:
    if args:
        return die("check takes no argument")
    run_dir = os.environ.get("XDG_RUNTIME_DIR", "")
    if not os.path.isdir(run_dir) or not os.path.exists(os.path.join(run_dir, "bus")):
        return die("no systemd user manager for uid %d (loginctl enable-linger?)"
                   % os.getuid())
    if systemctl("is-system-running").returncode not in (0, 1):
        # 1 = degraded, still usable; anything else means no manager at all
        return die("systemctl --user cannot reach the user manager")
    scope = yama_scope()
    if scope is None or scope < 1:
        # B4 of the #354 R2 review: the payload shares the job user's UID with
        # the manager and the hooks; Yama is the second lock under the filter.
        # None means the kernel has no Yama, or it is unreadable: install.sh
        # --check and the README refuse that kernel, and the filter does not
        # block open(/proc/<pid>/mem), so the launcher must refuse it too
        return die("kernel.yama.ptrace_scope=%r, expected >= 1 "
                   "(re-run deploy/guest/install.sh)" % scope)
    if filter_selftest() != 0:
        return die("the payload's seccomp filter cannot be installed "
                   "(libseccomp missing or too old?): refusing the job")
    sys.stdout.write("ok\n")
    return 0


def cmd_open(args: list[str]) -> int:
    job_id = job_arg(args)
    cfg = load_config()
    prune_job_volumes()
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
    # The after hook may never have run (cancelled/killed job). Remove its
    # container too, including legacy PGDATA volumes. A stuck Podman must
    # not prevent dropping the slice's runtime caps.
    podman_cleanup("rm", "-f", "-v", "-t", "2", "ameesh-job-%s-pg" % job_id)
    prune_job_volumes()
    systemctl("revert", slice_name)
    return 0


def podman_cleanup(*args: str) -> str | None:
    """Best effort, bounded cleanup as the dedicated job user, never root."""
    try:
        result = subprocess.run(["/usr/bin/podman", *args], capture_output=True,
                                text=True, check=False, timeout=30)
        if result.returncode:
            die("podman cleanup failed: %s" % result.stderr.strip())
            return None
        return result.stdout
    except (OSError, subprocess.TimeoutExpired) as exc:
        die("podman cleanup unavailable: %s" % exc)


def prune_job_volumes() -> None:
    # Older Podman prune has no anonymous filter and defaults to ALL unused
    # volumes. Inspect explicit metadata instead; missing metadata is spared.
    raw = podman_cleanup("volume", "inspect", "--all")
    if raw is None:
        return
    try:
        volumes = json.loads(raw)
        if not isinstance(volumes, list):
            raise ValueError("volume inventory is not a list")
        stale = []
        cutoff = time.time() - 3600
        for volume in volumes:
            if not isinstance(volume, dict) or volume.get("Anonymous") is not True:
                continue
            name = volume.get("Name", "")
            if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", name):
                continue
            try:
                created = datetime.fromisoformat(volume["CreatedAt"].replace("Z", "+00:00"))
                if created.tzinfo is not None and created.timestamp() < cutoff:
                    stale.append(name)
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
        if stale:
            # Never force: Podman checks references at deletion, including a
            # container created since inspect. Even stopped containers retain
            # their volumes; a concurrent live job cannot lose its data.
            removed = podman_cleanup("volume", "rm", *stale)
            if removed:
                # The inventory proves anonymity/age, Podman proves absence
                # of references at deletion. Log only names actually removed,
                # never container environment or the volume's private data.
                names = [name for name in removed.splitlines() if name in stale]
                if names:
                    sys.stderr.write("ameesh-job-launcher: removed %d stale anonymous "
                                     "volume(s): %s\n" % (len(names), ", ".join(names)))
    except (ValueError, TypeError) as exc:
        die("podman volume inventory unreadable: %s" % exc)


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
    manager = process_env()
    slice_name, unit = unit_names(job_id)
    if phase != "command":
        unit += "-" + phase
    if phase in HOOK_PHASES:
        # the VM's own code (root-owned profile): it keeps the manager
        # (rootless Podman needs it), but never a loader variable, and the
        # client no longer sends its `--env` here (B1 of the #354 R2 review)
        hook_env = strip_loader(env)
        hook_env.update(manager)
        os.environ.clear()
        os.environ.update(hook_env)
    else:
        # the client's own code, whatever the phase name (N1 of the #354 R2
        # review): `systemd-run` and the wrapper only get the manager's
        # address; the payload's environment travels as inert chunks and is
        # applied by `exec` once the filter is installed
        os.environ.clear()
        os.environ.update(manager)
        pack_env({k: v for k, v in env.items() if k not in BUS_ENV}, os.environ)
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
