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
import os
import stat
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


def pinned_version(harness: str, *, probe=None) -> dict:
    """La version installée, refusée si absente ou hors épinglage (L120)."""
    from . import harness_versions
    from . import login as login_mod
    result = (probe or harness_versions.probe)(harness)
    adapter = login_mod.ADAPTERS.get(harness)
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
        return sentinel.probe_cli("gh", ["gh", "auth", "status"],
                                  "gh auth login -h github.com", run or sentinel._run,
                                  sentinel.gh_store(environ),
                                  sentinel.env_override(environ, sentinel.GH_TOKEN_VARS))
    return None


def health_report(db, cfg: Config, profile, request, *, environ=None, run=None,
                  now: float | None = None) -> str:
    """Sonde APRÈS la connexion et écrit `credential_health` (L226b).

    L'interface ne change pas (`host_preflight` lit la même table) ; seule
    l'alimentation est nouvelle. Rend l'état de la sonde, `""` si inconnu.

    La source enregistrée est `sentinel` : c'est la même sonde, écrite par le
    même chemin que la passe à la demande `ameesh accounts check`, et la
    contrainte `credential_health_source_check` n'admet que
    `sentinel | turn | manual | relay` (trouvé par le témoin de bout en bout #366).
    """
    from . import storage
    verdict = probe_health(cfg, profile, request.harness, environ=environ, run=run, now=now)
    if verdict is None:
        return ""
    store = storage.of(db).credentials
    current = (store.health(cfg.host, request.harness) or {}).get(request.account) or {}
    store.report(cfg.host, request.harness, request.account, verdict.state,
                 reason=verdict.text(), source="sentinel", expires_at=verdict.expires_at,
                 fingerprint=verdict.fingerprint,
                 expect_failures=int(current.get("failures") or 0))
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
    adapter = login_mod.ADAPTERS.get(harness)
    if adapter is None:
        raise login_mod.LoginError("unsupported_version",
                                   "no login adapter for %r" % harness)
    environ = os.environ
    profile = resolve_profile(cfg, harness, getattr(args, "account", None), environ=environ)
    problems = home_problems(profile, environ)
    if problems:
        raise login_mod.LoginError("failed", "; ".join(problems))
    probe = pinned_version(harness)
    approver = args.approver
    who = getattr(args, "by", None) or actor(environ)
    scope = getattr(args, "scope", "") or ""
    wait = max(0.0, float(getattr(args, "wait", TTL_WAIT_S)))
    db = db_mod.open_db(cfg)
    try:
        supervisor = login_mod.Supervisor(
            publish=login_mod.publisher(approve_client, cfg, approver=approver,
                                        requested_by=who, scope=scope),
            health=lambda request: health_report(db, cfg, profile, request, environ=environ))
        request = supervisor.begin(
            host=cfg.host, harness=harness, account=profile.name,
            home=profile.home(environ), version=probe["version"], approver=approver,
            requested_by=who, scope=scope, environ=environ)
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
    finally:
        db.close()
