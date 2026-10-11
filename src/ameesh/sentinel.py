# SPDX-License-Identifier: AGPL-3.0-only
"""L226a — credential sentinel: the health of every credential agents use on
this host, probed every 10 minutes (`ameesh accounts check`, systemd timer
`ameesh-credentials.timer`).

Each probe reports to `credential_health` (migration 0060, owned by L226b)
through `storage.of(db).credentials.report(..., source='sentinel')`:

* `claude`, per declared account (or the default config dir): `claude auth
  status` in its config dir says `loggedIn` (an expired access token is not a
  failure: it is refreshed on use); forecast expiry = `refreshTokenExpiresAt`;
* `codex`, per declared account: `auth.json` has a refresh token or an API key;
* `gcloud`: `gcloud auth print-access-token` succeeds; forecast expiry = last
  interactive login (mtime of `credentials.db`) + the Workspace session length
  (`credential_sentinel.gcloud_session_hours`);
* `gh`: `gh auth status` succeeds;
* `scw`: `scw account project list` succeeds.

A harness without its binary, or without any credential file and no declared
account, is not used on this host: it is skipped. A probe that cannot conclude
(timeout, unreadable file) reports `unknown`, never a verdict.

A `failing` credential (detected by a turn, L226b, or by an earlier probe) is
only cleared by a positive proof of a fresh login: its credential store
(`.credentials.json`, `auth.json`, gcloud `credentials.db`, gh `hosts.yml`,
scw `config.yaml`, a key file) rewritten after the failure began. A probe
that reads `healthy` without that proof (a revoked refresh token is still in
the file), or `unknown`, leaves the failure in place.

The `reason` of a failing credential carries the exact command that fixes it:
the owner's alert (`credential_failing`, pushed by `ameesh notify`) shows it.
Only names, paths and dates are read or written, never a secret.
"""
from __future__ import annotations

import json
import os
import shlex
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
    #: mtime of the credential store: a login (or a refresh) after a failure
    #: began is the only proof that clears it; None = no proof possible
    proof: float | None = None

    def text(self) -> str:
        """The `reason` stored: the cause, then the fix command — also on a
        healthy credential with a forecast expiry (`credential_expiring`)."""
        if self.fix and (self.state != "healthy" or self.expires_at is not None):
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


def _run(argv: list, env: dict | None = None,
         whole: bool = False) -> tuple[int | None, str]:
    """(exit code, last line of output — or the whole stdout with `whole`);
    None on timeout or missing binary."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=PROBE_TIMEOUT_S,
                              env=env, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, type(exc).__name__
    if whole:
        return proc.returncode, proc.stdout
    lines = [l.strip() for l in (proc.stderr + "\n" + proc.stdout).splitlines() if l.strip()]
    return proc.returncode, (lines[-1][:200] if lines else "")


def _profiles(cfg, harness: str) -> list:
    items = accounts_mod.profiles(cfg, harness)
    return items or [accounts_mod.Profile(harness=harness, name="")]


def _mtime(path: str | None) -> float | None:
    try:
        return os.stat(path).st_mtime if path else None
    except OSError:
        return None


def _key_set(profile, environ) -> bool:
    if profile.key_file:
        return os.path.isfile(profile.key_file)
    name = profile.key_env or profile.env or accounts_mod.default_key_env_of(profile.harness)
    return bool(name and environ.get(name))


def _env_prefix(harness: str, profile) -> str:
    if profile.kind == "config_dir" and profile.path:
        return "%s=%s " % (accounts_mod.config_env_of(harness), shlex.quote(profile.path))
    return ""


def _probe_env(environ, declared, profile, variable: str, home: str) -> dict:
    """The environment of a probe: like the runner's (`accounts.launch_env`),
    a declared account sheds every inherited credential variable
    (`ANTHROPIC_API_KEY`…), which would otherwise outrank its config dir."""
    env = dict(environ)
    if declared and profile.name:
        for name in accounts_mod.auth_variables(declared, selected=True):
            env.pop(name, None)
    env[variable] = home
    return env


def probe_claude(profile, now: float, environ=None, run=None,
                 declared=None) -> Verdict | None:
    """`claude auth status` in the account's config dir decides; never the
    access token's `expiresAt` (it expires after a short idle time and is
    refreshed on use). `refreshTokenExpiresAt` only gives the forecast."""
    environ = os.environ if environ is None else environ
    run = _run if run is None else run
    name = profile.name if profile.name else ""
    if profile.kind == "api_key_env":
        ok = _key_set(profile, environ)
        return Verdict("claude", name, "healthy" if ok else "failing",
                       "API key present" if ok else "API key missing",
                       fix="" if ok else "set the key of account %s" % (name or "default"),
                       proof=_mtime(profile.key_file))
    home = profile.home(environ)
    path = os.path.join(home, ".credentials.json")
    fix = "CLAUDE_CONFIG_DIR=%s claude auth login" % shlex.quote(home)
    if not os.path.exists(path) and not profile.name and not profile.path:
        return None                   # no Claude credential here: not used on this host
    expires = None
    try:
        with open(path, encoding="utf-8") as fh:
            refresh_ms = ((json.load(fh) or {}).get("claudeAiOauth") or {}).get(
                "refreshTokenExpiresAt")
        if isinstance(refresh_ms, (int, float)):
            expires = float(refresh_ms) / 1000
    except (OSError, ValueError, AttributeError):
        pass
    if not shutil.which("claude", path=environ.get("PATH")):
        return Verdict("claude", name, "unknown", "claude binary not found", expires)
    code, out = run(["claude", "auth", "status"],
                    env=_probe_env(environ, declared, profile,
                                   accounts_mod.config_env_of("claude") or "CLAUDE_CONFIG_DIR",
                                   home), whole=True)
    if code is None:
        return Verdict("claude", name, "unknown", "probe failed: %s" % out, expires)
    try:
        status = json.loads(out)
    except ValueError:
        return Verdict("claude", name, "unknown",
                       "unreadable auth status (exit %s)" % code, expires)
    if not isinstance(status, dict) or not status.get("loggedIn"):
        return Verdict("claude", name, "failing", "claude auth status: not logged in",
                       expires, fix)
    if expires is not None and expires <= now:
        return Verdict("claude", name, "failing", "refresh token expired", expires, fix)
    return Verdict("claude", name, "healthy",
                   "logged in (%s)" % (status.get("authMethod") or "?"), expires, fix,
                   proof=_mtime(path))


def probe_codex(profile, now: float, environ=None) -> Verdict | None:
    environ = os.environ if environ is None else environ
    name = profile.name if profile.name else ""
    if profile.kind == "api_key_env":
        ok = _key_set(profile, environ)
        return Verdict("codex", name, "healthy" if ok else "failing",
                       "API key present" if ok else "API key missing",
                       fix="" if ok else "set the key of account %s" % (name or "default"),
                       proof=_mtime(profile.key_file))
    path = os.path.join(profile.home(environ), "auth.json")
    fix = "%scodex login" % _env_prefix("codex", profile)
    if not os.path.exists(path):
        if not profile.name and not profile.path:
            return None
        return Verdict("codex", name, "failing", "no credentials file %s" % path, fix=fix)
    try:
        with open(path, encoding="utf-8") as fh:
            auth = json.load(fh)
    except (OSError, ValueError):
        return Verdict("codex", name, "unknown", "unreadable %s" % path)
    if not isinstance(auth, dict):    # valid JSON, but not the object codex writes
        return Verdict("codex", name, "unknown", "unexpected content in %s" % path)
    tokens = auth.get("tokens")
    if (isinstance(tokens, dict) and tokens.get("refresh_token")) or auth.get("OPENAI_API_KEY"):
        return Verdict("codex", name, "healthy", "refresh token present", fix=fix,
                       proof=_mtime(path))
    return Verdict("codex", name, "failing", "no refresh token nor API key", fix=fix)


def gcloud_config_dir(environ=None) -> str:
    environ = os.environ if environ is None else environ
    return environ.get("CLOUDSDK_CONFIG") or os.path.expanduser("~/.config/gcloud")


def gcloud_login(environ=None) -> float | None:
    """Last interactive gcloud login: the credentials store changes on
    `gcloud auth login`, not on token refresh."""
    return _mtime(os.path.join(gcloud_config_dir(environ), "credentials.db"))


def gcloud_forecast(session_hours: float, environ=None) -> float | None:
    """Forecast expiry of the gcloud session: last interactive login plus the
    Workspace session length."""
    login = gcloud_login(environ)
    return None if login is None else login + session_hours * 3600


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
    return Verdict("gcloud", "", "healthy", "access token issued", expires, fix,
                   proof=gcloud_login(environ))


def gh_store(environ=None) -> str:
    environ = os.environ if environ is None else environ
    return os.path.join(environ.get("GH_CONFIG_DIR") or os.path.expanduser("~/.config/gh"),
                        "hosts.yml")


def scw_store(environ=None) -> str:
    environ = os.environ if environ is None else environ
    return environ.get("SCW_CONFIG_PATH") or os.path.expanduser("~/.config/scw/config.yaml")


def probe_cli(harness: str, argv: list, fix: str, run=_run,
              store: str | None = None) -> Verdict | None:
    if not shutil.which(argv[0]):
        return None
    code, last = run(argv)
    if code is None:
        return Verdict(harness, "", "unknown", "probe failed: %s" % last)
    if code != 0:
        return Verdict(harness, "", "failing", last or "%s failed" % " ".join(argv), fix=fix)
    return Verdict(harness, "", "healthy", "authenticated", fix=fix, proof=_mtime(store))


def probe_all(cfg, now: float | None = None, run=_run, environ=None) -> list[Verdict]:
    now = time.time() if now is None else now
    conf = settings(cfg)
    declared = accounts_mod.parse(getattr(cfg, "accounts", None) or {})
    out: list[Verdict | None] = []
    for harness in conf["probes"]:
        if harness == "claude":
            out += [probe_claude(p, now, environ, run, declared)
                    for p in _profiles(cfg, "claude")]
        elif harness == "codex":
            out += [probe_codex(p, now, environ) for p in _profiles(cfg, "codex")]
        elif harness == "gcloud":
            out.append(probe_gcloud(conf, now, run, environ))
        elif harness == "gh":
            out.append(probe_cli("gh", ["gh", "auth", "status"], "gh auth login -h github.com",
                                 run, gh_store(environ)))
        elif harness == "scw":
            out.append(probe_cli("scw", ["scw", "account", "project", "list", "-o", "json"],
                                 "scw init", run, scw_store(environ)))
    return [v for v in out if v is not None]


def kept_failure(current: dict, verdict: Verdict) -> str:
    """Why `verdict` must not overwrite the `failing` row `current` ('' if it
    may): only a `failing` verdict, or a `healthy` one whose credential store
    was rewritten after the failure began, replaces it."""
    if current.get("state") != "failing" or verdict.state == "failing":
        return ""
    since = current.get("failing_since_ts") or current.get("checked_ts")
    if verdict.state == "healthy" and verdict.proof is not None and since is not None \
            and verdict.proof > float(since):
        return ""
    return ("probe %s (%s) but no fresh login since the failure; still failing — "
            "log in again, or `ameesh accounts healthy %s%s` once repaired"
            % (verdict.state, verdict.reason, verdict.harness,
               " " + shlex.quote(verdict.account) if verdict.account else ""))


def check(cfg, db, *, now: float | None = None, run=_run, environ=None,
          dry_run: bool = False) -> list[dict]:
    """Probes every credential and reports it; the rows, with `previous`.

    A credential already `failing` stays so unless the probe proves a fresh
    login (`kept_failure`): the row then carries `kept` and nothing is
    written."""
    from . import storage
    rows = []
    known: dict = {}
    for verdict in probe_all(cfg, now, run, environ):
        row = {"host": cfg.host, "harness": verdict.harness, "account": verdict.account,
               "state": verdict.state, "reason": verdict.text(),
               "expires_at": verdict.expires_at, "previous": None}
        if not dry_run and verdict.harness not in known:
            known[verdict.harness] = storage.of(db).credentials.health(cfg.host,
                                                                       verdict.harness)
        current = (known.get(verdict.harness) or {}).get(verdict.account) or {}
        kept = kept_failure(current, verdict)
        if kept:
            row.update(state="failing", previous="failing", kept=kept)
            rows.append(row)
            continue
        if not dry_run:
            done = storage.of(db).credentials.report(
                cfg.host, verdict.harness, verdict.account, verdict.state,
                reason=verdict.text(), source="sentinel", expires_at=verdict.expires_at)
            row["previous"] = (done or {}).get("previous")
        rows.append(row)
    return rows
