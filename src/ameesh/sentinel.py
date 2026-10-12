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
only cleared by a positive proof of a fresh login: a healthy probe AND a
credential that is not the one which failed. Each failure records the
fingerprint of the credential then in place (`fingerprint`: the refresh token
of `.credentials.json` or `auth.json`, the key, or the whole gcloud
`credentials.db`, gh `hosts.yml`, scw `config.yaml`); the failure clears only
when the fingerprint read now differs from the one of the LAST failure — not
on the store's date: a turn that started with A can fail after the owner
installed B (#210 r4). Rewriting the same revoked token, a probe without a
fingerprint, or `unknown`, leaves the failure in place. The clearing
write is conditional (`expect_failures`): a failure a turn records between
the probe's read and its write is never overwritten.

A CLI probe (gcloud, gh, scw) that exits non-zero is `failing` only when its
output says the credential is refused; a network error (DNS, timeout, 5xx)
or anything unrecognized is `unknown` (#210 r4): a credential failure is
cleared only by another credential, a transient one would never clear.

The `reason` of a failing credential carries the exact command that fixes it:
the owner's alert (`credential_failing`, pushed by `ameesh notify`) shows it.
A failure detected by a turn gets it at the next probe (`Credentials.annotate`).
Only names, paths, dates and fingerprints (a truncated SHA-256, which does
not reveal the credential) are written, never a secret.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
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
    #: fingerprint of the credential (`fingerprint`): it must differ from the
    #: one recorded at the last failure; None = no proof possible
    fingerprint: str | None = None

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


def _run(argv: list, env: dict | None = None, whole: bool = False,
         output: bool = False) -> tuple[int | None, str]:
    """(exit code, last line of output — or the whole stdout with `whole`,
    or every non-empty line of stderr then stdout with `output`); None on
    timeout or missing binary."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=PROBE_TIMEOUT_S,
                              env=env, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, type(exc).__name__
    if whole:
        return proc.returncode, proc.stdout
    lines = [l.strip() for l in (proc.stderr + "\n" + proc.stdout).splitlines() if l.strip()]
    if output:
        return proc.returncode, "\n".join(lines)[-4000:]
    return proc.returncode, (lines[-1][:200] if lines else "")


#: #210 r4: a failed CLI probe that could not reach its service — never a
#: verdict on the credential (DNS, connection, timeout, server error)
NETWORK_ERROR = re.compile(
    r"dial tcp|no such host|name resolution|could not resolve|failed to resolve"
    r"|max retries exceeded|failed to establish a new connection|connection (aborted|refused|reset)"
    r"|network is unreachable|error connecting|unable to connect|timed? ?out|timeout"
    r"|tls handshake|\b50[0234]\b|service unavailable|bad gateway", re.IGNORECASE)
#: and what the CLIs say of a refused credential, besides the turn patterns
#: of `auth_failure`
CLI_REFUSED = re.compile(
    r"token in \S+ is invalid|not logged in|no active account"
    r"|do not currently have an active account|authentication is denied"
    r"|invalid (access|secret) key|unauthori[sz]ed", re.IGNORECASE)


def refused(output: str) -> str | None:
    """Why a failed CLI probe's `output` says its credential is refused; None
    when it says something else (network error, unrecognized): `unknown`."""
    from . import auth_failure
    if not output or NETWORK_ERROR.search(output):
        return None
    reason = auth_failure.classify(output)
    if reason:
        return reason
    found = CLI_REFUSED.search(output)
    return found.group(0).lower() if found else None


def _last_line(output: str) -> str:
    lines = [line for line in (output or "").splitlines() if line.strip()]
    return lines[-1][:200] if lines else ""


def _profiles(cfg, harness: str) -> list:
    items = accounts_mod.profiles(cfg, harness)
    return items or [accounts_mod.Profile(harness=harness, name="")]


def _mtime(path: str | None) -> float | None:
    try:
        return os.stat(path).st_mtime if path else None
    except OSError:
        return None


def _digest(secret) -> str | None:
    """A truncated SHA-256 of a credential: tells two credentials apart,
    reveals neither."""
    if not secret:
        return None
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    return hashlib.sha256(secret).hexdigest()[:24]


def file_fingerprint(path: str | None) -> str | None:
    """Fingerprint of a whole credential store (gcloud, gh, scw, key file)."""
    try:
        with open(path, "rb") as fh:
            return _digest(fh.read())
    except (OSError, TypeError):
        return None


def _json_secret(path: str, *keys) -> str | None:
    """The string at `keys` in a JSON file (None if absent or unreadable)."""
    try:
        with open(path, encoding="utf-8") as fh:
            node = json.load(fh)
    except (OSError, ValueError):
        return None
    for key in keys:
        node = node.get(key) if isinstance(node, dict) else None
    return node if isinstance(node, str) and node else None


def fingerprint(harness: str, profile=None, environ=None) -> str | None:
    """Fingerprint of the credential an account of `harness` uses now: the
    refresh token for Claude (`.credentials.json`) and Codex (`auth.json`, or
    its API key), the key of an `api_key_env` account. Recorded with each
    failure (by a probe or a turn, L226b); None when unknown."""
    environ = os.environ if environ is None else environ
    if profile is None:
        profile = accounts_mod.Profile(harness=harness, name="")
    if profile.kind == "api_key_env":
        # the key as the runner passes it (`accounts.apply_env` strips it):
        # `key` and `key\n` are the same credential
        if profile.key_file:
            try:
                with open(profile.key_file, encoding="utf-8") as fh:
                    return _digest(fh.read().strip())
            except (OSError, UnicodeDecodeError):
                return None
        name = profile.key_env or profile.env or accounts_mod.default_key_env_of(harness)
        return _digest(str(environ.get(name) or "").strip()) if name else None
    home = profile.home(environ)
    if harness == "claude":
        return _digest(_json_secret(os.path.join(home, ".credentials.json"),
                                    "claudeAiOauth", "refreshToken"))
    if harness == "codex":
        path = os.path.join(home, "auth.json")
        return _digest(_json_secret(path, "tokens", "refresh_token")
                       or _json_secret(path, "OPENAI_API_KEY"))
    return None


def launch_fingerprint(harness: str, profile, env: dict) -> str | None:
    """#210 r4: the fingerprint of the credential a turn is launched with,
    read from the child's environment `env` once the account is applied — an
    API key is the exact value handed to the child (its key file may be
    replaced before the turn fails), an OAuth store the file that
    environment points to."""
    if profile is not None and profile.kind == "api_key_env":
        # the variable the child reads (`accounts.apply_env`)
        name = profile.env or accounts_mod.default_key_env_of(harness)
        return _digest(str(env.get(name) or "").strip()) if name else None
    return fingerprint(harness, profile, env)


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
                       fix="set the key of account %s" % (name or "default"),
                       fingerprint=fingerprint("claude", profile, environ))
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
    held = fingerprint("claude", profile, environ)
    if not isinstance(status, dict) or not status.get("loggedIn"):
        return Verdict("claude", name, "failing", "claude auth status: not logged in",
                       expires, fix, fingerprint=held)
    if expires is not None and expires <= now:
        return Verdict("claude", name, "failing", "refresh token expired", expires, fix,
                       fingerprint=held)
    return Verdict("claude", name, "healthy",
                   "logged in (%s)" % (status.get("authMethod") or "?"), expires, fix,
                   fingerprint=held)


def probe_codex(profile, now: float, environ=None) -> Verdict | None:
    environ = os.environ if environ is None else environ
    name = profile.name if profile.name else ""
    if profile.kind == "api_key_env":
        ok = _key_set(profile, environ)
        return Verdict("codex", name, "healthy" if ok else "failing",
                       "API key present" if ok else "API key missing",
                       fix="set the key of account %s" % (name or "default"),
                       fingerprint=fingerprint("codex", profile, environ))
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
                       fingerprint=fingerprint("codex", profile, environ))
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
    code, out = run(["gcloud", "auth", "print-access-token", "--quiet"], output=True)
    expires = gcloud_forecast(conf["gcloud_session_hours"], environ)
    if code is None:
        return Verdict("gcloud", "", "unknown", "probe failed: %s" % out, expires)
    store = os.path.join(gcloud_config_dir(environ), "credentials.db")
    if code != 0:
        why = refused(out)
        if why is None:
            return Verdict("gcloud", "", "unknown", "probe failed (exit %s): %s"
                           % (code, _last_line(out) or "no output"), expires)
        return Verdict("gcloud", "", "failing",
                       "%s: %s" % (why, _last_line(out)) if out else why,
                       expires, fix, fingerprint=file_fingerprint(store))
    return Verdict("gcloud", "", "healthy", "access token issued", expires, fix,
                   fingerprint=file_fingerprint(store))


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
    code, out = run(argv, output=True)
    if code is None:
        return Verdict(harness, "", "unknown", "probe failed: %s" % out)
    if code != 0:
        why = refused(out)
        if why is None:
            return Verdict(harness, "", "unknown", "probe failed (exit %s): %s"
                           % (code, _last_line(out) or "no output"))
        return Verdict(harness, "", "failing", "%s: %s" % (why, _last_line(out)), fix=fix,
                       fingerprint=file_fingerprint(store))
    return Verdict(harness, "", "healthy", "authenticated", fix=fix,
                   fingerprint=file_fingerprint(store))


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
    may): only a `failing` verdict, or a `healthy` one holding another
    credential than the LAST failure's (fingerprints differ), replaces it —
    whenever that credential was installed (#210 r4: B installed while a turn
    with A still ran, then A failed). No fingerprint on either side: no
    proof, kept."""
    if current.get("state") != "failing" or verdict.state == "failing":
        return ""
    failed = current.get("failing_fingerprint")
    if verdict.state == "healthy" and failed and verdict.fingerprint \
            and verdict.fingerprint != failed:
        return ""
    return ("probe %s (%s) but no new credential since the last failure; still failing — "
            "log in again, or `ameesh accounts healthy %s%s` once repaired"
            % (verdict.state, verdict.reason, verdict.harness,
               " " + shlex.quote(verdict.account) if verdict.account else ""))


def check(cfg, db, *, now: float | None = None, run=_run, environ=None,
          dry_run: bool = False) -> list[dict]:
    """Probes every credential and reports it; the rows, with `previous`.

    A credential already `failing` stays so unless the probe proves a fresh
    login (`kept_failure`): the row then carries `kept` and nothing is
    written. A write other than `failing` is conditional on the failure count
    read (`expect_failures`): a failure recorded meanwhile by a turn wins."""
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
            reason = current.get("reason") or ""
            if verdict.fix and "fix:" not in reason:
                # #210 r4: a failure a turn detected names no fix; the owner's
                # alert shows the reason
                row["reason"] = "%s; fix: %s" % (reason or "authentication failed", verdict.fix)
                if not dry_run:
                    storage.of(db).credentials.annotate(
                        cfg.host, verdict.harness, verdict.account, row["reason"],
                        expect_failures=int(current.get("failures") or 0))
            else:
                row["reason"] = reason or row["reason"]
            rows.append(row)
            continue
        if not dry_run:
            done = storage.of(db).credentials.report(
                cfg.host, verdict.harness, verdict.account, verdict.state,
                reason=verdict.text(), source="sentinel", expires_at=verdict.expires_at,
                fingerprint=verdict.fingerprint,
                expect_failures=int(current.get("failures") or 0))
            if (done or {}).get("skipped"):
                row.update(state="failing", previous="failing",
                           kept="a failure was recorded during the probe; not overwritten")
            else:
                row["previous"] = (done or {}).get("previous")
        rows.append(row)
    return rows
