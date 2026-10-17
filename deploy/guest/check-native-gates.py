#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Read-only check of live native runners for the guest file gate (#501).

Prints only PID, account and gate selection, never runner credentials.
Run as root so the native services' environments can be read.
"""
import json
import os
from pathlib import Path
import pwd
import sys


def runner_args(args):
    return ("ameesh.runner" in args or any(Path(a).name == "agent-runner" for a in args)
            or any(Path(a).name == "ameesh" and i + 1 < len(args) and args[i+1] == "run"
                   for i, a in enumerate(args)))


def check(proc_root=Path("/proc"), emit=print):
    failed, count = False, 0
    for proc in sorted(proc_root.iterdir()):
        if not proc.name.isdigit():
            continue
        try:
            args = proc.joinpath("cmdline").read_bytes().decode().split("\0")
            if not runner_args(args):
                continue
            uid = int(next(line.split()[2] for line in proc.joinpath("status").read_text().splitlines()
                           if line.startswith("Uid:")))
            account = pwd.getpwuid(uid)
            env = dict(v.split("=", 1) for v in proc.joinpath("environ").read_bytes().decode().split("\0")
                       if "=" in v)
            ident = env.get("AMEESH_RUNNER_ID", "")
            if "--runner-id" in args:
                ident = args[args.index("--runner-id") + 1]
            if account.pw_name == "ameesh-guest" or ident.startswith("guest-"):
                continue
            count += 1
            home = env.get("HOME", account.pw_dir)
            config = env.get("AMEESH_CONFIG") or env.get("AGENT_MESH_CONFIG")
            if config:
                config_path = Path(config.replace("~/", home + "/", 1))
            else:
                config_path = Path(home) / ".config/ameesh/config.json"
                if not config_path.exists():
                    config_path = Path(home) / ".config/agent-mesh/config.json"
            raw = json.loads(config_path.read_text()) if config_path.exists() else {}
            if not isinstance(raw, dict):
                raise ValueError("config must be an object")
            kind = str(env.get("AMEESH_HOST_GATE", raw.get("host_gate", "")) or "").strip().lower()
            path = str(env.get("AMEESH_HOST_GATE_PATH", raw.get("host_gate_path")) or "/run/ameesh-gate/state/state.json")
            ack = str(env.get("AMEESH_HOST_GATE_ACK", raw.get("host_gate_ack")) or "/run/ameesh-gate/ack/ack.json")
            guest_file = kind == "file" and any(
                p == "/run/ameesh-gate" or p.startswith("/run/ameesh-gate/") for p in (path, ack))
            # A running older release may already hold a part despite having
            # no explicit override. Detect that actual use as well.
            for fd in proc.joinpath("fd").iterdir():
                try:
                    target = os.readlink(fd)
                except FileNotFoundError:
                    continue
                guest_file |= target.startswith("/run/ameesh-gate/ack/parts/")
            failed |= guest_file
            emit("%s native runner pid %s (%s): %s" % (
                "FAIL" if guest_file else "ok  ", proc.name, account.pw_name,
                "takes guest file gate" if guest_file else "guest file gate excluded"))
        except (FileNotFoundError, ProcessLookupError):
            continue  # process exited during the scan
        except (OSError, ValueError, KeyError, StopIteration, IndexError) as exc:
            failed = True
            # Do not print the exception text: malformed config may contain secrets.
            emit("FAIL runner pid %s: cannot verify gate (%s)" % (proc.name, type(exc).__name__))
    if not count:
        emit("ok   no live native runner to check; repeat after native restart")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(check())
