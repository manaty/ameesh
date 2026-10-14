# SPDX-License-Identifier: AGPL-3.0-only
"""L159i: render the rootless Podman launch recipe without executing it.

The host guard owns state/, the executor owns ack/. This module never reads
an enrolment code, starts a container or changes files on the lending VM.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import PurePosixPath
import re
import shlex
from urllib.parse import urlsplit


SCHEMA = "ameesh-vm-executor-recipe/1"
FIELDS = {"schema", "image", "server_url", "mesh", "host", "volume",
          "state_dir", "ack_dir", "enroll_dir", "cpus", "memory_mb", "tmp_mb"}


def command(recipe: dict) -> list[str]:
    """Validate a closed recipe and return argv, never shell-expanded text.

    Paths must be distinct absolute directories; their ownership, existence,
    mount boundaries and absence of symlinks are deployment checks.
    """
    if not isinstance(recipe, dict) or set(recipe) != FIELDS:
        raise ValueError("recipe fields must match ameesh-vm-executor-recipe/1")
    if recipe["schema"] != SCHEMA:
        raise ValueError("unknown recipe schema")
    image = recipe["image"]
    if not isinstance(image, str) or not re.fullmatch(
            r"[a-zA-Z0-9][a-zA-Z0-9./:_-]*/ameesh-executor@sha256:[0-9a-f]{64}", image):
        raise ValueError("image must be ameesh-executor pinned by sha256 digest")
    url = recipe["server_url"]
    if not isinstance(url, str):
        raise ValueError("server_url must be an HTTPS origin on port 443")
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.port not in (None, 443)
            or parsed.path or parsed.query or parsed.fragment
            or any(c.isspace() for c in url) or "\\" in url):
        raise ValueError("server_url must be an HTTPS origin on port 443")
    for key in ("mesh", "host"):
        if not isinstance(recipe[key], str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]*", recipe[key]):
            raise ValueError("%s must be a canon identifier" % key)
    paths = []
    for key in ("volume", "state_dir", "ack_dir", "enroll_dir"):
        value = recipe[key]
        if (not isinstance(value, str) or not value.startswith("/") or value == "/"
                or any(c in value for c in (":", ",", "\n", "\r", "\x00"))
                or ".." in value.split("/") or str(PurePosixPath(value)) != value):
            raise ValueError("%s must be a canonical absolute directory" % key)
        path = PurePosixPath(value)
        if any(path == other or path in other.parents or other in path.parents for other in paths):
            raise ValueError("mount directories must not overlap")
        paths.append(path)
    cpus = recipe["cpus"]
    if (isinstance(cpus, bool) or not isinstance(cpus, (int, float))
            or not math.isfinite(cpus) or cpus <= 0):
        raise ValueError("cpus must be positive and finite")
    for key in ("memory_mb", "tmp_mb"):
        if type(recipe[key]) is not int or recipe[key] <= 0:
            raise ValueError("%s must be a positive integer" % key)
    if recipe["tmp_mb"] >= recipe["memory_mb"]:
        raise ValueError("tmp_mb must be smaller than memory_mb")

    argv = ["podman", "--cgroup-manager=cgroupfs", "run", "--rm", "--pull=never",
            "--name=ameesh-vm-executor", "--userns=keep-id:uid=10001,gid=10001",
            "--user=10001:10001", "--read-only", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", "--http-proxy=false",
            "--stop-signal=SIGTERM", "--stop-timeout=100",
            "--env=AMEESH_HOST_GATE_LENDING=1",
            "--cpus=%s" % cpus, "--memory=%sm" % recipe["memory_mb"],
            "--memory-swap=%sm" % recipe["memory_mb"],
            "--tmpfs=/tmp:rw,nosuid,nodev,size=%sm,mode=1777" % recipe["tmp_mb"]]
    for key, target, mode in (
            ("volume", "/var/lib/ameesh-exec", "rw"),
            ("state_dir", "/run/ameesh-gate/state", "ro"),
            ("ack_dir", "/run/ameesh-gate/ack", "rw"),
            ("enroll_dir", "/run/ameesh-enroll", "ro")):
        argv += ["--volume", "%s:%s:%s" % (recipe[key], target, mode)]
    for key, env in (("server_url", "AMEESH_EXEC_URL"), ("mesh", "AMEESH_EXEC_MESH"),
                     ("host", "AMEESH_EXEC_HOST")):
        argv += ["--env", "%s=%s" % (env, recipe[key])]
    return argv + [image, "run"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recipe", help="JSON recipe file (contains no enrolment code)")
    args = parser.parse_args()
    try:
        with open(args.recipe, encoding="utf-8") as fh:
            argv = command(json.load(fh))
    except (OSError, ValueError) as exc:
        parser.exit(2, "invalid VM recipe: %s\n" % exc)
    print(shlex.join(argv))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
