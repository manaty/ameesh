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
cleared by a positive proof of a fresh login: a healthy probe AND a
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

A server-verified probe is an exception (#8200): `gh auth status` and
`scw account project list` ask the service on every pass, so a healthy answer
is the server's own proof. A refusal can then be transient (the same
credential is accepted again) and is lifted after two healthy probes of that
credential, at least fifteen minutes apart (`Credentials.probe_streak`).
`claude` and `codex` read a local presence and keep the guard; `gcloud` can
answer from a cached access token, so it keeps it too.

The server relay account is another exception: a positive authenticated
balance response can clear a transient provider refusal with the same key,
after one sentinel interval of backoff. It retains the conditional write
above.

A CLI probe (gcloud, gh, scw) that exits non-zero is `failing` only when its
output says the credential is refused; a network error (DNS, timeout, 5xx)
or anything unrecognized is `unknown` (#210 r4): an `unknown` is never a
verdict, so it neither becomes a failure nor clears one.

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
#: #8062 : nombre de périodes sans relevé au-delà duquel la sonde est muette
#: (`credential_probe_stale`, 3 × 10 min) ; le jitter de la minuterie (30 s)
#: est très en deçà.
PROBE_SILENCE_PERIODS = 3
#: a forecast expiry closer than this raises `credential_expiring`
DEFAULT_WARN_HOURS = 24.0
ACCOUNT_WARN_HOURS = 30 * 24.0
#: Google Workspace default session length (admin console: 16 h)
DEFAULT_GCLOUD_SESSION_HOURS = 16.0
PROBE_TIMEOUT_S = 30
PROBES = ("claude", "codex", "gcloud", "gh", "scw")
#: #8200 : sondes qui interrogent le service à chaque passe ; un verdict sain
#: est la preuve du serveur, pas une présence locale (`claude`, `codex`) ni un
#: jeton peut-être encore en cache (`gcloud`).
SERVER_VERIFIED = ("gh", "scw")
#: #8200 : nombre de sondes saines consécutives qui lèvent un échec gardé…
SERVER_HEAL_PROBES = 2
#: … et espacement minimal entre deux sondes comptées. La minuterie de 10 min
#: fait donc tomber la seconde ~20 min après la première.
SERVER_HEAL_INTERVAL_S = 15 * 60


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
        return proc.returncode, "\n".join(lines)   # whole: a refusal can come first
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


def _secret_at(node, *keys) -> str | None:
    """The string at `keys` in a parsed JSON file (None if absent)."""
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
    from . import setup_token
    if setup_token.present(profile, environ):
        try:
            return _digest(setup_token.read(profile, environ)[0])
        except setup_token.TokenError:
            return None
    home = profile.home(environ)
    if harness == "claude":
        try:
            with open(os.path.join(home, ".credentials.json"), encoding="utf-8") as fh:
                return _claude_fingerprint(json.load(fh))
        except (OSError, ValueError):
            return None
    if harness == "codex":
        try:
            with open(os.path.join(home, "auth.json"), encoding="utf-8") as fh:
                return _codex_fingerprint(json.load(fh))
        except (OSError, ValueError):
            return None
    return None


def _claude_fingerprint(node) -> str | None:
    return _digest(_secret_at(node, "claudeAiOauth", "refreshToken"))


def _codex_fingerprint(auth) -> str | None:
    return _digest(_secret_at(auth, "tokens", "refresh_token")
                   or _secret_at(auth, "OPENAI_API_KEY"))


def launch_fingerprint(harness: str, profile, env: dict) -> str | None:
    """#210 r4: the fingerprint of the credential a turn is launched with,
    read from the child's environment `env` once the account is applied — an
    API key is the exact value handed to the child (its key file may be
    replaced before the turn fails), a setup token is likewise the value
    handed to the child, an OAuth store the file that
    environment points to."""
    from . import setup_token
    if harness == "claude" and setup_token.VARIABLE in env:
        return _digest(str(env.get(setup_token.VARIABLE) or "").strip())
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


def login_command(harness: str, account: str = "", kind: str = "") -> str:
    """The human's real login gesture for an account, named by the alerts and
    the runner's auth-blocked reason (#8180).

    `ameesh accounts login` is the only remote path: the native
    `claude auth login` is a paste-back flow, refused by the login service on
    a distant host (#8110). A key account is repaired by writing its key.
    """
    if kind == "api_key_env":
        return "set the key of account %s" % (account or "default")
    if harness == "claude":
        command = ["ameesh", "accounts", "login", "claude"]
        if account:
            command.append(account)
        command += ["--token-file", "FILE"]
        return " ".join(command)
    if harness == "codex":
        command = ["ameesh", "accounts", "login", "codex"]
        if account:
            command.append(account)
        return " ".join(command)
    return "log in again to %s/%s" % (harness, account or "(default)")


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


#: the verdict of a probe during which the credential was replaced
REPLACED = "credential replaced during the probe; the next probe decides"


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
    from . import setup_token
    if setup_token.present(profile, environ):
        fix = login_command("claude", name, profile.kind)
        try:
            token, expires = setup_token.read(profile, environ)
        except setup_token.TokenError as exc:
            return Verdict("claude", name, "failing", str(exc), fix=fix)
        held = _digest(token)
        if expires is not None and expires <= now:
            return Verdict("claude", name, "failing", "setup-token expired", expires, fix,
                           fingerprint=held)
        # Presence cannot undo a server rejection of this same token: kept_failure
        # below keeps turn failures until the operator replaces the credential.
        return Verdict("claude", name, "healthy",
                       "setup-token installed" + ("; expiry unknown" if expires is None else ""),
                       expires, fix, fingerprint=held)
    home = profile.home(environ)
    path = os.path.join(home, ".credentials.json")
    fix = login_command("claude", name, profile.kind)
    if not os.path.exists(path) and not profile.name and not profile.path:
        return None                   # no Claude credential here: not used on this host
    # #210 r5: the credential is read before the probe; a store replaced
    # while `claude auth status` runs proves nothing about either credential.
    # #313 r2: its expiry and its fingerprint come from the same read
    expires = held = None
    try:
        with open(path, encoding="utf-8") as fh:
            node = json.load(fh)
        held = _claude_fingerprint(node)
        refresh_ms = ((node or {}).get("claudeAiOauth") or {}).get("refreshTokenExpiresAt")
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
    if fingerprint("claude", profile, environ) != held:
        return Verdict("claude", name, "unknown", REPLACED, expires)
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
    fix = login_command("codex", name, profile.kind)
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
        # #210 r5: the fingerprint of the file read, not of a second read
        return Verdict("codex", name, "healthy", "refresh token present", fix=fix,
                       fingerprint=_codex_fingerprint(auth))
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
    store = os.path.join(gcloud_config_dir(environ), "credentials.db")
    held = file_fingerprint(store)
    code, out = run(["gcloud", "auth", "print-access-token", "--quiet"], output=True)
    expires = gcloud_forecast(conf["gcloud_session_hours"], environ)
    if code is None:
        return Verdict("gcloud", "", "unknown", "probe failed: %s" % out, expires)
    if file_fingerprint(store) != held:
        return Verdict("gcloud", "", "unknown", REPLACED, expires)
    if code != 0:
        why = refused(out)
        if why is None:
            return Verdict("gcloud", "", "unknown", "probe failed (exit %s): %s"
                           % (code, _last_line(out) or "no output"), expires)
        return Verdict("gcloud", "", "failing",
                       "%s: %s" % (why, _last_line(out)) if out else why,
                       expires, fix, fingerprint=held)
    return Verdict("gcloud", "", "healthy", "access token issued", expires, fix,
                   fingerprint=held)


def gh_store(environ=None) -> str:
    environ = os.environ if environ is None else environ
    return os.path.join(environ.get("GH_CONFIG_DIR") or os.path.expanduser("~/.config/gh"),
                        "hosts.yml")


def gh_credential_fingerprint(environ=None, run=None) -> str | None:
    """#2178 N2/B2 : empreinte de la credential EFFECTIVE de github.com.

    `gh auth token --hostname github.com` rend le jeton que `gh` présenterait,
    qu'il vienne de `hosts.yml` ou du keyring ; seule son empreinte tronquée
    est gardée (aucun secret journalisé). None = pas de preuve possible
    (aucun jeton, keyring verrouillé, sonde en échec) : l'appelant refuse de
    conclure « credential neuve » plutôt que d'hasher le fichier, qu'un simple
    commentaire suffit à changer (B2 de la relecture Codex).
    """
    run = _run if run is None else run
    env = os.environ if environ is None else environ
    argv = ["gh", "auth", "token", "--hostname", "github.com"]
    if run is _run:
        code, out = run(argv, env=env, whole=True)
    else:
        # `run` fourni : son environnement est déjà lié (`probe_gh`), ne pas
        # le repasser en double
        code, out = run(argv, whole=True)
    if code != 0:
        return None
    return _digest((out or "").strip())


def scw_store(environ=None) -> str:
    environ = os.environ if environ is None else environ
    return environ.get("SCW_CONFIG_PATH") or os.path.expanduser("~/.config/scw/config.yaml")


def env_fingerprint(environ, names: tuple, store: str | None) -> str | None:
    """#210 r5: the credential in effect for a CLI — the first set variable
    of `names`, which overrides the CLI's store (gh: GH_TOKEN, GITHUB_TOKEN;
    scw: SCW_SECRET_KEY), else the store file."""
    return env_override(environ, names) or file_fingerprint(store)


def env_override(environ, names: tuple) -> str | None:
    """#313 r1: the fingerprint of the first set variable of `names`, which
    overrides the CLI's store; None when the store is in effect."""
    environ = os.environ if environ is None else environ
    for name in names:
        value = str(environ.get(name) or "").strip()
        if value:
            return _digest(value)
    return None


#: the variables that override each CLI's credential store
GH_TOKEN_VARS = ("GH_TOKEN", "GITHUB_TOKEN")
SCW_KEY_VARS = ("SCW_SECRET_KEY",)


#: `probe_cli(credential=...)`: la fingerprint de la credential réellement
#: présentée, même quand elle est inconnue (None). Distingue « non fournie »
#: (lire le magasin comme avant) de « fournie et inconnue » (ne jamais
#: retomber sur le fichier : le fichier n'est pas la credential, cf. #2178 B2).
_NO_CREDENTIAL = object()


def probe_cli(harness: str, argv: list, fix: str, run=_run,
              store: str | None = None, override: str | None = None,
              credential=_NO_CREDENTIAL) -> Verdict | None:
    """`override`: the fingerprint of a variable that overrides `store`
    (`env_override`); without one, the store is the credential in effect,
    fingerprinted once, before the probe (#313 r1). `credential` (#2178 B2):
    the fingerprint of the credential actually presented (e.g. the `gh` token,
    from `hosts.yml` or the keyring); when supplied it wins, even at None, and
    the store-file replacement check does not apply."""
    if not shutil.which(argv[0]):
        return None
    known = credential is not _NO_CREDENTIAL
    held = credential if known else (override or file_fingerprint(store))
    code, out = run(argv, output=True)
    if code is None:
        return Verdict(harness, "", "unknown", "probe failed: %s" % out)
    if not known and not override and file_fingerprint(store) != held:
        return Verdict(harness, "", "unknown", REPLACED)
    if code != 0:
        why = refused(out)
        if why is None:
            return Verdict(harness, "", "unknown", "probe failed (exit %s): %s"
                           % (code, _last_line(out) or "no output"))
        return Verdict(harness, "", "failing", "%s: %s" % (why, _last_line(out)), fix=fix,
                       fingerprint=held)
    return Verdict(harness, "", "healthy", "authenticated", fix=fix, fingerprint=held)


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
            # N5 (#2178) : la sonde vise github.com comme l'adaptateur de
            # connexion ; sans cela, GH_HOST peut faire lire un autre hôte et
            # rendre un faux « healthy ».
            out.append(probe_cli("gh", ["gh", "auth", "status",
                                        "--hostname", "github.com"],
                                 "gh auth login -h github.com",
                                 run, gh_store(environ),
                                 env_override(environ, GH_TOKEN_VARS)))
        elif harness == "scw":
            out.append(probe_cli("scw", ["scw", "account", "project", "list", "-o", "json"],
                                 "scw init", run, scw_store(environ),
                                 env_override(environ, SCW_KEY_VARS)))
    return [v for v in out if v is not None]


def kept_failure(current: dict, verdict: Verdict) -> str:
    """Why `verdict` must not overwrite the `failing` row `current` ('' if it
    may): only a `failing` verdict, or a `healthy` one holding another
    credential than the LAST failure's (fingerprints differ), replaces it —
    whenever that credential was installed (#210 r4: B installed while a turn
    with A still ran, then A failed). No fingerprint on either side: no
    proof, kept. A server-verified probe can also clear the failure after
    spaced healthy probes (#8200): `check` counts them (`probe_streak`) and
    then treats the verdict as clearing."""
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
    login (`kept_failure`), or, for a server-verified probe (`gh`, `scw`), two
    spaced healthy probes prove the service accepts it again (#8200): the row
    then carries `kept` and its `probed_at` is stamped (#8062: the probe's own
    heartbeat, which a `turn` write never refreshes). A write other than
    `failing` is conditional on the failure count read (`expect_failures`): a
    failure recorded meanwhile by a turn wins."""
    from . import storage
    rows = []
    # #210 r5: health is read BEFORE the probes — a failure a turn records
    # while they run then raises the count the conditional write expects
    known: dict = {} if dry_run else {
        harness: storage.of(db).credentials.health(cfg.host, harness)
        for harness in settings(cfg)["probes"]}
    for verdict in probe_all(cfg, now, run, environ):
        row = {"host": cfg.host, "harness": verdict.harness, "account": verdict.account,
               "state": verdict.state, "reason": verdict.text(),
               "expires_at": verdict.expires_at, "previous": None}
        current = (known.get(verdict.harness) or {}).get(verdict.account) or {}
        kept = kept_failure(current, verdict)
        if kept and not dry_run and verdict.harness in SERVER_VERIFIED:
            # #8200: a server-verified healthy probe counts toward the
            # recovery series; the second one, spaced, lifts the failure.
            # Any other kept verdict (unknown) breaks the series, and the
            # update stamps the probe heartbeat (no `touch` below for these).
            streak = storage.of(db).credentials.probe_streak(
                cfg.host, verdict.harness, verdict.account,
                healthy=verdict.state == "healthy",
                interval=SERVER_HEAL_INTERVAL_S,
                expect_failures=int(current.get("failures") or 0))
            if verdict.state == "healthy" and streak is not None:
                if streak >= SERVER_HEAL_PROBES:
                    kept = ""
                else:
                    kept += ("; server-verified healthy probe %d/%d counted — "
                             "a spaced one clears it" % (streak, SERVER_HEAL_PROBES))
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
            if not dry_run and verdict.harness not in SERVER_VERIFIED:
                # #8062 : la sonde a bien relu l'identifiant (même échec, ou
                # verdict non concluant) : `probed_at` est son battement propre,
                # que les écritures `turn` ne touchent pas, et
                # `credential_probe_stale` le lit. L'état, le nombre d'échecs et
                # `failing_since` ne changent pas. Pour `gh`/`scw`, c'est
                # `probe_streak` qui a déjà estampillé ce battement (#8200).
                storage.of(db).credentials.touch(
                    cfg.host, verdict.harness, verdict.account,
                    expect_failures=int(current.get("failures") or 0))
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
    if not dry_run:
        from . import relay
        rows.extend(relay.probe_credentials(cfg, db, environ=environ, now=now))
    return rows
