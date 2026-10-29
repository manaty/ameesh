# SPDX-License-Identifier: AGPL-3.0-only
"""L232d : `ameesh accounts login <harnais> [<compte>]` — défi privé de connexion.

La commande tourne **sur l'hôte qui porte le compte**, dans le foyer isolé du
profil : elle construit l'URL d'autorisation et le code à usage unique du CLI
épinglé, les remet au service `ameesh-approve` (état privé), affiche seulement
le lien à faire signer par l'humain, attend le reçu (borné), le vérifie et le
consomme, puis interroge la santé du compte et l'écrit dans
`credential_health` — un reçu « terminé » ne prouve pas le grant.

Rien n'est journalisé : ni URL, ni code, ni jeton. La commande est le
superviseur : elle reste vivante tant que le défi court, et l'interruption la
clôt en `cancel`.
"""
from __future__ import annotations

import getpass
import json
import math
import os
import stat
import subprocess
import sys
import time

from .config import Config

TTL_WAIT_S = 900.0
#: attente maximale de la publication de la vue privée
PUBLISH_WAIT_S = 30.0
#: pas d'interrogation du reçu
RECEIPT_POLL_S = 2.0


def actor(environ=None) -> str:
    """L'identité qui demande : l'agent lié, sinon l'utilisateur."""
    env = os.environ if environ is None else environ
    name = env.get("AGENT_MAIL_NAME")
    if name:
        return "agent:%s" % name
    return "human:%s" % (env.get("AMEESH_ACTOR") or env.get("USER") or getpass.getuser())


def adapter_names() -> tuple:
    """Les harnais qui ont un adaptateur de connexion épinglé."""
    from . import login as login_mod
    return tuple(sorted(login_mod.ADAPTERS))


def account_profiles(cfg: Config, harness: str):
    from . import accounts as accounts_mod
    declared = accounts_mod.parse(cfg.accounts)
    return declared.get(harness) or []


#: `gh` n'est pas un harnais à descripteur : une seule connexion par outil,
#: dont le dossier par défaut est celui du CLI (`credential_health` la range
#: au compte vide, comme `host_preflight` pour l'outil `gh`)
GH_HOME = "~/.config/gh"


def resolve_profile(cfg: Config, harness: str, name: str | None = None, *, environ=None):
    """Le profil du compte : déclaré pour un harnais, implicite pour `gh`."""
    from . import accounts as accounts_mod
    env = os.environ if environ is None else environ
    if harness == "gh":
        if name not in (None, "", "default"):
            raise _error("failed", "gh has a single login: %r is not an account" % name)
        return accounts_mod.Profile(harness="gh", name="", kind="config_dir",
                                    path=os.path.expanduser(
                                        env.get("GH_CONFIG_DIR") or GH_HOME),
                                    declared=True)
    items = account_profiles(cfg, harness)
    if not items:
        raise _error("failed", "no account declared for %s (host `accounts` key)" % harness)
    if name:
        profile = accounts_mod.by_name(items, name)
        if profile is None:
            raise _error("failed", "unknown account %r for %s" % (name, harness))
    else:
        profile = accounts_mod.primary_of(harness, items, environ) or items[0]
    if profile.kind != "config_dir":
        raise _error("failed", "OAuth login requires a config_dir account")
    return profile


def home_problems(profile, environ=None) -> list:
    """Le foyer du compte est utilisable (existe, privé) — sans exiger des
    identifiants, puisqu'on se connecte justement pour les poser."""
    environ = os.environ if environ is None else environ
    home = profile.home(environ)
    if profile.default:
        return []  # dossier par défaut du harnais : celui de l'utilisateur
    tool = profile.harness == "gh"
    try:
        info = os.stat(home)
    except OSError:
        # gh crée son dossier de configuration à la connexion (outil, pas
        # harnais) ; un compte déclaré, lui, doit exister
        return [] if tool else ["missing directory: %s" % home]
    problems = []
    if not stat.S_ISDIR(info.st_mode):
        problems.append("not a directory: %s" % home)
    uid = os.getuid() if hasattr(os, "getuid") else None
    if uid is not None and info.st_uid != uid:
        problems.append("directory owned by another user")
    mask = 0o022 if tool else 0o077
    if info.st_mode & mask:
        problems.append("directory %s (%o, expected %s)"
                        % ("writable by others" if tool else "not private",
                           info.st_mode & 0o777, "not writable by others" if tool else "700"))
    return problems


def pinned_version(harness: str, *, probe=None, environ=None, home: str = "") -> dict:
    """La version installée, refusée si absente ou hors épinglage (L120).

    N6 : la sonde de version tourne avec l'environnement ÉPURÉ de la connexion
    (`login_env`) : ni GH_TOKEN, ni ANTHROPIC_API_KEY, ni les autres clés
    héritées ne sont visibles par un binaire qu'on interroge seulement pour
    son numéro de version.
    """
    from . import harness_versions
    from . import login as login_mod
    adapter = login_mod.ADAPTERS.get(harness)
    if adapter is None:
        raise _error("unsupported_version", "no login adapter for %r" % harness)
    if probe is not None:
        result = probe(harness)
    else:
        # La commande de connexion utilise argv[0], pas le binaire d'un
        # descripteur (gh n'en a pas ; les surcharges d'un harnais divergent).
        try:
            answer = subprocess.run([adapter.argv[0], "--version"], capture_output=True,
                                    text=True, stdin=subprocess.DEVNULL,
                                    env=login_mod.login_env(adapter, home, environ),
                                    timeout=harness_versions.PROBE_TIMEOUT_S)
            result = {"version": harness_versions.parse_version(answer.stdout or answer.stderr),
                      "error": None if answer.returncode == 0 else "version probe failed"}
        except (OSError, subprocess.SubprocessError) as exc:
            result = {"version": None, "error": type(exc).__name__}
    version = result.get("version")
    if adapter is None or result.get("error") or not version:
        raise _error("unsupported_version",
                     "cannot read a pinned %s version: %s"
                     % (harness, result.get("error") or "no version"))
    if not login_mod.supports(adapter, version):
        raise _error("unsupported_version",
                     "%s %s is not a pinned version (%s)"
                     % (harness, version, ", ".join(adapter.versions)))
    return result


#: distingue « pas de credential avant » (None) de « non mesuré » (témoins)
_UNMEASURED = object()


def credential_fingerprint(harness: str, profile, *, environ=None) -> str | None:
    """Empreinte NON secrète de la credential en place (N2).

    Lue AVANT de lancer la connexion, puis comparée à celle que porte le
    verdict sain : une sonde saine sur la MÊME credential ne prouve pas une
    connexion neuve. Jamais une valeur : seulement le SHA-256 tronqué déjà
    journalisé par `credential_health`.
    """
    from . import login as login_mod
    from . import sentinel
    environ = os.environ if environ is None else environ
    adapter = login_mod.ADAPTERS.get(harness)
    if adapter is None:
        return None
    env = login_mod.login_env(adapter, profile.home(environ), environ)
    if harness == "gh":
        # la credential effective est le jeton que gh présente pour
        # github.com (hosts.yml ou keyring), pas le fichier hosts.yml : un
        # commentaire changerait l'empreinte du fichier sans changer la
        # credential (B2 de la relecture Codex)
        return sentinel.gh_credential_fingerprint(env)
    return sentinel.fingerprint(harness, profile, environ)


def _proves_new_credential(before: str | None, after: str | None) -> bool:
    """La credential d'après est-elle NEUVE par rapport à celle d'avant ?

    Sans empreinte après, rien n'est prouvé. Sans credential avant, toute
    empreinte après est nouvelle ; sinon elle doit différer de l'ancienne.
    """
    return after is not None and (before is None or after != before)


def probe_health(cfg: Config, profile, harness: str, *, environ=None, run=None,
                 now: float | None = None, declared=None):
    """La sonde de santé du harnais (`sentinel`), sur le foyer du compte."""
    from . import accounts as accounts_mod
    from . import sentinel
    now = time.time() if now is None else now
    environ = os.environ if environ is None else environ
    if declared is None:
        declared = accounts_mod.parse(getattr(cfg, "accounts", None) or {})
    if harness == "claude":
        return sentinel.probe_claude(profile, now, environ, run, declared)
    if harness == "codex":
        return sentinel.probe_codex(profile, now, environ)
    if harness == "gh":
        from . import login as login_mod
        env = login_mod.login_env(login_mod.ADAPTERS["gh"], profile.home(environ), environ)
        def probe_gh(argv, **kwargs):
            return (run or sentinel._run)(argv, env=env, **kwargs)
        # B2 : l'empreinte du verdict est celle de la credential effective
        # (jeton github.com, keyring compris), jamais celle du fichier ; si
        # elle est inconnue, le verdict n'en porte aucune et la preuve est
        # refusée par `health_report`.
        held = sentinel.gh_credential_fingerprint(env, run=probe_gh)
        verdict = sentinel.probe_cli("gh", ["gh", "auth", "status",
                                            "--hostname", "github.com"],
                                     "gh auth login -h github.com", probe_gh,
                                     sentinel.gh_store(env), credential=held)
        # Une sonde portant sur un autre jeton ne valide pas celui lu avant
        # son lancement (même garde que pour un magasin remplacé).
        if verdict is not None and sentinel.gh_credential_fingerprint(env, run=probe_gh) != held:
            return sentinel.Verdict("gh", "", "unknown", sentinel.REPLACED)
        return verdict
    return None


def health_report(db, cfg: Config, profile, request, *, environ=None, run=None,
                  now: float | None = None, credential_before=_UNMEASURED) -> str:
    """Sonde APRÈS la connexion et écrit `credential_health` (L226b).

    `credential_before` est l'empreinte lue AVANT de lancer la connexion
    (N2) : un verdict `healthy` sur la même empreinte ne prouve pas une
    credential neuve et ne devient jamais `healthy` ; la ligne existante
    n'est pas réécrite. `_UNMEASURED` (appelant historique/témoin) ne
    déclenche pas cette preuve.

    L'interface ne change pas (`host_preflight` lit la même table) ; seule
    l'alimentation est nouvelle. Rend l'état de la sonde, `""` si inconnu.

    La source enregistrée est `sentinel` : c'est la même sonde, écrite par le
    même chemin que la passe à la demande `ameesh accounts check`, et la
    contrainte `credential_health_source_check` n'admet que
    `sentinel | turn | manual | relay` (trouvé par le témoin de bout en bout #366).
    """
    from . import sentinel, storage
    store = storage.of(db).credentials
    current = (store.health(cfg.host, request.harness) or {}).get(request.account) or {}
    verdict = probe_health(cfg, profile, request.harness, environ=environ, run=run, now=now)
    if verdict is None:
        return ""
    if (verdict.state == "healthy" and credential_before is not _UNMEASURED
            and not _proves_new_credential(credential_before, verdict.fingerprint)):
        return "failing"
    if sentinel.kept_failure(current, verdict):
        return "failing"
    written = store.report(cfg.host, request.harness, request.account, verdict.state,
                 reason=verdict.text(), source="sentinel", expires_at=verdict.expires_at,
                 fingerprint=verdict.fingerprint,
                 expect_failures=int(current.get("failures") or 0))
    if (written or {}).get("skipped"):
        return "failing"
    return verdict.state


def _error(category: str, message: str):
    from . import login as login_mod
    return login_mod.LoginError(category, message)


def _line(request) -> str:
    where = "%s/%s on %s" % (request.harness, request.account, request.host)
    tail = (" [%s]" % request.category) if request.category else ""
    return "login %s: %s%s" % (where, request.state, tail)


def cmd_accounts_login(cfg: Config, args) -> int:
    """`ameesh accounts login <harnais> [<compte>] --approver human:<id>` (L232d)."""
    from . import approve_client
    from . import login as login_mod
    try:
        return _run(cfg, args)
    except login_mod.LoginError as exc:
        print("refused [%s]: %s" % (exc.category, exc.message), file=sys.stderr)
        return 1
    except approve_client.ApproveClientError as exc:
        print("ameesh-approve refused [%s]: %s" % (exc.code, exc.message), file=sys.stderr)
        return 1


def _run(cfg: Config, args) -> int:
    from . import actions
    from . import approve_client
    from . import db as db_mod
    from . import login as login_mod

    harness = args.harness
    token_file = getattr(args, "token_file", None)
    if token_file is not None:
        return import_setup_token(cfg, args)
    if getattr(args, "expires_at", None) is not None:
        raise _error("failed", "--expires-at requires --token-file")
    adapter = login_mod.ADAPTERS.get(harness)
    if adapter is None:
        raise login_mod.LoginError("unsupported_version",
                                   "no login adapter for %r" % harness)
    environ = os.environ
    profile = resolve_profile(cfg, harness, getattr(args, "account", None), environ=environ)
    problems = home_problems(profile, environ)
    if problems:
        raise login_mod.LoginError("failed", "; ".join(problems))
    probe = pinned_version(harness, environ=environ, home=profile.home(environ))
    # N2 : l'empreinte est lue AVANT que le CLI puisse toucher au foyer ; le
    # verdict sain devra porter une empreinte différente pour valoir « neuf ».
    credential_before = credential_fingerprint(harness, profile, environ=environ)
    approver = getattr(args, "approver", None)
    if not approver:
        from . import notify
        approver = notify.parse_config(getattr(cfg, "notify", {}) or {}).default_human
    if not approver:
        raise _error("failed", "set --approver human:<id> or notify.default_human")
    who = getattr(args, "by", None) or actor(environ)
    scope = getattr(args, "scope", "") or ""
    wait = float(getattr(args, "wait", TTL_WAIT_S))
    if not math.isfinite(wait) or wait < 0 or wait > TTL_WAIT_S:
        raise _error("failed", "--wait must be between 0 and %d seconds" % TTL_WAIT_S)
    db = db_mod.open_db(cfg)
    supervisor = None
    try:
        supervisor = login_mod.Supervisor(
            publish=login_mod.publisher(approve_client, cfg, approver=approver,
                                        requested_by=who, scope=scope),
            health=lambda request: health_report(
                db, cfg, profile, request, environ=environ,
                credential_before=credential_before),
            invalidate=lambda request: approve_client.close_login_request(
                cfg, request.login_id, request.close_token))
        request = supervisor.begin(
            host=cfg.host, harness=harness, account=profile.name,
            home=profile.home(environ), version=probe["version"], approver=approver,
            requested_by=who, scope=scope, environ=environ,
            cwd=os.path.expanduser("~") if harness == "gh" else None)
        supervisor.wait(request.id, timeout=min(PUBLISH_WAIT_S, wait))
        if request.state != login_mod.VIEWING:
            print("refused [%s]: the private challenge was not published"
                  % (request.category or request.state), file=sys.stderr)
            return 1
        if not getattr(args, "json", False):
            print("login challenge %s for %s/%s on %s"
                  % (request.login_id, harness, profile.name, cfg.host))
            print("private link (open it, assert WebAuthn, then decide): %s" % request.link)
            print("waiting for the decision (bounded to %d s)…" % int(wait))
        notify_login(cfg, approver, request)
        policy = actions.policy_from_env()
        try:
            login_mod.wait_for_decision(
                supervisor, request,
                fetch=lambda login_id: approve_client.fetch_login_receipt(cfg, login_id),
                verify=lambda receipt: login_mod.receipt_decision(
                    db, request, receipt, policy=policy, consume_by=who),
                timeout=wait, interval=RECEIPT_POLL_S)
        except KeyboardInterrupt:
            supervisor.finish(request.id, "cancel")
            print("cancelled: the challenge is closed and the login process killed",
                  file=sys.stderr)
            return 130
        if getattr(args, "json", False):
            print(json.dumps(request.describe(), ensure_ascii=False, indent=2))
        else:
            print(_line(request))
        return 0 if request.category == "healthy" else 1
    except KeyboardInterrupt:
        print("cancelled: stopping the login process", file=sys.stderr)
        return 130
    finally:
        try:
            if supervisor is not None:
                supervisor.close()
        finally:
            db.close()


def import_setup_token(cfg, args):
    """Import consent already granted by the human, without launching OAuth."""
    from types import SimpleNamespace
    from . import db as db_mod, setup_token
    if args.harness != "claude":
        raise _error("failed", "--token-file is only for Claude setup-token; Codex uses device-auth")
    profile = resolve_profile(cfg, "claude", getattr(args, "account", None))
    try:
        content = (sys.stdin.read(setup_token.MAX_BYTES + 1) if args.token_file == "-"
                   else setup_token.private_read(args.token_file))
        if len(content) > setup_token.MAX_BYTES:
            raise setup_token.TokenError("token input too large")
        setup_token.install(profile, content,
                            expires_at=getattr(args, "expires_at", None))
    except (setup_token.TokenError, ValueError):
        raise _error("failed", "setup-token import refused: private OAuth token and valid expiry required") from None
    db = db_mod.open_db(cfg)
    try:
        state = health_report(db, cfg, profile,
                              SimpleNamespace(harness="claude", account=profile.name))
    finally:
        db.close()
    if getattr(args, "json", False):
        print(json.dumps({"harness": "claude", "account": profile.name,
                          "host": cfg.host, "state": state or "unknown"}))
    else:
        print("setup-token installed for claude/%s on %s; health: %s"
              % (profile.name, cfg.host, state or "unknown"))
    return 0 if state == "healthy" else 1


def notify_login(cfg, approver, request):
    """Push a link to the private challenge, never the bearer URL/device code.

    Device codes must remain behind the existing authenticated approval view;
    notification transports and their logs are not credential stores.
    """
    from . import notify
    ncfg = notify.parse_config(getattr(cfg, "notify", {}) or {})
    channels = ncfg.channels_for(approver)
    if not channels:
        print("login notification unavailable: no approver channel; use the private link",
              file=sys.stderr)
        return
    sender = notify.Sender(timeout=ncfg.timeout)
    message = notify.Message("Account login consent required",
                            "%s/%s on %s: %s" % (request.harness, request.account,
                                                  request.host, request.link))
    for channel in channels:
        try:
            sender.send(channel, message)
        except Exception:
            # A channel may echo its request body in an exception: never print it.
            print("login notification delivery failed; use the private link", file=sys.stderr)
