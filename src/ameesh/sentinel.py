# SPDX-License-Identifier: AGPL-3.0-only
"""L226a — credential sentinel: the health of every credential agents use on
this host, probed every 10 minutes (`ameesh accounts check`, systemd timer
`ameesh-credentials.timer`).

Each probe reports to `credential_health` (migration 0060, owned by L226b)
through `storage.of(db).credentials.report(..., source='sentinel')`:

* `claude`, per declared account (or the default config dir): the OAuth file
  `.credentials.json` has a refresh token that has not expired (the access
  token refreshes itself); forecast expiry = `refreshTokenExpiresAt`;
* `codex`, per declared account: `auth.json` has a refresh token or an API key;
* `gcloud`: `gcloud auth print-access-token` succeeds; forecast expiry = last
  interactive login (mtime of `credentials.db`) + the Workspace session length
  (`credential_sentinel.gcloud_session_hours`);
* `gh`: `gh auth status` succeeds;
* `scw`: `scw account project list` succeeds.

A harness without its binary, or without any credential file and no declared
account, is not used on this host: it is skipped. A probe that cannot conclude
(timeout, unreadable file) reports `unknown`, never a verdict.

The `reason` of a failing credential carries the exact command that fixes it:
the owner's alert (`credential_failing`, pushed by `ameesh notify`) shows it.
Only names, paths and dates are read or written, never a secret.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass

from . import accounts as accounts_mod

PERIOD_S = 600
#: a forecast expiry closer than this raises `credential_expiring`
DEFAULT_WARN_HOURS = 24.0
#: Google Workspace default session length (admin console: 16 h)
DEFAULT_GCLOUD_SESSION_HOURS = 16.0
PROBE_TIMEOUT_S = 30
PROBES = ("claude", "codex", "gcloud", "gh", "scw")


@dataclass(frozen=True)
class Verdict:
    harness: str
    account: str          #: L30 account name, '' for the default credential
    state: str            #: healthy | failing | unknown
    reason: str = ""
    expires_at: float | None = None
    fix: str = ""         #: exact command that repairs a failing credential

    def text(self) -> str:
        """The `reason` stored: the cause, then the fix command."""
        if self.fix and self.state != "healthy":
            return "%s; fix: %s" % (self.reason, self.fix)
        return self.reason


def settings(cfg) -> dict:
    raw = getattr(cfg, "credential_sentinel", None) or {}
    probes = raw.get("probes")
    return {"probes": tuple(p for p in (probes if isinstance(probes, list) else PROBES)
                            if p in PROBES),
            "gcloud_session_hours": float(raw.get("gcloud_session_hours")
                                          or DEFAULT_GCLOUD_SESSION_HOURS),
            "warn_hours": float(raw.get("warn_hours") or DEFAULT_WARN_HOURS)}


def _run(argv: list, env: dict | None = None) -> tuple[int | None, str]:
    """(exit code, last line of output); None on timeout or missing binary."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=PROBE_TIMEOUT_S,
                              env=env, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, type(exc).__name__
    lines = [l.strip() for l in (proc.stderr + "\n" + proc.stdout).splitlines() if l.strip()]
    return proc.returncode, (lines[-1][:200] if lines else "")


def _profiles(cfg, harness: str) -> list:
    items = accounts_mod.profiles(cfg, harness)
    return items or [accounts_mod.Profile(harness=harness, name="")]


def _key_set(profile, environ) -> bool:
    if profile.key_file:
        return os.path.isfile(profile.key_file)
    name = profile.key_env or profile.env or accounts_mod.default_key_env_of(profile.harness)
    return bool(name and environ.get(name))


def _env_prefix(harness: str, profile) -> str:
    if profile.kind == "config_dir" and profile.path:
        return "%s=%s " % (accounts_mod.config_env_of(harness), profile.path)
    return ""


def probe_claude(profile, now: float, environ=None) -> Verdict | None:
    environ = os.environ if environ is None else environ
    name = profile.name if profile.name else ""
    if profile.kind == "api_key_env":
        ok = _key_set(profile, environ)
        return Verdict("claude", name, "healthy" if ok else "failing",
                       "API key present" if ok else "API key missing",
                       fix="" if ok else "set the key of account %s" % (name or "default"))
    path = os.path.join(profile.home(environ), ".credentials.json")
    fix = "%sclaude, then /login" % _env_prefix("claude", profile)
    if not os.path.exists(path):
        if not profile.name and not profile.path:
            return None               # no Claude credential here: not used on this host
        return Verdict("claude", name, "failing", "no credentials file %s" % path, fix=fix)
    try:
        with open(path, encoding="utf-8") as fh:
            oauth = (json.load(fh) or {}).get("claudeAiOauth") or {}
    except (OSError, ValueError, AttributeError):
        return Verdict("claude", name, "unknown", "unreadable %s" % path)
    refresh_ms = oauth.get("refreshTokenExpiresAt")
    expires = float(refresh_ms) / 1000 if isinstance(refresh_ms, (int, float)) else None
    if not oauth.get("refreshToken"):
        access_ms = oauth.get("expiresAt")
        if isinstance(access_ms, (int, float)) and access_ms / 1000 > now:
            return Verdict("claude", name, "healthy", "access token without refresh token",
                           expires_at=access_ms / 1000, fix=fix)
        return Verdict("claude", name, "failing", "no refresh token", fix=fix)
    if expires is not None and expires <= now:
        return Verdict("claude", name, "failing", "refresh token expired", expires, fix)
    return Verdict("claude", name, "healthy", "refresh token valid", expires, fix)


def probe_codex(profile, now: float, environ=None) -> Verdict | None:
    environ = os.environ if environ is None else environ
    name = profile.name if profile.name else ""
    if profile.kind == "api_key_env":
        ok = _key_set(profile, environ)
        return Verdict("codex", name, "healthy" if ok else "failing",
                       "API key present" if ok else "API key missing",
                       fix="" if ok else "set the key of account %s" % (name or "default"))
    path = os.path.join(profile.home(environ), "auth.json")
    fix = "%scodex login" % _env_prefix("codex", profile)
    if not os.path.exists(path):
        if not profile.name and not profile.path:
            return None
        return Verdict("codex", name, "failing", "no credentials file %s" % path, fix=fix)
    try:
        with open(path, encoding="utf-8") as fh:
            auth = json.load(fh) or {}
    except (OSError, ValueError):
        return Verdict("codex", name, "unknown", "unreadable %s" % path)
    tokens = auth.get("tokens") if isinstance(auth, dict) else None
    if (isinstance(tokens, dict) and tokens.get("refresh_token")) or auth.get("OPENAI_API_KEY"):
        return Verdict("codex", name, "healthy", "refresh token present", fix=fix)
    return Verdict("codex", name, "failing", "no refresh token nor API key", fix=fix)


def gcloud_config_dir(environ=None) -> str:
    environ = os.environ if environ is None else environ
    return environ.get("CLOUDSDK_CONFIG") or os.path.expanduser("~/.config/gcloud")


def gcloud_forecast(session_hours: float, environ=None) -> float | None:
    """Forecast expiry of the gcloud session: last interactive login (the
    credentials store changes on `gcloud auth login`, not on token refresh)
    plus the Workspace session length."""
    try:
        login = os.stat(os.path.join(gcloud_config_dir(environ), "credentials.db")).st_mtime
    except OSError:
        return None
    return login + session_hours * 3600


def probe_gcloud(conf: dict, now: float, run=_run, environ=None) -> Verdict | None:
    if not shutil.which("gcloud"):
        return None
    fix = "gcloud auth login --update-adc"
    code, last = run(["gcloud", "auth", "print-access-token", "--quiet"])
    expires = gcloud_forecast(conf["gcloud_session_hours"], environ)
    if code is None:
        return Verdict("gcloud", "", "unknown", "probe failed: %s" % last, expires)
    if code != 0:
        return Verdict("gcloud", "", "failing", last or "print-access-token failed",
                       expires, fix)
    return Verdict("gcloud", "", "healthy", "access token issued", expires, fix)


def probe_cli(harness: str, argv: list, fix: str, run=_run) -> Verdict | None:
    if not shutil.which(argv[0]):
        return None
    code, last = run(argv)
    if code is None:
        return Verdict(harness, "", "unknown", "probe failed: %s" % last)
    if code != 0:
        return Verdict(harness, "", "failing", last or "%s failed" % " ".join(argv), fix=fix)
    return Verdict(harness, "", "healthy", "authenticated", fix=fix)


def probe_all(cfg, now: float | None = None, run=_run, environ=None) -> list[Verdict]:
    now = time.time() if now is None else now
    conf = settings(cfg)
    out: list[Verdict | None] = []
    for harness in conf["probes"]:
        if harness == "claude":
            out += [probe_claude(p, now, environ) for p in _profiles(cfg, "claude")]
        elif harness == "codex":
            out += [probe_codex(p, now, environ) for p in _profiles(cfg, "codex")]
        elif harness == "gcloud":
            out.append(probe_gcloud(conf, now, run, environ))
        elif harness == "gh":
            out.append(probe_cli("gh", ["gh", "auth", "status"], "gh auth login -h github.com",
                                 run))
        elif harness == "scw":
            out.append(probe_cli("scw", ["scw", "account", "project", "list", "-o", "json"],
                                 "scw init", run))
    return [v for v in out if v is not None]


def check(cfg, db, *, now: float | None = None, run=_run, environ=None,
          dry_run: bool = False) -> list[dict]:
    """Probes every credential and reports it; the rows, with `previous`."""
    from . import storage
    rows = []
    for verdict in probe_all(cfg, now, run, environ):
        row = {"host": cfg.host, "harness": verdict.harness, "account": verdict.account,
               "state": verdict.state, "reason": verdict.text(),
               "expires_at": verdict.expires_at, "previous": None}
        if not dry_run:
            done = storage.of(db).credentials.report(
                cfg.host, verdict.harness, verdict.account, verdict.state,
                reason=verdict.text(), source="sentinel", expires_at=verdict.expires_at)
            row["previous"] = (done or {}).get("previous")
        rows.append(row)
    return rows
