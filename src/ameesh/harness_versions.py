# SPDX-License-Identifier: AGPL-3.0-only
"""Harness versions per host (lot L120, #119).

ameesh never updates a harness: it runs the `claude`, `codex`, `dsh` it
finds (L106). Each runner reads the version of the harnesses it serves
(`<binary> --version`, the binary resolved exactly as for a turn) at start
and every `PROBE_INTERVAL_S`, and records it per host (`harness_versions`,
0065). `ameesh harness versions` shows, per harness, the version of each
host, whether hosts differ, and with `--latest` the latest release and the
hosts behind it.

Read only: nothing here installs or updates a harness.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.parse
import urllib.request

from . import adapters, storage

#: a runner reads the versions again after this delay
PROBE_INTERVAL_S = 6 * 3600.0
#: `--version` of a harness answers in well under a second; a node start
#: on a loaded host can take a few
PROBE_TIMEOUT_S = 20.0
#: first line kept from the answer
RAW_MAX = 200
#: npm package of each harness, for the latest release (`--latest`)
NPM_PACKAGES = {"claude": "@anthropic-ai/claude-code", "codex": "@openai/codex"}
NPM_REGISTRY = "https://registry.npmjs.org"
LATEST_TIMEOUT_S = 5.0

_VERSION_RE = re.compile(r"(?<![\w.])v?(\d+\.\d+(?:\.\d+)?(?:-[0-9A-Za-z.]+)?)")


def parse_version(text: str | None) -> str | None:
    """The first version number in `text` (`2.1.286 (Claude Code)`,
    `codex-cli 0.159.3`, `v1.2.0`), or None."""
    match = _VERSION_RE.search(text or "")
    return match.group(1) if match else None


def version_key(version: str | None) -> tuple:
    """Sort key of a version: numbers first, a pre-release before its
    release; None before everything."""
    if not version:
        return (-1,)
    base, _, pre = version.partition("-")
    numbers = tuple(int(part) for part in base.split(".") if part.isdigit())
    return numbers + ((1,) if not pre else (0, pre))


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()[:RAW_MAX]
    return ""


def probe(harness: str, *, run=subprocess.run) -> dict:
    """Read the version of `harness` on this host: `harness`, `path`,
    `version` (None if unreadable), `raw` (first line of the answer),
    `error` (None, or why nothing was read)."""
    out = {"harness": harness, "path": None, "version": None, "raw": None, "error": None}
    try:
        adapter = adapters.adapter_for(harness)
    except adapters.HarnessMissing as exc:
        out["error"] = "not found: %s" % exc
        return out
    resolution = adapter.resolution
    path = resolution.path if resolution else adapter.binary
    out["path"] = path
    command = [path, "--version"]
    env = dict(os.environ)
    if resolution is not None:
        if resolution.interpreter_path:
            command = [resolution.interpreter_path, path, "--version"]
        if resolution.path_prepend:
            env["PATH"] = os.pathsep.join(list(resolution.path_prepend)
                                          + [env.get("PATH", "")])
    try:
        proc = run(command, capture_output=True, text=True, timeout=PROBE_TIMEOUT_S,
                   stdin=subprocess.DEVNULL, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        out["error"] = type(exc).__name__
        return out
    raw = _first_line(proc.stdout) or _first_line(proc.stderr)
    out["raw"] = raw or None
    out["version"] = parse_version(raw)
    if out["version"] is None:
        out["error"] = "exit %s, no version" % proc.returncode
    return out


def probe_and_record(db, host: str, harness_ids, *, run=subprocess.run) -> list[dict]:
    """Probe each harness and record what was read; a harness that is not
    found is not recorded (`ameesh doctor --harness` says it)."""
    found = []
    for harness in sorted(set(harness_ids)):
        item = probe(harness, run=run)
        if item["path"] is None:
            found.append(item)
            continue
        storage.of(db).hosts.record_harness_version(
            host, harness, path=item["path"], version=item["version"], raw=item["raw"])
        found.append(item)
    return found


def latest(harness: str, *, fetch=None) -> str | None:
    """The latest release of `harness` (npm `latest` tag), or None (no known
    package, no network)."""
    package = NPM_PACKAGES.get(harness)
    if not package:
        return None
    url = "%s/%s/latest" % (NPM_REGISTRY, urllib.parse.quote(package, safe="@/"))
    try:
        if fetch is not None:
            body = fetch(url)
        else:
            with urllib.request.urlopen(url, timeout=LATEST_TIMEOUT_S) as resp:  # noqa: S310
                body = resp.read().decode("utf-8")
        return parse_version(str(json.loads(body).get("version") or ""))
    except Exception:  # no network, unexpected answer: unknown, never fatal
        return None


def matrix(rows: list[dict], latest_versions: dict | None = None, *,
           now: float | None = None) -> list[dict]:
    """Per harness: `hosts` ({host: {version, path, raw, age_s}}), `newest`,
    `differs` (the hosts do not all run the same version), `latest` and
    `behind` (hosts older than the latest release, or than the newest host
    without `latest`)."""
    now = time.time() if now is None else now
    by_harness: dict[str, dict] = {}
    for row in rows:
        by_harness.setdefault(row["harness"], {})[row["host"]] = {
            "version": row.get("version"), "path": row.get("path"), "raw": row.get("raw"),
            "age_s": (round(now - float(row["observed_ts"]))
                      if row.get("observed_ts") is not None else None)}
    out = []
    for harness, hosts in sorted(by_harness.items()):
        versions = {h["version"] for h in hosts.values()}
        known = [v for v in versions if v]
        newest = max(known, key=version_key) if known else None
        last = (latest_versions or {}).get(harness)
        reference = last or newest
        behind = sorted(host for host, h in hosts.items()
                        if reference and version_key(h["version"]) < version_key(reference))
        out.append({"harness": harness, "hosts": hosts, "newest": newest,
                    "differs": len(versions) > 1, "latest": last, "behind": behind})
    return out
